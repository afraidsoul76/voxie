"""voxie's floating assistant pill.

Layout is zone-based rather than hardcoded offsets, because the previous
version drew the waveform and the text at the same x and they overlapped.

    [ orb ]  [            content zone            ]

The content zone shows EITHER the live waveform (while listening) or the
text block (everything else) - never both, cross-faded between the two.
Text is measured and ellipsised so it can never spill outside the card.

The card, its gradient and its anti-aliased rounded corners are rendered with
PIL at 4x and downsampled. Transparency is Tk's -transparentcolor chroma key;
the card composites onto that key, and since the key is near-black and the card
is dark navy, the anti-aliased edge reads as a soft rim, not a fringe.

Threading: all Tk mutation happens on the mainloop. Other threads set plain
flags or schedule work via post(); the animation tick runs on the mainloop.
"""
from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

from PIL import Image, ImageDraw, ImageTk

_CHROMA = "#010203"
_CHROMA_RGB = (1, 2, 3)

# --- geometry ---
W, H = 640, 116
PAD = 14                     # window edge -> card edge
RADIUS = 24
SS = 4                       # supersample factor

ORB_CX = 58
ORB_R = 12

CONTENT_X = 104              # content zone starts well clear of the orb
CONTENT_R = W - 34           # right edge of the content zone
CONTENT_W = CONTENT_R - CONTENT_X

# --- palette ---
CARD_TOP = (19, 26, 43)
CARD_BOT = (11, 16, 30)
BORDER = (36, 48, 70)
FG = "#f1f5f9"
FG_DIM = "#8ea0b8"

# --- motion ---
MAX_ALPHA = 0.97
FADE_STEP = 0.10
TICK_MS = 33
HIDE_TICKS = 150
SLIDE_PX = 16
XFADE_STEP = 0.18            # waveform <-> text cross-fade

# --- waveform ---
WAVE_BARS = 22
WAVE_BAR_W = 3
WAVE_GAP = 5


