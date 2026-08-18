"""Screen capture for vision-grounded clicking."""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import mss
from PIL import Image


@dataclass
class Screenshot:
    """Captured screen: base64-encoded PNG + the raw pixel dimensions.

    We downscale for the LLM (vision APIs bill per pixel + accuracy caps out
    around 1568px on the long side for Claude), but keep the ORIGINAL pixel
    dimensions so we can rescale coordinates the model returns back to real
    screen coordinates for pyautogui.
    """

    image_b64: str
    media_type: str  # "image/png"
    orig_width: int  # real screen pixels
    orig_height: int
    sent_width: int  # what Claude saw
    sent_height: int

    def to_screen_coords(self, x: int, y: int) -> tuple[int, int]:
        """Rescale a coordinate reported against sent_* back to orig_* pixels."""
        sx = round(x * self.orig_width / self.sent_width)
        sy = round(y * self.orig_height / self.sent_height)
        return sx, sy


def capture_primary(max_long_side: int = 1568) -> Screenshot:
    with mss.mss() as sct:
        mon = sct.monitors[1]  # index 0 is the union of all monitors
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.rgb)

    orig_w, orig_h = img.size
    long_side = max(orig_w, orig_h)
    if long_side > max_long_side:
        scale = max_long_side / long_side
        new_size = (round(orig_w * scale), round(orig_h * scale))
        img = img.resize(new_size, Image.LANCZOS)

    sent_w, sent_h = img.size
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return Screenshot(
        image_b64=b64,
        media_type="image/png",
        orig_width=orig_w,
        orig_height=orig_h,
        sent_width=sent_w,
        sent_height=sent_h,
    )
