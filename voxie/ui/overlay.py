"""voxie's floating assistant pill.

A frameless translucent card that hovers near the bottom of the screen. The
card, its gradient and its rounded corners are rendered with PIL (anti-aliased
via supersampling) rather than drawn with Tk primitives, which is what makes it
look like a modern overlay instead of a dialog box.

Transparency uses Tk's -transparentcolor chroma key. The card is composited
onto that key colour, and because the key is near-black and the card is dark
navy, the anti-aliased corner pixels blend into a soft dark edge instead of an
ugly fringe.

Threading: all Tk mutation happens on the mainloop. Other threads set plain
flags (_pinned, _busy, _listening) or schedule work via post(); the animation
tick runs on the mainloop and reads those flags.
"""
from __future__ import annotations

import math
import tkinter as tk
from typing import Callable

from PIL import Image, ImageDraw, ImageTk

# Chroma key: made fully transparent by the window manager.
_CHROMA = "#010203"
_CHROMA_RGB = (1, 2, 3)

W, H = 620, 132
PAD = 12                      # gap between window edge and card
RADIUS = 26
CARD_TOP = (17, 24, 39)       # slate-900-ish
CARD_BOT = (10, 15, 28)       # deeper navy
BORDER = (31, 41, 60)
SS = 4                        # supersample factor for smooth corners

MAX_ALPHA = 0.97
FADE_STEP = 0.09
TICK_MS = 33                  # ~30fps
HIDE_TICKS = 150              # ~5s idle before it fades out
SLIDE_PX = 18                 # entrance slide distance

ORB_CX, ORB_CY = 52, H // 2
ORB_R = 11

WAVE_X = 84                   # waveform bars start
WAVE_BARS = 14
WAVE_BAR_W = 3
WAVE_GAP = 4

TEXT_X = 84


def _render_card() -> Image.Image:
    """Rounded, vertically-graded card with a 1px border, anti-aliased."""
    w, h = (W - 2 * PAD) * SS, (H - 2 * PAD) * SS
    grad = Image.new("RGB", (1, h))
    gd = ImageDraw.Draw(grad)
    for y in range(h):
        t = y / max(1, h - 1)
        gd.point((0, y), fill=tuple(
            round(CARD_TOP[i] + (CARD_BOT[i] - CARD_TOP[i]) * t) for i in range(3)
        ))
    grad = grad.resize((w, h))

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), RADIUS * SS, fill=255)

    card = Image.new("RGB", (w, h), _CHROMA_RGB)
    card.paste(grad, (0, 0), mask)
    ImageDraw.Draw(card).rounded_rectangle(
        (0, 0, w - 1, h - 1), RADIUS * SS, outline=BORDER, width=SS
    )

    out = Image.new("RGB", (W, H), _CHROMA_RGB)
    out.paste(card.resize((W - 2 * PAD, H - 2 * PAD), Image.LANCZOS), (PAD, PAD))
    return out


