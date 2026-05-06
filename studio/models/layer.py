"""Timeline layer model — multi-track layer system."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional
from uuid import uuid4

if TYPE_CHECKING:
    from .segment import Segment

LayerKind = Literal[
    "background", "side_rails", "floor", "stickman", "countdown", "start_gate", "combo"
]

# Category colours used by timeline UI (hex strings).
LAYER_KIND_COLORS: dict[str, str] = {
    "background": "#2563eb",   # blue
    "side_rails":  "#a21caf",  # magenta
    "floor":       "#0891b2",  # cyan
    "stickman":    "#ca8a04",  # yellow
    "countdown":   "#15803d",  # green
    "start_gate":  "#ea580c",  # orange
    "combo":       "#dc2626",  # red
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
    """Auto-generate Background + Floor + Stickman layers for a new segment.

    Creates one default layer per kind for this segment's exact time range.
    Repeated calls for the same segment are idempotent: a kind is skipped
    only when an exact-range layer of that kind already exists.
    """
    s_start = segment.start_time_sec
    s_end = segment.end_time_sec
    eps = 1e-6

    defaults: list[tuple[LayerKind, dict]] = [
        ("background", {"bg_type": "solid", "bg_color": "#000000"}),
        ("floor", _default_floor_config()),
        ("stickman", {
            "stickman": True,
            "stickman_location": {"x": 0.010, "y": 0.090, "w": 0.135, "h": 0.540},
        }),
    ]

    for kind, default_config in defaults:
        existing = [
            la for la in project.layers
            if la.kind == kind
            and abs(la.start_time_sec - s_start) <= eps
            and abs(la.end_time_sec - s_end) <= eps
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

# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------

# Visual fields per kind.  If any of these appear in segment.render_settings,
# migration will extract them into a layer block.
_VISUAL_FIELDS_BY_KIND: dict[str, list[str]] = {
    "background": ["bg_type", "bg_color", "background_type", "background_color",
                   "background_image", "background_video",
                   "bg_image", "bg_video"],
    "side_rails": [
        "side_rails", "rail_color", "rail_shape", "rail_height", "rail_offset_x",
        "rail_image", "rail_texture_non_loop", "rail_pulse", "rail_pulse_intensity",
        "rail_chevron_depth", "rail_chevron_density",
        "rail_pillar_count", "rail_pillar_highlight_count", "rail_pillar_radius",
        "rail_chase_mode", "rail_chase_speed_frames",
        "rail_dot_count", "rail_dot_lines", "rail_dot_size_px",
        "rail_dot_anim_mode", "rail_dot_color_near", "rail_dot_color_far",
    ],
    "floor": [
        "floor_panels", "floor_panel_color", "floor_panel_opacity",
        "floor_panel_blink", "floor_panel_image", "floor_full_static_image",
        "floor_layout", "floor_bg_color", "floor_bg_opacity",
        "chevron_color", "chevron_scroll", "chevron_blink",
        "chevron_width_frac", "chevron_count",
    ],
    "stickman": ["stickman", "stickman_location"],
    "countdown": [
        "relax_countdown_enabled", "relax_countdown_color",
        "relax_countdown_max_sec", "relax_countdown_x", "relax_countdown_y",
        "relax_countdown_w", "relax_countdown_h",
        "relax_countdown_anim",
        "relax_countdown_audio_enabled",
        "relax_countdown_audio_mode",
        "relax_countdown_audio_file",
        "relax_countdown_audio_volume",
        "relax_countdown_audio_last_mode",
        "relax_countdown_audio_last_file",
        "relax_countdown_border_thickness",
        "relax_countdown_glow_strength",
    ],
    "start_gate": [
        "start_gate_enabled",
        "start_gate_type",
        "start_gate_color",
        "start_gate_border_color",
        "start_gate_border_thickness",
        "start_gate_image",
        "start_gate_video",
        "start_gate_x",
        "start_gate_y",
        "start_gate_w",
        "start_gate_h",
    ],
    "combo": [
        "combo_enabled",
        "combo_color",
        "combo_label",
        "combo_font_family",
        "combo_fade_after_break_sec",
        "combo_anim",
        "combo_audio_enabled",
        "combo_audio_mode",
        "combo_audio_file",
        "combo_audio_volume",
        "combo_audio_milestone_mode",
        "combo_audio_milestone_file",
        "combo_x", "combo_y", "combo_w", "combo_h",
        "combo_border_thickness",
        "combo_glow_strength",
        "combo_tier1_threshold", "combo_tier1_label",
        "combo_tier2_threshold", "combo_tier2_label",
        "combo_tier3_threshold", "combo_tier3_label",
        "combo_tier4_threshold", "combo_tier4_label",
        "combo_tier1_color", "combo_tier2_color",
        "combo_tier3_color", "combo_tier4_color",
        "combo_number_font_scale", "combo_label_font_scale", "combo_tier_font_scale",
    ],
}


def migrate_render_settings_to_layers(project: "object") -> None:
    """One-time migration: extract visual layer fields from each segment's
    render_settings into layer blocks.

    Idempotent — skips kind if a layer of that kind already overlaps the
    segment's range.  Called by ProjectStore.load when project.layers is
    empty so old projects transparently gain layer blocks on first load.
    """
    migrated_count = 0
    for segment in project.segments:
        rs = segment.render_settings or {}
        for kind, fields in _VISUAL_FIELDS_BY_KIND.items():
            extracted = {f: rs[f] for f in fields if f in rs}
            if not extracted:
                continue
            existing = [
                la for la in project.layers
                if la.kind == kind
                and la.overlaps(segment.start_time_sec, segment.end_time_sec)
            ]
            if existing:
                continue
            project.layers.append(Layer(
                kind=kind,
                start_time_sec=segment.start_time_sec,
                end_time_sec=segment.end_time_sec,
                z_index=0,
                name=f"Migrated {kind.replace('_', ' ').title()}",
                config=extracted,
            ))
            migrated_count += 1
    if migrated_count:
        print(
            f"[migration] Extracted {migrated_count} layer block(s) "
            f"from segment.render_settings"
        )


def resolve_segment_config(
    segment: "Segment",
    project_layers: "list[Layer]",
) -> dict:
    """Compute effective render config for a segment.

    Visual layer fields (background, floor, rails, stickman, countdown) are
    ONLY sourced from layer blocks, never from segment.render_settings.
    This prevents stale pydantic defaults (e.g. background_color='#000000')
    baked into render_settings from shadowing a layer's actual config.

    Non-visual fields (mode, BPM, beat_source, lanes, …) come from
    segment.render_settings as before.
    """
    # Collect all visual field names that must come from layers only.
    _visual_keys: set[str] = set()
    for keys in _VISUAL_FIELDS_BY_KIND.values():
        _visual_keys.update(keys)
    # Also include the long-form background aliases used by the renderer.
    _visual_keys.update({"background_type", "background_color",
                         "background_image", "background_video"})

    # Start from segment render_settings minus all visual fields.
    effective = {
        k: v
        for k, v in (segment.render_settings or {}).items()
        if k not in _visual_keys
    }

    s_start = segment.start_time_sec
    s_end = segment.end_time_sec

    for kind in (
        "background", "side_rails", "floor", "stickman", "countdown", "start_gate", "combo"
    ):
        overlapping = [
            la for la in project_layers
            if la.kind == kind and la.overlaps(s_start, s_end)
        ]
        if not overlapping:
            continue
        top_layer = max(overlapping, key=lambda la: la.z_index)
        effective.update(top_layer.config)

    # Normalize background key aliases.
    # _BackgroundSection.get_config() writes the short form (bg_type/bg_color/…)
    # while the renderer reads the long form (background_type/background_color/…).
    # When both forms are present the SHORT form (from the layer) must win,
    # because layers always override the segment's render_settings.
    _BG_ALIASES = (
        ("bg_type",  "background_type"),
        ("bg_color", "background_color"),
        ("bg_image", "background_image"),
        ("bg_video", "background_video"),
    )
    for short, long in _BG_ALIASES:
        if short in effective:
            # Layer wrote the short form.  Propagate to the long alias only
            # when the value is concrete; ``None`` means "unset", and for
            # required long-form fields like ``background_color`` we must keep
            # the key absent so Pydantic can apply its own defaults.
            short_val = effective[short]
            if short_val is not None:
                effective[long] = short_val
        if long in effective:
            # Only the long form exists (legacy segment rs) → mirror to short.
            long_val = effective[long]
            if long_val is not None:
                effective[short] = long_val

    return effective
