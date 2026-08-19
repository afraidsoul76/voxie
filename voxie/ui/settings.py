"""Settings window: configuration and routines, without touching files.

Everything here writes to the same .env and routines.json a user could edit by
hand - this is a friendlier front door to them, not a separate store.

Chrome is custom-drawn (frameless window, canvas-rendered sidebar and header)
because Tk's native widgets can't be styled into anything that sits happily
next to the overlay. Pages are swapped with a short slide+fade rather than
appearing instantly.

Settings are labelled by kind - applied-now vs needs-a-restart - rather than
pretending everything is live.

Must be constructed on the Tk mainloop thread; tray callbacks come from the
pystray thread and go through Overlay.post().
"""
from __future__ import annotations

import json
import logging
import tkinter as tk
from tkinter import messagebox
from typing import Callable

from ..envfile import ENV_PATH, read_env, write_env
from ..tools import routines as routines_mod

log = logging.getLogger("voxie.settings")

# palette - shares the overlay's language
BG = "#0b1120"
SIDEBAR = "#0e1526"
CARD = "#141d31"
FIELD = "#0a1020"
LINE = "#22304b"
FG = "#eef2f8"
FG_DIM = "#8ea0bb"
FG_FAINT = "#5b6b87"
ACCENT = "#10b981"
ACCENT_DK = "#04150d"
WARN = "#f59e0b"
DANGER = "#f87171"

W, H = 780, 580
SIDEBAR_W = 176

WHISPER_MODELS = ["tiny.en", "base.en", "small.en", "medium.en"]
CLAUDE_MODELS = ["claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"]

PAGES = [
    ("general", "General", "⚙"),
    ("voice", "Voice", "\U0001F50A"),
    ("wake", "Wake word", "\U0001F44B"),
    ("routines", "Routines", "⚡"),
]


class Field(tk.Frame):
    """Label + hint + entry, styled consistently."""

    def __init__(self, parent, label: str, hint: str = "", **kw):
        super().__init__(parent, bg=BG, **kw)
        tk.Label(self, text=label, bg=BG, fg=FG, anchor="w",
                 font=("Segoe UI Semibold", 10)).pack(fill="x")
        if hint:
            tk.Label(self, text=hint, bg=BG, fg=FG_DIM, anchor="w", justify="left",
                     wraplength=520, font=("Segoe UI", 8)).pack(fill="x", pady=(1, 0))

    def entry(self, var: tk.Variable, mono: bool = True) -> tk.Entry:
        e = tk.Entry(self, textvariable=var, bg=FIELD, fg=FG, relief="flat",
                     insertbackground=ACCENT, highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=ACCENT,
                     font=("Consolas", 10) if mono else ("Segoe UI", 10))
        e.pack(fill="x", ipady=6, pady=(6, 0))
        return e


class Dropdown(tk.Frame):
    """Menubutton-based select - tk's OptionMenu is themable, ttk's is not."""

    def __init__(self, parent, var: tk.StringVar, options: list[str]):
        super().__init__(parent, bg=BG)
        self.var = var
        self.mb = tk.Menubutton(self, textvariable=var, bg=FIELD, fg=FG, relief="flat",
                                anchor="w", padx=10, pady=6, highlightthickness=1,
                                highlightbackground=LINE, activebackground=CARD,
                                activeforeground=FG, font=("Consolas", 10))
        self.mb.pack(fill="x")
        menu = tk.Menu(self.mb, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT,
                       activeforeground=ACCENT_DK, relief="flat", borderwidth=0)
        for o in options:
            menu.add_command(label=o or "(system default)",
                             command=lambda v=o: var.set(v))
        self.mb.config(menu=menu)


