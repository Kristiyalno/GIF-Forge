"""Project state, .gfp serialization, and the undo/redo stack.

A .gfp file is plain JSON. GIF pixel data is never embedded, only the file
paths layers point to, per the spec.
"""

from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from layers import GifLayer, Layer, SpaceLayer, layer_from_dict
from utils import ensure_dir, resource_path

PROJECT_EXTENSION = ".gfp"
FORMAT_VERSION = 1

AUTOSAVE_DIR = resource_path("projects", ".autosave")
AUTOSAVE_PATH = os.path.join(AUTOSAVE_DIR, "autosave.gfp")


@dataclass
class Project:
    layers: List[Layer] = field(default_factory=list)
    output_width: int = 480
    output_height: int = 480
    output_filename: str = "output.gif"
    last_byte_option: str = "None"  # "None" | "21" | "2C"
    duration_mode: str = "auto"  # "auto" | "custom"
    custom_duration_ms: int = 4000
    theme: str = "system"
    keybinds: dict = field(default_factory=dict)
    recent_output_path: Optional[str] = None

    # Runtime-only (not serialized)
    project_path: Optional[str] = field(default=None, compare=False)
    dirty: bool = field(default=False, compare=False)

    # ---------------------------------------------------------------- new
    @classmethod
    def new(cls) -> "Project":
        return cls()

    # ----------------------------------------------------------- dict io
    def to_dict(self) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "layers": [l.to_dict() for l in self.layers],
            "output_width": self.output_width,
            "output_height": self.output_height,
            "output_filename": self.output_filename,
            "last_byte_option": self.last_byte_option,
            "duration_mode": self.duration_mode,
            "custom_duration_ms": self.custom_duration_ms,
            "theme": self.theme,
            "keybinds": self.keybinds,
            "recent_output_path": self.recent_output_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        layers = []
        for ld in d.get("layers", []):
            layer = layer_from_dict(ld)
            if layer is not None:
                layers.append(layer)
        return cls(
            layers=layers,
            output_width=d.get("output_width", 480),
            output_height=d.get("output_height", 480),
            output_filename=d.get("output_filename", "output.gif"),
            last_byte_option=d.get("last_byte_option", "None"),
            duration_mode=d.get("duration_mode", "auto"),
            custom_duration_ms=d.get("custom_duration_ms", 4000),
            theme=d.get("theme", "system"),
            keybinds=d.get("keybinds", {}),
            recent_output_path=d.get("recent_output_path"),
        )

    # ------------------------------------------------------------- file io
    def save(self, path: str) -> None:
        ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp_path, path)
        self.project_path = path
        self.dirty = False

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        project = cls.from_dict(d)
        project.project_path = path
        project.dirty = False
        return project

    # --------------------------------------------------------------- misc
    def mark_dirty(self) -> None:
        self.dirty = True

    def mark_clean(self) -> None:
        self.dirty = False

    def gif_layers(self) -> List[GifLayer]:
        return [l for l in self.layers if isinstance(l, GifLayer)]

    def find_layer(self, layer_id: str) -> Optional[Layer]:
        for l in self.layers:
            if l.id == layer_id:
                return l
        return None

    def index_of(self, layer_id: str) -> int:
        for i, l in enumerate(self.layers):
            if l.id == layer_id:
                return i
        return -1

    # ----------------------------------------------------------- autosave
    def save_autosave(self) -> None:
        ensure_dir(AUTOSAVE_DIR)
        d = self.to_dict()
        d["_autosave_timestamp"] = time.time()
        d["_autosave_source_path"] = self.project_path
        tmp_path = AUTOSAVE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp_path, AUTOSAVE_PATH)

    @staticmethod
    def autosave_exists() -> bool:
        return os.path.isfile(AUTOSAVE_PATH)

    @staticmethod
    def load_autosave() -> "Project":
        with open(AUTOSAVE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        project = Project.from_dict(d)
        project.project_path = d.get("_autosave_source_path")
        project.dirty = True
        return project

    @staticmethod
    def clear_autosave() -> None:
        try:
            os.remove(AUTOSAVE_PATH)
        except FileNotFoundError:
            pass


class UndoManager:
    """A bounded undo/redo stack of full project-state snapshots.

    Snapshots are cheap plain-dict copies (via Project.to_dict), so pushing
    one is fast even for large layer stacks, and restoring is just
    Project.from_dict.
    """

    def __init__(self, max_levels: int = 20):
        self.max_levels = max_levels
        self._undo_stack: List[dict] = []
        self._redo_stack: List[dict] = []

    def clear(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()

    def push(self, project: Project) -> None:
        """Record project's current state as an undo point and clear the redo stack."""
        snapshot = copy.deepcopy(project.to_dict())
        self._undo_stack.append(snapshot)
        if len(self._undo_stack) > self.max_levels:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    def undo(self, project: Project) -> Optional[Project]:
        """Restore the previous state, pushing the current state onto the redo stack."""
        if not self._undo_stack:
            return None
        current_snapshot = copy.deepcopy(project.to_dict())
        self._redo_stack.append(current_snapshot)
        previous = self._undo_stack.pop()
        restored = Project.from_dict(previous)
        restored.project_path = project.project_path
        restored.dirty = True
        return restored

    def redo(self, project: Project) -> Optional[Project]:
        if not self._redo_stack:
            return None
        current_snapshot = copy.deepcopy(project.to_dict())
        self._undo_stack.append(current_snapshot)
        nxt = self._redo_stack.pop()
        restored = Project.from_dict(nxt)
        restored.project_path = project.project_path
        restored.dirty = True
        return restored
