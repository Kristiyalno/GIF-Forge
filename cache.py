"""Decoding cache for source GIFs.

GIF Forge may reference the same source GIF from several layers (duplicated
layers, or the same file used twice). Decoding a GIF into RGBA frames is the
most expensive step in the whole pipeline, so every decode is cached keyed by
the file's path + modification time + size, meaning edits to the file on disk
are picked up automatically without the cache going stale.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from PIL import Image, ImageSequence


@dataclass
class DecodedGif:
    """A fully decoded GIF: one RGBA frame per source frame, with original delays."""

    frames: List[Image.Image] = field(default_factory=list)
    durations_ms: List[int] = field(default_factory=list)
    loop: int = 0
    size: Tuple[int, int] = (0, 0)

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def cycle_ms(self) -> int:
        """Total time in milliseconds for one full loop of this GIF."""
        return sum(self.durations_ms)


class GifCache:
    """Thread-safe cache of decoded GIFs and small preview thumbnails."""

    def __init__(self):
        self._lock = threading.RLock()
        self._gifs: Dict[str, DecodedGif] = {}
        self._keys: Dict[str, Tuple[float, int]] = {}  # path -> (mtime, filesize) used to decode
        self._thumbs: Dict[Tuple[str, int, int], List[Image.Image]] = {}

    def _fingerprint(self, path: str) -> Tuple[float, int]:
        stat = os.stat(path)
        return (stat.st_mtime, stat.st_size)

    def get_or_decode(self, path: str) -> DecodedGif:
        """Return a DecodedGif for path, decoding (or re-decoding on change) as needed."""
        path = os.path.abspath(path)
        fingerprint = self._fingerprint(path)
        with self._lock:
            cached_fp = self._keys.get(path)
            if cached_fp == fingerprint and path in self._gifs:
                return self._gifs[path]

        decoded = self._decode(path)

        with self._lock:
            self._gifs[path] = decoded
            self._keys[path] = fingerprint
            # Invalidate any thumbnails tied to the old version of this file.
            stale = [k for k in self._thumbs if k[0] == path]
            for k in stale:
                del self._thumbs[k]
        return decoded

    def _decode(self, path: str) -> DecodedGif:
        frames: List[Image.Image] = []
        durations: List[int] = []
        with Image.open(path) as img:
            loop = img.info.get("loop", 0)
            size = img.size
            for frame in ImageSequence.Iterator(img):
                # Sequential .convert on a seeked frame lets Pillow apply the
                # GIF's internal disposal method, so partial-frame GIFs still
                # composite correctly here.
                rgba = frame.convert("RGBA")
                if rgba.size != size:
                    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
                    canvas.paste(rgba, (0, 0))
                    rgba = canvas
                frames.append(rgba)
                duration = frame.info.get("duration", 100)
                if not isinstance(duration, int) or duration < 0:
                    duration = 100
                durations.append(duration)
        if not frames:
            raise ValueError(f"'{path}' contains no readable frames")
        return DecodedGif(frames=frames, durations_ms=durations, loop=loop, size=size)

    def get_thumbnail_frames(self, path: str, max_size: Tuple[int, int]) -> List[Image.Image]:
        """Return cached, downscaled preview frames for path, sized to fit within max_size."""
        path = os.path.abspath(path)
        key = (path, max_size[0], max_size[1])
        with self._lock:
            if key in self._thumbs:
                return self._thumbs[key]
        decoded = self.get_or_decode(path)
        thumbs = []
        for frame in decoded.frames:
            thumb = frame.copy()
            thumb.thumbnail(max_size, Image.LANCZOS)
            thumbs.append(thumb)
        with self._lock:
            self._thumbs[key] = thumbs
        return thumbs

    def invalidate(self, path: str) -> None:
        path = os.path.abspath(path)
        with self._lock:
            self._gifs.pop(path, None)
            self._keys.pop(path, None)
            stale = [k for k in self._thumbs if k[0] == path]
            for k in stale:
                del self._thumbs[k]

    def clear(self) -> None:
        with self._lock:
            self._gifs.clear()
            self._keys.clear()
            self._thumbs.clear()
