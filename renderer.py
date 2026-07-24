"""Compositing and export.

Layout: layers stack vertically in list order. A layer's default Y position
is the summed height of every layer above it (GIF layers contribute their
decoded frame height, space layers contribute their configured height); the
layer's own x_offset/y_offset then nudges it from that default position.
This is what lets "Add Transparent Space" insert a gap that pushes
everything below it down, while GIF layers can still be fine-tuned by hand.

Rendering walks the master timeline built by timeline.py and, for each
master frame, composites every visible GIF layer's currently-active source
frame at its computed position onto a transparent canvas, then writes the
whole sequence out as one GIF with each master frame's exact duration
preserved.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from PIL import Image

from cache import GifCache
from layers import GifLayer, Layer, SpaceLayer
from project import Project
from timeline import compute_auto_duration_ms, compute_master_timeline, master_frame_durations

STAGE_DECODING = "Decoding GIFs"
STAGE_TIMELINE = "Building Timeline"
STAGE_RENDERING = "Rendering Frames"
STAGE_WRITING = "Writing Output"
STAGE_FINISHED = "Finished"


class RenderCancelled(Exception):
    pass


@dataclass
class LayerLayout:
    layer: Layer
    x: int
    y: int
    width: int
    height: int


def effective_size(layer: GifLayer, cache: GifCache) -> Tuple[int, int]:
    """The layer's rendered size: its explicit width/height if set (resized),
    otherwise the source GIF's native decoded size (Auto)."""
    native_w = native_h = 0
    if layer.file_path:
        try:
            decoded = cache.get_or_decode(layer.file_path)
            native_w, native_h = decoded.size
        except (OSError, ValueError):
            pass
    width = layer.width if layer.width else native_w
    height = layer.height if layer.height else native_h
    return width, height


def compute_layout(layers: List[Layer], cache: GifCache) -> List[LayerLayout]:
    """Compute each layer's stacked (x, y, width, height) box in canvas space."""
    result = []
    running_y = 0
    for layer in layers:
        if isinstance(layer, SpaceLayer):
            result.append(LayerLayout(layer=layer, x=0, y=running_y, width=0, height=layer.height))
            running_y += layer.height
        elif isinstance(layer, GifLayer):
            width, height = effective_size(layer, cache)
            x = layer.x_offset
            y = running_y + layer.y_offset
            result.append(LayerLayout(layer=layer, x=x, y=y, width=width, height=height))
            running_y += height
    return result


def gather_layer_durations(project: Project, cache: GifCache) -> dict:
    """Collect each visible GIF layer's stored frame delays, keyed by layer id."""
    durations = {}
    for layer in project.gif_layers():
        if not layer.visible or not layer.file_path:
            continue
        try:
            decoded = cache.get_or_decode(layer.file_path)
        except (OSError, ValueError):
            continue
        durations[layer.id] = decoded.durations_ms
    return durations


def resolve_duration_ms(project: Project, durations: dict) -> int:
    """Resolve the project's Auto/Custom duration setting into a concrete
    millisecond total."""
    if project.duration_mode == "custom":
        return max(1, int(project.custom_duration_ms))
    return compute_auto_duration_ms(durations)


def estimate_frames_and_duration(project: Project, cache: GifCache) -> Tuple[int, int]:
    """Return (frame_count, total_ms) without rendering any pixels."""
    durations = gather_layer_durations(project, cache)
    total_ms = resolve_duration_ms(project, durations)
    master = compute_master_timeline(durations, total_ms)
    return len(master.cut_points_ms), master.total_ms


