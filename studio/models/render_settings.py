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
    rail_pulse: RailPulse = RailPulse.BEAT
    rail_pulse_intensity: float = 0.6


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
    floor_panel_blink: bool = False            # tiles flash on every beat
    floor_panel_image: Optional[str] = None   # image file overlaid on tiles; None = draw shapes
    stickman: bool = True
    # Camera perspective overrides (None = use per-mode default)
    floor_hit_frac: Optional[float] = None    # where floor meets near-camera edge (0.7-0.95)
    horizon_frac: Optional[float] = None      # vanishing point height fraction (0.3-0.60)
    floor_spread_frac: Optional[float] = None # near-end runway width fraction (0.3-0.85)
    far_spread_frac: Optional[float] = None   # far-end (horizon) spread, independent of near


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

