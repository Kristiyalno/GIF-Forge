"""Keybind manager and configurable keybinding editor, styled directly after
the keybind editor built for the ReDone project:

* Each shortcut is a combo of keys (e.g. control+shift+s), captured by
  holding them down and releasing one, or built by hand in a text field.
* "Ordered" means the keys must be pressed in that specific order (a
  sequence/chord), not just all held together.
* "Exact" means the combo only fires when *exactly* those keys are held and
  nothing else - so, for example, holding an extra stray modifier won't
  also trigger it.

Unlike ReDone (which hooks the OS via pynput for global hotkeys), GIF Forge
only needs shortcuts while the app itself has focus, so this tracks key
state via ordinary Tkinter <KeyPress>/<KeyRelease> events bound at the root
window rather than an OS-level listener.
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional

from utils import ensure_dir, resource_path

KEYBINDS_PATH = resource_path("settings", "keybinds.json")

_HISTORY_MAX = 50

# Toggle-lock keys are excluded from held-key tracking entirely: some
# platforms only ever deliver a press (no matching release) for these,
# which would otherwise leave them "stuck" in the held set forever.
_IGNORED_KEYS = {"caps_lock", "num_lock", "scroll_lock"}

# action_id -> {label, keys, ordered, exact}. Shipped defaults use "exact"
# so that e.g. Move Up (control+up) doesn't also fire when the user is
# actually holding Move To Top (control+shift+up).
DEFAULT_KEYBINDS: Dict[str, Dict] = {
    "delete_layer":    {"label": "Delete Layer",         "keys": ["delete"],                  "ordered": False, "exact": True},
    "duplicate_layer": {"label": "Duplicate Layer",       "keys": ["control", "d"],             "ordered": False, "exact": True},
    "move_up":         {"label": "Move Layer Up",         "keys": ["control", "up"],            "ordered": False, "exact": True},
    "move_down":       {"label": "Move Layer Down",       "keys": ["control", "down"],          "ordered": False, "exact": True},
    "move_to_top":     {"label": "Move Layer To Top",     "keys": ["control", "shift", "up"],   "ordered": False, "exact": True},
    "move_to_bottom":  {"label": "Move Layer To Bottom",  "keys": ["control", "shift", "down"], "ordered": False, "exact": True},
    "save":            {"label": "Save Project",          "keys": ["control", "s"],             "ordered": False, "exact": True},
    "save_as":         {"label": "Save Project As",       "keys": ["control", "shift", "s"],    "ordered": False, "exact": True},
    "open":            {"label": "Open Project",          "keys": ["control", "o"],             "ordered": False, "exact": True},
    "new":             {"label": "New Project",           "keys": ["control", "n"],             "ordered": False, "exact": True},
    "undo":            {"label": "Undo",                  "keys": ["control", "z"],             "ordered": False, "exact": True},
    "redo":            {"label": "Redo",                  "keys": ["control", "y"],             "ordered": False, "exact": True},
    "play_pause":      {"label": "Play / Pause Preview",   "keys": ["space"],                    "ordered": False, "exact": True},
}

# Pickable keys shown in the capture dialog's list.
COMMON_KEYS = (
    list("abcdefghijklmnopqrstuvwxyz") +
    [str(n) for n in range(10)] +
    [f"f{n}" for n in range(1, 13)] +
    ["control", "shift", "alt",
     "space", "return", "tab", "backspace", "delete", "escape", "insert",
     "home", "end", "page_up", "page_down",
     "up", "down", "left", "right"]
)

_DISPLAY_OVERRIDES = {
    "control": "Ctrl", "alt": "Alt", "shift": "Shift", "space": "Space",
    "return": "Enter", "escape": "Esc", "delete": "Delete", "backspace": "Backspace",
    "tab": "Tab", "insert": "Insert", "home": "Home", "end": "End",
    "page_up": "PageUp", "page_down": "PageDown",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
}

_KEYSYM_MAP = {
    "Control_L": "control", "Control_R": "control",
    "Shift_L": "shift", "Shift_R": "shift",
    "Alt_L": "alt", "Alt_R": "alt",
    "Meta_L": "alt", "Meta_R": "alt",
    "Super_L": "super", "Super_R": "super",
    "Return": "return", "KP_Enter": "return",
    "Escape": "escape",
    "Delete": "delete",
    "BackSpace": "backspace",
    "Tab": "tab",
    "Insert": "insert",
    "Home": "home", "End": "end",
    "Prior": "page_up", "Next": "page_down",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Caps_Lock": "caps_lock", "Num_Lock": "num_lock", "Scroll_Lock": "scroll_lock",
    "space": "space",
}


def normalize_keysym(keysym: str) -> Optional[str]:
    """Map a raw Tk keysym to a canonical, side-independent, lowercase key name."""
    if keysym in _KEYSYM_MAP:
        return _KEYSYM_MAP[keysym]
    if keysym.lower().startswith("f") and keysym[1:].isdigit():
        return keysym.lower()
    return keysym.lower()


def accelerator_to_display(keys: List[str]) -> str:
    if not keys:
        return "(unassigned)"
    parts = []
    for k in keys:
        k = k.lower()
        parts.append(_DISPLAY_OVERRIDES.get(k, k.upper() if len(k) == 1 else k.capitalize()))
    return "+".join(parts)


def normalise_bind(raw) -> Dict:
    if isinstance(raw, dict):
        return {
            "keys": [str(k).lower() for k in raw.get("keys", [])],
            "ordered": bool(raw.get("ordered", False)),
            "exact": bool(raw.get("exact", False)),
        }
    return {"keys": [], "ordered": False, "exact": False}


# --------------------------------------------------------------- matching

def bind_matches(bind: Dict, pressed: set) -> bool:
    """Unordered match: are the bind's keys currently held (subset), or -
    if exact - held with nothing else?"""
    keys = bind["keys"]
    if not keys:
        return False
    bind_set = frozenset(keys)
    current = frozenset(pressed)
    if bind["exact"]:
        return current == bind_set
    return bind_set <= current


def sequence_complete(bind: Dict, press_history: List[tuple]) -> bool:
    """Ordered match: walk press_history backwards and confirm the bind's
    keys were pressed in order, with all of them held together at the
    moment the last one was pressed."""
    keys = bind["keys"]
    if not keys:
        return False
    idx = len(keys) - 1
    for i in range(len(press_history) - 1, -1, -1):
        k_str, held_at_press = press_history[i]
        if k_str == keys[idx]:
            if idx == len(keys) - 1:
                if not all(bk in held_at_press for bk in keys):
                    return False
            idx -= 1
            if idx < 0:
                return True
    return False


_TEXT_ENTRY_CLASSES = {"Entry", "TEntry", "TSpinbox", "Spinbox", "TCombobox", "Text"}


class KeybindManager:
    def __init__(self):
        self.bindings: Dict[str, Dict] = {
            aid: {"keys": list(v["keys"]), "ordered": v["ordered"], "exact": v["exact"]}
            for aid, v in DEFAULT_KEYBINDS.items()
        }
        self.load()

        self._pressed: set = set()
        self._press_history: List[tuple] = []
        self._active_actions: set = set()
        self._suspended = False
        self._root: Optional[tk.Misc] = None
        self._callbacks: Dict[str, Callable] = {}

    # ------------------------------------------------------------- io
    def load(self) -> None:
        if os.path.isfile(KEYBINDS_PATH):
            try:
                with open(KEYBINDS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for aid, raw in data.items():
                    if aid in DEFAULT_KEYBINDS:
                        self.bindings[aid] = normalise_bind(raw)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        ensure_dir(os.path.dirname(KEYBINDS_PATH))
        tmp = KEYBINDS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.bindings, f, indent=2)
        os.replace(tmp, KEYBINDS_PATH)

    # --------------------------------------------------------- accessors
    def get(self, action_id: str) -> Dict:
        return self.bindings.get(action_id) or {"keys": [], "ordered": False, "exact": False}

    def set_bind(self, action_id: str, keys: List[str], ordered: bool, exact: bool) -> None:
        self.bindings[action_id] = {"keys": [k.lower() for k in keys], "ordered": ordered, "exact": exact}
        self.save()

    def restore_defaults(self) -> None:
        self.bindings = {
            aid: {"keys": list(v["keys"]), "ordered": v["ordered"], "exact": v["exact"]}
            for aid, v in DEFAULT_KEYBINDS.items()
        }
        self.save()
        self.reset_state()

    def find_conflict(self, keys: List[str], ordered: bool, exclude_action: Optional[str] = None) -> Optional[str]:
        key_set = frozenset(k.lower() for k in keys)
        if not key_set:
            return None
        for aid, bind in self.bindings.items():
            if aid == exclude_action or not bind["keys"]:
                continue
            if frozenset(bind["keys"]) == key_set and bind["ordered"] == ordered:
                return aid
        return None

    def export_to(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.bindings, f, indent=2)

    def import_from(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for aid, raw in data.items():
            if aid in DEFAULT_KEYBINDS:
                self.bindings[aid] = normalise_bind(raw)
        self.save()
        self.reset_state()

    # ---------------------------------------------------------- runtime
    def attach(self, root: tk.Misc, callbacks: Dict[str, Callable]) -> None:
        """Start tracking key state at the root window and dispatching
        matched actions. Call once; bindings are read live afterwards, so
        editing a shortcut later takes effect immediately without
        re-attaching."""
        self._root = root
        self._callbacks = callbacks
        root.bind_all("<KeyPress>", self._on_press)
        root.bind_all("<KeyRelease>", self._on_release)

    def suspend(self) -> None:
        """Stop dispatching actions (but keep tracking) while the keybind
        editor is open, so testing a combo there doesn't also fire it."""
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False

    def reset_state(self) -> None:
        self._pressed.clear()
        self._press_history.clear()
        self._active_actions.clear()

    def _is_text_entry_focused(self) -> bool:
        if self._root is None:
            return False
        try:
            widget = self._root.focus_get()
        except KeyError:
            return False
        if widget is None:
            return False
        try:
            return widget.winfo_class() in _TEXT_ENTRY_CLASSES
        except tk.TclError:
            return False

    def _on_press(self, event: tk.Event) -> None:
        key = normalize_keysym(event.keysym)
        if key is None or key in _IGNORED_KEYS:
            return
        is_new = key not in self._pressed
        self._pressed.add(key)
        if is_new:
            self._press_history.append((key, frozenset(self._pressed)))
            if len(self._press_history) > _HISTORY_MAX:
                self._press_history.pop(0)

        if self._suspended:
            return

        # While a text field has focus, only modifier-qualified shortcuts
        # (control/alt combos) are allowed through; bare keys like Delete or
        # Space should do their normal text-editing thing instead.
        text_focused = self._is_text_entry_focused()

        for action_id, bind in self.bindings.items():
            if action_id in self._active_actions or not bind["keys"]:
                continue
            if text_focused and "control" not in bind["keys"] and "alt" not in bind["keys"]:
                continue
            matched = sequence_complete(bind, self._press_history) if bind["ordered"] else bind_matches(bind, self._pressed)
            if not matched:
                continue
            callback = self._callbacks.get(action_id)
            if callback is None:
                continue
            self._active_actions.add(action_id)
            self._root.after(0, callback)

    def _on_release(self, event: tk.Event) -> None:
        key = normalize_keysym(event.keysym)
        if key is None or key in _IGNORED_KEYS:
            return
        self._pressed.discard(key)
        self._press_history.clear()
        stale = [aid for aid in self._active_actions
                 if not frozenset(self.bindings[aid]["keys"]) <= self._pressed]
        for aid in stale:
            self._active_actions.discard(aid)


