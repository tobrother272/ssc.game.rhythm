"""Timeline segment model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from uuid import uuid4


class RenderStatus(str, Enum):
    """Lifecycle state of a segment render job."""

    IDLE = "idle"
    QUEUED = "queued"
    RENDERING = "rendering"
    DONE = "done"
    ERROR = "error"


@dataclass
class Segment:
    """Editable segment living on project timeline."""

    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = "Segment"
    start_time_sec: float = 0.0
    end_time_sec: float = 0.0
    audio_path: str = ""
    audio_offset_sec: float = 0.0
    audio_duration_sec: float = 0.0
    mode: str = "punch"
    render_settings: dict = field(default_factory=dict)
    # Absolute path to the rendered MP4 produced by the render service.
    # Lives in <app_root>/temps/ — globally named by segment id so it
    # survives across project moves.  Persisted in the .htproj file so
    # the segment knows where to find its render after re-opening.
    video_path: Optional[str] = None
    render_status: RenderStatus = RenderStatus.IDLE
    last_rendered_at: Optional[str] = None
    last_render_error: Optional[str] = None
    # Live render progress 0..100 (RUNTIME only — never persisted as the
    # value is only meaningful while a job is running).
    last_render_progress: int = 0
    thumbnail_path: Optional[str] = None

    @property
    def duration_sec(self) -> float:
        """Return segment duration in seconds."""
        return max(0.0, self.end_time_sec - self.start_time_sec)