class Toggle(tk.Frame):
    """Animated pill switch."""

    TRACK_W, TRACK_H = 44, 22

    def __init__(self, parent, var: tk.BooleanVar, text: str):
        super().__init__(parent, bg=BG)
        self.var = var
        self._pos = 1.0 if var.get() else 0.0

        self.c = tk.Canvas(self, width=self.TRACK_W, height=self.TRACK_H, bg=BG,
                           highlightthickness=0, cursor="hand2")
        self.c.pack(side="left")
        self._track = self.c.create_rectangle(0, 2, self.TRACK_W, self.TRACK_H - 2,
                                              outline="", fill=LINE)
        self._knob = self.c.create_oval(0, 0, 0, 0, outline="", fill="#cbd5e1")

        lbl = tk.Label(self, text=text, bg=BG, fg=FG, font=("Segoe UI", 10))
        lbl.pack(side="left", padx=(10, 0))

        for w in (self.c, lbl):
            w.bind("<Button-1>", self._flip)
        self._animate()

    def _flip(self, _e=None):
        self.var.set(not self.var.get())

    def _animate(self):
        target = 1.0 if self.var.get() else 0.0
        self._pos += (target - self._pos) * 0.35
        r = (self.TRACK_H - 6) / 2
        cx = 3 + r + self._pos * (self.TRACK_W - 2 * r - 6)
        cy = self.TRACK_H / 2
        self.c.coords(self._knob, cx - r, cy - r, cx + r, cy + r)
        # track fades between grey and accent as the knob travels
        t = self._pos
        col = "#%02x%02x%02x" % (
            round(0x22 + (0x10 - 0x22) * t),
            round(0x30 + (0xB9 - 0x30) * t),
            round(0x4B + (0x81 - 0x4B) * t),
        )
        self.c.itemconfig(self._track, fill=col)
        self.after(16, self._animate)


