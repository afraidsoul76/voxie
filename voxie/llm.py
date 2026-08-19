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

from .tools import apps, files, input as input_tools, media, routines, screen, shell, system

log = logging.getLogger("voxie.llm")

SYSTEM_PROMPT = """You are voxie, a voice assistant that can both ANSWER and ACT.

First decide which the user wants:

ANSWER - they asked a question, wanted an explanation, or were chatting
  ("what is X", "how do I Y", "what time is it in Tokyo", "explain closures").
  Just reply. Do NOT take a screenshot, do NOT touch their desktop. Answer in
  2-3 short spoken sentences - conversational, no bullet points, no code blocks,
  no markdown. It is read aloud, so write how you would say it.
  If you genuinely do not know or it needs live data you cannot see, say so
  briefly rather than guessing.

ACT - they asked you to do something to their computer ("open X", "click Y",
  "make a file", "send this"). Use the tools. Then confirm in ONE short
  sentence. Skip preamble like "Sure!" or "I'll do that."

If a request mixes both ("what's in this file?" - read it, then answer), act
first, then answer from what you found.

Acting guidelines:
- Prefer the SIMPLEST tool that does the job. Most actions (open an app, run a
  command, press a key, focus a window) need NO screenshot - don't take one.
- Only when you must click something by position: call `take_screenshot` first,
  then `click_xy` with coordinates in that screenshot's pixel space (top-left is
  0,0). The screenshot result tells you its width and height.
- If a click doesn't land where you expected, take a fresh screenshot to see the
  new state before trying again - don't guess twice.
- After opening an app or navigating to a page, `wait` a second or two before you
  take_screenshot - acting before it renders is the #1 cause of missed clicks.
- Use `scroll` to reach things off-screen before trying to click them.
- Keyboard shortcuts are almost always better than clicking. Prefer `press_key`
  when the target has a shortcut (Ctrl+T for new tab, Alt+F4 to close, etc.).
- To create or save a file (a script, a note, some code), use `write_file` -
  never echo into a file with run_shell. Desktop/Documents/Downloads paths work.
- To SEND a screenshot into a chat app: screenshot_to_clipboard, click the
  message box, press_key ctrl+v to paste, then press_key enter. Don't fight the
  file-upload dialog. Use save_screenshot when they want it as a file on disk.
- If the user's request is destructive (delete, remove, close everything,
  shut down), the shell tool will refuse without confirmation. Ask them out
  loud to confirm and only re-run if they clearly say yes.
- If a tool call fails, tell the user briefly what went wrong instead of
  silently retrying five times.

Always reply in English, regardless of app names or on-screen text in other
languages - the reply is spoken aloud by an English voice.
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
        "description": "Run a shell command via cmd. Destructive commands (rm, del, shutdown, kill, etc.) require confirmed=true. Do NOT use this to write files — use write_file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": "Write text to a file. Use this to create files or save code/notes — NOT run_shell. Paths may use ~, env vars, or start with Desktop/Documents/Downloads. Refuses to overwrite an existing file unless overwrite=true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "e.g. 'Desktop/hello.py' or 'C:/Users/me/notes.txt'"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file's contents. Paths may use ~, env vars, or start with Desktop/Documents/Downloads.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "save_screenshot",
        "description": "Capture the screen and SAVE it to a PNG file (default: Desktop/voxie_screenshot.png). Use when the user wants a screenshot as a file, or to attach/upload one.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "optional destination, e.g. 'Desktop/shot.png'"}},
        },
    },
    {
        "name": "screenshot_to_clipboard",
        "description": "Capture the screen and put it on the clipboard as an image. To send a screenshot into a chat (Telegram, Discord, etc.): call this, focus the message box, then press_key ctrl+v to paste, then press_key enter to send. This avoids the file-upload dialog.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "scroll",
        "description": "Scroll the surface under the cursor. direction: up/down/left/right; amount is in notches (default 5).",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "default": "down"},
                "amount": {"type": "integer", "default": 5},
            },
        },
    },
    {
        "name": "wait",
        "description": "Pause for N seconds before your next action — use this to let an app launch or a page load before you take_screenshot. Max 10s.",
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "default": 1.0}},
        },
    },
    {
        "name": "clipboard_write",
        "description": "Put text on the clipboard (paste later with press_key ctrl+v).",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "clipboard_read",
        "description": "Read the text currently on the clipboard.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_routines",
        "description": "List the user's saved routines (named multi-step workflows) with their descriptions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_routine",
        "description": "Run a saved routine by name, e.g. 'dev setup'. Executes all of its steps in order. If unsure which routine they meant, call list_routines first.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "save_routine",
        "description": "Create or replace a routine so the user can trigger a whole workflow by name later. Steps are a list of {tool, args} using voxie's own tools, run in order. Insert a wait step after opening an app.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["tool"],
                    },
                },
            },
            "required": ["name", "description", "steps"],
        },
    },
    {
        "name": "delete_routine",
        "description": "Delete a saved routine by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "snap_window",
        "description": "Lay out the focused window: left / right / maximize / minimize / restore.",
        "input_schema": {
            "type": "object",
            "properties": {"where": {"type": "string", "enum": ["left", "right", "maximize", "minimize", "restore"]}},
            "required": ["where"],
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
        "write_file": files.write_file,
        "read_file": files.read_file,
        "save_screenshot": media.save_screenshot,
        "screenshot_to_clipboard": media.screenshot_to_clipboard,
        "scroll": input_tools.scroll,
        "wait": system.wait,
        "clipboard_write": system.clipboard_write,
        "clipboard_read": system.clipboard_read,
        "snap_window": system.snap_window,
        "list_routines": routines.list_routines,
        "delete_routine": routines.delete_routine,
    }


def _run_routine(name: str, dispatcher: dict, on_tool) -> dict[str, Any]:
    """Execute a saved routine's steps in order.

    Steps run through the same dispatcher as normal tool calls, so a routine
    can use anything voxie can do. A failing step stops the run rather than
    ploughing on - later steps usually assume the earlier ones worked.
    """
    routine = routines.get_routine(name)
    if routine is None:
        avail = [r["name"] for r in routines.list_routines()["routines"]]
        return {"ok": False, "error": f"no routine called {name!r}", "available": avail}

    done = []
    for i, step in enumerate(routine.get("steps", [])):
        tool = step.get("tool", "")
        args = step.get("args", {}) or {}
        handler = dispatcher.get(tool)
        if handler is None:
            return {"ok": False, "error": f"step {i}: unknown tool {tool!r}", "completed": done}
        try:
            res = handler(**args)
        except Exception as e:
            return {"ok": False, "error": f"step {i} ({tool}) crashed: {e}", "completed": done}
        if on_tool:
            try:
                on_tool(f"{tool} [routine]", res)
            except Exception:
                log.exception("on_tool callback raised")
        if not res.get("ok", True):
            return {"ok": False, "error": f"step {i} ({tool}) failed: {res.get('error')}",
                    "completed": done}
        done.append(tool)
    return {"ok": True, "routine": name, "steps_run": len(done)}


class Assistant:
    """Runs one full turn: transcript in → actions performed + spoken reply out.

    Keeps a short rolling history of prior turns (your words + voxie's final
    reply, NOT the tool-call noise or screenshots) so follow-up commands like
    "now search for pyodide" after "open Chrome" have context.
    """

    MAX_TOOL_ROUNDS = 14     # safety cap; multi-step tasks (open app, navigate,
                             # click, paste, send) legitimately need ~10+ rounds
    MAX_HISTORY_TURNS = 6    # how many prior (user, assistant) pairs to keep

    def __init__(self, api_key: str, base_url: str | None, model: str) -> None:
        self.client = Anthropic(api_key=api_key, base_url=base_url)
        self.model = model
        # Compact history: list of {"role": "user"|"assistant", "content": str}.
        # Only plain text — tool_use/tool_result/image blocks are dropped so the
        # context stays small and we never re-send stale screenshots.
        self._history: list[dict[str, str]] = []

    def reset_memory(self) -> None:
        """Forget the conversation so far ('never mind' / tray → Clear memory)."""
        self._history.clear()

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

        # Seed with prior turns for context, then this turn's transcript.
        messages: list[dict[str, Any]] = [
            {"role": h["role"], "content": [{"type": "text", "text": h["content"]}]}
            for h in self._history
        ]
        messages.append({"role": "user", "content": [{"type": "text", "text": transcript}]})
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
                reply = final_text or "Done."
                self._remember(transcript, reply)
                return reply

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

                # run_routine and save_routine both need the dispatcher: one to
                # execute a routine's steps, the other to validate tool names.
                if name == "run_routine":
                    result = _run_routine(args.get("name", ""), dispatcher, on_tool)
                    if on_tool:
                        try:
                            on_tool(name, result)
                        except Exception:
                            log.exception("on_tool callback raised")
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id, "content": str(result),
                    })
                    continue

                if name == "save_routine":
                    result = routines.save_routine(
                        args.get("name", ""), args.get("description", ""),
                        args.get("steps", []), valid_tools=set(dispatcher) | {"take_screenshot"},
                    )
                    if on_tool:
                        try:
                            on_tool(name, result)
                        except Exception:
                            log.exception("on_tool callback raised")
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id, "content": str(result),
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

        reply = final_text or "I stopped after too many steps. Try a smaller command."
        self._remember(transcript, reply)
        return reply

    def _remember(self, user_text: str, reply: str) -> None:
        """Append this turn to history and trim to the last MAX_HISTORY_TURNS."""
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": reply})
        # Each turn is a (user, assistant) pair → keep 2× that many entries.
        max_entries = self.MAX_HISTORY_TURNS * 2
        if len(self._history) > max_entries:
            self._history = self._history[-max_entries:]
