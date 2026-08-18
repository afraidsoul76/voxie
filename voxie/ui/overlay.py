"""Tiny always-on-top overlay that shows the current transcript + action trace.

Runs on Tk's mainloop in the main thread; every mutation from other threads
must go through `.post(...)` which uses `after(0, ...)` for thread-safety.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable

from ..llm import screen  # for isinstance check on screenshot media type


class Overlay:
    def __init__(self, on_close: Callable[[], None]) -> None:
        self.root = tk.Tk()
        self.root.title("voxie")
        self.root.geometry("420x160+40+40")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#0b1220")
        self.root.protocol("WM_DELETE_WINDOW", on_close)
        # Hide the window frame chrome — feels more like an overlay, less like a tool.
        try:
            self.root.overrideredirect(True)
        except tk.TclError:
            pass

        self.status = tk.Label(
            self.root, text="voxie · idle",
            font=("Segoe UI", 11, "bold"),
            fg="#94a3b8", bg="#0b1220", anchor="w", padx=10, pady=6,
        )
        self.status.pack(fill="x")

        self.transcript = tk.Label(
            self.root, text="", wraplength=400, justify="left", anchor="nw",
            font=("Segoe UI", 10), fg="#e5e7eb", bg="#0b1220", padx=10,
        )
        self.transcript.pack(fill="x")

        self.trace = tk.Text(
            self.root, height=4, bg="#0f172a", fg="#94a3b8",
            font=("Consolas", 9), bd=0, wrap="word", state="disabled",
        )
        self.trace.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # Drag anywhere on the window.
        for w in (self.status, self.transcript):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._drag)

        # Start hidden. `_visible` mirrors the window state as a plain bool so
        # the tray thread can check it without calling into Tk.
        self._visible = False
        self.root.withdraw()

    def _start_drag(self, e):
        self._drag_start = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag(self, e):
        dx, dy = getattr(self, "_drag_start", (0, 0))
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def post(self, fn: Callable[[], None]) -> None:
        """Schedule `fn` on the Tk mainloop."""
        self.root.after(0, fn)

    @property
    def is_visible(self) -> bool:
        """Plain bool, safe to read from the tray thread without touching Tk."""
        return self._visible

    def show(self) -> None:
        self._visible = True
        self.post(lambda: (self.root.deiconify(), self.root.lift()))

    def hide(self) -> None:
        self._visible = False
        self.post(self.root.withdraw)

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    def set_status(self, text: str, color: str = "#94a3b8") -> None:
        self.post(lambda: (self.status.config(text=text, fg=color)))

    def set_transcript(self, text: str) -> None:
        self.post(lambda: self.transcript.config(text=text))

    def append_trace(self, line: str) -> None:
        def do():
            self.trace.config(state="normal")
            self.trace.insert("end", line + "\n")
            self.trace.see("end")
            self.trace.config(state="disabled")
        self.post(do)

    def clear_trace(self) -> None:
        def do():
            self.trace.config(state="normal")
            self.trace.delete("1.0", "end")
            self.trace.config(state="disabled")
        self.post(do)

    def mainloop(self) -> None:
        self.root.mainloop()

    def quit(self) -> None:
        self.post(self.root.quit)


# Silence "unused import" — we keep it so screen module resolves early and
# tk doesn't lazy-load ctypes at click time.
_ = screen