class SettingsWindow:
    def __init__(self, master: tk.Tk, input_devices: list[tuple[int, str]],
                 on_applied: Callable[[dict[str, str]], None] | None = None,
                 compose: Callable[[str], dict] | None = None) -> None:
        self.on_applied = on_applied
        self.compose = compose
        self.env = read_env()
        self.vars: dict[str, tk.Variable] = {}
        self._device_labels = {f"[{i}] {n}": str(i) for i, n in input_devices}

        self.win = tk.Toplevel(master)
        self.win.title("voxie settings")
        self.win.configure(bg=BG)
        self.win.geometry(f"{W}x{H}")
        self.win.minsize(W, H)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.transient(master)

        self._build_sidebar()
        self._build_body()

        self.pages: dict[str, tk.Frame] = {}
        self._build_general()
        self._build_voice()
        self._build_wake()
        self._build_routines()

        self._current = None
        self._show("general")

    # ---------- chrome ----------

    def _build_sidebar(self) -> None:
        side = tk.Frame(self.win, bg=SIDEBAR, width=SIDEBAR_W)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        head = tk.Frame(side, bg=SIDEBAR)
        head.pack(fill="x", pady=(22, 18), padx=18)
        tk.Label(head, text="voxie", bg=SIDEBAR, fg=FG,
                 font=("Segoe UI Semibold", 15)).pack(anchor="w")
        tk.Label(head, text="settings", bg=SIDEBAR, fg=FG_FAINT,
                 font=("Segoe UI", 9)).pack(anchor="w")

        self._nav: dict[str, tk.Frame] = {}
        for key, label, icon in PAGES:
            row = tk.Frame(side, bg=SIDEBAR, cursor="hand2")
            row.pack(fill="x")
            bar = tk.Frame(row, bg=SIDEBAR, width=3)
            bar.pack(side="left", fill="y")
            lbl = tk.Label(row, text=f"  {icon}   {label}", bg=SIDEBAR, fg=FG_DIM,
                           anchor="w", font=("Segoe UI", 10), padx=10, pady=11)
            lbl.pack(side="left", fill="x", expand=True)
            row._bar, row._lbl = bar, lbl
            for w in (row, lbl, bar):
                w.bind("<Button-1>", lambda _e, k=key: self._show(k))
                w.bind("<Enter>", lambda _e, r=row: self._hover(r, True))
                w.bind("<Leave>", lambda _e, r=row: self._hover(r, False))
            self._nav[key] = row

        foot = tk.Frame(side, bg=SIDEBAR)
        foot.pack(side="bottom", fill="x", pady=14, padx=14)
        tk.Label(foot, text=ENV_PATH.name, bg=SIDEBAR, fg=FG_FAINT,
                 font=("Consolas", 7)).pack(anchor="w")

    def _hover(self, row: tk.Frame, on: bool) -> None:
        if getattr(row, "_active", False):
            return
        row._lbl.config(fg=FG if on else FG_DIM)

    def _build_body(self) -> None:
        self.body = tk.Frame(self.win, bg=BG)
        self.body.pack(side="left", fill="both", expand=True)

        self.host = tk.Frame(self.body, bg=BG)
        self.host.pack(fill="both", expand=True, padx=26, pady=(24, 0))

        bar = tk.Frame(self.body, bg=BG)
        bar.pack(fill="x", padx=26, pady=16)
        tk.Frame(bar, bg=LINE, height=1).pack(fill="x", pady=(0, 14))
        self.status = tk.Label(bar, text="", bg=BG, fg=ACCENT, font=("Segoe UI", 9))
        self.status.pack(side="left")
        self._btn(bar, "Close", self.close, primary=False).pack(side="right", padx=(8, 0))
        self._btn(bar, "Save settings", self.save, primary=True).pack(side="right")

    def _btn(self, parent, text: str, cmd, primary: bool = False, danger: bool = False):
        bg = ACCENT if primary else CARD
        fg = ACCENT_DK if primary else (DANGER if danger else FG)
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, relief="flat",
                      padx=16, pady=7, cursor="hand2", borderwidth=0,
                      activebackground="#0ea371" if primary else LINE,
                      activeforeground=fg,
                      font=("Segoe UI Semibold", 9) if primary else ("Segoe UI", 9))
        return b

    def _page(self, key: str, title: str, note: str = "", note_color: str = FG_DIM) -> tk.Frame:
        f = tk.Frame(self.host, bg=BG)
        tk.Label(f, text=title, bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 16)).pack(anchor="w")
        if note:
            tk.Label(f, text=note, bg=BG, fg=note_color, anchor="w",
                     font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        tk.Frame(f, bg=BG, height=16).pack()
        self.pages[key] = f
        return f

    def _show(self, key: str) -> None:
        if key == self._current:
            return
        for k, row in self._nav.items():
            active = k == key
            row._active = active
            row._bar.config(bg=ACCENT if active else SIDEBAR)
            row._lbl.config(fg=FG if active else FG_DIM,
                            bg=CARD if active else SIDEBAR)
            row.config(bg=CARD if active else SIDEBAR)
        for p in self.pages.values():
            p.place_forget()
        self._current = key
        self._slide_in(self.pages[key])

    def _slide_in(self, page: tk.Frame, step: int = 0) -> None:
        """Short slide+settle so switching pages doesn't feel like a jump cut."""
        frames = 8
        if step > frames:
            page.place(relx=0, rely=0, relwidth=1, relheight=1)
            return
        t = step / frames
        eased = 1 - (1 - t) ** 3
        page.place(relx=(1 - eased) * 0.05, rely=0, relwidth=1, relheight=1)
        self.win.after(12, lambda: self._slide_in(page, step + 1))

    # ---------- shared field helpers ----------

    def _str_var(self, key: str, default: str = "") -> tk.StringVar:
        v = tk.StringVar(value=self.env.get(key, default))
        self.vars[key] = v
        return v

    def _bool_var(self, key: str, default: bool) -> tk.BooleanVar:
        cur = self.env.get(key, "on" if default else "off").lower()
        v = tk.BooleanVar(value=cur in ("on", "1", "true", "yes"))
        self.vars[key] = v
        return v

    # ---------- pages ----------

    def _build_general(self) -> None:
        p = self._page("general", "General", "Takes effect after restarting voxie", WARN)

        f = Field(p, "Command hotkey", "Press to start talking, press again to send.")
        f.pack(fill="x", pady=(0, 16)); f.entry(self._str_var("VOXIE_HOTKEY", "<ctrl>+<alt>+<space>"))

        f = Field(p, "Dictation hotkey", "Transcribe and type at the cursor - no AI in the loop.")
        f.pack(fill="x", pady=(0, 16)); f.entry(self._str_var("VOXIE_DICTATE_HOTKEY", "<ctrl>+<alt>+d"))

        f = Field(p, "Microphone", "Blank uses the Windows default.")
        f.pack(fill="x", pady=(0, 16))
        cur = self.env.get("VOXIE_INPUT_DEVICE", "")
        label = ""
        for lab, idx in self._device_labels.items():
            if cur and (cur == idx or cur.lower() in lab.lower()):
                label = lab
                break
        var = tk.StringVar(value=label)
        self.vars["_device_label"] = var
        Dropdown(f, var, [""] + list(self._device_labels)).pack(fill="x", pady=(6, 0))

        two = tk.Frame(p, bg=BG); two.pack(fill="x")
        left = Field(two, "Claude model", "Vision-capable is recommended.")
        left.pack(side="left", fill="x", expand=True, padx=(0, 8))
        Dropdown(left, self._str_var("VOXIE_MODEL", "claude-sonnet-4-5"),
                 CLAUDE_MODELS).pack(fill="x", pady=(6, 0))
        right = Field(two, "Speech-to-text", "Larger is slower but more accurate.")
        right.pack(side="left", fill="x", expand=True, padx=(8, 0))
        Dropdown(right, self._str_var("VOXIE_WHISPER_MODEL", "base.en"),
                 WHISPER_MODELS).pack(fill="x", pady=(6, 0))

    def _build_voice(self) -> None:
        p = self._page("voice", "Voice", "Applied immediately", ACCENT)
        Toggle(p, self._bool_var("VOXIE_TTS", True), "Speak replies out loud").pack(anchor="w", pady=(0, 20))

        f = Field(p, "Speaking rate", "Words per minute. Blank uses the system default (~200).")
        f.pack(fill="x", pady=(0, 16)); f.entry(self._str_var("VOXIE_VOICE_RATE"))

        f = Field(p, "Lead silence (ms)",
                  "Silence before each phrase so a Bluetooth speaker waking up does not "
                  "clip the first word. Raise it if you still hear clipping.")
        f.pack(fill="x"); f.entry(self._str_var("VOXIE_TTS_LEAD_MS", "300"))

    def _build_wake(self) -> None:
        p = self._page("wake", "Wake word", "Takes effect after restarting voxie", WARN)
        Toggle(p, self._bool_var("VOXIE_WAKE", False),
               "Listen for a wake word (no hotkey needed)").pack(anchor="w", pady=(0, 20))

        f = Field(p, "Wake phrase", "Real words are recognised far more reliably than invented ones.")
        f.pack(fill="x", pady=(0, 16)); f.entry(self._str_var("VOXIE_WAKE_PHRASE", "voxie"))

        f = Field(p, "Extra spellings",
                  "Comma separated. Speech-to-text writes invented words however they sounded; "
                  "if the log shows an unrecognised spelling, add it here.")
        f.pack(fill="x", pady=(0, 16)); f.entry(self._str_var("VOXIE_WAKE_ALIASES"))

        f = Field(p, "Detection model", "Only runs on speech bursts, not continuously.")
        f.pack(fill="x")
        Dropdown(f, self._str_var("VOXIE_WAKE_MODEL", "base.en"),
                 WHISPER_MODELS).pack(fill="x", pady=(6, 0))

    def _build_routines(self) -> None:
        p = self._page("routines", "Routines", "Named workflows you can trigger by voice")

        wrap = tk.Frame(p, bg=BG)
        wrap.pack(fill="both", expand=True)

        # left: list
        left = tk.Frame(wrap, bg=BG, width=180)
        left.pack(side="left", fill="y", padx=(0, 18))
        left.pack_propagate(False)
        self.r_list = tk.Listbox(left, bg=FIELD, fg=FG, relief="flat", highlightthickness=1,
                                 highlightbackground=LINE, selectbackground=ACCENT,
                                 selectforeground=ACCENT_DK, activestyle="none",
                                 font=("Segoe UI", 10))
        self.r_list.pack(fill="both", expand=True)
        self.r_list.bind("<<ListboxSelect>>", lambda _e: self._load_selected())
        row = tk.Frame(left, bg=BG); row.pack(fill="x", pady=(8, 0))
        self._btn(row, "New", self._new_routine).pack(side="left")
        self._btn(row, "Delete", self._delete_routine, danger=True).pack(side="right")

        # right: editor
        right = tk.Frame(wrap, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self.r_name = tk.StringVar()
        f = Field(right, "Name"); f.pack(fill="x", pady=(0, 12)); f.entry(self.r_name, mono=False)

        f = Field(right, "What should it do?",
                  "Describe it in plain English - voxie turns it into steps.")
        f.pack(fill="x")
        self.r_desc = tk.StringVar()
        f.entry(self.r_desc, mono=False)

        gen = tk.Frame(right, bg=BG); gen.pack(fill="x", pady=(8, 12))
        self.gen_btn = self._btn(gen, "✨  Build steps for me", self._compose, primary=True)
        self.gen_btn.pack(side="left")
        self.gen_note = tk.Label(gen, text="", bg=BG, fg=FG_DIM, font=("Segoe UI", 8))
        self.gen_note.pack(side="left", padx=10)

        tk.Label(right, text="Steps", bg=BG, fg=FG, anchor="w",
                 font=("Segoe UI Semibold", 10)).pack(fill="x")
        self.steps_box = tk.Frame(right, bg=FIELD, highlightthickness=1,
                                  highlightbackground=LINE)
        self.steps_box.pack(fill="both", expand=True, pady=(6, 10))
        self._steps: list[dict] = []

        self._btn(right, "Save routine", self._save_routine, primary=True).pack(anchor="e")

        self._refresh_routines()

    # ---------- routines behaviour ----------

    def _render_steps(self) -> None:
        for w in self.steps_box.winfo_children():
            w.destroy()
        if not self._steps:
            tk.Label(self.steps_box, text="No steps yet - describe the routine above\nand let voxie build them.",
                     bg=FIELD, fg=FG_FAINT, font=("Segoe UI", 9), justify="left").pack(anchor="w", padx=12, pady=12)
            return
        for i, step in enumerate(self._steps, 1):
            row = tk.Frame(self.steps_box, bg=FIELD)
            row.pack(fill="x", padx=10, pady=3)
            tk.Label(row, text=f"{i}", bg=FIELD, fg=FG_FAINT, width=2,
                     font=("Consolas", 9)).pack(side="left")
            tk.Label(row, text=step.get("tool", "?"), bg=FIELD, fg=ACCENT,
                     font=("Consolas", 9, "bold")).pack(side="left", padx=(4, 8))
            args = step.get("args") or {}
            summary = ", ".join(f"{k}={v}" for k, v in args.items())
            tk.Label(row, text=summary, bg=FIELD, fg=FG_DIM, anchor="w",
                     font=("Consolas", 9)).pack(side="left", fill="x", expand=True)
            tk.Button(row, text="✕", command=lambda idx=i - 1: self._drop_step(idx),
                      bg=FIELD, fg=FG_FAINT, relief="flat", borderwidth=0,
                      activebackground=FIELD, activeforeground=DANGER,
                      cursor="hand2").pack(side="right")

    def _drop_step(self, idx: int) -> None:
        if 0 <= idx < len(self._steps):
            self._steps.pop(idx)
            self._render_steps()

    def _compose(self) -> None:
        desc = self.r_desc.get().strip()
        if not desc:
            self.gen_note.config(text="describe it first", fg=WARN)
            return
        if self.compose is None:
            self.gen_note.config(text="not available", fg=DANGER)
            return
        self.gen_btn.config(state="disabled", text="thinking...")
        self.gen_note.config(text="", fg=FG_DIM)
        self.win.update_idletasks()

        res = self.compose(desc)
        self.gen_btn.config(state="normal", text="✨  Build steps for me")
        if not res.get("ok"):
            self.gen_note.config(text=str(res.get("error"))[:60], fg=DANGER)
            return
        self._steps = res["steps"]
        self._render_steps()
        self.gen_note.config(text=f"built {len(self._steps)} steps", fg=ACCENT)

    def _refresh_routines(self, select: str | None = None) -> None:
        self.r_list.delete(0, "end")
        for r in routines_mod.list_routines()["routines"]:
            self.r_list.insert("end", r["name"])
        if select:
            items = list(self.r_list.get(0, "end"))
            if select in items:
                self.r_list.selection_clear(0, "end")
                self.r_list.selection_set(items.index(select))
                self._load_selected()

    def _load_selected(self) -> None:
        sel = self.r_list.curselection()
        if not sel:
            return
        name = self.r_list.get(sel[0])
        data = routines_mod.get_routine(name) or {}
        self.r_name.set(name)
        self.r_desc.set(data.get("description", ""))
        self._steps = list(data.get("steps", []))
        self._render_steps()

    def _new_routine(self) -> None:
        self.r_list.selection_clear(0, "end")
        self.r_name.set("")
        self.r_desc.set("")
        self._steps = []
        self._render_steps()
        self.gen_note.config(text="")

    def _save_routine(self) -> None:
        name = self.r_name.get().strip()
        if not name:
            messagebox.showerror("Name required", "Give the routine a name.", parent=self.win)
            return
        if not self._steps:
            messagebox.showerror("No steps", "Describe the routine and build its steps first.",
                                 parent=self.win)
            return
        from ..llm import _make_dispatcher

        valid = set(_make_dispatcher({"shot": None})) | {"take_screenshot", "run_routine"}
        res = routines_mod.save_routine(name, self.r_desc.get().strip(), self._steps,
                                        valid_tools=valid)
        if not res.get("ok"):
            messagebox.showerror("Could not save", str(res.get("error")), parent=self.win)
            return
        self._refresh_routines(select=name.lower())
        self.gen_note.config(text="saved", fg=ACCENT)

    def _delete_routine(self) -> None:
        sel = self.r_list.curselection()
        if not sel:
            return
        name = self.r_list.get(sel[0])
        if not messagebox.askyesno("Delete routine", f"Delete {name!r}?", parent=self.win):
            return
        routines_mod.delete_routine(name)
        self._new_routine()
        self._refresh_routines()

    # ---------- save / close ----------

    def save(self) -> None:
        updates: dict[str, str] = {}
        for key, var in self.vars.items():
            if key.startswith("_"):
                continue
            updates[key] = ("on" if var.get() else "off") if isinstance(var, tk.BooleanVar) \
                else str(var.get()).strip()
        updates["VOXIE_INPUT_DEVICE"] = self._device_labels.get(
            str(self.vars["_device_label"].get()), "")

        try:
            write_env(updates)
        except Exception as e:
            messagebox.showerror("Could not save", str(e), parent=self.win)
            return

        if self.on_applied:
            try:
                self.on_applied(updates)
            except Exception:
                log.exception("on_applied failed")

        self.status.config(text="Saved - voice applies now, the rest on restart", fg=ACCENT)
        self.win.after(4000, lambda: self.status.config(text=""))

    def close(self) -> None:
        self.win.destroy()
