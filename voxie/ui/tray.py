"""System tray icon. Color reflects state:
   gray  = idle
   red   = listening
   amber = thinking / transcribing / calling Claude
   green = acting (running tools)
"""
from __future__ import annotations

from typing import Callable

import pystray
from PIL import Image, ImageDraw


def _make_icon(color: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=color, outline="#222", width=2)
    # A little "mic" glyph
    d.rectangle((28, 20, 36, 38), fill="#222")
    d.ellipse((28, 16, 36, 24), fill="#222")
    d.ellipse((28, 34, 36, 42), fill="#222")
    d.line((32, 42, 32, 50), fill="#222", width=3)
    d.line((24, 50, 40, 50), fill="#222", width=3)
    return img


class Tray:
    STATES = {
        "idle":      ("#95a5a6", "voxie · idle"),
        "listening": ("#ef4444", "voxie · listening"),
        "thinking":  ("#f59e0b", "voxie · thinking"),
        "acting":    ("#10b981", "voxie · acting"),
    }

    def __init__(
        self,
        on_toggle_listen: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("Start / stop listening", on_toggle_listen, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        )
        self._icon = pystray.Icon("voxie", _make_icon("#95a5a6"), "voxie · idle", menu)

    def set_state(self, state: str) -> None:
        color, title = self.STATES.get(state, self.STATES["idle"])
        self._icon.icon = _make_icon(color)
        self._icon.title = title

    def run_detached(self) -> None:
        """Run the tray loop in a background thread (pystray blocks)."""
        self._icon.run_detached()

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            pass
