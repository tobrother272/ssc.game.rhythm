"""Project models exposed for studio modules."""

from .layer import (
    Layer,
    LayerKind,
    auto_create_default_layers,
    migrate_render_settings_to_layers,
    resolve_segment_config,
)
from .media_item import MediaItem, MediaKind
from .project import Project
from .render_settings import (
    BaseRenderSettings,
    BeatSource,
    ComboSettings,
    DanceSettings,
    LineSettings,
    PunchSettings,
    RelaxSettings,
    RenderMode,
    build_settings,
)
from .segment import RenderStatus, Segment

__all__ = [
    "Layer",
    "LayerKind",
    "auto_create_default_layers",
    "migrate_render_settings_to_layers",
    "resolve_segment_config",
    "BaseRenderSettings",
    "BeatSource",
    "ComboSettings",
    "DanceSettings",
    "LineSettings",
    "MediaItem",
    "MediaKind",
    "Project",
    "PunchSettings",
    "RelaxSettings",
    "RenderMode",
    "RenderStatus",
    "Segment",
    "build_settings",
]
