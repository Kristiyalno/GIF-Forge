"""Main application window."""

from __future__ import annotations

import os
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Optional

from cache import GifCache
from keybinds import KeybindEditorDialog, KeybindManager
from layers import GifLayer, Layer, SpaceLayer
from project import PROJECT_EXTENSION, Project, UndoManager
from preview import PreviewPanel
from renderer import RenderEngine, compute_layout, estimate_frames_and_duration
from settings import SettingsManager
from theme import ThemeManager
from utils import format_bytes, format_duration_ms, safe_basename

APP_TITLE = "GIF Forge"
BYTES_PER_PIXEL_PER_FRAME_ESTIMATE = 0.35  # rough palette+LZW heuristic for size warnings
LARGE_OUTPUT_WARN_BYTES = 50 * 1024 * 1024
AUTOSAVE_INTERVAL_MS = 30_000


class Tooltip:
    """A minimal hover tooltip for buttons and controls."""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None):
        if self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(self.tip, text=self.text, background="#222222", foreground="#f0f0f0",
                          relief="solid", borderwidth=1, padx=6, pady=2, font=("Segoe UI", 8))
        label.pack()

    def _hide(self, _event=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class ProgressDialog(tk.Toplevel):
    def __init__(self, parent, on_cancel):
        super().__init__(parent)
        self.title("Generating GIF")
        self.geometry("360x140")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._on_cancel = on_cancel

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        self.stage_var = tk.StringVar(value="Starting...")
        ttk.Label(frame, textvariable=self.stage_var).pack(anchor="w")

        self.progress = ttk.Progressbar(frame, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(10, 10))

        self.pct_var = tk.StringVar(value="0%")
        ttk.Label(frame, textvariable=self.pct_var, style="Muted.TLabel").pack(anchor="w")

        ttk.Button(frame, text="Cancel", command=self._cancel).pack(anchor="e", pady=(8, 0))
        self.grab_set()

    def update_progress(self, stage: str, fraction: float) -> None:
        self.stage_var.set(stage)
        pct = max(0, min(100, int(fraction * 100)))
        self.progress["value"] = pct
        self.pct_var.set(f"{pct}%")

    def _cancel(self) -> None:
        self._on_cancel()


class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()

        self.settings = SettingsManager()
        self.keybind_manager = KeybindManager()
        self.cache = GifCache()
        self.render_engine = RenderEngine(self.cache)
        self.undo_manager = UndoManager(max_levels=20)
        self.project = Project.new()
        self.project.theme = self.settings.get("theme_mode", "system")

        self._render_queue: "queue.Queue" = queue.Queue()
        self._progress_dialog: Optional[ProgressDialog] = None
        self._autosave_job = None
        self._selected_layer_id: Optional[str] = None
        self._drag_start_index: Optional[int] = None

        self.title(APP_TITLE)
        self.geometry(self.settings.get("window_geometry", "1200x760"))
        self.minsize(900, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.theme_manager = ThemeManager(self, mode=self.project.theme, on_change=self._on_theme_applied)

        self._build_menu()
        self._build_layout()
        self._setup_hotkeys()

        self.theme_manager.apply()
        self.theme_manager.start_system_watch()

        self._refresh_layer_list()
        self._update_output_info()
        self._update_title()

        self.deiconify()
        self.after(200, self._check_autosave_recovery)
        self._schedule_autosave()

    # ============================================================ menus
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="New", command=self.new_project)
        file_menu.add_command(label="Open...", command=self.open_project_dialog)
        self.recent_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Open Recent", menu=self.recent_menu)
        file_menu.add_command(label="Save", command=self.save_project)
        file_menu.add_command(label="Save As...", command=self.save_project_as)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)
        self._populate_recent_menu()

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Undo", command=self.undo)
        edit_menu.add_command(label="Redo", command=self.redo)
        edit_menu.add_separator()
        edit_menu.add_command(label="Duplicate Layer", command=self.duplicate_selected)
        edit_menu.add_command(label="Delete Layer", command=self.remove_selected)
        edit_menu.add_command(label="Remove All", command=self.remove_all)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        self.theme_var = tk.StringVar(value=self.project.theme)
        view_menu.add_radiobutton(label="System Theme", variable=self.theme_var, value="system",
                                   command=self._apply_theme_mode)
        view_menu.add_radiobutton(label="Dark Theme", variable=self.theme_var, value="dark",
                                   command=self._apply_theme_mode)
        view_menu.add_radiobutton(label="Light Theme", variable=self.theme_var, value="light",
                                   command=self._apply_theme_mode)
        menubar.add_cascade(label="View", menu=view_menu)

        keybinds_menu = tk.Menu(menubar, tearoff=0)
        keybinds_menu.add_command(label="Edit Keybinds...", command=self._open_keybind_editor)
        menubar.add_cascade(label="Keybinds", menu=keybinds_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About GIF Forge", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

    def _populate_recent_menu(self) -> None:
        self.recent_menu.delete(0, "end")
        recents = self.settings.recent_projects()
        if not recents:
            self.recent_menu.add_command(label="(No recent projects)", state="disabled")
            return
        for path in recents:
            self.recent_menu.add_command(
                label=safe_basename(path),
                command=lambda p=path: self.open_project(p),
            )

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About GIF Forge",
            "GIF Forge\n"
            "A desktop GIF composition editor for stacking transparent GIFs, "
            "managing layered animations, preserving original timing, and "
            "exporting optimized GIFs with advanced rendering options.",
        )

    # =========================================================== layout
    def _build_layout(self) -> None:
        self.paned = ttk.Panedwindow(self, orient="horizontal")
        self.paned.pack(fill="both", expand=True, side="top")

        left_container = ttk.Frame(self.paned, style="Panel.TFrame")
        right_container = ttk.Frame(self.paned)
        self.paned.add(left_container, weight=1)
        self.paned.add(right_container, weight=3)

        self._build_left_panel(left_container)
        self._build_right_panel(right_container)
        self._build_bottom_panel(self)

        self.after(50, lambda: self._safe_sashpos(self.settings.get("sash_position", 280)))

    def _safe_sashpos(self, pos: int) -> None:
        try:
            self.paned.sashpos(0, pos)
        except tk.TclError:
            pass

    # ----------------------------------------------------------- left
    def _build_left_panel(self, parent: ttk.Frame) -> None:
        header = ttk.Label(parent, text="Layers", style="Panel.TLabel", font=("Segoe UI", 10, "bold"))
        header.pack(anchor="w", padx=10, pady=(10, 4))

        btn_row = ttk.Frame(parent, style="Panel.TFrame")
        btn_row.pack(fill="x", padx=8, pady=(0, 6))
        add_gif_btn = ttk.Button(btn_row, text="Add GIF", command=self.add_gif)
        add_gif_btn.pack(side="left", padx=2)
        Tooltip(add_gif_btn, "Add a GIF layer from a file")
        add_space_btn = ttk.Button(btn_row, text="Add Space", command=self.add_space)
        add_space_btn.pack(side="left", padx=2)
        Tooltip(add_space_btn, "Add a transparent spacing layer")
        remove_btn = ttk.Button(btn_row, text="Remove", command=self.remove_selected)
        remove_btn.pack(side="left", padx=2)
        remove_all_btn = ttk.Button(btn_row, text="Remove All", command=self.remove_all)
        remove_all_btn.pack(side="left", padx=2)

        tree_frame = ttk.Frame(parent, style="Panel.TFrame")
        tree_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        columns = ("visible", "locked")
        self.layer_tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="browse")
        self.layer_tree.heading("#0", text="Layer")
        self.layer_tree.heading("visible", text="Visible")
        self.layer_tree.heading("locked", text="Locked")
        self.layer_tree.column("#0", width=150, anchor="w")
        self.layer_tree.column("visible", width=54, anchor="center")
        self.layer_tree.column("locked", width=54, anchor="center")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.layer_tree.yview)
        self.layer_tree.configure(yscrollcommand=vsb.set)
        self.layer_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.layer_tree.bind("<<TreeviewSelect>>", self._on_layer_select)
        self.layer_tree.bind("<Double-1>", self._on_layer_double_click)
        self.layer_tree.bind("<Button-1>", self._on_layer_click)
        self.layer_tree.bind("<ButtonPress-1>", self._on_drag_start, add="+")
        self.layer_tree.bind("<B1-Motion>", self._on_drag_motion, add="+")
        self.layer_tree.bind("<ButtonRelease-1>", self._on_drag_release, add="+")

        context_button = "<Button-3>"
        self.layer_tree.bind(context_button, self._show_context_menu)

        offset_frame = ttk.Frame(parent, style="Panel.TFrame")
        offset_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(offset_frame, text="X Offset", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.x_offset_var = tk.IntVar(value=0)
        self.x_offset_spin = ttk.Spinbox(offset_frame, from_=-99999, to=99999, textvariable=self.x_offset_var,
                                          width=8, command=self._on_offset_change)
        self.x_offset_spin.grid(row=0, column=1, padx=(6, 12))
        self.x_offset_spin.bind("<Return>", self._on_offset_change)
        self.x_offset_spin.bind("<FocusOut>", self._on_offset_change)

        ttk.Label(offset_frame, text="Y Offset", style="Panel.TLabel").grid(row=0, column=2, sticky="w")
        self.y_offset_var = tk.IntVar(value=0)
        self.y_offset_spin = ttk.Spinbox(offset_frame, from_=-99999, to=99999, textvariable=self.y_offset_var,
                                          width=8, command=self._on_offset_change)
        self.y_offset_spin.grid(row=0, column=3, padx=(6, 0))
        self.y_offset_spin.bind("<Return>", self._on_offset_change)
        self.y_offset_spin.bind("<FocusOut>", self._on_offset_change)
        self._set_offset_controls_state("disabled")

    def _set_offset_controls_state(self, state: str) -> None:
        self.x_offset_spin.configure(state=state)
        self.y_offset_spin.configure(state=state)

    # ---------------------------------------------------------- right
    def _build_right_panel(self, parent: ttk.Frame) -> None:
        header = ttk.Label(parent, text="Preview", font=("Segoe UI", 10, "bold"))
        header.pack(anchor="w", padx=10, pady=(10, 4))
        self.preview_panel = PreviewPanel(parent, self.cache, self.theme_manager.palette)
        self.preview_panel.pack(fill="both", expand=True)

    # --------------------------------------------------------- bottom
    def _build_bottom_panel(self, parent: tk.Tk) -> None:
        bottom = ttk.Frame(parent, padding=10)
        bottom.pack(fill="x", side="bottom")

        row1 = ttk.Frame(bottom)
        row1.pack(fill="x")

        ttk.Label(row1, text="Width").pack(side="left")
        self.width_var = tk.IntVar(value=self.project.output_width)
        width_spin = ttk.Spinbox(row1, from_=1, to=10000, textvariable=self.width_var, width=7,
                                  command=self._on_canvas_size_change)
        width_spin.pack(side="left", padx=(4, 12))
        width_spin.bind("<Return>", self._on_canvas_size_change)
        width_spin.bind("<FocusOut>", self._on_canvas_size_change)

        ttk.Label(row1, text="Height").pack(side="left")
        self.height_var = tk.IntVar(value=self.project.output_height)
        height_spin = ttk.Spinbox(row1, from_=1, to=10000, textvariable=self.height_var, width=7,
                                   command=self._on_canvas_size_change)
        height_spin.pack(side="left", padx=(4, 12))
        height_spin.bind("<Return>", self._on_canvas_size_change)
        height_spin.bind("<FocusOut>", self._on_canvas_size_change)

        ttk.Label(row1, text="Output Filename").pack(side="left")
        self.filename_var = tk.StringVar(value=self.project.output_filename)
        filename_entry = ttk.Entry(row1, textvariable=self.filename_var, width=22)
        filename_entry.pack(side="left", padx=(4, 12))
        filename_entry.bind("<FocusOut>", self._on_filename_change)
        filename_entry.bind("<Return>", self._on_filename_change)

        row1b = ttk.Frame(bottom)
        row1b.pack(fill="x", pady=(8, 0))

        ttk.Label(row1b, text="Last Byte").pack(side="left")
        self.last_byte_var = tk.StringVar(value=self.project.last_byte_option)
        last_byte_combo = ttk.Combobox(row1b, textvariable=self.last_byte_var, values=["None", "21", "2C"],
                                        state="readonly", width=6)
        last_byte_combo.pack(side="left", padx=(4, 12))
        last_byte_combo.bind("<<ComboboxSelected>>", self._on_last_byte_change)

        ttk.Label(row1b, text="Duration").pack(side="left")
        self.duration_mode_var = tk.StringVar(value="Auto" if self.project.duration_mode == "auto" else "Custom")
        duration_combo = ttk.Combobox(row1b, textvariable=self.duration_mode_var, values=["Auto", "Custom"],
                                       state="readonly", width=7)
        duration_combo.pack(side="left", padx=(4, 6))
        duration_combo.bind("<<ComboboxSelected>>", self._on_duration_mode_change)

        self.custom_duration_var = tk.IntVar(value=self.project.custom_duration_ms)
        self.custom_duration_spin = ttk.Spinbox(row1b, from_=100, to=600000, increment=100,
                                                 textvariable=self.custom_duration_var, width=8,
                                                 command=self._on_custom_duration_change)
        self.custom_duration_spin.pack(side="left", padx=(0, 4))
        self.custom_duration_spin.bind("<Return>", self._on_custom_duration_change)
        self.custom_duration_spin.bind("<FocusOut>", self._on_custom_duration_change)
        ttk.Label(row1b, text="ms", style="Muted.TLabel").pack(side="left", padx=(0, 12))
        self._update_duration_controls_state()

        self.generate_btn = ttk.Button(row1b, text="Generate", style="Accent.TButton", command=self.on_generate)
        self.generate_btn.pack(side="right")

        row2 = ttk.Frame(bottom)
        row2.pack(fill="x", pady=(8, 0))
        self.output_info_var = tk.StringVar(value="")
        ttk.Label(row2, textvariable=self.output_info_var, style="Muted.TLabel").pack(side="left")

    # ============================================================ theme
    def _on_theme_applied(self, palette: dict) -> None:
        if hasattr(self, "preview_panel"):
            self.preview_panel.set_palette(palette)

    def _apply_theme_mode(self) -> None:
        mode = self.theme_var.get()
        self.project.theme = mode
        self.settings.set("theme_mode", mode)
        self.theme_manager.set_mode(mode)
        self._mark_dirty()

    # =========================================================== layers
    def _selected_layer(self) -> Optional[Layer]:
        if not self._selected_layer_id:
            return None
        return self.project.find_layer(self._selected_layer_id)

    def add_gif(self) -> None:
        paths = filedialog.askopenfilenames(title="Add GIF", filetypes=[("GIF files", "*.gif")])
        if not paths:
            return
        self._push_undo()
        new_id = None
        for path in paths:
            layer = GifLayer(file_path=path)
            self.project.layers.append(layer)
            new_id = layer.id
        self._mark_dirty()
        self._refresh_layer_list(select_id=new_id)

    def add_space(self) -> None:
        height = simpledialog.askinteger("Add Transparent Space", "Height (px):", initialvalue=64,
                                          minvalue=1, maxvalue=10000, parent=self)
        if height is None:
            return
        self._push_undo()
        layer = SpaceLayer(height=height)
        self.project.layers.append(layer)
        self._mark_dirty()
        self._refresh_layer_list(select_id=layer.id)

    def remove_selected(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        if layer.locked:
            messagebox.showwarning("Layer Locked", "Unlock this layer before removing it.")
            return
        self._push_undo()
        self.project.layers = [l for l in self.project.layers if l.id != layer.id]
        self._mark_dirty()
        self._selected_layer_id = None
        self._refresh_layer_list()

    def remove_all(self) -> None:
        if not self.project.layers:
            return
        if not messagebox.askyesno("Remove All", "Remove every layer? This cannot be undone from this dialog."):
            return
        self._push_undo()
        self.project.layers = [l for l in self.project.layers if l.locked]
        self._mark_dirty()
        self._selected_layer_id = None
        self._refresh_layer_list()

    def duplicate_selected(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        self._push_undo()
        clone = layer.clone()
        idx = self.project.index_of(layer.id)
        self.project.layers.insert(idx + 1, clone)
        self._mark_dirty()
        self._refresh_layer_list(select_id=clone.id)

    def toggle_lock_selected(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        self._push_undo()
        layer.locked = not layer.locked
        self._mark_dirty()
        self._refresh_layer_list(select_id=layer.id)

    def toggle_visibility_selected(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        self._push_undo()
        layer.visible = not layer.visible
        self._mark_dirty()
        self._refresh_layer_list(select_id=layer.id)

    def change_file_selected(self) -> None:
        layer = self._selected_layer()
        if not isinstance(layer, GifLayer):
            return
        path = filedialog.askopenfilename(title="Change Source File", filetypes=[("GIF files", "*.gif")])
        if not path:
            return
        self._push_undo()
        layer.change_source(path)
        self._mark_dirty()
        self._refresh_layer_list(select_id=layer.id)

    def edit_height_selected(self) -> None:
        layer = self._selected_layer()
        if not isinstance(layer, SpaceLayer):
            return
        height = simpledialog.askinteger("Edit Height", "Height (px):", initialvalue=layer.height,
                                          minvalue=1, maxvalue=10000, parent=self)
        if height is None:
            return
        self._push_undo()
        layer.height = height
        self._mark_dirty()
        self._refresh_layer_list(select_id=layer.id)

    def _move_selected(self, delta: Optional[int] = None, to_top: bool = False, to_bottom: bool = False) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        self._push_undo()
        idx = self.project.index_of(layer.id)
        layers = self.project.layers
        layers.pop(idx)
        if to_top:
            layers.insert(0, layer)
        elif to_bottom:
            layers.append(layer)
        else:
            new_idx = max(0, min(len(layers), idx + delta))
            layers.insert(new_idx, layer)
        self._mark_dirty()
        self._refresh_layer_list(select_id=layer.id)

    def move_up(self) -> None:
        self._move_selected(delta=-1)

    def move_down(self) -> None:
        self._move_selected(delta=1)

    def move_to_top(self) -> None:
        self._move_selected(to_top=True)

    def move_to_bottom(self) -> None:
        self._move_selected(to_bottom=True)

    # ------------------------------------------------------ tree events
    def _refresh_layer_list(self, select_id: Optional[str] = None) -> None:
        self.layer_tree.delete(*self.layer_tree.get_children())
        for layer in self.project.layers:
            visible_text = "Yes" if layer.visible else "No"
            locked_text = "Yes" if layer.locked else "No"
            self.layer_tree.insert("", "end", iid=layer.id, text=layer.display_name,
                                    values=(visible_text, locked_text))
        target = select_id or self._selected_layer_id
        if target and self.layer_tree.exists(target):
            self.layer_tree.selection_set(target)
            self.layer_tree.see(target)
        else:
            self._selected_layer_id = None
            self.preview_panel.clear()
            self._set_offset_controls_state("disabled")
        self._update_output_info()
        self._update_title()

    def _on_layer_select(self, _event=None) -> None:
        selection = self.layer_tree.selection()
        if not selection:
            self._selected_layer_id = None
            self.preview_panel.clear()
            self._set_offset_controls_state("disabled")
            return
        self._selected_layer_id = selection[0]
        layer = self._selected_layer()
        if isinstance(layer, GifLayer):
            self.preview_panel.show_gif_layer(layer)
            self.x_offset_var.set(layer.x_offset)
            self.y_offset_var.set(layer.y_offset)
            self._set_offset_controls_state("normal")
        elif isinstance(layer, SpaceLayer):
            self.preview_panel.show_space_layer(layer)
            self._set_offset_controls_state("disabled")

    def _on_layer_double_click(self, event) -> None:
        item = self.layer_tree.identify_row(event.y)
        if not item:
            return
        self.layer_tree.selection_set(item)
        self._selected_layer_id = item
        layer = self._selected_layer()
        if isinstance(layer, GifLayer):
            self.change_file_selected()
        elif isinstance(layer, SpaceLayer):
            self.edit_height_selected()

    def _on_layer_click(self, event) -> None:
        region = self.layer_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        column = self.layer_tree.identify_column(event.x)
        item = self.layer_tree.identify_row(event.y)
        if not item:
            return
        self.layer_tree.selection_set(item)
        self._selected_layer_id = item
        layer = self._selected_layer()
        if layer is None:
            return
        if column == "#1":  # visible
            self._push_undo()
            layer.visible = not layer.visible
            self._mark_dirty()
            self._refresh_layer_list(select_id=layer.id)
        elif column == "#2":  # locked
            self._push_undo()
            layer.locked = not layer.locked
            self._mark_dirty()
            self._refresh_layer_list(select_id=layer.id)

    def _on_offset_change(self, _event=None) -> None:
        layer = self._selected_layer()
        if not isinstance(layer, GifLayer):
            return
        try:
            new_x = int(self.x_offset_var.get())
            new_y = int(self.y_offset_var.get())
        except (tk.TclError, ValueError):
            return
        if new_x == layer.x_offset and new_y == layer.y_offset:
            return
        self._push_undo()
        layer.x_offset = new_x
        layer.y_offset = new_y
        self._mark_dirty()
        self._update_output_info()
        self._update_title()

    def _on_canvas_size_change(self, _event=None) -> None:
        try:
            w = max(1, int(self.width_var.get()))
            h = max(1, int(self.height_var.get()))
        except (tk.TclError, ValueError):
            return
        if w == self.project.output_width and h == self.project.output_height:
            return
        self._push_undo()
        self.project.output_width = w
        self.project.output_height = h
        self._mark_dirty()
        self._update_output_info()
        self._update_title()

    def _on_filename_change(self, _event=None) -> None:
        name = self.filename_var.get().strip()
        if not name:
            return
        if name == self.project.output_filename:
            return
        self._push_undo()
        self.project.output_filename = name
        self._mark_dirty()

    def _on_last_byte_change(self, _event=None) -> None:
        value = self.last_byte_var.get()
        if value == self.project.last_byte_option:
            return
        self._push_undo()
        self.project.last_byte_option = value
        self._mark_dirty()

    def _update_duration_controls_state(self) -> None:
        is_custom = self.duration_mode_var.get() == "Custom"
        self.custom_duration_spin.configure(state="normal" if is_custom else "disabled")

    def _on_duration_mode_change(self, _event=None) -> None:
        mode = "custom" if self.duration_mode_var.get() == "Custom" else "auto"
        self._update_duration_controls_state()
        if mode == self.project.duration_mode:
            return
        self._push_undo()
        self.project.duration_mode = mode
        self._mark_dirty()
        self._update_output_info()

    def _on_custom_duration_change(self, _event=None) -> None:
        try:
            value = max(1, int(self.custom_duration_var.get()))
        except (tk.TclError, ValueError):
            return
        if value == self.project.custom_duration_ms:
            return
        self._push_undo()
        self.project.custom_duration_ms = value
        self._mark_dirty()
        if self.project.duration_mode == "custom":
            self._update_output_info()

    # --------------------------------------------------------- context menu
    def _show_context_menu(self, event) -> None:
        item = self.layer_tree.identify_row(event.y)
        if not item:
            return
        self.layer_tree.selection_set(item)
        self._selected_layer_id = item
        layer = self._selected_layer()
        if layer is None:
            return

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Move Up", command=self.move_up)
        menu.add_command(label="Move Down", command=self.move_down)
        menu.add_command(label="Move To Top", command=self.move_to_top)
        menu.add_command(label="Move To Bottom", command=self.move_to_bottom)
        menu.add_separator()
        menu.add_command(label="Duplicate", command=self.duplicate_selected)
        menu.add_command(label="Unlock" if layer.locked else "Lock", command=self.toggle_lock_selected)
        if isinstance(layer, GifLayer):
            menu.add_command(label="Show" if not layer.visible else "Hide", command=self.toggle_visibility_selected)
            menu.add_command(label="Change File...", command=self.change_file_selected)
        else:
            menu.add_command(label="Show" if not layer.visible else "Hide", command=self.toggle_visibility_selected)
            menu.add_command(label="Edit Height...", command=self.edit_height_selected)
        menu.add_separator()
        menu.add_command(label="Remove", command=self.remove_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # -------------------------------------------------------- drag reorder
    def _on_drag_start(self, event) -> None:
        item = self.layer_tree.identify_row(event.y)
        self._drag_start_index = self.project.index_of(item) if item else None

    def _on_drag_motion(self, event) -> None:
        pass  # rely on ButtonRelease to commit; native Treeview row highlight is enough feedback

    def _on_drag_release(self, event) -> None:
        if self._drag_start_index is None:
            return
        target_item = self.layer_tree.identify_row(event.y)
        if not target_item:
            self._drag_start_index = None
            return
        target_index = self.project.index_of(target_item)
        if target_index == -1 or target_index == self._drag_start_index:
            self._drag_start_index = None
            return
        self._push_undo()
        layers = self.project.layers
        layer = layers.pop(self._drag_start_index)
        layers.insert(target_index, layer)
        self._drag_start_index = None
        self._mark_dirty()
        self._refresh_layer_list(select_id=layer.id)

    # ============================================================= undo
    def _push_undo(self) -> None:
        self.undo_manager.push(self.project)

    def undo(self) -> None:
        restored = self.undo_manager.undo(self.project)
        if restored is None:
            return
        self.project = restored
        self._selected_layer_id = None
        self._sync_bottom_controls()
        self._refresh_layer_list()

    def redo(self) -> None:
        restored = self.undo_manager.redo(self.project)
        if restored is None:
            return
        self.project = restored
        self._selected_layer_id = None
        self._sync_bottom_controls()
        self._refresh_layer_list()

    def _sync_bottom_controls(self) -> None:
        self.width_var.set(self.project.output_width)
        self.height_var.set(self.project.output_height)
        self.filename_var.set(self.project.output_filename)
        self.last_byte_var.set(self.project.last_byte_option)
        self.duration_mode_var.set("Custom" if self.project.duration_mode == "custom" else "Auto")
        self.custom_duration_var.set(self.project.custom_duration_ms)
        self._update_duration_controls_state()
        self.theme_var.set(self.project.theme)

    # ============================================================ output info
    def _update_output_info(self) -> None:
        frame_count, total_ms = estimate_frames_and_duration(self.project, self.cache)
        est_bytes = self.project.output_width * self.project.output_height * frame_count * BYTES_PER_PIXEL_PER_FRAME_ESTIMATE
        text = (
            f"Resolution: {self.project.output_width}x{self.project.output_height}   \u2022   "
            f"Estimated Frames: {frame_count}   \u2022   "
            f"Estimated Duration: {format_duration_ms(total_ms)}   \u2022   "
            f"Estimated File Size: ~{format_bytes(est_bytes)}"
        )
        self.output_info_var.set(text)

    # ============================================================= project io
    def _confirm_discard_changes(self) -> bool:
        if not self.project.dirty:
            return True
        response = messagebox.askyesnocancel("Unsaved Changes", "Save changes to the current project first?")
        if response is None:
            return False
        if response:
            return self.save_project()
        return True

    def new_project(self) -> None:
        if not self._confirm_discard_changes():
            return
        self.project = Project.new()
        self.project.theme = self.theme_var.get()
        self.undo_manager.clear()
        self._selected_layer_id = None
        self._sync_bottom_controls()
        self._refresh_layer_list()
        Project.clear_autosave()

    def open_project_dialog(self) -> None:
        if not self._confirm_discard_changes():
            return
        path = filedialog.askopenfilename(title="Open Project", filetypes=[("GIF Forge Project", f"*{PROJECT_EXTENSION}")])
        if not path:
            return
        self.open_project(path)

    def open_project(self, path: str) -> None:
        if not os.path.isfile(path):
            messagebox.showerror("Open Project", f"File not found:\n{path}")
            self.settings.remove_recent_project(path)
            self._populate_recent_menu()
            return
        try:
            self.project = Project.load(path)
        except (OSError, ValueError) as e:
            messagebox.showerror("Open Project", f"Could not open project:\n{e}")
            return
        self.undo_manager.clear()
        self._selected_layer_id = None
        self.settings.add_recent_project(path)
        self.settings.set("last_opened_project", path)
        self._populate_recent_menu()
        self.theme_var.set(self.project.theme)
        self.theme_manager.set_mode(self.project.theme)
        self._sync_bottom_controls()
        self._refresh_layer_list()
        Project.clear_autosave()

    def save_project(self) -> bool:
        if self.project.project_path:
            self.project.save(self.project.project_path)
            self.settings.add_recent_project(self.project.project_path)
            self.settings.set("last_opened_project", self.project.project_path)
            self._populate_recent_menu()
            self._update_title()
            Project.clear_autosave()
            return True
        return self.save_project_as()

    def save_project_as(self) -> bool:
        path = filedialog.asksaveasfilename(title="Save Project As", defaultextension=PROJECT_EXTENSION,
                                             filetypes=[("GIF Forge Project", f"*{PROJECT_EXTENSION}")])
        if not path:
            return False
        self.project.save(path)
        self.settings.add_recent_project(path)
        self.settings.set("last_opened_project", path)
        self._populate_recent_menu()
        self._update_title()
        Project.clear_autosave()
        return True

    def _mark_dirty(self) -> None:
        self.project.mark_dirty()
        self._update_title()

    def _update_title(self) -> None:
        name = safe_basename(self.project.project_path) if self.project.project_path else "Untitled"
        star = "*" if self.project.dirty else ""
        self.title(f"{APP_TITLE} - {name}{star}")

    # ============================================================= autosave
    def _schedule_autosave(self) -> None:
        self._autosave_job = self.after(AUTOSAVE_INTERVAL_MS, self._do_autosave)

    def _do_autosave(self) -> None:
        if self.project.dirty and self.project.layers:
            try:
                self.project.save_autosave()
            except OSError:
                pass
        self._schedule_autosave()

    def _check_autosave_recovery(self) -> None:
        if not Project.autosave_exists():
            self._maybe_show_startup_dialog()
            return
        response = messagebox.askyesno(
            "Recover Unsaved Work",
            "GIF Forge found an autosaved project from a previous session. Recover it?",
        )
        if response:
            try:
                self.project = Project.load_autosave()
                self._sync_bottom_controls()
                self._refresh_layer_list()
                self.theme_manager.set_mode(self.project.theme)
            except (OSError, ValueError) as e:
                messagebox.showerror("Recovery Failed", str(e))
        else:
            Project.clear_autosave()
            self._maybe_show_startup_dialog()

    def _maybe_show_startup_dialog(self) -> None:
        recents = self.settings.recent_projects()
        if not recents or self.project.layers:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Welcome to GIF Forge")
        dialog.geometry("380x320")
        dialog.transient(self)
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Recent Projects", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        listbox = tk.Listbox(frame, height=10)
        for path in recents:
            listbox.insert("end", safe_basename(path))
        listbox.pack(fill="both", expand=True, pady=(8, 8))

        def open_selected():
            sel = listbox.curselection()
            if not sel:
                return
            path = recents[sel[0]]
            dialog.destroy()
            self.open_project(path)

        btns = ttk.Frame(frame)
        btns.pack(fill="x")
        ttk.Button(btns, text="Open", command=open_selected).pack(side="left")
        ttk.Button(btns, text="New Project", command=dialog.destroy).pack(side="right")
        listbox.bind("<Double-1>", lambda e: open_selected())

    # ============================================================= keybinds
    def _setup_hotkeys(self) -> None:
        callbacks = {
            "delete_layer": self.remove_selected,
            "duplicate_layer": self.duplicate_selected,
            "move_up": self.move_up,
            "move_down": self.move_down,
            "move_to_top": self.move_to_top,
            "move_to_bottom": self.move_to_bottom,
            "save": self.save_project,
            "save_as": self.save_project_as,
            "open": self.open_project_dialog,
            "new": self.new_project,
            "undo": self.undo,
            "redo": self.redo,
            "play_pause": lambda: self.preview_panel.toggle_play(),
        }
        self.keybind_manager.attach(self, callbacks)

    def _open_keybind_editor(self) -> None:
        KeybindEditorDialog(self, self.keybind_manager, on_change=self.keybind_manager.reset_state)

    # ============================================================= render
    def _validate_before_render(self) -> bool:
        if self.project.output_width <= 0 or self.project.output_height <= 0:
            return False  # spinboxes already prevent this
        if not self.project.gif_layers():
            messagebox.showwarning("Nothing To Render", "Add at least one GIF layer first.")
            return False

        warnings = []
        if self.project.output_width < 8 or self.project.output_height < 8:
            warnings.append("The output canvas is very small.")

        layout = compute_layout(self.project.layers, self.cache)
        for lo in layout:
            if not isinstance(lo.layer, GifLayer) or not lo.layer.visible:
                continue
            if lo.x < 0 or lo.y < 0 or lo.x + lo.width > self.project.output_width \
                    or lo.y + lo.height > self.project.output_height:
                warnings.append(f"'{lo.layer.display_name}' extends outside the canvas.")
                break

        frame_count, total_ms = estimate_frames_and_duration(self.project, self.cache)
        est_bytes = self.project.output_width * self.project.output_height * frame_count * BYTES_PER_PIXEL_PER_FRAME_ESTIMATE
        if est_bytes > LARGE_OUTPUT_WARN_BYTES:
            warnings.append(f"Estimated output size is very large (~{format_bytes(est_bytes)}).")

        if warnings:
            proceed = messagebox.askyesno(
                "Warnings",
                "\n".join(warnings) + "\n\nContinue anyway?",
            )
            if not proceed:
                return False

        return True

    def on_generate(self) -> None:
        if not self._validate_before_render():
            return

        directory = os.path.dirname(self.project.recent_output_path or "") or os.getcwd()
        output_path = filedialog.asksaveasfilename(
            title="Generate GIF",
            initialdir=directory,
            initialfile=self.project.output_filename,
            defaultextension=".gif",
            filetypes=[("GIF files", "*.gif")],
        )
        if not output_path:
            return

        self.project.recent_output_path = output_path
        self._mark_dirty()

        self._progress_dialog = ProgressDialog(self, on_cancel=self.render_engine.cancel)
        self.generate_btn.state(["disabled"])

        def progress_callback(stage: str, fraction: float) -> None:
            self._render_queue.put(("progress", stage, fraction))

        def done_callback(error) -> None:
            self._render_queue.put(("done", error))

        self.render_engine.start(self.project, output_path, progress_callback, done_callback)
        self.after(50, self._poll_render_queue)

    def _poll_render_queue(self) -> None:
        try:
            while True:
                message = self._render_queue.get_nowait()
                if message[0] == "progress":
                    _, stage, fraction = message
                    if self._progress_dialog is not None:
                        self._progress_dialog.update_progress(stage, fraction)
                elif message[0] == "done":
                    _, error = message
                    self._on_render_done(error)
                    return
        except queue.Empty:
            pass
        self.after(50, self._poll_render_queue)

    def _on_render_done(self, error) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.destroy()
            self._progress_dialog = None
        self.generate_btn.state(["!disabled"])
        if error is not None:
            from renderer import RenderCancelled
            if isinstance(error, RenderCancelled):
                return
            messagebox.showerror("Render Failed", str(error))
            return
        messagebox.showinfo("Generate GIF", "GIF generated successfully.")

    # ============================================================== close
    def _on_close(self) -> None:
        if not self._confirm_discard_changes():
            return
        self.settings.set("window_geometry", self.geometry())
        try:
            self.settings.set("sash_position", self.paned.sashpos(0))
        except tk.TclError:
            pass
        self.settings.save()
        Project.clear_autosave()
        self.theme_manager.stop_system_watch()
        self.destroy()
