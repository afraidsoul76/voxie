"""Text-to-speech so voxie can talk back.

Uses pyttsx3 (offline, no API, uses the OS voices — SAPI5 on Windows). Speaking
happens on a dedicated worker thread fed by a queue, because pyttsx3's
runAndWait() blocks, and we never want that to stall the Tk mainloop or the
Claude loop.
"""
from __future__ import annotations

import logging
import os
import queue
import threading

log = logging.getLogger("voxie.speech")

# Milliseconds of silence prepended to each spoken phrase so a Bluetooth or
# power-saving output device that spins up on first sound doesn't clip the
# opening word. Bump it if you still hear clipping on a slow-to-wake device.
try:
    LEAD_SILENCE_MS = int(os.environ.get("VOXIE_TTS_LEAD_MS", "300"))
except ValueError:
    LEAD_SILENCE_MS = 300


class Speaker:
    def __init__(self, enabled: bool = True, rate: int | None = None) -> None:
        self.enabled = enabled
        self._rate = rate
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = False
        self._rebuild = threading.Event()

    def start(self) -> None:
        if self._started or not self.enabled:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # COM must be initialised on the thread that drives a Windows voice.
        com_inited = False
        try:
            import pythoncom  # from pywin32

            pythoncom.CoInitialize()
            com_inited = True
        except Exception:
            pass

        speak = self._make_speak_fn()
        if speak is None:
            self.enabled = False
            return

        try:
            while True:
                text = self._queue.get()
                if text is None:  # shutdown sentinel
                    break
                if not text.strip():
                    continue
                if self._rebuild.is_set():
                    self._rebuild.clear()
                    rebuilt = self._make_speak_fn()
                    if rebuilt is not None:
                        speak = rebuilt
                        log.info("TTS rebuilt at rate=%s", self._rate)
                try:
                    log.info("speaking: %s", text[:80])
                    speak(text)
                except Exception:
                    log.exception("TTS failed")
        finally:
            if com_inited:
                try:
                    import pythoncom

                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def _make_speak_fn(self):
        """Prefer Windows SAPI directly (via win32com) — it's synchronous and
        doesn't have pyttsx3's flaky run-loop. Fall back to pyttsx3 elsewhere."""
        # --- Windows SAPI path ---
        try:
            import win32com.client
            from xml.sax.saxutils import escape

            voice = win32com.client.Dispatch("SAPI.SpVoice")
            if self._rate is not None:
                # SAPI Rate is -10..10 (0 ≈ 200 wpm). Map from words-per-minute.
                sapi_rate = max(-10, min(10, round((self._rate - 200) / 20)))
                voice.Rate = sapi_rate

            # Leading silence covers Bluetooth/audio-device wake-up latency, so
            # the first word isn't clipped ("Firefox" -> "ox"). SVSFIsXML = 8
            # tells SAPI the string is SSML; we stay synchronous (no async flag).
            SVSFIsXML = 8

            def speak(text: str) -> None:
                ssml = f'<silence msec="{LEAD_SILENCE_MS}"/>{escape(text)}'
                voice.Speak(ssml, SVSFIsXML)  # blocking, no run-loop

            log.info("TTS ready (Windows SAPI)")
            return speak
        except Exception as e:
            log.info("SAPI unavailable (%s), falling back to pyttsx3", e)

        # --- pyttsx3 fallback (non-Windows) ---
        try:
            import pyttsx3

            def speak(text: str) -> None:
                # Fresh engine per utterance avoids the wedged-run-loop bug.
                engine = pyttsx3.init()
                if self._rate is not None:
                    engine.setProperty("rate", self._rate)
                engine.say(text)
                engine.runAndWait()
                engine.stop()

            log.info("TTS ready (pyttsx3)")
            return speak
        except Exception as e:
            log.warning("no TTS backend available, disabling: %s", e)
            return None

    def set_rate(self, rate: int) -> None:
        """Change speaking rate. Takes effect on the next utterance."""
        if rate == self._rate:
            return
        self._rate = rate
        # The worker owns the voice object; flag it to rebuild rather than
        # touching it from this thread (SAPI is COM and thread-affine).
        self._rebuild.set()

    def say(self, text: str) -> None:
        """Queue text to be spoken. No-op if TTS is disabled."""
        if not self.enabled:
            return
        if not self._started:
            self.start()
        self._queue.put(text)

    def stop(self) -> None:
        if self._started:
            self._queue.put(None)
