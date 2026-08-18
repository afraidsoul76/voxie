"""The Claude tool-use loop.

We keep the tool schema in one place so it stays in sync with the actual
handler dispatch below. Each turn the model may either:
    - return a final text response (loop ends), or
    - request one or more tool_use blocks, which we execute and feed back as
      tool_result blocks in the next request.

Optional screenshot: passed as an image content block on the FIRST user turn
so the model can ground click coordinates against what's actually on screen.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from anthropic import Anthropic

from .tools import apps, input as input_tools, screen, shell

log = logging.getLogger("voxie.llm")

SYSTEM_PROMPT = """You are voxie, a voice-controlled desktop assistant.
The user speaks a command; you carry it out by calling the tools below.

Guidelines:
- Prefer the SIMPLEST tool that does the job. Most commands (open an app, run a
  command, press a key, focus a window) need NO screenshot — don't take one.
- Only when you must click something by position: call `take_screenshot` first,
  then `click_xy` with coordinates in that screenshot's pixel space (top-left is
  0,0). The screenshot result tells you its width and height.
- If a click doesn't land where you expected, take a fresh screenshot to see the
  new state before trying again — don't guess twice.
- Keyboard shortcuts are almost always better than clicking. Prefer `press_key`
  when the target has a shortcut (Ctrl+T for new tab, Alt+F4 to close, etc.).
- If the user's request is destructive (delete, remove, close everything,
  shut down), the shell tool will refuse without confirmation. Ask them out
  loud to confirm and only re-run if they clearly say yes.
- Keep your final spoken reply to ONE short sentence — the user will hear
  it via text-to-speech. Skip preamble like "Sure!" or "I'll do that."
- ALWAYS reply in English, regardless of app names or on-screen text in
  other languages. The reply is spoken aloud by an English voice.
- If a tool call fails, tell the user briefly what went wrong instead of
  silently retrying five times.
"""


TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "open_app",
        "description": "Launch a desktop application by its name (e.g. 'chrome', 'code', 'notepad', 'terminal').",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "focus_window",
        "description": "Bring a window to the front. Matches the first window whose title contains the given substring (case-insensitive).",
        "input_schema": {
            "type": "object",
            "properties": {"title_contains": {"type": "string"}},
            "required": ["title_contains"],
        },
    },
    {
        "name": "list_windows",
        "description": "List all visible window titles — useful when you're not sure which window the user meant.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "take_screenshot",
        "description": "Capture the current screen so you can SEE it. Call this before clicking anything by position, or when the user asks about what's on screen. Returns an image; note its pixel dimensions for your click coordinates.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "click_xy",
        "description": "Click at an (x, y) coordinate on the primary display. Coordinates must be relative to the most recent screenshot you took with take_screenshot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text at the current keyboard focus.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key or hotkey combo (e.g. 'enter', 'esc', 'ctrl+t', 'alt+tab').",
        "input_schema": {
            "type": "object",
            "properties": {"keys": {"type": "string"}},
            "required": ["keys"],
        },
    },
    {
        "name": "open_url",
        "description": "Open a URL in the default browser.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "run_shell",
        "description": "Run a shell command via cmd. Destructive commands (rm, del, shutdown, kill, etc.) require confirmed=true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["command"],
        },
    },
]


def _make_dispatcher(shot_holder: dict[str, screen.Screenshot | None]) -> dict[str, Callable[..., dict]]:
    """Wire each tool name to its handler.

    click_xy reads the most-recent screenshot from `shot_holder` so it can
    rescale the coords Claude gave (against the downscaled image it saw) back
    to real screen pixels. take_screenshot is handled specially in the loop
    (it returns an image block, not a plain dict), so it isn't here.
    """

    def click_xy(x: int, y: int, button: str = "left") -> dict:
        shot = shot_holder.get("shot")
        if shot is not None:
            x, y = shot.to_screen_coords(x, y)
            return input_tools.click_xy(x, y, button=button)
        return {
            "ok": False,
            "error": "no screenshot yet — call take_screenshot before clicking by position",
        }

    return {
        "open_app": apps.open_app,
        "focus_window": apps.focus_window,
        "list_windows": apps.list_windows,
        "click_xy": click_xy,
        "type_text": input_tools.type_text,
        "press_key": input_tools.press_key,
        "open_url": apps.open_url,
        "run_shell": shell.run_shell,
    }


class Assistant:
    """Runs one full turn: transcript in → actions performed + spoken reply out."""

    MAX_TOOL_ROUNDS = 6  # safety cap on tool-use loop

    def __init__(self, api_key: str, base_url: str | None, model: str) -> None:
        self.client = Anthropic(api_key=api_key, base_url=base_url)
        self.model = model

    def run(
        self,
        transcript: str,
        on_tool: Callable[[str, dict], None] | None = None,
    ) -> str:
        """Execute the loop. Returns the model's final spoken text.

        No screenshot is sent up front — Claude calls take_screenshot only when
        it actually needs to see the screen. Most commands (open an app, run a
        command, press a key) never need vision, so this skips the capture +
        upload cost on the common path.

        `on_tool` is called after each tool executes so the UI can render a
        live "voxie is doing X" trace.
        """
        shot_holder: dict[str, screen.Screenshot | None] = {"shot": None}
        dispatcher = _make_dispatcher(shot_holder)

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"type": "text", "text": transcript}]}
        ]
        final_text = ""

        for _ in range(self.MAX_TOOL_ROUNDS):
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS_SCHEMA,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                for block in resp.content:
                    if getattr(block, "type", None) == "text":
                        final_text = block.text.strip()
                        break
                return final_text or "Done."

            # Execute every tool_use block the model requested this turn.
            tool_results: list[dict[str, Any]] = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                name = block.name
                args = dict(block.input or {})

                # take_screenshot is special: it returns an image block so the
                # model can actually see the screen, and it stashes the shot so
                # click_xy can rescale coordinates against it.
                if name == "take_screenshot":
                    shot = screen.capture_primary()
                    shot_holder["shot"] = shot
                    note = {"ok": True, "width": shot.sent_width, "height": shot.sent_height}
                    if on_tool:
                        try:
                            on_tool(name, note)
                        except Exception:
                            log.exception("on_tool callback raised")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": [
                            {"type": "text",
                             "text": f"Screenshot captured. Image is {shot.sent_width}x{shot.sent_height} px; "
                                     f"give click coordinates in that space."},
                            {"type": "image",
                             "source": {"type": "base64", "media_type": shot.media_type, "data": shot.image_b64}},
                        ],
                    })
                    continue

                handler = dispatcher.get(name)
                if handler is None:
                    result = {"ok": False, "error": f"unknown tool: {name}"}
                else:
                    try:
                        result = handler(**args)
                    except TypeError as e:
                        result = {"ok": False, "error": f"bad arguments to {name}: {e}"}
                    except Exception as e:
                        log.exception("tool %s crashed", name)
                        result = {"ok": False, "error": f"{name} crashed: {e}"}
                if on_tool:
                    try:
                        on_tool(name, result)
                    except Exception:
                        log.exception("on_tool callback raised")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                })
            messages.append({"role": "user", "content": tool_results})

        return final_text or "I stopped after too many steps. Try a smaller command."