# ------------------------------------------------------------- picker ui

class KeyPickerDialog(tk.Toplevel):
    """Hold keys then release to capture a combo in press order, or build
    one by double-clicking entries in the list. Escape cancels a capture in
    progress (or closes the dialog if nothing is being captured)."""

    def __init__(self, parent, on_pick: Callable[[List[str]], None], initial_keys: Optional[List[str]] = None):
        super().__init__(parent)
        self.title("Set Keybind")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._on_pick = on_pick

        self._capture_active = False
        self._capture_order: List[str] = []
        self._capture_set: set = set()

        self._result_var = tk.StringVar(value="+".join(initial_keys or []))

        pad = {"padx": 8, "pady": 4}
        combo_row = ttk.Frame(self)
        combo_row.pack(fill="x", **pad)
        ttk.Label(combo_row, text="Combo:").pack(side="left")
        entry = ttk.Entry(combo_row, textvariable=self._result_var, width=24)
        entry.pack(side="left", padx=6)
        ttk.Button(combo_row, text="Clear", width=6, command=lambda: self._result_var.set("")).pack(side="left")

        self._cap_btn = ttk.Button(self, text="Hold keys, then release", command=self._start_capture)
        self._cap_btn.pack(**pad)

        ttk.Label(self, text="Escape cancels capture \u2022 or pick from the list below:",
                  style="Muted.TLabel").pack(**pad)

        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self._lb = tk.Listbox(list_frame, width=20, height=16, exportselection=False)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self._lb.yview)
        self._lb.configure(yscrollcommand=sb.set)
        self._lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for k in COMMON_KEYS:
            self._lb.insert("end", k)
        self._lb.bind("<Double-Button-1>", self._append_from_list)

        btn_row = ttk.Frame(self)
        btn_row.pack(pady=(0, 8))
        ttk.Button(btn_row, text="Append", width=10, command=self._append_from_list).pack(side="left", padx=4)
        ttk.Button(btn_row, text="OK", width=10, command=self._commit).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Cancel", width=10, command=self._on_close).pack(side="left", padx=4)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda e: self._stop_capture() if self._capture_active else self._on_close())

    def _start_capture(self) -> None:
        self._capture_active = True
        self._capture_order = []
        self._capture_set = set()
        self._cap_btn.configure(text="Listening... (release a key to finish)")
        self.focus_set()
        self.bind("<KeyPress>", self._cap_press)
        self.bind("<KeyRelease>", self._cap_release)

    def _stop_capture(self) -> None:
        self._capture_active = False
        self.unbind("<KeyPress>")
        self.unbind("<KeyRelease>")
        self._cap_btn.configure(text="Hold keys, then release")

    def _cap_press(self, event: tk.Event) -> None:
        key = normalize_keysym(event.keysym)
        if key is None or key in _IGNORED_KEYS:
            return
        if key not in self._capture_set:
            self._capture_order.append(key)
            self._capture_set.add(key)

    def _cap_release(self, event: tk.Event) -> None:
        if event.keysym == "Escape":
            self._stop_capture()
            return
        parts = list(self._capture_order)
        self._stop_capture()
        if parts:
            self._result_var.set("+".join(parts))

    def _append_from_list(self, _event=None) -> None:
        sel = self._lb.curselection()
        if not sel:
            return
        picked = self._lb.get(sel[0])
        cur = self._result_var.get().strip()
        if cur and not cur.endswith("+"):
            self._result_var.set(cur + "+" + picked)
        else:
            self._result_var.set(picked)

    def _commit(self) -> None:
        self._stop_capture()
        raw = self._result_var.get().strip()
        parts = [p.strip().lower() for p in raw.split("+") if p.strip()]
        self._on_pick(parts)
        self.destroy()

    def _on_close(self) -> None:
        self._stop_capture()
        self.destroy()