def build_composite_preview_frames(
    project: Project,
    cache: GifCache,
    max_size: Tuple[int, int] = (240, 240),
    max_preview_ms: int = 15000,
    max_preview_frames: int = 240,
):
    """Composite the whole canvas (every visible layer, at its real position
    and size) into a small set of preview frames, scaled to fit within
    max_size. Used for the "nothing selected" preview - bounded by
    max_preview_ms/max_preview_frames so an oversized Custom duration can't
    hang the UI, independent of the real export which stays exact.

    Returns (frames, durations_ms, canvas_size) - frames is a list of RGBA
    PIL Images already sized to canvas_size, or ([], [], (w, h)) if there's
    nothing visible to composite.
    """
    canvas_w, canvas_h = max(1, project.output_width), max(1, project.output_height)
    gif_layers = [l for l in project.gif_layers() if l.visible and l.file_path]

    decoded_by_id = {}
    for layer in gif_layers:
        try:
            decoded_by_id[layer.id] = cache.get_or_decode(layer.file_path)
        except (OSError, ValueError):
            pass
    gif_layers = [l for l in gif_layers if l.id in decoded_by_id]
    if not gif_layers:
        return [], [], (canvas_w, canvas_h)

    durations = {lid: d.durations_ms for lid, d in decoded_by_id.items()}
    total_ms = min(resolve_duration_ms(project, durations), max_preview_ms)
    master = compute_master_timeline(durations, total_ms)

    cut_points = master.cut_points_ms
    sampled = len(cut_points) > max_preview_frames
    if sampled:
        step = len(cut_points) / max_preview_frames
        cut_points = [cut_points[int(i * step)] for i in range(max_preview_frames)]

    layout = compute_layout(project.layers, cache)
    layout_by_id = {lo.layer.id: lo for lo in layout if isinstance(lo.layer, GifLayer)}

    # Composite at a scaled-down working resolution from the start (rather
    # than full canvas size then downscale) so this stays fast regardless
    # of how large the configured output canvas is.
    scale = min(max_size[0] / canvas_w, max_size[1] / canvas_h, 1.0)
    preview_canvas_size = (max(1, round(canvas_w * scale)), max(1, round(canvas_h * scale)))

    scaled_frames_by_id = {}
    scaled_pos_by_id = {}
    for layer in gif_layers:
        lo = layout_by_id.get(layer.id)
        if lo is None or lo.width <= 0 or lo.height <= 0:
            continue
        target_w = max(1, round(lo.width * scale))
        target_h = max(1, round(lo.height * scale))
        decoded = decoded_by_id[layer.id]
        scaled_frames_by_id[layer.id] = [
            frame.resize((target_w, target_h), Image.LANCZOS) for frame in decoded.frames
        ]
        scaled_pos_by_id[layer.id] = (round(lo.x * scale), round(lo.y * scale))

    frames = []
    for t in cut_points:
        canvas = Image.new("RGBA", preview_canvas_size, (0, 0, 0, 0))
        for layer in gif_layers:
            if layer.id not in scaled_frames_by_id:
                continue
            tl = master.layer_timelines.get(layer.id)
            if tl is None:
                continue
            frame_idx = tl.frame_index_at(t)
            canvas.alpha_composite(scaled_frames_by_id[layer.id][frame_idx], dest=scaled_pos_by_id[layer.id])
        frames.append(canvas)

    if sampled:
        per_frame = max(20, total_ms // max(1, len(cut_points)))
        frame_durations = [per_frame] * len(cut_points)
    else:
        frame_durations = master_frame_durations(master)

    return frames, frame_durations, preview_canvas_size


def _to_gif_frame(rgba_image: Image.Image) -> Image.Image:
    """Convert an RGBA composite into a palette-mode frame with a single
    transparent index, since GIF only supports 1-bit transparency."""
    alpha = rgba_image.split()[3]
    transparent_mask = alpha.point(lambda a: 255 if a < 128 else 0)
    rgb = rgba_image.convert("RGB")
    pal = rgb.convert("P", palette=Image.ADAPTIVE, colors=255)
    transparent_index = 255
    pal.paste(transparent_index, transparent_mask)
    pal.info["transparency"] = transparent_index
    return pal


def _apply_last_byte(path: str, option: str) -> None:
    if option not in ("21", "2C"):
        return
    value = 0x21 if option == "21" else 0x2C
    with open(path, "r+b") as f:
        f.seek(-1, 2)
        f.write(bytes([value]))


class RenderEngine:
    """Runs a full render on a background thread, reporting progress and
    honoring cancellation."""

    def __init__(self, cache: GifCache):
        self.cache = cache
        self._thread: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()

    def start(
        self,
        project: Project,
        output_path: str,
        progress_callback: Callable[[str, float], None],
        done_callback: Callable[[Optional[Exception]], None],
    ) -> None:
        self.cancel_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(project, output_path, progress_callback, done_callback),
            daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()

    def _run(self, project, output_path, progress_callback, done_callback) -> None:
        try:
            self._render(project, output_path, progress_callback)
            done_callback(None)
        except RenderCancelled:
            done_callback(RenderCancelled("Cancelled"))
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            done_callback(e)

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise RenderCancelled()

    def _render(self, project: Project, output_path: str, progress_callback) -> None:
        progress_callback(STAGE_DECODING, 0.0)
        gif_layers = [l for l in project.gif_layers() if l.visible and l.file_path]
        decoded_by_id = {}
        for i, layer in enumerate(gif_layers):
            self._check_cancelled()
            decoded_by_id[layer.id] = self.cache.get_or_decode(layer.file_path)
            progress_callback(STAGE_DECODING, (i + 1) / max(1, len(gif_layers)))

        progress_callback(STAGE_TIMELINE, 0.0)
        durations = {lid: d.durations_ms for lid, d in decoded_by_id.items()}
        total_ms = resolve_duration_ms(project, durations)
        master = compute_master_timeline(durations, total_ms)
        frame_durations = master_frame_durations(master)
        self._check_cancelled()
        progress_callback(STAGE_TIMELINE, 1.0)

        layout = compute_layout(project.layers, self.cache)
        layout_by_id = {lo.layer.id: lo for lo in layout if isinstance(lo.layer, GifLayer)}

        # Layers with an explicit (non-Auto) size get their frames resized
        # once here, rather than repeatedly during compositing.
        render_frames_by_id: dict = {}
        for layer in gif_layers:
            self._check_cancelled()
            decoded = decoded_by_id[layer.id]
            lo = layout_by_id.get(layer.id)
            if lo is None:
                continue
            target_size = (max(1, lo.width), max(1, lo.height))
            if target_size == decoded.size:
                render_frames_by_id[layer.id] = decoded.frames
            else:
                render_frames_by_id[layer.id] = [
                    frame.resize(target_size, Image.LANCZOS) for frame in decoded.frames
                ]

        canvas_size = (max(1, project.output_width), max(1, project.output_height))
        total_frames = len(master.cut_points_ms)
        output_frames: List[Image.Image] = []

        progress_callback(STAGE_RENDERING, 0.0)
        for i, cut_ms in enumerate(master.cut_points_ms):
            self._check_cancelled()
            canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
            for layer in gif_layers:
                lo = layout_by_id.get(layer.id)
                if lo is None:
                    continue
                tl = master.layer_timelines.get(layer.id)
                if tl is None:
                    continue
                frame_idx = tl.frame_index_at(cut_ms)
                source_frame = render_frames_by_id[layer.id][frame_idx]
                canvas.alpha_composite(source_frame, dest=(lo.x, lo.y))
            output_frames.append(_to_gif_frame(canvas))
            if total_frames:
                progress_callback(STAGE_RENDERING, (i + 1) / total_frames)

        progress_callback(STAGE_WRITING, 0.0)
        self._check_cancelled()
        if not output_frames:
            output_frames = [_to_gif_frame(Image.new("RGBA", canvas_size, (0, 0, 0, 0)))]
            frame_durations = [100]

        output_frames[0].save(
            output_path,
            save_all=True,
            append_images=output_frames[1:],
            duration=frame_durations,
            loop=0,
            disposal=2,
            transparency=255,
            optimize=False,
        )
        progress_callback(STAGE_WRITING, 0.5)

        _apply_last_byte(output_path, project.last_byte_option)
        progress_callback(STAGE_WRITING, 1.0)
        progress_callback(STAGE_FINISHED, 1.0)
