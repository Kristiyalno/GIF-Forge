"""Theme management: System / Dark / Light, applied live across ttk and raw tk widgets."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

DARK = {
    "bg": "#1e1f22",
    "bg_alt": "#2b2d31",
    "panel": "#232428",
    "fg": "#e3e3e3",
    "fg_muted": "#9a9ba0",
    "accent": "#5865f2",
    "border": "#3a3b40",
    "select_bg": "#3a3f52",
    "select_fg": "#ffffff",
    "entry_bg": "#2b2d31",
    "danger": "#e05252",
    "checker_a": "#3a3b40",
    "checker_b": "#2b2d31",
}

LIGHT = {
    "bg": "#f4f4f5",
    "bg_alt": "#ffffff",
    "panel": "#ececee",
    "fg": "#1c1c1e",
    "fg_muted": "#6b6b6f",
    "accent": "#4a56e2",
    "border": "#d4d4d8",
    "select_bg": "#dbe1ff",
    "select_fg": "#111111",
    "entry_bg": "#ffffff",
    "danger": "#c23b3b",
    "checker_a": "#e2e2e5",
    "checker_b": "#ffffff",
}


def get_system_theme() -> str:
    """Best-effort detection of the OS light/dark preference. Defaults to 'light'."""
    if sys.platform.startswith("win"):
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
        except Exception:
            return "light"
    if sys.platform == "darwin":
        try:
            import subprocess

            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=1,
            )
            return "dark" if "Dark" in result.stdout else "light"
        except Exception:
            return "light"
    # Linux/other: no reliable universal API, default to light.
    return "light"


class ThemeManager:
    """Owns the current palette and pushes it into ttk styles + raw tk widgets."""

    def __init__(self, root: tk.Tk, mode: str = "system", on_change=None):
        self.root = root
        self.mode = mode
        self.on_change = on_change
        self.style = ttk.Style(root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self._poll_job = None
        self._last_system_theme = get_system_theme()
        self.palette = self._resolve_palette()

    def _resolve_palette(self) -> dict:
        effective = self.mode
        if effective == "system":
            effective = get_system_theme()
        return DARK if effective == "dark" else LIGHT

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.apply()

    def apply(self) -> None:
        self.palette = self._resolve_palette()
        p = self.palette
        s = self.style

        self.root.configure(bg=p["bg"])

        s.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["entry_bg"])
        s.configure("TFrame", background=p["bg"])
        s.configure("Panel.TFrame", background=p["panel"])
        s.configure("TLabel", background=p["bg"], foreground=p["fg"])
        s.configure("Muted.TLabel", background=p["bg"], foreground=p["fg_muted"])
        s.configure("Panel.TLabel", background=p["panel"], foreground=p["fg"])
        s.configure("TButton", background=p["bg_alt"], foreground=p["fg"], borderwidth=1,
                    focuscolor=p["accent"])
        s.map("TButton",
              background=[("active", p["accent"]), ("disabled", p["panel"])],
              foreground=[("active", p["select_fg"]), ("disabled", p["fg_muted"])])
        s.configure("Accent.TButton", background=p["accent"], foreground="#ffffff")
        s.map("Accent.TButton", background=[("active", p["accent"]), ("disabled", p["panel"])])
        s.configure("TEntry", fieldbackground=p["entry_bg"], foreground=p["fg"],
                    insertcolor=p["fg"], bordercolor=p["border"])
        s.configure("TSpinbox", fieldbackground=p["entry_bg"], foreground=p["fg"],
                    arrowsize=12, bordercolor=p["border"])
        s.configure("TCombobox", fieldbackground=p["entry_bg"], foreground=p["fg"],
                    background=p["entry_bg"])
        s.map("TCombobox", fieldbackground=[("readonly", p["entry_bg"])],
              foreground=[("readonly", p["fg"])])
        s.configure("TCheckbutton", background=p["bg"], foreground=p["fg"])
        s.configure("TSeparator", background=p["border"])
        s.configure("TPanedwindow", background=p["bg"])
        s.configure("TProgressbar", background=p["accent"], troughcolor=p["panel"])
        s.configure("Treeview", background=p["bg_alt"], fieldbackground=p["bg_alt"],
                    foreground=p["fg"], borderwidth=0, rowheight=24)
        s.map("Treeview", background=[("selected", p["select_bg"])],
              foreground=[("selected", p["select_fg"])])
        s.configure("Treeview.Heading", background=p["panel"], foreground=p["fg"])
        s.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
        s.configure("TNotebook.Tab", background=p["panel"], foreground=p["fg"])
        s.map("TNotebook.Tab", background=[("selected", p["bg_alt"])])

        self._style_tk_tree(self.root)

        if self.on_change:
            self.on_change(p)

    def _style_tk_tree(self, widget) -> None:
        """Recursively apply palette colors to raw tk widgets (Listbox/Canvas/Menu/Text)
        which don't participate in ttk styling."""
        p = self.palette
        cls = widget.winfo_class()
        try:
            if cls == "Listbox":
                widget.configure(bg=p["bg_alt"], fg=p["fg"], selectbackground=p["select_bg"],
                                  selectforeground=p["select_fg"], highlightbackground=p["border"],
                                  highlightcolor=p["accent"], borderwidth=0)
            elif cls == "Canvas":
                widget.configure(bg=p["bg_alt"], highlightbackground=p["border"], highlightthickness=0)
            elif cls == "Menu":
                widget.configure(bg=p["bg_alt"], fg=p["fg"], activebackground=p["accent"],
                                  activeforeground="#ffffff", borderwidth=0)
            elif cls == "Text":
                widget.configure(bg=p["bg_alt"], fg=p["fg"], insertbackground=p["fg"],
                                  selectbackground=p["select_bg"])
            elif cls == "Toplevel" or cls == "Tk":
                widget.configure(bg=p["bg"])
            elif cls == "Frame":
                widget.configure(bg=p["bg"])
            elif cls == "Label":
                widget.configure(bg=p["bg"], fg=p["fg"])
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._style_tk_tree(child)

    def start_system_watch(self, interval_ms: int = 4000) -> None:
        """When mode == 'system', poll periodically and re-apply if the OS preference changes."""
        self.stop_system_watch()

        def poll():
            if self.mode == "system":
                current = get_system_theme()
                if current != self._last_system_theme:
                    self._last_system_theme = current
                    self.apply()
            self._poll_job = self.root.after(interval_ms, poll)

        self._poll_job = self.root.after(interval_ms, poll)

    def stop_system_watch(self) -> None:
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
