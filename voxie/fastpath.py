"""Handle the common commands locally, without calling the model at all.

"Open notepad" was costing two Claude round-trips - one to choose the tool and
another purely to word the spoken confirmation - so a trivial action took the
better part of ten seconds. Most of what anyone actually says to a desktop
assistant is a small set of shapes, and those can be matched here in
microseconds.

The rule is confidence, not coverage: a pattern only matches when the intent is
unambiguous. Anything else - anything needing the screen, judgement, or more
than one step - falls through to the model, which is what it is good at.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("voxie.fastpath")


@dataclass
class Match:
    tool: str
    args: dict
    reply: str          # spoken confirmation, so no second API call is needed


# Spoken app names -> what open_app should receive.
APPS = {
    "notepad": "notepad", "note pad": "notepad",
    "calculator": "calc", "calc": "calc",
    "chrome": "chrome", "google chrome": "chrome",
    "firefox": "firefox", "edge": "msedge",
    "explorer": "explorer", "file explorer": "explorer", "files": "explorer",
    "terminal": "wt", "command prompt": "cmd", "cmd": "cmd",
    "powershell": "powershell",
    "vs code": "code", "vscode": "code", "visual studio code": "code",
    "code": "code", "spotify": "spotify", "discord": "discord",
    "steam": "steam", "paint": "mspaint", "task manager": "taskmgr",
    "settings": "ms-settings:", "word": "winword", "excel": "excel",
}

SITES = {
    "youtube": "https://youtube.com", "google": "https://google.com",
    "github": "https://github.com", "gmail": "https://mail.google.com",
    "reddit": "https://reddit.com", "twitter": "https://twitter.com",
    "x": "https://x.com", "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai", "telegram": "https://web.telegram.org",
    "whatsapp": "https://web.whatsapp.com", "netflix": "https://netflix.com",
    "amazon": "https://amazon.com", "wikipedia": "https://wikipedia.org",
    "stack overflow": "https://stackoverflow.com",
    "linkedin": "https://linkedin.com", "instagram": "https://instagram.com",
}

KEYS = {
    "copy": "ctrl+c", "paste": "ctrl+v", "cut": "ctrl+x",
    "undo": "ctrl+z", "redo": "ctrl+y", "save": "ctrl+s",
    "select all": "ctrl+a", "find": "ctrl+f", "print": "ctrl+p",
    "new tab": "ctrl+t", "close tab": "ctrl+w", "reopen tab": "ctrl+shift+t",
    "refresh": "f5", "reload": "f5", "escape": "esc", "enter": "enter",
    "delete": "delete", "backspace": "backspace", "tab": "tab",
    "switch window": "alt+tab", "switch windows": "alt+tab",
    "close window": "alt+f4", "lock": "win+l", "show desktop": "win+d",
}

# Filler that speech leaves on the front of a command.
_LEAD = re.compile(
    r"^(?:hey\s+)?(?:voxie|computer)?[,\s]*"
    r"(?:can you|could you|please|now|go ahead and|i want to|i'd like to)?[,\s]*",
    re.I,
)


def _clean(text: str) -> str:
    t = _LEAD.sub("", text.strip().lower())
    return t.strip(" .,!?")


def _lookup(name: str, table: dict[str, str]) -> str | None:
    name = name.strip(" .,!?'\"")
    if name in table:
        return table[name]
    # "chrome browser", "the notepad app"
    for k, v in table.items():
        if re.search(rf"\b{re.escape(k)}\b", name):
            return v
    return None


def match(transcript: str) -> Match | None:
    """Return a Match when the intent is unmistakable, otherwise None."""
    t = _clean(transcript)
    if not t:
        return None

    for rule in _RULES:
        m = rule(t)
        if m:
            log.info("fast path: %s -> %s(%s)", transcript, m.tool, m.args)
            return m
    return None


# --- individual rules -------------------------------------------------------

def _r_open(t: str) -> Match | None:
    m = re.match(r"^(?:open|launch|start|run)\s+(.+)$", t)
    if not m:
        return None
    target = m.group(1)

    # A website beats an app: "open youtube" means the site.
    site = _lookup(target, SITES)
    if site:
        return Match("open_url", {"url": site}, f"Opening {target}.")

    app = _lookup(target, APPS)
    if app:
        return Match("open_app", {"name": app}, f"Opening {target}.")

    # Bare domain - "open example.com"
    if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(?:/\S*)?", target):
        return Match("open_url", {"url": f"https://{target}"}, f"Opening {target}.")
    return None


def _r_scroll(t: str) -> Match | None:
    m = re.match(r"^scroll\s+(up|down|left|right)(?:\s+(a lot|a little|more))?$", t)
    if not m:
        return None
    amount = {"a lot": 12, "a little": 3, "more": 10}.get(m.group(2) or "", 5)
    return Match("scroll", {"direction": m.group(1), "amount": amount}, "")


def _r_window(t: str) -> Match | None:
    table = {
        r"^maximi[sz]e(?:\s+(?:this|the)?\s*window)?$": ("maximize", "Maximised."),
        r"^minimi[sz]e(?:\s+(?:this|the)?\s*window)?$": ("minimize", "Minimised."),
        r"^(?:snap\s+)?(?:this\s+)?(?:window\s+)?(?:to\s+the\s+)?left$": ("left", "Snapped left."),
        r"^(?:snap\s+)?(?:this\s+)?(?:window\s+)?(?:to\s+the\s+)?right$": ("right", "Snapped right."),
    }
    for pat, (where, reply) in table.items():
        if re.match(pat, t):
            return Match("snap_window", {"where": where}, reply)
    return None


def _r_key(t: str) -> Match | None:
    # "press ctrl+t" / "hit escape"
    m = re.match(r"^(?:press|hit|do)\s+(.+)$", t)
    if m:
        phrase = m.group(1).strip()

        # Raw combos are checked FIRST. The fuzzy lookup matches on word
        # boundaries, so "alt+tab" would otherwise hit the "tab" entry and
        # press the wrong key.
        spoken = phrase.replace(" plus ", "+").replace(" ", "")
        if re.fullmatch(r"(?:ctrl|alt|shift|win)(?:\+(?:ctrl|alt|shift|win))*\+\w+", spoken):
            return Match("press_key", {"keys": spoken}, "")
        if re.fullmatch(r"f\d{1,2}|enter|escape|esc|tab|space|delete|backspace", spoken):
            return Match("press_key", {"keys": spoken}, "")

        keys = KEYS.get(phrase) or _lookup(phrase, KEYS)
        return Match("press_key", {"keys": keys}, "") if keys else None

    # Bare shortcut names - "copy", "new tab"
    keys = KEYS.get(t)
    return Match("press_key", {"keys": keys}, "") if keys else None


def _r_clipboard(t: str) -> Match | None:
    if re.match(r"^(?:what'?s?\s+(?:on|in)\s+my\s+clipboard|read\s+(?:my\s+)?clipboard)", t):
        return Match("clipboard_read", {}, "")
    return None


def _r_screenshot(t: str) -> Match | None:
    if re.match(r"^(?:take\s+a\s+)?screenshot(?:\s+.*)?$", t):
        if "clipboard" in t or "copy" in t:
            return Match("screenshot_to_clipboard", {}, "Screenshot copied.")
        return Match("save_screenshot", {}, "Screenshot saved to your desktop.")
    return None


def _r_windows(t: str) -> Match | None:
    if re.match(r"^(?:what|which)\s+windows?\s+(?:do i have|are)\s+open", t) or \
       re.match(r"^list\s+(?:my\s+)?windows$", t):
        return Match("list_windows", {}, "")
    return None


_RULES: list[Callable[[str], Match | None]] = [
    _r_open, _r_scroll, _r_window, _r_key, _r_clipboard, _r_screenshot, _r_windows,
]
