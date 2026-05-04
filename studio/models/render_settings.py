"""Pydantic models for render settings."""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class BeatSource(str, Enum):
    """Beat extraction source from audio."""

    TEMPO = "tempo"
    BEAT = "beat"
    ONSET = "onset"


class RailShape(str, Enum):
    """Side-rail visual style."""
    CHUNKY  = "chunky"    # fence blocks (default)
    TUBE    = "tube"      # continuous extruded strip
    CHEVRON = "chevron"   # >>> inward-pointing arrows
    PILLAR  = "pillar"    # row of 3-D columns with running LED highlight
    DOT     = "dot"       # row of glowing dots with anim + gradient


class RailPulse(str, Enum):
    """Audio-reactive pulse mode for side rails."""
    NONE = "none"   # static
    BEAT = "beat"   # flash on each beat hit
    RMS  = "rms"    # breathe continuously with bass RMS


class SideRailMixin(BaseModel):
    """Per-segment side-rail config, mixed into every mode's settings."""
    model_config = {"extra": "ignore"}

    side_rails: bool = False
    rail_color: str = "#FF60FF"
    rail_shape: RailShape = RailShape.CHUNKY
    rail_height: float = 0.14     # box height (world units)
    rail_offset_x: float = 0.08  # X gap from outer tile edge to box inner face
    rail_image: Optional[str] = None
    rail_texture_non_loop: bool = False
    rail_pulse: RailPulse = RailPulse.BEAT
    rail_pulse_intensity: float = 0.6
    rail_pillar_count: int = Field(default=16, ge=4, le=32)
    rail_pillar_highlight_count: int = Field(default=1, ge=1, le=32)
    rail_pillar_radius: float = Field(default=1.0, ge=0.2, le=2.0)
    rail_chase_mode: Literal["time", "beat"] = "time"
    rail_chase_speed_frames: int = Field(default=4, ge=1, le=60)
    rail_dot_count: int = Field(default=24, ge=8, le=64)
    rail_dot_lines: int = Field(default=1, ge=1, le=8)
    rail_dot_size_px: int = Field(default=6, ge=2, le=20)
    rail_dot_anim_mode: Literal["audio", "twinkle", "wave"] = "audio"
    rail_dot_color_near: str = "#FF60FF"
    rail_dot_color_far: str = "#00FFFF"


class RenderMode(str, Enum):
    """Supported render modes."""

    PUNCH = "punch"
    DANCE = "dance"
    LINE = "line"
    RELAX = "relax"
    COMBO = "combo"


class BaseRenderSettings(SideRailMixin, BaseModel):
    """Shared options used across all modes."""

    model_config = {"extra": "ignore"}

    mode_list: list[Literal["punch", "dance", "line", "relax"]] = Field(
        default_factory=lambda: ["punch"]
    )
    travel: int = -1
    speed: float = 0.8
    density: float = 0.5
    max_per_lane: int = 2
    lanes: list[int] = Field(default_factory=lambda: [1, 2, 3, 4])
    beat_source: BeatSource = BeatSource.ONSET
    bpm: Optional[float] = None
    beat_sens: float = 0.7
    beat_subdiv: int = 1
    # NOTE: int (not float) so it survives the trip to argparse type=int in
    # src/rhythm.py (--beat_min_gap 0.0 would otherwise raise ValueError).
    # Must match rhythm.py's argparse default (4) so a studio render with
    # default settings gives identical output to the same CLI command without
    # the flag.
    beat_min_gap: int = 4
    bloom: bool = True
    floor_panels: bool = True
    floor_panel_color: Optional[str] = None    # hex e.g. "#4af0c8"; None = default grey
    floor_panel_opacity: float = 1.0           # 0.0 (transparent) … 1.0 (solid)
    floor_panel_blink: bool = False            # tiles flash on every beat
    floor_panel_image: Optional[str] = None   # image file overlaid on tiles; None = draw shapes
    # When True AND ``floor_panel_image`` is set, the image is stretched to fill
    # the entire floor trapezoid as a single static graphic — chevrons, tiles,
    # blink, BG color and opacity are all skipped.  Ignored when no image.
    floor_full_static_image: bool = False
    # Floor layout override ('auto' = mode-dependent legacy behaviour;
    # 'chevron_strip' = single centre column of >>>-arrow shapes).
    floor_layout: str = "auto"
    # Solid background color for the runway trapezoid (drawn UNDER tiles/chevron).
    # None = transparent (canvas black / skybox shows through).
    floor_bg_color: Optional[str] = None          # hex e.g. "#5A1A8C"
    floor_bg_opacity: float = 1.0                 # 0.0 (transparent) … 1.0 (solid)
    background_type: Literal["solid", "image", "video"] = "solid"
    background_color: str = "#000000"
    background_image: Optional[str] = None
    background_video: Optional[str] = None
    # Chevron-specific — only active when floor_layout='chevron_strip'.
    chevron_color: str = "#FFD700"                 # arrow fill color (gold default)
    chevron_scroll: bool = True                    # scroll toward camera continuously
    chevron_blink: bool = False                    # flash on/off every 15 frames (~0.5s)
    chevron_width_frac: float = Field(default=0.45, ge=0.1, le=2.0)  # strip width as fraction of lane spread
    chevron_count: int = 6                         # number of arrows visible simultaneously
    stickman: bool = True
    # Camera perspective overrides (None = use per-mode default)
    floor_hit_frac: Optional[float] = None    # where floor meets near-camera edge (0.7-0.95)
    horizon_frac: Optional[float] = None      # vanishing point height fraction (0.3-0.60)
    floor_spread_frac: Optional[float] = None # near-end runway width fraction (0.3-0.85)
    far_spread_frac: Optional[float] = None   # far-end (horizon) spread, independent of near
    wall_floor_gap_frac: Optional[float] = None  # vertical gap between near wall bottom and floor (0-0.30)
    rail_chevron_depth: float = 1.0    # pointedness multiplier (1.0 = 120° opening angle)
    rail_chevron_density: int = 6      # number of chevrons per wall (2-20)
    start_gate_enabled: bool = False
    start_gate_type: Literal["color", "image", "video"] = "color"
    start_gate_color: str = "#1a1a1a"
    start_gate_border_color: str = "#ffffff"
    start_gate_border_thickness: float = Field(default=0.0, ge=0.0, le=10.0)
    start_gate_image: Optional[str] = None
    start_gate_video: Optional[str] = None
    start_gate_x: float = Field(default=0.30, ge=0.0, le=1.0)
    start_gate_y: float = Field(default=0.18, ge=0.0, le=1.0)
    start_gate_w: float = Field(default=0.40, ge=0.02, le=1.0)
    start_gate_h: float = Field(default=0.14, ge=0.03)
    viewport_panel_depth: Optional[float] = None  # None = use mode default (0.6 punch / tile-depth dance)


