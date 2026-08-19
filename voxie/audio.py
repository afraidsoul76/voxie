"""Microphone capture + local Whisper transcription.

Recording is push-to-talk: `Recorder.start()` opens a stream and buffers samples;
`Recorder.stop()` returns the buffer. Transcription happens off the main thread
via `Transcriber.transcribe`, which loads faster-whisper lazily so app startup
isn't blocked by the ~150 MB model download on first run.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

import numpy as np
import sounddevice as sd

log = logging.getLogger("voxie.audio")

SAMPLE_RATE = 16000  # Whisper is trained on 16k
CHANNELS = 1
DTYPE = "float32"


def log_audio_devices() -> None:
    """Dump input devices to the log so the user can see which mic will be used."""
    try:
        devices = sd.query_devices()
        default_in = sd.default.device[0] if sd.default.device else None
    except Exception as e:
        log.warning("could not enumerate audio devices: %s", e)
        return
    log.info("input devices (★ = default):")
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) < 1:
            continue
        star = "★" if i == default_in else " "
        log.info("  %s [%d] %s  (channels=%d, samplerate=%s)",
                 star, i, d.get("name"), d["max_input_channels"], d.get("default_samplerate"))


def resolve_input_device(pref: str | None) -> tuple[int | None, str]:
    """Resolve VOXIE_INPUT_DEVICE to a device index (or None for OS default).

    Accepts either a numeric index ("18") or a substring of the device name
    ("GPods"). Returns (index_or_none, human_label).
    """
    if not pref:
        return None, "OS default"
    try:
        devices = sd.query_devices()
    except Exception as e:
        log.warning("could not enumerate audio devices for lookup: %s", e)
        return None, "OS default"

    # Numeric index.
    if pref.lstrip("-").isdigit():
        idx = int(pref)
        if 0 <= idx < len(devices) and devices[idx].get("max_input_channels", 0) >= 1:
            return idx, f"[{idx}] {devices[idx].get('name')}"
        log.warning("VOXIE_INPUT_DEVICE=%s is not a valid input device index; using OS default", pref)
        return None, "OS default (fallback)"

    # Substring match on name.
    needle = pref.lower()
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) < 1:
            continue
        if needle in str(d.get("name", "")).lower():
            return i, f"[{i}] {d.get('name')}"
    log.warning("VOXIE_INPUT_DEVICE=%r did not match any input device name; using OS default", pref)
    return None, "OS default (fallback)"


class Recorder:
    def __init__(self, device: int | None = None) -> None:
        self._device = device
        self._stream: Optional[sd.InputStream] = None
        self._buffer: queue.Queue[np.ndarray] = queue.Queue()
        self._recording = False
        self._level = 0.0

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _callback(self, indata, frames, time, status):
        if status:
            # Overflows / underflows aren't fatal — just drop the chunk.
            return
        # Track a smoothed input level so the UI can draw a live waveform.
        try:
            rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            # Attack fast, release slow - reads better than a raw RMS.
            self._level = max(rms, self._level * 0.82)
        except Exception:
            pass
        self._buffer.put(indata.copy())

    @property
    def level(self) -> float:
        """Smoothed mic level, roughly 0..1, for the UI waveform."""
        return min(1.0, self._level * 12.0)

    def start(self) -> None:
        if self._recording:
            return
        # Drain any leftovers from an interrupted session.
        while not self._buffer.empty():
            try:
                self._buffer.get_nowait()
            except queue.Empty:
                break
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=self._callback,
            device=self._device,
        )
        self._stream.start()
        self._recording = True

    def stop(self) -> np.ndarray:
        if not self._recording:
            return np.zeros(0, dtype=np.float32)
        assert self._stream is not None
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self._recording = False

        chunks: list[np.ndarray] = []
        while not self._buffer.empty():
            try:
                chunks.append(self._buffer.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks, axis=0).flatten().astype(np.float32)
        return audio


class Transcriber:
    """faster-whisper wrapper. Loads model lazily on first .transcribe() call."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model = None
        self._load_lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            # Import here so a broken faster-whisper install doesn't crash at
            # module load — the error surfaces only when someone actually tries
            # to record + transcribe.
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type="int8",  # CPU-friendly, decent accuracy
            )

    def transcribe(self, audio: np.ndarray) -> str:
        """Return the recognised text, or '' for empty/noise-only clips."""
        if audio.size < SAMPLE_RATE // 4:  # less than 250 ms of audio
            log.warning("audio too short: %.2fs", audio.size / SAMPLE_RATE)
            return ""

        # Log peak amplitude so a silent-mic problem is diagnosable at a glance.
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
        log.info(
            "audio: %.2fs, peak=%.3f, rms=%.4f%s",
            audio.size / SAMPLE_RATE,
            peak,
            rms,
            "  ⚠ MIC IS SILENT — check Windows input device" if peak < 0.001 else "",
        )

        self._ensure_loaded()
        assert self._model is not None
        # VAD off: push-to-talk already tells us when to start/stop, and the
        # default VAD threshold sometimes eats quiet mics whole.
        segments, _info = self._model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()
