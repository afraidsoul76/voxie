"""voxie entrypoint.

Runs the Tk mainloop on the main thread; the tray icon, hotkey listener, and
Claude/audio work happen on background threads and marshal UI updates back
through Overlay.post(...).
"""
from __future__ import annotations

import logging
import sys
import threading
from enum import Enum

from pynput import keyboard

from .audio import Recorder, Transcriber, log_audio_devices, resolve_input_device
from .config import Config
from .llm import Assistant
from .speech import Speaker
from .ui.overlay import Overlay
from .ui.tray import Tray

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("voxie")


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    ACTING = "acting"


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
        self.tray = Tray(
            on_toggle_window=self.overlay_toggle,
            on_toggle_listen=self.toggle,
            on_quit=self.quit,
            is_window_visible=lambda: self.overlay.is_visible,
        )
        self._state = State.IDLE
        self._busy_lock = threading.Lock()

    # ---- state ----
    def _set_state(self, s: State, note: str | None = None) -> None:
        self._state = s
        color = {
            State.IDLE: "#94a3b8",
            State.LISTENING: "#ef4444",
            State.THINKING: "#f59e0b",
            State.ACTING: "#10b981",
        }[s]
        label = note or f"voxie · {s.value}"
        self.overlay.set_status(label, color=color)
        self.tray.set_state(s.value)

    def overlay_toggle(self) -> None:
        """Show/hide the floating window. Bound to a tray-icon click."""
        self.overlay.toggle()

    # ---- hotkey / tray toggle ----
    def toggle(self) -> None:
        if self._state == State.IDLE:
            self._start_listen()
        elif self._state == State.LISTENING:
            self._stop_and_process()
        # ignore toggles during thinking/acting — user has to wait

    def _start_listen(self) -> None:
        self.overlay.clear_trace()
        self.overlay.set_transcript("(speak now — press the hotkey again to send)")
        self.overlay.show()
        self._set_state(State.LISTENING)
        self.recorder.start()

    def _stop_and_process(self) -> None:
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

    # ---- lifecycle ----
    def quit(self) -> None:
        try:
            if self.recorder.is_recording:
                self.recorder.stop()
        finally:
            self.speaker.stop()
            self.tray.stop()
            self.overlay.quit()

    def run(self) -> None:
        log_audio_devices()
        self.speaker.start()
        self.tray.run_detached()

        hotkeys = keyboard.GlobalHotKeys({self.cfg.hotkey: self.toggle})
        hotkeys.start()

        self.overlay.show()
        self._set_state(State.IDLE, f"voxie · press {self.cfg.hotkey} to talk")
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
