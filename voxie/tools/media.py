"""Get a screenshot OUT of voxie — to a file or onto the clipboard — so it can
be attached, uploaded, or pasted into another app (Telegram, Discord, docs...).
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from . import screen
from .files import _resolve  # reuse ~/Desktop/etc path resolution


def save_screenshot(path: str | None = None) -> dict[str, Any]:
    """Capture the primary display and write it to a PNG file. Defaults to a
    file on the Desktop. Returns the saved path so it can be attached/uploaded."""
    img = screen.capture_full_pil()
    if path:
        target = _resolve(path)
        if target.suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp"):
            target = target.with_suffix(".png")
    else:
        # Stable default name; overwrites the previous quick-capture.
        target = _resolve("Desktop/voxie_screenshot.png")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(target))
        return {"ok": True, "path": str(target), "size": list(img.size)}
    except Exception as e:
        return {"ok": False, "error": f"could not save screenshot: {e}"}


def screenshot_to_clipboard() -> dict[str, Any]:
    """Put a screenshot of the primary display on the clipboard as an image, so
    it can be pasted (Ctrl+V) straight into a chat box, doc, or editor."""
    img = screen.capture_full_pil()
    try:
        import win32clipboard  # from pywin32

        # Windows clipboard wants a DIB — a BMP with its 14-byte file header
        # stripped off.
        out = io.BytesIO()
        img.convert("RGB").save(out, "BMP")
        dib = out.getvalue()[14:]
        out.close()

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
        finally:
            win32clipboard.CloseClipboard()
        return {"ok": True, "note": "screenshot is on the clipboard; paste with press_key ctrl+v"}
    except Exception as e:
        return {"ok": False, "error": f"could not copy image to clipboard: {e}"}
