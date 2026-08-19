"""Always-listening wake-word detection ("hey voxie") with no cloud service.

Rather than a dedicated wake-word engine (Porcupine needs a business-email
account; openWakeWord has no pretrained "voxie" model), this reuses the Whisper
stack that's already here. The cost of running speech-to-text continuously is
avoided with an energy gate:

    silence  -> just RMS arithmetic on each audio chunk, effectively free
    speech   -> buffer it; when the speaker pauses, transcribe that segment
                with the TINY model and look for the wake phrase

So the CPU only does real work while someone is actually talking, and the
phrase is matched as text — meaning any wake phrase works without training.

The listener owns the mic while armed and must be paused before the main
recorder opens its own stream.
"""
from __future__ import annotations

import difflib
import logging
import queue
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

from .audio import SAMPLE_RATE

log = logging.getLogger("voxie.wake")

BLOCK = 1600                 # 0.1s at 16k
SPEECH_RMS = 0.020           # above this counts as speech (higher = less noise pickup)
SILENCE_BLOCKS = 5           # 0.5s of quiet ends a segment
MIN_SPEECH_BLOCKS = 3        # ignore blips shorter than 0.3s
MAX_SEGMENT_BLOCKS = 25      # 2.5s cap - a wake phrase is short; longer
                             # segments are someone talking, not calling us
COOLDOWN_S = 2.0             # don't re-fire immediately after a hit


def _normalize(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum() or c.isspace()).strip()


# Wake phrases are usually invented words, so Whisper spells them however they
# sounded: "voxie" comes back as voxy / foxy / boxy / vox. Exact matching almost
# never fires.
#
# Character similarity turns out to be the wrong tool - it scores "voxy" at 0.67
# but "moxie" at 0.80, i.e. backwards from how they actually sound. So the
# primary mechanism is an explicit alias list (deterministic, tunable), with a
# conservative fuzzy check as a fallback for spellings nobody predicted.
#
# Unmatched transcripts are logged, so if your voice trips a spelling that isn't
# listed you can see it and add it via VOXIE_WAKE_ALIASES.
SIMILARITY = 0.8

DEFAULT_ALIASES = {
    "voxie": ["voxy", "voxi", "vocksy", "voxie", "vox", "voxxy", "foxy", "boxy",
              "vauxy", "vocksie", "walksy", "vaux"],
}


def build_vocab(phrase: str, extra: str = "") -> set[str]:
    """All spellings that should count as the wake phrase."""
    vocab = {phrase}
    vocab.update(DEFAULT_ALIASES.get(phrase, []))
    vocab.update(a.strip().lower() for a in extra.split(",") if a.strip())
    return {v for v in vocab if v}


def _phrase_hit(vocab: set[str], canonical: str, heard: str) -> tuple[bool, str]:
    """True if `heard` plausibly contains any spelling of the wake phrase."""
    if not heard:
        return False, ""

    # Exact containment of any known spelling, on word boundaries.
    words = heard.split()
    wordset = set(words)
    for v in vocab:
        if " " in v:
            if v in heard:
                return True, v
        elif v in wordset:
            return True, v

    # Fuzzy fallback, against the CANONICAL phrase only. Fuzzy-matching the
    # aliases too was a mistake: with "foxy" listed, the ordinary word "fox"
    # scored 0.86 and tripped the wake on any sentence mentioning a fox.
    for w in words:
        if difflib.SequenceMatcher(None, canonical, w).ratio() >= SIMILARITY:
            return True, w
    return False, ""


class WakeListener:
    """Listens for a wake phrase and calls `on_wake()` when it hears one."""

    def __init__(
        self,
        phrase: str,
        on_wake: Callable[[], None],
        model_name: str = "base.en",
        device: int | None = None,
        aliases: str = "",
    ) -> None:
        self.phrase = _normalize(phrase)
        self.vocab = build_vocab(self.phrase, aliases)
        self.on_wake = on_wake
        self.model_name = model_name
        self.device = device

        self._model = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._last_fire = 0.0

    # ---- lifecycle ----

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        """Release the mic so the main recorder can use it."""
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    # ---- internals ----

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("loading wake model (%s)", self.model_name)
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self._model

    def _callback(self, indata, frames, time_info, status):
        if status:
            return
        self._q.put(indata.copy())

    def _run(self) -> None:
        # Load the model up front so the first wake isn't delayed by it.
        try:
            self._ensure_model()
        except Exception as e:
            log.warning("wake model unavailable, wake word disabled: %s", e)
            return

        segment: list[np.ndarray] = []
        speech_blocks = 0
        silence_blocks = 0
        stream: sd.InputStream | None = None

        while not self._stop.is_set():
            # While paused, hold the mic closed and drop anything queued.
            if self._paused.is_set():
                if stream is not None:
                    stream.stop()
                    stream.close()
                    stream = None
                    segment, speech_blocks, silence_blocks = [], 0, 0
                    while not self._q.empty():
                        try:
                            self._q.get_nowait()
                        except queue.Empty:
                            break
                time.sleep(0.1)
                continue

            if stream is None:
                try:
                    stream = sd.InputStream(
                        samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=BLOCK, callback=self._callback, device=self.device,
                    )
                    stream.start()
                    log.info("wake listener armed - phrase %r, %d spellings accepted", self.phrase, len(self.vocab))
                except Exception as e:
                    log.warning("could not open mic for wake listening: %s", e)
                    time.sleep(1.0)
                    continue

            try:
                block = self._q.get(timeout=0.2)
            except queue.Empty:
                continue

            rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))

            if rms >= SPEECH_RMS:
                segment.append(block)
                speech_blocks += 1
                silence_blocks = 0
                if speech_blocks >= MAX_SEGMENT_BLOCKS:
                    self._maybe_wake(segment)
                    segment, speech_blocks, silence_blocks = [], 0, 0
            elif segment:
                # Trailing quiet: keep it (helps the model) until the pause is
                # long enough to call the segment finished.
                segment.append(block)
                silence_blocks += 1
                if silence_blocks >= SILENCE_BLOCKS:
                    if speech_blocks >= MIN_SPEECH_BLOCKS:
                        self._maybe_wake(segment)
                    segment, speech_blocks, silence_blocks = [], 0, 0

        if stream is not None:
            stream.stop()
            stream.close()

    def _maybe_wake(self, segment: list[np.ndarray]) -> None:
        if time.time() - self._last_fire < COOLDOWN_S:
            return
        audio = np.concatenate(segment, axis=0).flatten().astype(np.float32)
        try:
            model = self._ensure_model()
            segs, _ = model.transcribe(
                audio, language="en", beam_size=1, vad_filter=False,
                condition_on_previous_text=False,  # stop it inventing continuations
                no_speech_threshold=0.6,
            )
            text = _normalize(" ".join(s.text for s in segs))
        except Exception:
            log.exception("wake transcription failed")
            return

        if not text:
            return
        hit, matched = _phrase_hit(self.vocab, self.phrase, text)
        log.info("heard %r -> %s", text, f"WAKE (matched {matched!r})" if hit else "no match")
        if hit:
            self._last_fire = time.time()
            try:
                self.on_wake()
            except Exception:
                log.exception("on_wake callback raised")
