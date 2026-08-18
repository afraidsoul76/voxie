"""Read + write files by path.

Writing a file used to go through `run_shell` with echo + redirection, which
is fragile (cmd escaping) and trips the destructive-command gate (redirection
can clobber files). A dedicated tool is cleaner and safer.

Path handling resolves ~, environment variables, and the common special
folders (Desktop / Documents / Downloads) — including their OneDrive-redirected
locations, which is the usual Windows gotcha.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _special_dir(name: str) -> Path:
    home = Path.home()
    # OneDrive often redirects these; prefer the real one that exists.
    candidates = [home / name, home / "OneDrive" / name]
    for c in candidates:
        if c.is_dir():
            return c
    return home / name


_SPECIALS = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads"}


def _resolve(path: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(path.strip()))
    p = Path(expanded)

    # If the path is relative and starts with a known special folder name,
    # anchor it to the real (possibly OneDrive) location.
    if not p.is_absolute():
        parts = p.parts
        if parts and parts[0].lower() in _SPECIALS:
            base = _special_dir(_SPECIALS[parts[0].lower()])
            return base.joinpath(*parts[1:]) if len(parts) > 1 else base
        return Path.home() / p
    return p


def write_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Write text to a file. Refuses to overwrite an existing file unless
    overwrite=true, so 'make a file' can't silently clobber something."""
    target = _resolve(path)
    if target.exists() and not overwrite:
        return {
            "ok": False,
            "needs_confirmation": True,
            "error": f"{target} already exists. Confirm with the user, then re-call with overwrite=true.",
        }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(target), "bytes": len(content)}
    except Exception as e:
        return {"ok": False, "error": f"could not write {target}: {e}"}


def read_file(path: str, max_chars: int = 5000) -> dict[str, Any]:
    target = _resolve(path)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
        truncated = len(text) > max_chars
        return {"ok": True, "path": str(target), "content": text[:max_chars], "truncated": truncated}
    except FileNotFoundError:
        return {"ok": False, "error": f"no such file: {target}"}
    except Exception as e:
        return {"ok": False, "error": f"could not read {target}: {e}"}
