"""Timing, text clipboard, and window layout helpers."""
from __future__ import annotations

import time
from typing import Any

import pyautogui
import pygetwindow as gw


def wait(seconds: float = 1.0) -> dict[str, Any]:
    """Pause before the next action — e.g. to let an app launch or a page load
    before taking a screenshot. Capped so a bad value can't hang voxie."""
    secs = max(0.0, min(10.0, float(seconds)))
    time.sleep(secs)
    return {"ok": True, "waited": secs}


def clipboard_write(text: str) -> dict[str, Any]:
    """Put text on the clipboard (paste it later with press_key ctrl+v)."""
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        return {"ok": True, "chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": f"clipboard write failed: {e}"}


def clipboard_read() -> dict[str, Any]:
    """Read text currently on the clipboard."""
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return {"ok": True, "text": str(data)[:5000]}
            return {"ok": True, "text": "", "note": "clipboard has no text"}
        finally:
            win32clipboard.CloseClipboard()
    except Exception as e:
        return {"ok": False, "error": f"clipboard read failed: {e}"}


def snap_window(where: str) -> dict[str, Any]:
    """Lay out the focused window: left / right / maximize / minimize / restore.
    Uses the Win+Arrow shortcuts for snapping so it matches native behaviour."""
    action = where.lower().strip()
    try:
        win = gw.getActiveWindow()
        if win is None:
            return {"ok": False, "error": "no active window"}

        if action in ("left", "right"):
            # Win+Left / Win+Right snap the focused window to that half.
            pyautogui.hotkey("win", action)
        elif action in ("maximize", "max", "full"):
            pyautogui.hotkey("win", "up")
        elif action in ("minimize", "min"):
            pyautogui.hotkey("win", "down")
        elif action in ("restore",):
            pyautogui.hotkey("win", "down")  # from maximized, one Win+Down restores
        else:
            return {"ok": False, "error": f"unknown layout: {where}"}
        return {"ok": True, "window": win.title, "layout": action}
    except Exception as e:
        return {"ok": False, "error": f"snap failed: {e}"}
