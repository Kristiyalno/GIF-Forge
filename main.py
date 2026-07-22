"""GIF Forge entry point."""

from __future__ import annotations

import sys
import tkinter as tk
import traceback
from tkinter import messagebox

from utils import ensure_dir, resource_path


def _ensure_project_dirs() -> None:
    ensure_dir(resource_path("assets"))
    ensure_dir(resource_path("projects"))
    ensure_dir(resource_path("projects", ".autosave"))
    ensure_dir(resource_path("settings"))


def _install_excepthook() -> None:
    def handle(exc_type, exc_value, exc_tb):
        message = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write(message)
        try:
            messagebox.showerror("GIF Forge - Unexpected Error", str(exc_value))
        except tk.TclError:
            pass

    sys.excepthook = handle


def main() -> None:
    _ensure_project_dirs()
    _install_excepthook()

    from gui import MainWindow

    app = MainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
