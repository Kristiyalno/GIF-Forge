"""Right panel: a large checkerboard preview with playback controls for the
selected GIF layer, or a height readout for the selected transparent space."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional

from PIL import ImageTk

from cache import GifCache
from layers import GifLayer, SpaceLayer

PREVIEW_SIZE = (240, 240)


class PreviewPanel(ttk.Frame):
    def __init__(self, parent, cache: GifCache, palette: dict, **kwargs):
        super().__init__(parent, **kwargs)
        self.cache = cache
        self.palette = palette

        self.canvas = tk.Canvas(self, width=PREVIEW_SIZE[0], height=PREVIEW_SIZE[1],
                                 highlightthickness=0, borderwidth=0)
        self.canvas.pack(padx=10, pady=10)

        self.controls = ttk.Frame(self)
        self.controls.pack(pady=(0, 6))

        self.btn_prev = ttk.Button(self.controls, text="\u23ee", width=3, command=self.prev_frame)
        self.btn_play = ttk.Button(self.controls, text="\u25b6", width=3, command=self.toggle_play)
        self.btn_next = ttk.Button(self.controls, text="\u23ed", width=3, command=self.next_frame)
        self.btn_prev.pack(side="left", padx=2)
        self.btn_play.pack(side="left", padx=2)
        self.btn_next.pack(side="left", padx=2)

        self.frame_counter_var = tk.StringVar(value="")
        self.frame_counter = ttk.Label(self, textvariable=self.frame_counter_var, style="Muted.TLabel")
        self.frame_counter.pack()

        self.info_var = tk.StringVar(value="No layer selected")
        self.info_label = ttk.Label(self, textvariable=self.info_var, style="Muted.TLabel")
        self.info_label.pack(pady=(4, 0))

        self._photo_frames = []
        self._durations = []
        self._frame_index = 0
        self._playing = False
        self._after_id: Optional[str] = None
        self._checker_ids = []
        self._image_item = None
        self._mode = "empty"  # empty | gif | space

        self._draw_checkerboard()
        self.set_palette(palette)

    # ------------------------------------------------------------- palette
    def set_palette(self, palette: dict) -> None:
        self.palette = palette
        self.canvas.configure(bg=palette["bg_alt"])
        self._draw_checkerboard()
        self._render_current()

    def _draw_checkerboard(self, height: Optional[int] = None) -> None:
        for cid in self._checker_ids:
            self.canvas.delete(cid)
        self._checker_ids.clear()
        tile = 12
        h = height if height is not None else PREVIEW_SIZE[1]
        w = PREVIEW_SIZE[0]
        a, b = self.palette["checker_a"], self.palette["checker_b"]
        for y in range(0, h, tile):
            for x in range(0, w, tile):
                color = a if ((x // tile) + (y // tile)) % 2 == 0 else b
                cid = self.canvas.create_rectangle(x, y, x + tile, y + tile, fill=color, outline="")
                self._checker_ids.append(cid)
        self.canvas.tag_lower(self._checker_ids[0]) if self._checker_ids else None
        for cid in self._checker_ids:
            self.canvas.tag_lower(cid)

    # --------------------------------------------------------------- api
    def clear(self) -> None:
        self._stop_playback()
        self._mode = "empty"
        self._photo_frames = []
        self._durations = []
        if self._image_item is not None:
            self.canvas.delete(self._image_item)
            self._image_item = None
        self.canvas.configure(height=PREVIEW_SIZE[1])
        self._draw_checkerboard()
        self.frame_counter_var.set("")
        self.info_var.set("No layer selected")
        self.btn_prev.state(["disabled"])
        self.btn_play.state(["disabled"])
        self.btn_next.state(["disabled"])

    def show_gif_layer(self, layer: GifLayer) -> None:
        self._stop_playback()
        self._mode = "gif"
        self.canvas.configure(height=PREVIEW_SIZE[1])
        try:
            decoded = self.cache.get_or_decode(layer.file_path)
        except (OSError, ValueError) as e:
            self.clear()
            self.info_var.set(f"Could not load: {e}")
            return

        thumbs = self.cache.get_thumbnail_frames(layer.file_path, PREVIEW_SIZE)
        self._photo_frames = [ImageTk.PhotoImage(f) for f in thumbs]
        self._durations = decoded.durations_ms
        self._frame_index = 0

        self._draw_checkerboard()
        if self._image_item is not None:
            self.canvas.delete(self._image_item)
        cx, cy = PREVIEW_SIZE[0] // 2, PREVIEW_SIZE[1] // 2
        self._image_item = self.canvas.create_image(cx, cy, image=self._photo_frames[0])

        self.info_var.set(f"{decoded.size[0]}x{decoded.size[1]} px  \u2022  {decoded.frame_count} frames")
        self._update_frame_counter()
        self.btn_prev.state(["!disabled"])
        self.btn_play.state(["!disabled"])
        self.btn_next.state(["!disabled"])
        self._playing = True
        self.btn_play.configure(text="\u23f8")
        self._schedule_next_frame()

    def show_space_layer(self, layer: SpaceLayer) -> None:
        self._stop_playback()
        self._mode = "space"
        self._photo_frames = []
        height = max(1, min(layer.height, 400))
        self.canvas.configure(height=height)
        if self._image_item is not None:
            self.canvas.delete(self._image_item)
            self._image_item = None
        self._draw_checkerboard(height=height)
        self.frame_counter_var.set("")
        self.info_var.set(f"Height: {layer.height} px")
        self.btn_prev.state(["disabled"])
        self.btn_play.state(["disabled"])
        self.btn_next.state(["disabled"])

    # ---------------------------------------------------------- playback
    def toggle_play(self) -> None:
        if self._mode != "gif":
            return
        self._playing = not self._playing
        self.btn_play.configure(text="\u23f8" if self._playing else "\u25b6")
        if self._playing:
            self._schedule_next_frame()
        elif self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None

    def next_frame(self) -> None:
        if self._mode != "gif" or not self._photo_frames:
            return
        self._frame_index = (self._frame_index + 1) % len(self._photo_frames)
        self._render_current()

    def prev_frame(self) -> None:
        if self._mode != "gif" or not self._photo_frames:
            return
        self._frame_index = (self._frame_index - 1) % len(self._photo_frames)
        self._render_current()

    def _schedule_next_frame(self) -> None:
        if not self._playing or self._mode != "gif" or not self._photo_frames:
            return
        duration = self._durations[self._frame_index] if self._durations else 100
        duration = max(20, duration)
        self._after_id = self.after(duration, self._advance)

    def _advance(self) -> None:
        if not self._playing:
            return
        self._frame_index = (self._frame_index + 1) % len(self._photo_frames)
        self._render_current()
        self._schedule_next_frame()

    def _render_current(self) -> None:
        if self._mode != "gif" or not self._photo_frames:
            return
        if self._image_item is not None:
            self.canvas.itemconfigure(self._image_item, image=self._photo_frames[self._frame_index])
        self._update_frame_counter()

    def _update_frame_counter(self) -> None:
        if self._mode == "gif" and self._photo_frames:
            self.frame_counter_var.set(f"Frame {self._frame_index + 1} / {len(self._photo_frames)}")

    def _stop_playback(self) -> None:
        self._playing = False
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
