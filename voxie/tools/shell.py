"""Shell execution — deliberately conservative because voice input into a
shell is a foot-gun."""
from __future__ import annotations

import subprocess
from typing import Any

# Anything matching these keywords requires the caller to have run a
# confirmation flow first. The tool refuses to run them without confirm=True.
DESTRUCTIVE_KEYWORDS = (
    "rm ", "del ", "rmdir", "format", "diskpart",
    "shutdown", "restart", "reboot",
    "> ", ">>",  # output redirection can trash files
    "reg delete", "reg add",
    "sudo", "runas",
    "kill", "taskkill",
    "curl -X DELETE", "curl -x delete",
)


def is_destructive(command: str) -> bool:
    lc = command.lower()
    return any(kw in lc for kw in DESTRUCTIVE_KEYWORDS)


def run_shell(command: str, confirmed: bool = False) -> dict[str, Any]:
    """Run a shell command via cmd. Destructive commands need `confirmed=True`."""
    if is_destructive(command) and not confirmed:
        return {
            "ok": False,
            "needs_confirmation": True,
            "error": (
                f"Command '{command}' looks destructive. Ask the user to confirm "
                "out loud, then call this tool again with confirmed=true."
            ),
        }
    try:
        # 15-second cap — voice control shouldn't be running long-lived processes.
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=15
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout[-2000:],  # cap payload
            "stderr": result.stderr[-1000:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "command timed out after 15s"}
    except Exception as e:
        return {"ok": False, "error": f"shell failed: {e}"}
