"""Launch apps + focus windows."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pygetwindow as gw


def open_app(name: str) -> dict[str, Any]:
    """Launch an app by name. Uses `start` on Windows so anything on PATH or in
    the Start Menu resolves. Common aliases are pre-mapped."""
    aliases = {
        "vscode": "code",
        "visual studio code": "code",
        "code editor": "code",
        "browser": "chrome",
        "google chrome": "chrome",
        "file explorer": "explorer",
        "files": "explorer",
        "terminal": "wt",  # Windows Terminal
        "cmd": "cmd",
        "powershell": "powershell",
        "notepad": "notepad",
    }
    key = name.lower().strip()
    target = aliases.get(key, name)
    try:
        # `start` is a cmd builtin — needs shell=True.
        subprocess.Popen(["start", "", target], shell=True)
        return {"ok": True, "opened": target}
    except Exception as e:
        return {"ok": False, "error": f"could not launch '{name}': {e}"}


def focus_window(title_contains: str) -> dict[str, Any]:
    """Bring the first window whose title contains the given substring to front."""
    needle = title_contains.lower().strip()
    matches = [w for w in gw.getAllWindows() if needle in w.title.lower() and w.title]
    if not matches:
        return {"ok": False, "error": f"no window title contains '{title_contains}'"}
    win = matches[0]
    try:
        if win.isMinimized:
            win.restore()
        win.activate()
        return {"ok": True, "focused": win.title}
    except Exception as e:
        return {"ok": False, "error": f"could not focus window: {e}"}


def list_windows() -> dict[str, Any]:
    """Return the titles of all visible windows — useful when Claude is looking
    for the right window to focus."""
    titles = [w.title for w in gw.getAllWindows() if w.title]
    return {"ok": True, "windows": titles}


def open_url(url: str) -> dict[str, Any]:
    """Open a URL in the default browser."""
    try:
        os.startfile(url) if hasattr(os, "startfile") else subprocess.Popen(["xdg-open", url])
        return {"ok": True, "opened": url}
    except Exception as e:
        return {"ok": False, "error": f"could not open URL: {e}"}
