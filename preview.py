"""Right panel: a checkerboard preview with playback controls.

Selecting a GIF layer previews that layer alone; selecting a transparent
space shows a height readout; deselecting everything (Escape, or clicking
empty space in the layer list) shows the whole composited canvas instead of
an empty placeholder, so there's always something meaningful on screen.

The preview canvas is sized to fit whatever it's showing (scaled down to a
max bounding box, preserving aspect ratio) rather than always being a fixed
square - a wide or tall GIF fills the preview edge-to-edge instead of
floating in a padded square with extra checkerboard around it.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional

from PIL import ImageTk

from cache import GifCache
from layers import GifLayer, SpaceLayer

# Bounding box the preview fits its content inside - not a fixed shape.
MAX_PREVIEW_SIZE = (240, 240)


def _fit_within(native_w: int, native_h: int, max_w: int, max_h: int):
    """Scale (native_w, native_h) down to fit within (max_w, max_h),
    preserving aspect ratio. Never upscales past the native size."""
    if native_w <= 0 or native_h <= 0:
        return max_w, max_h
    scale = min(max_w / native_w, max_h / native_h, 1.0)
    return max(1, round(native_w * scale)), max(1, round(native_h * scale))


class PreviewPanel(ttk.Frame):
    def __init__(self, parent, cache: GifCache, palette: dict, **kwargs):
        super().__init__(parent, **kwargs)
        self.cache = cache
        self.palette = palette

        self.canvas = tk.Canvas(self, width=MAX_PREVIEW_SIZE[0], height=MAX_PREVIEW_SIZE[1],
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

        self._canvas_size = MAX_PREVIEW_SIZE
        self._photo_frames = []
        self._durations = []
        self._frame_index = 0
        self._playing = False
        self._after_id: Optional[str] = None
        self._checker_ids = []
        self._image_item = None
        self._mode = "empty"  # empty | gif | space | composite

        self._draw_checkerboard(*MAX_PREVIEW_SIZE)
        self.set_palette(palette)

    @property
    def showing_composite(self) -> bool:
        return self._mode == "composite"

    # ------------------------------------------------------------- palette
    def set_palette(self, palette: dict) -> None:
        self.palette = palette
        self.canvas.configure(bg=palette["bg_alt"])
        self._draw_checkerboard(*self._canvas_size)
        self._render_current()

    def _draw_checkerboard(self, width: int, height: int) -> None:
        for cid in self._checker_ids:
            self.canvas.delete(cid)
        self._checker_ids.clear()
        tile = 12
        a, b = self.palette["checker_a"], self.palette["checker_b"]
        for y in range(0, height, tile):
            for x in range(0, width, tile):
                color = a if ((x // tile) + (y // tile)) % 2 == 0 else b
                cid = self.canvas.create_rectangle(x, y, x + tile, y + tile, fill=color, outline="")
                self._checker_ids.append(cid)
        for cid in self._checker_ids:
            self.canvas.tag_lower(cid)

    def _resize_canvas(self, width: int, height: int) -> None:
        self._canvas_size = (width, height)
        self.canvas.configure(width=width, height=height)
        self._draw_checkerboard(width, height)

    # --------------------------------------------------------------- api
    def clear(self) -> None:
        self._stop_playback()
        self._mode = "empty"
        self._photo_frames = []
        self._durations = []
        if self._image_item is not None:
            self.canvas.delete(self._image_item)
            self._image_item = None
        self._resize_canvas(*MAX_PREVIEW_SIZE)
        self.frame_counter_var.set("")
        self.info_var.set("No layer selected")
        self.btn_prev.state(["disabled"])
        self.btn_play.state(["disabled"])
        self.btn_next.state(["disabled"])

    def show_gif_layer(self, layer: GifLayer) -> None:
        self._stop_playback()
        self._mode = "gif"
        try:
            decoded = self.cache.get_or_decode(layer.file_path)
        except (OSError, ValueError) as e:
            self.clear()
            self.info_var.set(f"Could not load: {e}")
            return

        fit_w, fit_h = _fit_within(decoded.size[0], decoded.size[1], *MAX_PREVIEW_SIZE)
        thumbs = self.cache.get_thumbnail_frames(layer.file_path, (fit_w, fit_h))
        self._photo_frames = [ImageTk.PhotoImage(f) for f in thumbs]
        self._durations = decoded.durations_ms
        self._frame_index = 0

        self._resize_canvas(fit_w, fit_h)
        if self._image_item is not None:
            self.canvas.delete(self._image_item)
        self._image_item = self.canvas.create_image(fit_w // 2, fit_h // 2, image=self._photo_frames[0])

        size_note = "" if (fit_w, fit_h) == decoded.size else f" (preview {fit_w}x{fit_h})"
        self.info_var.set(f"{decoded.size[0]}x{decoded.size[1]} px{size_note}  \u2022  {decoded.frame_count} frames")
        self._update_frame_counter()
        self.btn_prev.state(["!disabled"])
        self.btn_play.state(["!disabled"])
        self.btn_next.state(["!disabled"])
        self._playing = True
        self.btn_play.configure(text="\u23f8")
        self._schedule_next_frame()

    def show_composite(self, project, cache: GifCache) -> None:
        """Show the whole canvas composited from every visible layer, at
        their real relative positions and sizes - what Generate would
        actually produce, scaled down to fit the preview."""
        from renderer import build_composite_preview_frames

        self._stop_playback()
        frames, durations_ms, canvas_size = build_composite_preview_frames(project, cache, max_size=MAX_PREVIEW_SIZE)
        if not frames:
            self.clear()
            if project.layers:
                self.info_var.set("Nothing visible to preview")
            return

        self._mode = "composite"
        fit_w, fit_h = canvas_size
        self._photo_frames = [ImageTk.PhotoImage(f) for f in frames]
        self._durations = durations_ms
        self._frame_index = 0

        self._resize_canvas(fit_w, fit_h)
        if self._image_item is not None:
            self.canvas.delete(self._image_item)
        self._image_item = self.canvas.create_image(fit_w // 2, fit_h // 2, image=self._photo_frames[0])

        self.info_var.set(
            f"Full composite  \u2022  {project.output_width}x{project.output_height} px  "
            f"\u2022  {len(frames)} frames"
        )
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
        if self._image_item is not None:
            self.canvas.delete(self._image_item)
            self._image_item = None
        self._resize_canvas(MAX_PREVIEW_SIZE[0], height)
        self.frame_counter_var.set("")
        self.info_var.set(f"Height: {layer.height} px")
        self.btn_prev.state(["disabled"])
        self.btn_play.state(["disabled"])
        self.btn_next.state(["disabled"])

    # ---------------------------------------------------------- playback
    def _is_playable(self) -> bool:
        return self._mode in ("gif", "composite")

    def toggle_play(self) -> None:
        if not self._is_playable():
            return
        self._playing = not self._playing
        self.btn_play.configure(text="\u23f8" if self._playing else "\u25b6")
        if self._playing:
            self._schedule_next_frame()
        elif self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None

    def next_frame(self) -> None:
        if not self._is_playable() or not self._photo_frames:
            return
        self._frame_index = (self._frame_index + 1) % len(self._photo_frames)
        self._render_current()

    def prev_frame(self) -> None:
        if not self._is_playable() or not self._photo_frames:
            return
        self._frame_index = (self._frame_index - 1) % len(self._photo_frames)
        self._render_current()

    def _schedule_next_frame(self) -> None:
        if not self._playing or not self._is_playable() or not self._photo_frames:
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
        if not self._is_playable() or not self._photo_frames:
            return
        if self._image_item is not None:
            self.canvas.itemconfigure(self._image_item, image=self._photo_frames[self._frame_index])
        self._update_frame_counter()

    def _update_frame_counter(self) -> None:
        if self._is_playable() and self._photo_frames:
            self.frame_counter_var.set(f"Frame {self._frame_index + 1} / {len(self._photo_frames)}")

    def _stop_playback(self) -> None:
        self._playing = False
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
