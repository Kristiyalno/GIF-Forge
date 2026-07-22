"""Master timeline construction.

GIF Forge does not use the least common multiple of every layer's loop
length. Waiting for every GIF to line up again can force a render minutes
long even when every source GIF is only a few seconds - the point isn't to
wait for perfect realignment, it's to preserve each GIF's original timing
while it loops on its own.

Every GIF layer has its own internal timeline, built straight from its
stored frame delays. At any render time `t`, a layer's active frame is
found independently of every other layer:

    local_time = t % layer_cycle_ms

`local_time` then falls within one of that layer's frames. Each layer does
this on its own, so every animation keeps its original speed no matter what
else is on the canvas.

Total output duration is no longer LCM-driven either. The user picks:

* Auto   - a practical duration: the longest single layer's loop, doubled.
* Custom - any duration the user enters.

All layers keep looping independently until that chosen duration is
reached.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Dict, List

# Auto duration = longest single layer cycle * this multiplier.
AUTO_DURATION_MULTIPLIER = 2
DEFAULT_CUSTOM_DURATION_MS = 4000


@dataclass
class LayerTimeline:
    layer_id: str
    durations_ms: List[int]
    boundaries_ms: List[int]  # cumulative start time of each frame, length == len(durations_ms)
    cycle_ms: int

    def frame_index_at(self, t_ms: int) -> int:
        """Which source frame this layer shows at time t_ms, computed
        independently of every other layer via local_time = t % cycle_ms."""
        if self.cycle_ms <= 0:
            return 0
        local_time = t_ms % self.cycle_ms
        idx = bisect.bisect_right(self.boundaries_ms, local_time) - 1
        if idx < 0:
            idx = 0
        return idx


def build_layer_timeline(layer_id: str, durations_ms: List[int]) -> LayerTimeline:
    durations = [max(1, d) for d in durations_ms] if durations_ms else [100]
    boundaries = []
    running = 0
    for d in durations:
        boundaries.append(running)
        running += d
    return LayerTimeline(layer_id=layer_id, durations_ms=durations, boundaries_ms=boundaries, cycle_ms=running)


def compute_auto_duration_ms(layer_durations: Dict[str, List[int]],
                              multiplier: int = AUTO_DURATION_MULTIPLIER) -> int:
    """Auto duration: the longest single layer's loop cycle, times a
    practical multiplier, so short GIFs still get more than one loop."""
    cycles = [sum(d) for d in layer_durations.values() if d]
    if not cycles:
        return DEFAULT_CUSTOM_DURATION_MS
    return max(cycles) * multiplier


@dataclass
class MasterTimeline:
    total_ms: int
    cut_points_ms: List[int]  # sorted, includes 0, excludes total_ms
    layer_timelines: Dict[str, LayerTimeline]


def compute_master_timeline(layer_durations: Dict[str, List[int]], total_ms: int) -> MasterTimeline:
    """Build the master timeline for a set of GIF layers (id -> per-frame
    durations) over exactly total_ms, with every layer looping
    independently (local_time = t % cycle_ms) for as long as it takes to
    fill that duration."""
    timelines = {lid: build_layer_timeline(lid, d) for lid, d in layer_durations.items()}
    total_ms = max(1, int(total_ms))

    if not timelines:
        return MasterTimeline(total_ms=total_ms, cut_points_ms=[0], layer_timelines={})

    # Union of every layer's own frame boundaries, replicated across
    # however many times that layer loops within total_ms, gives the exact
    # set of moments where *something* on the canvas changes.
    cut_set = {0}
    for tl in timelines.values():
        if tl.cycle_ms <= 0:
            continue
        repeats = (total_ms // tl.cycle_ms) + 2
        for r in range(repeats):
            base = r * tl.cycle_ms
            if base >= total_ms:
                break
            for b in tl.boundaries_ms:
                point = base + b
                if point < total_ms:
                    cut_set.add(point)
    cut_points = sorted(cut_set)

    return MasterTimeline(total_ms=total_ms, cut_points_ms=cut_points, layer_timelines=timelines)


def master_frame_durations(master: MasterTimeline) -> List[int]:
    """Convert cut points into a list of (integer, >=1 ms) frame durations covering total_ms."""
    points = master.cut_points_ms + [master.total_ms]
    durations = []
    for i in range(len(points) - 1):
        d = points[i + 1] - points[i]
        durations.append(max(1, d))
    return durations
