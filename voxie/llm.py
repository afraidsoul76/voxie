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
- Prefer the SIMPLEST tool that does the job. Don't screenshot if the user just
  asked you to open an app or run a command.
- When you need to click something on screen, base your coordinates on the
  attached screenshot. The screenshot's coordinate system starts at (0,0) in
  the top-left. Use `click_xy` with those coordinates.
- Keyboard shortcuts are almost always better than clicking. Prefer `press_key`
  when the target has a shortcut (Ctrl+T for new tab, Alt+F4 to close, etc.).
- If the user's request is destructive (delete, remove, close everything,
  shut down), the shell tool will refuse without confirmation. Ask them out
  loud to confirm and only re-run if they clearly say yes.
- Keep your final spoken reply to ONE short sentence — the user will hear
  it via text-to-speech. Skip preamble like "Sure!" or "I'll do that."
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
        "name": "click_xy",
        "description": "Click at an (x, y) coordinate on the primary display. Coordinates are relative to the screenshot you were given.",
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


def _make_dispatcher(shot: screen.Screenshot | None) -> dict[str, Callable[..., dict]]:
    """Wire each tool name to its handler. click_xy is closed over the screenshot
    so we can rescale coords from what-Claude-saw back to real-pixels."""

    def click_xy(x: int, y: int, button: str = "left") -> dict:
        if shot is not None:
            x, y = shot.to_screen_coords(x, y)
        return input_tools.click_xy(x, y, button=button)

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
        include_screenshot: bool = True,
        on_tool: Callable[[str, dict], None] | None = None,
    ) -> str:
        """Execute the loop. Returns the model's final spoken text.

        `on_tool` is called after each tool executes so the UI can render a
        live "voxie is doing X" trace.
        """
        shot = screen.capture_primary() if include_screenshot else None
        dispatcher = _make_dispatcher(shot)

        user_content: list[dict[str, Any]] = []
        if shot is not None:
            user_content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": shot.media_type, "data": shot.image_b64},
            })
        user_content.append({"type": "text", "text": transcript})

        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
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
