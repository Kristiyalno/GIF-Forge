"""Layer data model.

Two kinds of layers exist:

* GifLayer  - references a source GIF file on disk plus a manual (x, y)
              nudge offset.
* SpaceLayer - a fixed-height transparent gap.

Layers are arranged top-to-bottom in a list. For layout purposes they stack
vertically in list order: each layer's default position is directly below
the combined height of every layer above it, and its x/y offset then nudges
it further from that default. Transparent spaces simply add to that running
height without painting anything, which is what "contribute vertical
spacing before following layers" means in practice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from utils import new_id, safe_basename


@dataclass
class Layer:
    id: str = field(default_factory=new_id)
    visible: bool = True
    locked: bool = False

    kind: str = "layer"  # overridden by subclasses

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "visible": self.visible,
            "locked": self.locked,
            "kind": self.kind,
        }


@dataclass
class GifLayer(Layer):
    file_path: str = ""
    x_offset: int = 0
    y_offset: int = 0

    kind: str = "gif"

    @property
    def display_name(self) -> str:
        return safe_basename(self.file_path)

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update({
            "file_path": self.file_path,
            "x_offset": self.x_offset,
            "y_offset": self.y_offset,
        })
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GifLayer":
        return cls(
            id=d.get("id") or new_id(),
            visible=d.get("visible", True),
            locked=d.get("locked", False),
            file_path=d.get("file_path", ""),
            x_offset=d.get("x_offset", 0),
            y_offset=d.get("y_offset", 0),
        )

    def clone(self) -> "GifLayer":
        return GifLayer(
            id=new_id(),
            visible=self.visible,
            locked=self.locked,
            file_path=self.file_path,
            x_offset=self.x_offset,
            y_offset=self.y_offset,
        )

    def change_source(self, new_path: str) -> None:
        """Swap the source file while preserving offsets/visibility/lock."""
        self.file_path = new_path


@dataclass
class SpaceLayer(Layer):
    height: int = 64

    kind: str = "space"

    @property
    def display_name(self) -> str:
        return f"Space \u2022 {self.height} px"

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update({"height": self.height})
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SpaceLayer":
        return cls(
            id=d.get("id") or new_id(),
            visible=d.get("visible", True),
            locked=d.get("locked", False),
            height=d.get("height", 64),
        )

    def clone(self) -> "SpaceLayer":
        return SpaceLayer(id=new_id(), visible=self.visible, locked=self.locked, height=self.height)


def layer_from_dict(d: dict) -> Optional[Layer]:
    kind = d.get("kind")
    if kind == "gif":
        return GifLayer.from_dict(d)
    if kind == "space":
        return SpaceLayer.from_dict(d)
    return None
