"""Timeline layer model — multi-track layer system."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional
from uuid import uuid4

if TYPE_CHECKING:
    from .segment import Segment

LayerKind = Literal["background", "side_rails", "floor", "stickman", "countdown"]

# Category colours used by timeline UI (hex strings).
LAYER_KIND_COLORS: dict[str, str] = {
    "background": "#2563eb",   # blue
    "side_rails":  "#a21caf",  # magenta
    "floor":       "#0891b2",  # cyan
    "stickman":    "#ca8a04",  # yellow
    "countdown":   "#15803d",  # green
}


@dataclass
class Layer:
    """A configurable layer block on the timeline.

    Each layer block lives on its own track row, has a time range, and
    holds a config dict for its category.  Multiple non-overlapping layer
    blocks can exist on the same track.  When blocks overlap on the same
    track the one with the highest z_index wins.
    """

    id: str = field(default_factory=lambda: str(uuid4()))
    kind: LayerKind = "background"
    start_time_sec: float = 0.0
    end_time_sec: float = 0.0
    z_index: int = 0
    name: str = ""
    config: dict = field(default_factory=dict)

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_time_sec - self.start_time_sec)

    def overlaps(self, start: float, end: float) -> bool:
        """Return True if this layer overlaps time range [start, end)."""
        return self.start_time_sec < end and self.end_time_sec > start


# ---------------------------------------------------------------------------
# Default configs
# ---------------------------------------------------------------------------

def _default_floor_config() -> dict:
    """Default floor config — matches BaseRenderSettings defaults."""
    return {
        "floor_panels": True,
        "floor_panel_color": None,
        "floor_panel_opacity": 1.0,
        "floor_panel_blink": False,
        "floor_panel_image": None,
        "floor_layout": "auto",
        "floor_bg_color": None,
        "floor_bg_opacity": 1.0,
        "chevron_color": "#FFD700",
        "chevron_scroll": True,
        "chevron_blink": False,
        "chevron_width_frac": 0.45,
        "chevron_count": 6,
    }


# ---------------------------------------------------------------------------
# Auto-create helpers
# ---------------------------------------------------------------------------

def auto_create_default_layers(project: "object", segment: "Segment") -> None:
    """Auto-generate Background + Floor layers covering a new segment.

    Skips creation if a layer of the same kind already overlaps the
    segment's range (avoids stacking duplicates when a segment is
    created under an existing layer).
    """
    s_start = segment.start_time_sec
    s_end = segment.end_time_sec

    defaults: list[tuple[LayerKind, dict]] = [
        ("background", {"bg_type": "solid", "bg_color": "#000000"}),
        ("floor", _default_floor_config()),
    ]

    for kind, default_config in defaults:
        existing = [
            la for la in project.layers
            if la.kind == kind and la.overlaps(s_start, s_end)
        ]
        if existing:
            continue
        project.layers.append(Layer(
            kind=kind,
            start_time_sec=s_start,
            end_time_sec=s_end,
            z_index=0,
            name=f"Auto {kind.replace('_', ' ').title()}",
            config=default_config,
        ))


# ---------------------------------------------------------------------------
# Effective config resolution
# ---------------------------------------------------------------------------

def resolve_segment_config(
    segment: "Segment",
    project_layers: "list[Layer]",
) -> dict:
    """Compute effective render config for a segment.

    Merges segment.render_settings (legacy fallback) with any overlapping
    layer configs.  For each layer kind the highest-z_index overlap wins.
    Layer config keys override the matching keys in render_settings.
    """
    effective = dict(segment.render_settings or {})

    s_start = segment.start_time_sec
    s_end = segment.end_time_sec

    for kind in ("background", "side_rails", "floor", "stickman", "countdown"):
        overlapping = [
            la for la in project_layers
            if la.kind == kind and la.overlaps(s_start, s_end)
        ]
        if not overlapping:
            continue
        top_layer = max(overlapping, key=lambda la: la.z_index)
        effective.update(top_layer.config)

    return effective
