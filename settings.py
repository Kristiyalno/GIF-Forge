"""Persistent app-level settings: window geometry, splitter position, recent
projects, last opened project, theme mode. Stored as JSON under settings/."""

from __future__ import annotations

import json
import os
from typing import List, Optional

from utils import ensure_dir, resource_path

SETTINGS_PATH = resource_path("settings", "app_settings.json")

DEFAULTS = {
    "window_geometry": "1200x760",
    "sash_position": 280,
    "recent_projects": [],
    "last_opened_project": None,
    "theme_mode": "system",
    "max_recent": 10,
}


class SettingsManager:
    def __init__(self):
        self.data = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        if os.path.isfile(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                merged = dict(DEFAULTS)
                merged.update(loaded)
                self.data = merged
            except (json.JSONDecodeError, OSError):
                self.data = dict(DEFAULTS)

    def save(self) -> None:
        ensure_dir(os.path.dirname(SETTINGS_PATH))
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value

    def add_recent_project(self, path: str) -> None:
        recents: List[str] = list(self.data.get("recent_projects", []))
        path = os.path.abspath(path)
        recents = [p for p in recents if os.path.abspath(p) != path]
        recents.insert(0, path)
        max_recent = self.data.get("max_recent", 10)
        self.data["recent_projects"] = recents[:max_recent]

    def recent_projects(self) -> List[str]:
        return [p for p in self.data.get("recent_projects", []) if os.path.isfile(p)]

    def remove_recent_project(self, path: str) -> None:
        recents = list(self.data.get("recent_projects", []))
        path = os.path.abspath(path)
        recents = [p for p in recents if os.path.abspath(p) != path]
        self.data["recent_projects"] = recents