# ------------------------------------------------------------- editor ui

class KeybindEditorDialog(tk.Toplevel):
    """One row per action: Action | Keys (editable) | Set... | Ordered | Exact.
    Type a combo directly (e.g. control+shift+s) or click Set to capture it.
    Changes save automatically."""

    def __init__(self, parent, manager: KeybindManager, on_change: Optional[Callable] = None):
        super().__init__(parent)
        self.manager = manager
        self.on_change = on_change
        self.title("Keybinds")
        self.resizable(False, False)
        self.transient(parent)
        self.manager.suspend()

        self._entry_vars: Dict[str, tk.StringVar] = {}
        self._ordered_vars: Dict[str, tk.BooleanVar] = {}
        self._exact_vars: Dict[str, tk.BooleanVar] = {}

        container = ttk.Frame(self, padding=12)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text="Type directly, or click Set to capture a combo.",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 8))

        for col, text in enumerate(["Action", "Keys", "", "Ordered", "Exact"]):
            ttk.Label(container, text=text, style="Muted.TLabel").grid(row=1, column=col, padx=6, pady=2, sticky="w")

        for row_i, action_id in enumerate(DEFAULT_KEYBINDS.keys(), start=2):
            meta = DEFAULT_KEYBINDS[action_id]
            bind = manager.get(action_id)

            ttk.Label(container, text=meta["label"]).grid(row=row_i, column=0, sticky="w", padx=6, pady=3)

            var = tk.StringVar(value="+".join(bind["keys"]))
            self._entry_vars[action_id] = var
            entry = ttk.Entry(container, textvariable=var, width=22)
            entry.grid(row=row_i, column=1, padx=6, pady=3)
            entry.bind("<Return>", lambda e, a=action_id: self._commit_entry(a))
            entry.bind("<FocusOut>", lambda e, a=action_id: self._commit_entry(a))

            ttk.Button(container, text="Set", width=4,
                       command=lambda a=action_id: self._open_picker(a)).grid(row=row_i, column=2, padx=(0, 6))

            ov = tk.BooleanVar(value=bind["ordered"])
            self._ordered_vars[action_id] = ov
            ttk.Checkbutton(container, variable=ov,
                             command=lambda a=action_id: self._commit_entry(a)).grid(row=row_i, column=3)

            ev = tk.BooleanVar(value=bind["exact"])
            self._exact_vars[action_id] = ev
            ttk.Checkbutton(container, variable=ev,
                             command=lambda a=action_id: self._commit_entry(a)).grid(row=row_i, column=4)

        btn_row = ttk.Frame(container)
        btn_row.grid(row=len(DEFAULT_KEYBINDS) + 2, column=0, columnspan=5, pady=(12, 0), sticky="ew")
        ttk.Button(btn_row, text="Restore Defaults", command=self._restore_defaults).pack(side="left")
        ttk.Button(btn_row, text="Import...", command=self._import).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Export...", command=self._export).pack(side="left")
        ttk.Button(btn_row, text="Close", command=self._close).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._close)

    def _commit_entry(self, action_id: str) -> None:
        raw = self._entry_vars[action_id].get().strip()
        keys = [p.strip().lower() for p in raw.split("+") if p.strip()]
        ordered = self._ordered_vars[action_id].get()
        exact = self._exact_vars[action_id].get()

        if keys:
            conflict = self.manager.find_conflict(keys, ordered, exclude_action=action_id)
            if conflict:
                conflict_label = DEFAULT_KEYBINDS[conflict]["label"]
                proceed = messagebox.askyesno(
                    "Keybind Conflict",
                    f"'{accelerator_to_display(keys)}' is already used by '{conflict_label}'.\n"
                    "Assign it here too?",
                    parent=self,
                )
                if not proceed:
                    bind = self.manager.get(action_id)
                    self._entry_vars[action_id].set("+".join(bind["keys"]))
                    self._ordered_vars[action_id].set(bind["ordered"])
                    self._exact_vars[action_id].set(bind["exact"])
                    return

        self.manager.set_bind(action_id, keys, ordered, exact)
        self._entry_vars[action_id].set("+".join(keys))
        if self.on_change:
            self.on_change()

    def _open_picker(self, action_id: str) -> None:
        current = self._entry_vars[action_id].get().strip()
        initial = [p.strip() for p in current.split("+") if p.strip()]

        def on_pick(parts: List[str]) -> None:
            self._entry_vars[action_id].set("+".join(parts))
            self._commit_entry(action_id)

        KeyPickerDialog(self, on_pick=on_pick, initial_keys=initial)

    def _restore_defaults(self) -> None:
        if not messagebox.askyesno("Restore Defaults", "Reset all keybinds to their defaults?", parent=self):
            return
        self.manager.restore_defaults()
        self._refresh_rows()
        if self.on_change:
            self.on_change()

    def _import(self) -> None:
        path = filedialog.askopenfilename(parent=self, filetypes=[("Keybind JSON", "*.json")])
        if not path:
            return
        try:
            self.manager.import_from(path)
        except (json.JSONDecodeError, OSError) as e:
            messagebox.showerror("Import Failed", str(e), parent=self)
            return
        self._refresh_rows()
        if self.on_change:
            self.on_change()

    def _export(self) -> None:
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                             filetypes=[("Keybind JSON", "*.json")])
        if not path:
            return
        try:
            self.manager.export_to(path)
        except OSError as e:
            messagebox.showerror("Export Failed", str(e), parent=self)

    def _refresh_rows(self) -> None:
        for action_id in DEFAULT_KEYBINDS:
            bind = self.manager.get(action_id)
            self._entry_vars[action_id].set("+".join(bind["keys"]))
            self._ordered_vars[action_id].set(bind["ordered"])
            self._exact_vars[action_id].set(bind["exact"])

    def _close(self) -> None:
        self.manager.resume()
        self.manager.reset_state()
        self.destroy()
