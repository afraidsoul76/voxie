"""voxie entrypoint.

Runs the Tk mainloop on the main thread; the tray icon, hotkey listener, and
Claude/audio work happen on background threads and marshal UI updates back
through Overlay.post(...).
"""
from __future__ import annotations

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("voxie")

# Declare DPI awareness FIRST — before pyautogui/mss get imported (transitively
# via .llm below), because a process's DPI awareness can only be set once and
# the first setter wins. This keeps screen-capture and click coordinates in the
# same physical-pixel space on scaled displays.
from .dpi import set_dpi_awareness

set_dpi_awareness()

import sys
import threading
import time
from enum import Enum

from pynput import keyboard

from .audio import Recorder, Transcriber, log_audio_devices, resolve_input_device
from .config import Config
from .llm import Assistant
from .tools.input import type_text
from .speech import Speaker
from .ui.overlay import Overlay
from .ui.tray import Tray
from .wake import WakeListener


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    ACTING = "acting"
    DICTATING = "dictating"


class Voxie:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        device_idx, device_label = resolve_input_device(cfg.input_device)
        log.info("using input device: %s", device_label)
        self.recorder = Recorder(device=device_idx)
        self.transcriber = Transcriber(cfg.whisper_model)
        self.assistant = Assistant(
            api_key=cfg.anthropic_api_key,
            base_url=cfg.anthropic_base_url,
            model=cfg.model,
        )
        self.speaker = Speaker(enabled=cfg.tts_enabled, rate=cfg.voice_rate)
        # Closing the frameless overlay hides it rather than quitting — the
        # tray icon is the real lifecycle control.
        self.overlay = Overlay(on_close=self.overlay_toggle)
        # Drive the overlay waveform from the real mic level.
        self.overlay.set_level_source(lambda: self.recorder.level)
        self.tray = Tray(
            on_toggle_window=self.overlay_toggle,
            on_toggle_listen=self.toggle,
            on_clear_memory=self.clear_memory,
            on_quit=self.quit,
            is_window_visible=lambda: self.overlay.is_visible,
        )
        self._state = State.IDLE
        self._busy_lock = threading.Lock()
        self._dictating = False  # True when the active recording is dictation
        self.wake: WakeListener | None = None
        if cfg.wake_enabled:
            self.wake = WakeListener(
                phrase=cfg.wake_phrase,
                on_wake=self._on_wake,
                model_name=cfg.wake_model,
                device=device_idx,
                aliases=cfg.wake_aliases,
            )

    # ---- state ----
    def _set_state(self, s: State, note: str | None = None) -> None:
        self._state = s
        color = {
            State.IDLE: "#94a3b8",
            State.LISTENING: "#ef4444",
            State.THINKING: "#f59e0b",
            State.ACTING: "#10b981",
            State.DICTATING: "#8b5cf6",
        }[s]
        if s == State.IDLE:
            self._mic_back_to_wake()
        label = note or f"voxie · {s.value}"
        self.overlay.set_status(label, color=color)
        self.overlay.set_busy(s != State.IDLE)
        self.tray.set_state(s.value)

    def overlay_toggle(self) -> None:
        """Show/hide the floating window. Bound to a tray-icon click."""
        self.overlay.toggle()

    def clear_memory(self, *_args) -> None:
        self.assistant.reset_memory()
        self.overlay.append_trace("· memory cleared")
        self.speaker.say("Memory cleared.")

    # ---- hotkey / tray toggle ----
    def toggle(self) -> None:
        if self._state == State.IDLE:
            self._start_listen()
        elif self._state == State.LISTENING:
            self._stop_and_process()
        # ignore toggles during thinking/acting — user has to wait

    def _start_listen(self) -> None:
        self._mic_for_recorder()
        self.overlay.clear_trace()
        self.overlay.set_transcript("listening...")
        self.overlay.set_listening(True)
        self._set_state(State.LISTENING)  # sets busy → pill fades in
        self.recorder.start()

    def _stop_and_process(self) -> None:
        self.overlay.set_listening(False)
        audio = self.recorder.stop()
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _process(self, audio) -> None:
        if not self._busy_lock.acquire(blocking=False):
            self.overlay.append_trace("(already working on something — try again in a sec)")
            return
        try:
            self._set_state(State.THINKING, "voxie · transcribing…")
            transcript = self.transcriber.transcribe(audio)
            if not transcript:
                self.overlay.set_transcript("(no speech detected)")
                self._set_state(State.IDLE)
                return
            self.overlay.set_transcript(f"you: {transcript}")

            # Voice escape hatch: clear context without spending an LLM call.
            normalized = transcript.lower().strip().rstrip(".!")
            if normalized in ("never mind", "nevermind", "forget that", "forget it", "clear memory", "start over"):
                self.clear_memory()
                self._set_state(State.IDLE)
                return

            self._set_state(State.THINKING, "voxie · thinking…")

            def on_tool(name: str, result: dict) -> None:
                self._set_state(State.ACTING)
                ok = "✓" if result.get("ok") else "✗"
                brief = ", ".join(f"{k}={v!r}" for k, v in list(result.items())[:2])
                self.overlay.append_trace(f"{ok} {name}({brief})")

            reply = self.assistant.run(transcript, on_tool=on_tool)
            self.overlay.append_trace(f"→ {reply}")
            self.speaker.say(reply)
            self._set_state(State.IDLE)
        except Exception as e:
            log.exception("processing failed")
            self.overlay.append_trace(f"✗ error: {e}")
            self._set_state(State.IDLE)
        finally:
            self._busy_lock.release()

    # ---- wake word ----
    def _on_wake(self) -> None:
        """Heard the wake phrase - start listening for the actual command.

        This fires ON the wake listener's thread. Starting the recording from
        here would deadlock: _mic_for_recorder() pauses the listener and waits
        for it to close its stream, but the listener can't act on that pause
        while it's blocked inside this callback. So hand off to another thread
        and let the listener get straight back to its loop.
        """
        if self._state != State.IDLE:
            return
        threading.Thread(target=self._wake_start, daemon=True).start()

    def _wake_start(self) -> None:
        self.speaker.say("Yes?")
        self._start_listen()

    def _mic_for_recorder(self) -> None:
        """Wake listener and recorder can't hold the mic at once."""
        if self.wake is not None:
            self.wake.pause()
            time.sleep(0.15)  # let the stream actually close

    def _mic_back_to_wake(self) -> None:
        if self.wake is not None:
            self.wake.resume()

    # ---- dictation (no LLM: transcribe and type at the cursor) ----
    def toggle_dictation(self) -> None:
        if self._state == State.IDLE:
            self._dictating = True
            self._mic_for_recorder()
            self.overlay.clear_trace()
            self.overlay.set_transcript("dictating - speak, then press the key again")
            self.overlay.set_listening(True)
            self._set_state(State.DICTATING)
            self.recorder.start()
        elif self._state == State.DICTATING:
            self.overlay.set_listening(False)
            audio = self.recorder.stop()
            threading.Thread(target=self._process_dictation, args=(audio,), daemon=True).start()

    def _process_dictation(self, audio) -> None:
        if not self._busy_lock.acquire(blocking=False):
            self.overlay.append_trace("(busy - try again in a sec)")
            return
        try:
            self._set_state(State.THINKING, "voxie - transcribing")
            text = self.transcriber.transcribe(audio)
            if not text:
                self.overlay.set_transcript("(no speech detected)")
                self._set_state(State.IDLE)
                return
            self.overlay.set_transcript(text)
            # Give focus a moment to settle back on the target field, then type.
            time.sleep(0.25)
            res = type_text(text)
            if res.get("ok"):
                self.overlay.append_trace(f"typed {res.get('typed_chars', 0)} chars")
            else:
                self.overlay.append_trace(f"type failed: {res.get('error')}")
            self._set_state(State.IDLE)
        except Exception as e:
            log.exception("dictation failed")
            self.overlay.append_trace(f"error: {e}")
            self._set_state(State.IDLE)
        finally:
            self._dictating = False
            self._busy_lock.release()

    # ---- lifecycle ----
    def quit(self) -> None:
        try:
            if self.recorder.is_recording:
                self.recorder.stop()
        finally:
            if self.wake is not None:
                self.wake.stop()
            self.speaker.stop()
            self.tray.stop()
            self.overlay.quit()

    def run(self) -> None:
        log_audio_devices()
        self.speaker.start()
        self.tray.run_detached()
        if self.wake is not None:
            log.info('wake word enabled (say %r)', self.cfg.wake_phrase)
            self.wake.start()

        hotkeys = keyboard.GlobalHotKeys({
            self.cfg.hotkey: self.toggle,
            self.cfg.dictate_hotkey: self.toggle_dictation,
        })
        hotkeys.start()

        # Brief hello on launch, then let it auto-fade.
        self.overlay.set_transcript(f"press {self.cfg.hotkey} to talk to me")
        self._set_state(State.IDLE, "voxie · ready")
        self.overlay.set_busy(True)
        self.overlay.post(lambda: self.overlay.root.after(2500, lambda: self.overlay.set_busy(False)))
        try:
            self.overlay.mainloop()
        finally:
            hotkeys.stop()
            self.tray.stop()


def main() -> None:
    try:
        cfg = Config.from_env()
    except RuntimeError as e:
        print(f"[voxie] {e}", file=sys.stderr)
        sys.exit(1)
    Voxie(cfg).run()


if __name__ == "__main__":
    main()
