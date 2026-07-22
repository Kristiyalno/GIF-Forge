"""General-purpose helper functions shared across GIF Forge modules."""

from __future__ import annotations

import math
import os
import uuid


def new_id() -> str:
    """Return a short unique identifier for a layer or other runtime object."""
    return uuid.uuid4().hex


def ensure_dir(path: str) -> None:
    """Create a directory (and parents) if it does not already exist."""
    os.makedirs(path, exist_ok=True)


def clamp(value, low, high):
    """Clamp value into the inclusive range [low, high]."""
    return max(low, min(high, value))


def gcd(a: int, b: int) -> int:
    return math.gcd(int(a), int(b))


def lcm(a: int, b: int) -> int:
    a, b = int(a), int(b)
    if a == 0 or b == 0:
        return 0
    return abs(a * b) // gcd(a, b)


def lcm_list(values):
    """Least common multiple of a list of positive integers. Returns 0 for an empty list."""
    values = [int(v) for v in values if v]
    if not values:
        return 0
    result = values[0]
    for v in values[1:]:
        result = lcm(result, v)
    return result


def format_bytes(num_bytes: float) -> str:
    """Human readable byte size, e.g. 1536 -> '1.5 KB'."""
    if num_bytes is None:
        return "--"
    num_bytes = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(num_bytes)} {unit}"
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} GB"


def format_duration_ms(ms: float) -> str:
    """Human readable duration, e.g. 65_500 -> '1m 5.5s'."""
    if ms is None:
        return "--"
    ms = float(ms)
    total_seconds = ms / 1000.0
    if total_seconds < 60:
        return f"{total_seconds:.2f}s"
    minutes = int(total_seconds // 60)
    seconds = total_seconds - minutes * 60
    if minutes < 60:
        return f"{minutes}m {seconds:.1f}s"
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours}h {minutes}m {seconds:.0f}s"


def safe_basename(path: str) -> str:
    try:
        return os.path.basename(path)
    except Exception:
        return str(path)


def resource_path(*parts) -> str:
    """Return an absolute path relative to this file's directory (for assets/settings/projects)."""
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)
