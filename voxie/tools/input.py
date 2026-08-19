"""Mouse + keyboard emulation via pyautogui."""
from __future__ import annotations

from typing import Any

import pyautogui

# pyautogui slams events with no delay by default. A small pause between
# actions is safer + more reliable when clicking things in real UIs.
pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True  # slam cursor to corner to abort


def click_xy(x: int, y: int, button: str = "left") -> dict[str, Any]:
    """Click at a specific screen coordinate. `button` is left/right/middle."""
    try:
        pyautogui.click(x, y, button=button)
        return {"ok": True, "clicked": [x, y]}
    except Exception as e:
        return {"ok": False, "error": f"click failed: {e}"}


def type_text(text: str) -> dict[str, Any]:
    """Type text at the current focus. Uses a small interval so fast typing
    doesn't drop characters in some apps."""
    try:
        pyautogui.typewrite(text, interval=0.01)
        return {"ok": True, "typed_chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": f"type failed: {e}"}


def press_key(keys: str) -> dict[str, Any]:
    """Press a key or hotkey. Hotkeys use `+`, e.g. 'ctrl+c', 'alt+tab'."""
    try:
        if "+" in keys:
            parts = [k.strip() for k in keys.split("+")]
            pyautogui.hotkey(*parts)
        else:
            pyautogui.press(keys)
        return {"ok": True, "pressed": keys}
    except Exception as e:
        return {"ok": False, "error": f"press failed: {e}"}


def scroll(direction: str = "down", amount: int = 5) -> dict[str, Any]:
    """Scroll the surface under the cursor. direction: up/down/left/right.
    `amount` is in notches; one notch ≈ a few lines."""
    clicks = amount * 120  # pyautogui scroll unit ≈ 120 per notch on Windows
    try:
        d = direction.lower().strip()
        if d in ("up", "down"):
            pyautogui.scroll(clicks if d == "up" else -clicks)
        elif d in ("left", "right"):
            pyautogui.hscroll(clicks if d == "right" else -clicks)
        else:
            return {"ok": False, "error": f"unknown scroll direction: {direction}"}
        return {"ok": True, "scrolled": direction, "amount": amount}
    except Exception as e:
        return {"ok": False, "error": f"scroll failed: {e}"}
