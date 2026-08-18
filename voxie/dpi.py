"""Make the process DPI-aware so screen capture and mouse clicks share ONE
coordinate space.

The subtle bug this prevents: if the process is DPI-unaware on a scaled display
(125%/150%), Windows virtualises coordinates — mss captures physical pixels
while pyautogui clicks in logical pixels, so every click lands off by the
scaling factor. Declaring per-monitor DPI awareness makes both operate in real
physical pixels and agree.

MUST be called before anything imports pyautogui or grabs the screen — a
process's DPI awareness can only be set once, and the first setter wins.
"""
from __future__ import annotations

import ctypes
import logging

log = logging.getLogger("voxie.dpi")

# shcore PROCESS_DPI_AWARENESS values
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def set_dpi_awareness() -> None:
    # Prefer per-monitor awareness (best for mixed-DPI multi-monitor). Fall back
    # to the older system-wide call on Windows versions without shcore.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)
        log.info("set per-monitor DPI awareness")
        return
    except (AttributeError, OSError) as e:
        # OSError E_ACCESSDENIED means awareness was already set by something
        # else (e.g. pyautogui at import) — that's fine, coordinates are still
        # consistent, just not per-monitor.
        log.info("SetProcessDpiAwareness unavailable/already set (%s)", e)

    try:
        ctypes.windll.user32.SetProcessDPIAware()
        log.info("set system DPI awareness (fallback)")
    except Exception as e:
        log.info("could not set DPI awareness: %s", e)