def _lerp_hex(c1: str, c2: str, t: float) -> str:
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _render_card() -> Image.Image:
    """Rounded, vertically-graded card with a hairline border + top highlight."""
    w, h = (W - 2 * PAD) * SS, (H - 2 * PAD) * SS

    grad = Image.new("RGB", (1, h))
    gd = ImageDraw.Draw(grad)
    for y in range(h):
        t = y / max(1, h - 1)
        gd.point((0, y), fill=tuple(
            round(CARD_TOP[i] + (CARD_BOT[i] - CARD_TOP[i]) * t) for i in range(3)))
    grad = grad.resize((w, h))

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), RADIUS * SS, fill=255)

    card = Image.new("RGB", (w, h), _CHROMA_RGB)
    card.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(card)
    d.rounded_rectangle((0, 0, w - 1, h - 1), RADIUS * SS, outline=BORDER, width=SS)
    # A brighter arc along the top edge gives the card a lit, glassy feel.
    d.arc((SS, SS, w - SS, h * 2), start=180, end=360, fill=(58, 74, 102), width=SS)

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
        self._home_y = sh - H - 84
        self.root.geometry(f"{W}x{H}+{self._home_x}+{self._home_y}")

        self.canvas = tk.Canvas(self.root, width=W, height=H, bg=_CHROMA,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self._card_img = ImageTk.PhotoImage(_render_card())
        self.canvas.create_image(0, 0, anchor="nw", image=self._card_img)

        cy = H // 2

        # --- orb: halo ring, mid ring, core ---
        self._accent = "#94a3b8"
        self._halo = self.canvas.create_oval(0, 0, 0, 0, outline="", width=1)
        self._ring = self.canvas.create_oval(0, 0, 0, 0, outline="", width=2)
        self._core = self.canvas.create_oval(0, 0, 0, 0, outline="", fill=self._accent)

        # --- content zone A: waveform ---
        self._bars = [self.canvas.create_rectangle(0, 0, 0, 0, outline="", fill="")
                      for _ in range(WAVE_BARS)]
        self._bar_h = [0.0] * WAVE_BARS

        # --- content zone B: text ---
        self._f_label = tkfont.Font(family="Segoe UI Semibold", size=8)
        self._f_main = tkfont.Font(family="Segoe UI", size=13)
        self._f_detail = tkfont.Font(family="Consolas", size=8)

        self._t_label = self.canvas.create_text(
            CONTENT_X, cy - 24, anchor="w", text="", fill=FG_DIM, font=self._f_label)
        self._t_main = self.canvas.create_text(
            CONTENT_X, cy + 1, anchor="w", text="", fill=FG, font=self._f_main)
        self._t_detail = self.canvas.create_text(
            CONTENT_X, cy + 25, anchor="w", text="", fill=FG_DIM, font=self._f_detail)

        # --- state ---
        self._pinned = False
        self._busy = False
        self._listening = False
        self._level_fn: Callable[[], float] | None = None
        self._hide_ticks = 0
        self._alpha = 0.0
        self._phase = 0.0
        self._wave_mix = 0.0        # 0 = text visible, 1 = waveform visible
        self._label_raw = ""
        self._main_raw = ""
        self._detail_raw = ""
        self.root.attributes("-alpha", 0.0)

        self._make_click_through()
        self._tick()

    # -------------------- window plumbing --------------------

    def _make_click_through(self) -> None:
        try:
            import ctypes

            GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x00080000, 0x00000020
            self._hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                self._hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        except Exception:
            self._hwnd = None

    def _reassert_topmost(self) -> None:
        """Keep the pill above other windows without stealing focus.

        A frameless (overrideredirect) window quietly loses its topmost z-order
        when another app is activated - the symptom was the pill vanishing the
        moment you clicked another app and never coming back. Tk's
        attributes("-topmost") can also pull focus, so this goes through
        SetWindowPos with SWP_NOACTIVATE instead.
        """
        if not getattr(self, "_hwnd", None):
            return
        try:
            import ctypes

            HWND_TOPMOST = -1
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
            ctypes.windll.user32.SetWindowPos(
                self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        except Exception:
            pass

    def post(self, fn: Callable[[], None]) -> None:
        self.root.after(0, fn)

    def set_level_source(self, fn: Callable[[], float]) -> None:
        self._level_fn = fn

    # -------------------- text fitting --------------------

    def _fit(self, text: str, font: tkfont.Font) -> str:
        """Ellipsise text so it always fits the content zone."""
        if not text:
            return ""
        text = " ".join(text.split())          # collapse newlines/runs
        if font.measure(text) <= CONTENT_W:
            return text
        ell = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            if font.measure(text[:mid] + ell) <= CONTENT_W:
                lo = mid + 1
            else:
                hi = mid
        return text[:max(0, lo - 1)].rstrip() + ell

    def _refresh_text(self) -> None:
        self.canvas.itemconfig(self._t_label, text=self._fit(self._label_raw, self._f_label))
        self.canvas.itemconfig(self._t_main, text=self._fit(self._main_raw, self._f_main))
        self.canvas.itemconfig(self._t_detail, text=self._fit(self._detail_raw, self._f_detail))

    # -------------------- animation --------------------

    def _tick(self) -> None:
        active = self._pinned or self._busy
        if not active and self._hide_ticks > 0:
            self._hide_ticks -= 1
        visible = active or self._hide_ticks > 0

        target = MAX_ALPHA if visible else 0.0
        if self._alpha < target:
            self._alpha = min(target, self._alpha + FADE_STEP)
        elif self._alpha > target:
            self._alpha = max(target, self._alpha - FADE_STEP)
        try:
            self.root.attributes("-alpha", self._alpha)
            offset = round(SLIDE_PX * (1 - self._alpha / MAX_ALPHA))
            self.root.geometry(f"{W}x{H}+{self._home_x}+{self._home_y + offset}")
        except tk.TclError:
            pass

        self._phase += 0.15
        pulse = (math.sin(self._phase) + 1) / 2 if self._busy else 0.0

        # cross-fade waveform <-> text
        wt = 1.0 if self._listening else 0.0
        self._wave_mix += (wt - self._wave_mix) * XFADE_STEP

        # Cheap insurance against losing z-order to a newly focused app.
        self._topmost_tick = getattr(self, '_topmost_tick', 0) + 1
        if self._alpha > 0.05 and self._topmost_tick % 15 == 0:
            self._reassert_topmost()

        self._draw_orb(pulse)
        self._draw_wave()
        self._draw_text_fade()

        self.root.after(TICK_MS, self._tick)

    def _draw_orb(self, pulse: float) -> None:
        cy = H // 2
        r = ORB_R + 1.5 * pulse
        self.canvas.coords(self._core, ORB_CX - r, cy - r, ORB_CX + r, cy + r)
        self.canvas.itemconfig(self._core, fill=self._accent)

        rr = ORB_R + 7 + 4 * pulse
        self.canvas.coords(self._ring, ORB_CX - rr, cy - rr, ORB_CX + rr, cy + rr)
        # Ring dims as the pulse falls, so it breathes rather than blinks.
        self.canvas.itemconfig(
            self._ring,
            outline=_lerp_hex("#1b2537", self._accent, pulse) if self._busy else "")

        hr = ORB_R + 15 + 6 * pulse
        self.canvas.coords(self._halo, ORB_CX - hr, cy - hr, ORB_CX + hr, cy + hr)
        self.canvas.itemconfig(
            self._halo,
            outline=_lerp_hex("#141d2c", self._accent, pulse * 0.45) if self._busy else "")

    def _draw_wave(self) -> None:
        cy = H // 2
        mix = self._wave_mix
        if mix < 0.02:
            for b in self._bars:
                self.canvas.coords(b, 0, 0, 0, 0)
            return

        lvl = 0.0
        if self._listening and self._level_fn is not None:
            try:
                lvl = max(0.0, min(1.0, float(self._level_fn())))
            except Exception:
                lvl = 0.0

        span = WAVE_BARS * WAVE_BAR_W + (WAVE_BARS - 1) * WAVE_GAP
        x0 = CONTENT_X + max(0, (CONTENT_W - span) // 2)

        for i, bar in enumerate(self._bars):
            centre = 1 - abs(i - (WAVE_BARS - 1) / 2) / ((WAVE_BARS - 1) / 2)
            if self._listening:
                wobble = 0.5 + 0.5 * math.sin(self._phase * 1.7 + i * 0.55)
                tgt = 4 + lvl * 40 * (0.3 + 0.7 * centre) * wobble
            else:
                tgt = 0.0
            self._bar_h[i] += (tgt - self._bar_h[i]) * 0.4

            h = max(0.0, self._bar_h[i]) * mix
            if h < 0.8:
                self.canvas.coords(bar, 0, 0, 0, 0)
                continue
            x = x0 + i * (WAVE_BAR_W + WAVE_GAP)
            self.canvas.coords(bar, x, cy - h / 2, x + WAVE_BAR_W, cy + h / 2)
            # Outer bars sit dimmer than the centre for a softer silhouette.
            self.canvas.itemconfig(
                bar, fill=_lerp_hex("#243348", self._accent, 0.35 + 0.65 * centre))

    def _draw_text_fade(self) -> None:
        """Text dims out as the waveform takes over the content zone."""
        t = 1 - self._wave_mix
        if t < 0.02:
            for item in (self._t_label, self._t_main, self._t_detail):
                self.canvas.itemconfig(item, fill=_CHROMA)
            return
        self.canvas.itemconfig(self._t_label, fill=_lerp_hex("#0e1626", self._accent, t))
        self.canvas.itemconfig(self._t_main, fill=_lerp_hex("#0e1626", FG, t))
        self.canvas.itemconfig(self._t_detail, fill=_lerp_hex("#0e1626", FG_DIM, t))

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
        self._listening = listening

    def set_status(self, text: str, color: str = "#94a3b8") -> None:
        def do():
            self._accent = color
            self._label_raw = text.upper()
            self._refresh_text()
        self.post(do)

    def set_transcript(self, text: str) -> None:
        def do():
            self._main_raw = text
            self._refresh_text()
        self.post(do)

    def append_trace(self, line: str) -> None:
        def do():
            self._detail_raw = line
            self._refresh_text()
        self.post(do)

    def clear_trace(self) -> None:
        def do():
            self._detail_raw = ""
            self._refresh_text()
        self.post(do)

    def mainloop(self) -> None:
        self.root.mainloop()

    def quit(self) -> None:
        self.post(self.root.quit)
