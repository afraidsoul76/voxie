"""Settings window: edit configuration and routines without touching files.

Everything here writes to the same .env and routines.json a user could edit by
hand - this is a friendlier front door to them, not a separate store.

Settings split into two kinds:
  * applied immediately (TTS on/off, speaking rate)
  * needs a restart (hotkeys, wake word, models, input device)
The window says which is which rather than pretending everything is live.

Must be constructed on the Tk mainloop thread. Tray callbacks arrive on the
pystray thread, so they go through Overlay.post().
"""
from __future__ import annotations

import json
import logging
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from ..envfile import ENV_PATH, read_env, write_env
from ..tools import routines as routines_mod

log = logging.getLogger("voxie.settings")

BG = "#0f1729"
PANEL = "#161f33"
FG = "#e5e7eb"
FG_DIM = "#94a3b8"
ACCENT = "#10b981"
ENTRY_BG = "#0b1220"

WHISPER_MODELS = ["tiny.en", "base.en", "small.en", "medium.en"]
CLAUDE_MODELS = ["claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"]


class SettingsWindow:
    def __init__(
        self,
        master: tk.Tk,
        input_devices: list[tuple[int, str]],
        on_applied: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self.on_applied = on_applied
        self.env = read_env()

        self.win = tk.Toplevel(master)
        self.win.title("voxie settings")
        self.win.configure(bg=BG)
        self.win.geometry("620x560")
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self._style()

        self.nb = ttk.Notebook(self.win)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(12, 0))

        self.vars: dict[str, tk.Variable] = {}
        self._device_labels = {f"[{i}] {n}": str(i) for i, n in input_devices}

        self._tab_general()
        self._tab_voice()
        self._tab_wake()
        self._tab_routines()

        bar = tk.Frame(self.win, bg=BG)
        bar.pack(fill="x", padx=12, pady=10)
        tk.Label(bar, text=str(ENV_PATH), bg=BG, fg="#5b667d",
                 font=("Consolas", 7)).pack(side="left")
        tk.Button(bar, text="Close", command=self.close, bg=PANEL, fg=FG,
                  relief="flat", padx=14, pady=5).pack(side="right", padx=(6, 0))
        tk.Button(bar, text="Save settings", command=self.save, bg=ACCENT, fg="#04150d",
                  relief="flat", padx=14, pady=5,
                  font=("Segoe UI Semibold", 9)).pack(side="right")

    # ---------- styling ----------

    def _style(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=PANEL, foreground=FG_DIM,
                     padding=(16, 8), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", BG)],
               foreground=[("selected", FG)])
        st.configure("TFrame", background=BG)
        st.configure("TCombobox", fieldbackground=ENTRY_BG, background=PANEL)

    def _page(self, title: str) -> tk.Frame:
        f = tk.Frame(self.nb, bg=BG, padx=18, pady=16)
        self.nb.add(f, text=title)
        return f

    def _row(self, parent: tk.Frame, label: str, hint: str = "") -> tk.Frame:
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="x", pady=(0, 12))
        tk.Label(wrap, text=label, bg=BG, fg=FG, font=("Segoe UI Semibold", 9),
                 anchor="w").pack(fill="x")
        if hint:
            tk.Label(wrap, text=hint, bg=BG, fg=FG_DIM, font=("Segoe UI", 8),
                     anchor="w", justify="left", wraplength=540).pack(fill="x")
        return wrap

    def _entry(self, parent: tk.Frame, key: str, default: str = "") -> None:
        var = tk.StringVar(value=self.env.get(key, default))
        self.vars[key] = var
        tk.Entry(parent, textvariable=var, bg=ENTRY_BG, fg=FG, relief="flat",
                 insertbackground=FG, font=("Consolas", 9)).pack(fill="x", ipady=5, pady=(4, 0))

    def _combo(self, parent: tk.Frame, key: str, options: list[str], default: str = "") -> None:
        var = tk.StringVar(value=self.env.get(key, default))
        self.vars[key] = var
        ttk.Combobox(parent, textvariable=var, values=options,
                     state="normal").pack(fill="x", pady=(4, 0))

    def _toggle(self, parent: tk.Frame, key: str, text: str, default: bool) -> None:
        cur = self.env.get(key, "on" if default else "off").lower()
        var = tk.BooleanVar(value=cur in ("on", "1", "true", "yes"))
        self.vars[key] = var
        tk.Checkbutton(parent, text=text, variable=var, bg=BG, fg=FG, selectcolor=ENTRY_BG,
                       activebackground=BG, activeforeground=FG, relief="flat",
                       font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 10))

    # ---------- tabs ----------

    def _tab_general(self) -> None:
        p = self._page("General")
        tk.Label(p, text="Changes here take effect after restarting voxie.",
                 bg=BG, fg="#f59e0b", font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 12))

        r = self._row(p, "Command hotkey", "Hold-to-talk toggle. pynput format, e.g. <ctrl>+<alt>+<space>")
        self._entry(r, "VOXIE_HOTKEY", "<ctrl>+<alt>+<space>")

        r = self._row(p, "Dictation hotkey", "Transcribe and type at the cursor, no AI in the loop.")
        self._entry(r, "VOXIE_DICTATE_HOTKEY", "<ctrl>+<alt>+d")

        r = self._row(p, "Microphone", "Blank uses the Windows default.")
        opts = [""] + list(self._device_labels)
        var = tk.StringVar()
        cur = self.env.get("VOXIE_INPUT_DEVICE", "")
        for label, idx in self._device_labels.items():
            if cur and (cur == idx or cur.lower() in label.lower()):
                var.set(label)
                break
        self.vars["_device_label"] = var
        ttk.Combobox(r, textvariable=var, values=opts, state="readonly").pack(fill="x", pady=(4, 0))

        r = self._row(p, "Claude model", "Vision-capable model recommended for clicking by sight.")
        self._combo(r, "VOXIE_MODEL", CLAUDE_MODELS, "claude-sonnet-4-5")

        r = self._row(p, "Speech-to-text model", "Larger is more accurate but slower.")
        self._combo(r, "VOXIE_WHISPER_MODEL", WHISPER_MODELS, "base.en")

    def _tab_voice(self) -> None:
        p = self._page("Voice")
        tk.Label(p, text="Applied immediately.", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 12))

        self._toggle(p, "VOXIE_TTS", "Speak replies out loud", True)

        r = self._row(p, "Speaking rate", "Words per minute. Blank uses the system default (~200).")
        self._entry(r, "VOXIE_VOICE_RATE")

        r = self._row(p, "Lead silence (ms)",
                      "Silence before each phrase so a Bluetooth speaker waking up "
                      "does not clip the first word. Raise if you hear clipping.")
        self._entry(r, "VOXIE_TTS_LEAD_MS", "300")

    def _tab_wake(self) -> None:
        p = self._page("Wake word")
        tk.Label(p, text="Changes here take effect after restarting voxie.",
                 bg=BG, fg="#f59e0b", font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 12))

        self._toggle(p, "VOXIE_WAKE", "Listen for a wake word (no hotkey needed)", False)

        r = self._row(p, "Wake phrase",
                      "Real words are recognised far more reliably than invented ones.")
        self._entry(r, "VOXIE_WAKE_PHRASE", "voxie")

        r = self._row(p, "Extra spellings",
                      "Comma separated. Speech-to-text writes invented words however they "
                      "sounded; if the log shows an unrecognised spelling, add it here.")
        self._entry(r, "VOXIE_WAKE_ALIASES")

        r = self._row(p, "Wake detection model", "Only runs on speech bursts, not continuously.")
        self._combo(r, "VOXIE_WAKE_MODEL", WHISPER_MODELS, "base.en")

    def _tab_routines(self) -> None:
        p = self._page("Routines")
        tk.Label(p, text="Named multi-step workflows. voxie can also create these by voice.",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 10))

        body = tk.Frame(p, bg=BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 12))
        self.r_list = tk.Listbox(left, bg=ENTRY_BG, fg=FG, relief="flat", width=20,
                                 highlightthickness=0, selectbackground=ACCENT,
                                 selectforeground="#04150d", font=("Segoe UI", 9))
        self.r_list.pack(fill="y", expand=True)
        self.r_list.bind("<<ListboxSelect>>", lambda _e: self._load_selected())

        btns = tk.Frame(left, bg=BG)
        btns.pack(fill="x", pady=(6, 0))
        tk.Button(btns, text="New", command=self._new_routine, bg=PANEL, fg=FG,
                  relief="flat", padx=8).pack(side="left")
        tk.Button(btns, text="Delete", command=self._delete_routine, bg=PANEL, fg="#f87171",
                  relief="flat", padx=8).pack(side="right")

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        tk.Label(right, text="Name", bg=BG, fg=FG, font=("Segoe UI Semibold", 9),
                 anchor="w").pack(fill="x")
        self.r_name = tk.StringVar()
        tk.Entry(right, textvariable=self.r_name, bg=ENTRY_BG, fg=FG, relief="flat",
                 insertbackground=FG).pack(fill="x", ipady=4, pady=(2, 8))

        tk.Label(right, text="Description", bg=BG, fg=FG, font=("Segoe UI Semibold", 9),
                 anchor="w").pack(fill="x")
        self.r_desc = tk.StringVar()
        tk.Entry(right, textvariable=self.r_desc, bg=ENTRY_BG, fg=FG, relief="flat",
                 insertbackground=FG).pack(fill="x", ipady=4, pady=(2, 8))

        tk.Label(right, text="Steps", bg=BG, fg=FG, font=("Segoe UI Semibold", 9),
                 anchor="w").pack(fill="x")
        tk.Label(right, text='A JSON list of {"tool": ..., "args": {...}}',
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8), anchor="w").pack(fill="x")
        self.r_steps = tk.Text(right, bg=ENTRY_BG, fg=FG, relief="flat", height=10,
                               insertbackground=FG, font=("Consolas", 9), wrap="none")
        self.r_steps.pack(fill="both", expand=True, pady=(2, 8))

        tk.Button(right, text="Save routine", command=self._save_routine, bg=ACCENT,
                  fg="#04150d", relief="flat", padx=12, pady=4,
                  font=("Segoe UI Semibold", 9)).pack(anchor="e")

        self._refresh_routines()

    # ---------- routines ----------

    def _refresh_routines(self, select: str | None = None) -> None:
        self.r_list.delete(0, "end")
        for r in routines_mod.list_routines()["routines"]:
            self.r_list.insert("end", r["name"])
        if select:
            items = list(self.r_list.get(0, "end"))
            if select in items:
                i = items.index(select)
                self.r_list.selection_clear(0, "end")
                self.r_list.selection_set(i)
                self._load_selected()

    def _selected_name(self) -> str | None:
        sel = self.r_list.curselection()
        return self.r_list.get(sel[0]) if sel else None

    def _load_selected(self) -> None:
        name = self._selected_name()
        if not name:
            return
        data = routines_mod.get_routine(name) or {}
        self.r_name.set(name)
        self.r_desc.set(data.get("description", ""))
        self.r_steps.delete("1.0", "end")
        self.r_steps.insert("1.0", json.dumps(data.get("steps", []), indent=2))

    def _new_routine(self) -> None:
        self.r_list.selection_clear(0, "end")
        self.r_name.set("")
        self.r_desc.set("")
        self.r_steps.delete("1.0", "end")
        self.r_steps.insert("1.0", json.dumps(
            [{"tool": "open_app", "args": {"name": "notepad"}}], indent=2))

    def _save_routine(self) -> None:
        name = self.r_name.get().strip()
        if not name:
            messagebox.showerror("Name required", "Give the routine a name.", parent=self.win)
            return
        try:
            steps = json.loads(self.r_steps.get("1.0", "end").strip() or "[]")
        except json.JSONDecodeError as e:
            messagebox.showerror("Invalid JSON", f"Steps are not valid JSON:\n\n{e}",
                                 parent=self.win)
            return
        if not isinstance(steps, list):
            messagebox.showerror("Invalid steps", "Steps must be a JSON list.", parent=self.win)
            return

        # Validate tool names against what voxie can actually do.
        from ..llm import _make_dispatcher

        valid = set(_make_dispatcher({"shot": None})) | {"take_screenshot", "run_routine"}
        res = routines_mod.save_routine(name, self.r_desc.get().strip(), steps, valid_tools=valid)
        if not res.get("ok"):
            messagebox.showerror("Could not save", str(res.get("error")), parent=self.win)
            return
        self._refresh_routines(select=name.lower())

    def _delete_routine(self) -> None:
        name = self._selected_name()
        if not name:
            return
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
            if isinstance(var, tk.BooleanVar):
                updates[key] = "on" if var.get() else "off"
            else:
                updates[key] = str(var.get()).strip()

        label = str(self.vars["_device_label"].get())
        updates["VOXIE_INPUT_DEVICE"] = self._device_labels.get(label, "")

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

        messagebox.showinfo(
            "Saved",
            "Settings saved.\n\nVoice settings apply now. Hotkeys, wake word, "
            "microphone and model changes take effect when you restart voxie.",
            parent=self.win,
        )

    def close(self) -> None:
        self.win.destroy()