class PunchSettings(BaseRenderSettings):
    """Render settings for punch mode."""

    cube_radius: float = 0.154
    cube_image: Optional[str] = None
    cube_image_left: Optional[str] = None
    cube_image_right: Optional[str] = None
    cube_model: Optional[str] = None
    cube_model_left: Optional[str] = None
    cube_model_right: Optional[str] = None
    mesh_wireframe: bool = False
    cube_color_left: Optional[str] = None
    cube_color_right: Optional[str] = None
    punch_pair_cycle: int = 4


class DanceSettings(BaseRenderSettings):
    """Render settings for dance mode."""

    dance_pair_cycle: int = 4


class LineSettings(BaseRenderSettings):
    """Render settings for line mode."""

    line_beats: int = 2
    line_debug: bool = False
    line_zigzag: Optional[Literal["vertical", "horizontal"]] = None


class RelaxSettings(BaseRenderSettings):
    """Render settings for relax mode."""

    relax_interval: float = 0.0
    relax_travel_sec: float = Field(default=3.0, ge=0.5, le=10.0)
    relax_wait_sec: float = Field(default=0.0, ge=0.0, le=10.0)
    relax_texture_low: Optional[str] = None
    relax_texture_high: Optional[str] = None
    relax_texture_middle: Optional[str] = None
    relax_kind_ratio_middle: float = Field(default=0.33, ge=0.0, le=1.0)
    relax_show_low: bool = True
    relax_show_high: bool = True
    relax_show_middle: bool = True
    relax_countdown_enabled: bool = True
    relax_countdown_color: str = "#FFFFFF"
    relax_countdown_max_sec: float = 5.0
    relax_countdown_anim: Literal["pop", "flash", "fade_cross", "shake"] = "pop"
    relax_countdown_audio_enabled: bool = False
    relax_countdown_audio_mode: Literal["default", "file"] = "default"
    relax_countdown_audio_file: Optional[str] = None
    relax_countdown_audio_volume: float = Field(default=0.65, ge=0.0, le=1.0)
    relax_countdown_audio_last_mode: Literal["default", "file", "same"] = "default"
    relax_countdown_audio_last_file: Optional[str] = None
    relax_countdown_x: float = Field(default=0.88, ge=0.0, le=1.0)
    relax_countdown_y: float = Field(default=0.04, ge=0.0, le=1.0)
    relax_countdown_w: float = Field(default=0.10, ge=0.02, le=1.0)
    relax_countdown_h: float = Field(default=0.16, ge=0.02, le=1.0)
    relax_countdown_border_thickness: float = Field(default=2.0, ge=0.0, le=10.0)
    relax_countdown_glow_strength: float = Field(default=60.0, ge=0.0, le=100.0)
    relax_hole_mask_path: Optional[str] = None


class ComboSettings(PunchSettings, DanceSettings, LineSettings, RelaxSettings):
    """Settings used when mode_list contains multiple modes."""

    mode_list: list[Literal["punch", "dance", "line", "relax"]] = Field(
        default_factory=lambda: ["punch", "dance"]
    )


SETTINGS_BY_MODE: dict[str, type[BaseRenderSettings]] = {
    RenderMode.PUNCH.value: PunchSettings,
    RenderMode.DANCE.value: DanceSettings,
    RenderMode.LINE.value: LineSettings,
    RenderMode.RELAX.value: RelaxSettings,
    RenderMode.COMBO.value: ComboSettings,
}


def build_settings(mode: str, raw_dict: dict) -> BaseRenderSettings:
    """Build concrete settings model from raw dictionary."""
    model_type = SETTINGS_BY_MODE.get(mode, BaseRenderSettings)
    return model_type.model_validate(raw_dict or {})

