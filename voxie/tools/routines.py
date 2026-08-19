"""Named multi-step routines ("start my dev setup").

A routine is a list of steps, each naming one of voxie's own tools and the
arguments to call it with. They live in a JSON file under %APPDATA%\\voxie so
they survive reinstalls, and they can be created by voice - Claude knows the
tool schema, so it can compose the steps itself and call save_routine.

Execution deliberately does NOT happen here: the step runner needs the same
dispatcher the tool loop uses, so llm.py handles run_routine specially. This
module only owns loading, saving and validation.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("voxie.routines")

STORE = Path(os.environ.get("APPDATA", str(Path.home()))) / "voxie" / "routines.json"

# Shipped as a starting point the first time voxie runs.
SEED: dict[str, Any] = {
    "dev setup": {
        "description": "Open VS Code and a terminal for a coding session",
        "steps": [
            {"tool": "open_app", "args": {"name": "code"}},
            {"tool": "wait", "args": {"seconds": 2}},
            {"tool": "open_app", "args": {"name": "wt"}},
        ],
    },
    "wind down": {
        "description": "Close the browser and open a music tab",
        "steps": [
            {"tool": "open_url", "args": {"url": "https://music.youtube.com"}},
        ],
    },
}


def _load_raw() -> dict[str, Any]:
    if not STORE.exists():
        try:
            STORE.parent.mkdir(parents=True, exist_ok=True)
            STORE.write_text(json.dumps(SEED, indent=2), encoding="utf-8")
            log.info("seeded routines at %s", STORE)
        except Exception as e:
            log.warning("could not seed routines: %s", e)
            return dict(SEED)
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("routines file unreadable (%s), falling back to defaults", e)
        return dict(SEED)


def _save_raw(data: dict[str, Any]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_routines() -> dict[str, Any]:
    """Names + descriptions of every saved routine."""
    data = _load_raw()
    return {
        "ok": True,
        "routines": [
            {"name": k, "description": v.get("description", ""), "steps": len(v.get("steps", []))}
            for k, v in data.items()
        ],
        "file": str(STORE),
    }


# Words people pad a spoken routine name with; ignored when matching.
_FILLER = {"run", "start", "launch", "do", "execute", "my", "the", "a", "an",
           "please", "routine", "workflow", "begin", "open", "up"}


def _keywords(text: str) -> set[str]:
    words = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower()).split()
    return {w for w in words if w not in _FILLER}


def get_routine(name: str) -> dict[str, Any] | None:
    """Find a routine by spoken name.

    Speech gives us padded, punctuated phrasings - "run my dev setup, please" -
    so exact and substring matching both miss. Compare the meaningful words
    instead and take the best-scoring routine.
    """
    data = _load_raw()
    key = name.lower().strip()
    if key in data:
        return data[key]

    said = _keywords(key)
    if not said:
        return None

    best, best_score = None, 0.0
    for k, v in data.items():
        words = _keywords(k)
        if not words:
            continue
        # How much of the routine's own name did they actually say?
        score = len(words & said) / len(words)
        if score > best_score:
            best, best_score = v, score

    # Needs most of the name, so "wind down" can't be triggered by "open down".
    return best if best_score >= 0.6 else None


def save_routine(name: str, description: str, steps: list[dict[str, Any]],
                 valid_tools: set[str] | None = None) -> dict[str, Any]:
    """Create or replace a routine. Steps are validated so a typo'd tool name
    fails now, with a clear message, rather than midway through a run."""
    if not steps:
        return {"ok": False, "error": "a routine needs at least one step"}

    for i, step in enumerate(steps):
        if not isinstance(step, dict) or "tool" not in step:
            return {"ok": False, "error": f"step {i} must be an object with a 'tool' key"}
        tool = step["tool"]
        if valid_tools is not None and tool not in valid_tools:
            return {"ok": False, "error": f"step {i}: unknown tool {tool!r}"}
        if "args" in step and not isinstance(step["args"], dict):
            return {"ok": False, "error": f"step {i}: 'args' must be an object"}

    data = _load_raw()
    data[name.lower().strip()] = {"description": description, "steps": steps}
    try:
        _save_raw(data)
    except Exception as e:
        return {"ok": False, "error": f"could not save: {e}"}
    return {"ok": True, "saved": name, "steps": len(steps)}


def delete_routine(name: str) -> dict[str, Any]:
    data = _load_raw()
    key = name.lower().strip()
    if key not in data:
        return {"ok": False, "error": f"no routine called {name!r}"}
    del data[key]
    try:
        _save_raw(data)
    except Exception as e:
        return {"ok": False, "error": f"could not save: {e}"}
    return {"ok": True, "deleted": name}
