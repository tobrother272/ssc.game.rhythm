"""Project aggregate model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from .layer import Layer, LayerKind
from .media_item import MediaItem
from .segment import Segment


def utc_now_iso() -> str:
    """Get current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Project:
    """Root project state for the entire editor."""

    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = "Untitled Project"
    project_dir: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    main_audio_path: Optional[str] = None
    output_width: int = 1920
    output_height: int = 1080
    output_fps: int = 30
    media_items: list[MediaItem] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    layers: list[Layer] = field(default_factory=list)

    def sorted_segments(self) -> list[Segment]:
        """Return segments sorted by start time."""
        return sorted(self.segments, key=lambda item: item.start_time_sec)

    def get_media(self, media_id: str) -> Optional[MediaItem]:
        """Find media item by id."""
        return next((item for item in self.media_items if item.id == media_id), None)

    def get_segment(self, segment_id: str) -> Optional[Segment]:
        """Find segment by id."""
        return next((item for item in self.segments if item.id == segment_id), None)

    def get_layer(self, layer_id: str) -> Optional[Layer]:
        """Find layer by id."""
        return next((la for la in self.layers if la.id == layer_id), None)

    def layers_by_kind(self, kind: LayerKind) -> list[Layer]:
        """Return all layers of given kind."""
        return [la for la in self.layers if la.kind == kind]

    def layers_overlapping(
        self, kind: LayerKind, start: float, end: float
    ) -> list[Layer]:
        """Return layers of given kind overlapping [start, end), sorted by z_index DESC."""
        hits = [la for la in self.layers_by_kind(kind) if la.overlaps(start, end)]
        return sorted(hits, key=lambda la: -la.z_index)

