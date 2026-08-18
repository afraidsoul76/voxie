"""Microphone capture + local Whisper transcription.

Recording is push-to-talk: `Recorder.start()` opens a stream and buffers samples;
`Recorder.stop()` returns the buffer. Transcription happens off the main thread
via `Transcriber.transcribe`, which loads faster-whisper lazily so app startup
isn't blocked by the ~150 MB model download on first run.
"""
from __future__ import annotations

import queue
import threading
from typing import Optional

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # Whisper is trained on 16k
CHANNELS = 1
DTYPE = "float32"


class Recorder:
    def __init__(self) -> None:
        self._stream: Optional[sd.InputStream] = None
        self._buffer: queue.Queue[np.ndarray] = queue.Queue()
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _callback(self, indata, frames, time, status):
        if status:
            # Overflows / underflows aren't fatal — just drop the chunk.
            return
        self._buffer.put(indata.copy())

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
            return ""
        self._ensure_loaded()
        assert self._model is not None
        segments, _info = self._model.transcribe(
            audio,
            language="en",
            beam_size=1,       # fast; accuracy loss is small for short commands
            vad_filter=True,   # drop silence at start/end
        )
        return " ".join(s.text.strip() for s in segments).strip()
