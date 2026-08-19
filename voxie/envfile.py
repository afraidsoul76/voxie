"""Read and write .env in place, preserving comments and key order.

The settings window edits the same .env a user might hand-edit, so rewriting
the file from a dict would throw away their comments and layout. This updates
the value on an existing key's line and only appends genuinely new keys.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("voxie.envfile")

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        values[k.strip()] = v.strip()
    return values


def write_env(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Apply `updates` to the file, keeping every comment and blank line."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)

    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        for k, v in remaining.items():
            out.append(f"{k}={v}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    log.info("wrote %d setting(s) to %s", len(updates), path)