class Overlay:
    def __init__(self, on_close: Callable[[], None]) -> None:
        self.root = tk.Tk()
        self.root.title("voxie")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", _CHROMA)
        except tk.TclError:
            pass
        self.root.configure(bg=_CHROMA)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self._home_x = (sw - W) // 2
        self._home_y = sh - H - 80
        self.root.geometry(f"{W}x{H}+{self._home_x}+{self._home_y}")

        self.canvas = tk.Canvas(self.root, width=W, height=H, bg=_CHROMA,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self._card_img = ImageTk.PhotoImage(_render_card())
        self.canvas.create_image(0, 0, anchor="nw", image=self._card_img)

        # --- status orb: outer halo, mid ring, solid core ---
        self._orb_color = "#94a3b8"
        self._halo = self.canvas.create_oval(0, 0, 0, 0, outline="", fill="")
        self._ring = self.canvas.create_oval(0, 0, 0, 0, outline="", width=2)
        self._core = self.canvas.create_oval(0, 0, 0, 0, outline="", fill=self._orb_color)

        # --- waveform bars (shown while listening) ---
        self._bars = [
            self.canvas.create_rectangle(0, 0, 0, 0, outline="", fill="")
            for _ in range(WAVE_BARS)
        ]
        self._bar_h = [0.0] * WAVE_BARS

        # --- text ---
        self._t_status = self.canvas.create_text(
            TEXT_X, 42, anchor="w", text="VOXIE", fill="#94a3b8",
            font=("Segoe UI Semibold", 9))
        self._t_main = self.canvas.create_text(
            TEXT_X, 66, anchor="w", text="", fill="#f1f5f9",
            font=("Segoe UI", 14), width=W - TEXT_X - 34)
        self._t_detail = self.canvas.create_text(
            TEXT_X, 92, anchor="w", text="", fill="#7c8ba1",
            font=("Consolas", 9), width=W - TEXT_X - 34)

        # --- state ---
        self._pinned = False
        self._busy = False
        self._listening = False
        self._level_fn: Callable[[], float] | None = None
        self._hide_ticks = 0
        self._alpha = 0.0
        self._phase = 0.0
        self.root.attributes("-alpha", 0.0)

        self._make_click_through()
        self._tick()

    # -------------------- window plumbing --------------------

    def _make_click_through(self) -> None:
        """Mouse events pass straight through to whatever is underneath."""
        try:
            import ctypes

            GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x00080000, 0x00000020
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        except Exception:
            pass

    def post(self, fn: Callable[[], None]) -> None:
        self.root.after(0, fn)

    def set_level_source(self, fn: Callable[[], float]) -> None:
        """Give the overlay a callable returning the live mic level (0..1)."""
        self._level_fn = fn

    # -------------------- animation --------------------

    def _tick(self) -> None:
        active = self._pinned or self._busy
        if not active and self._hide_ticks > 0:
            self._hide_ticks -= 1
        visible_target = active or self._hide_ticks > 0

        target = MAX_ALPHA if visible_target else 0.0
        if self._alpha < target:
            self._alpha = min(target, self._alpha + FADE_STEP)
        elif self._alpha > target:
            self._alpha = max(target, self._alpha - FADE_STEP)
        try:
            self.root.attributes("-alpha", self._alpha)
            # slide up as it fades in
            offset = round(SLIDE_PX * (1 - self._alpha / MAX_ALPHA))
            self.root.geometry(f"{W}x{H}+{self._home_x}+{self._home_y + offset}")
        except tk.TclError:
            pass

        self._phase += 0.16
        pulse = (math.sin(self._phase) + 1) / 2 if self._busy else 0.0

        # orb: core + ring + halo
        r = ORB_R + 1.5 * pulse
        self.canvas.coords(self._core, ORB_CX - r, ORB_CY - r, ORB_CX + r, ORB_CY + r)
        self.canvas.itemconfig(self._core, fill=self._orb_color)

        rr = ORB_R + 6 + 4 * pulse
        self.canvas.coords(self._ring, ORB_CX - rr, ORB_CY - rr, ORB_CX + rr, ORB_CY + rr)
        self.canvas.itemconfig(self._ring, outline=self._orb_color if self._busy else "")

        hr = ORB_R + 13 + 7 * pulse
        self.canvas.coords(self._halo, ORB_CX - hr, ORB_CY - hr, ORB_CX + hr, ORB_CY + hr)
        self.canvas.itemconfig(self._halo,
                               outline=self._orb_color if (self._busy and pulse > 0.55) else "")

        self._draw_wave()
        self.root.after(TICK_MS, self._tick)

    def _draw_wave(self) -> None:
        """Live bars while listening; collapse to nothing otherwise."""
        lvl = 0.0
        if self._listening and self._level_fn is not None:
            try:
                lvl = max(0.0, min(1.0, float(self._level_fn())))
            except Exception:
                lvl = 0.0

        for i, bar in enumerate(self._bars):
            if not self._listening:
                self._bar_h[i] *= 0.7
            else:
                # Centre bars react more; a travelling wave keeps it alive even
                # at a steady input level.
                centre = 1 - abs(i - (WAVE_BARS - 1) / 2) / ((WAVE_BARS - 1) / 2)
                wobble = 0.55 + 0.45 * math.sin(self._phase * 1.6 + i * 0.7)
                target = 3 + lvl * 34 * (0.35 + 0.65 * centre) * wobble
                self._bar_h[i] += (target - self._bar_h[i]) * 0.45

            h = max(0.0, self._bar_h[i])
            if h < 0.6:
                self.canvas.coords(bar, 0, 0, 0, 0)
                continue
            x = WAVE_X + i * (WAVE_BAR_W + WAVE_GAP)
            self.canvas.coords(bar, x, ORB_CY - h / 2, x + WAVE_BAR_W, ORB_CY + h / 2)
            self.canvas.itemconfig(bar, fill=self._orb_color)

    # -------------------- public API --------------------

    @property
    def is_visible(self) -> bool:
        return self._alpha > 0.05

    def show(self) -> None:
        self._pinned = True

    def hide(self) -> None:
        self._pinned = False
        self._busy = False
        self._hide_ticks = 0

    def toggle(self) -> None:
        self.hide() if (self._pinned or self.is_visible) else self.show()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        if not busy:
            self._hide_ticks = HIDE_TICKS

    def set_listening(self, listening: bool) -> None:
        """While true the waveform animates from the live mic level."""
        self._listening = listening

    def set_status(self, text: str, color: str = "#94a3b8") -> None:
        def do():
            self._orb_color = color
            self.canvas.itemconfig(self._t_status, text=text.upper(), fill=color)
        self.post(do)

    def set_transcript(self, text: str) -> None:
        self.post(lambda: self.canvas.itemconfig(self._t_main, text=text))

    def append_trace(self, line: str) -> None:
        self.post(lambda: self.canvas.itemconfig(self._t_detail, text=line))

    def clear_trace(self) -> None:
        self.post(lambda: self.canvas.itemconfig(self._t_detail, text=""))

    def mainloop(self) -> None:
        self.root.mainloop()

    def quit(self) -> None:
        self.post(self.root.quit)
