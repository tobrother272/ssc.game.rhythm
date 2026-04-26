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
    # Absolute path to the pre-trimmed WAV produced by AudioTrimService.
    # Lives in <project_dir>/temps/audio_<id>.wav so it's co-located with
    # the project and can be inspected / played back externally.  When this
    # file exists, RenderService uses it directly as the `-i` input (no
    # pydub re-trim at render time).  Persisted in the .htproj file.
    trimmed_audio_path: Optional[str] = None
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
    # Last successful Auto-Gen-Block detection result for this segment,
    # stored as ``[(t_sec, kind), …]`` in segment-local time (t=0 is the
    # start of the trimmed audio).  Persisted to the .htproj file so the
    # ticks shown on the timeline strip survive a project close/re-open
    # — no need to re-run rhythm.py --detect_only just to inspect the
    # block layout.  Cleared on demand when the user re-runs Auto Gen
    # (overwritten with the fresh result) or never if detection fails
    # (we keep the last good list so a transient ffmpeg blip doesn't
    # wipe their preview).
    beat_events: list = field(default_factory=list)
    # Position + size of the stickman draw-box, expressed as fractions
    # of the rendered video frame (0..1) so the value is resolution-
    # independent and survives changes to project output_width /
    # output_height.  Defaults match StickmanHUD's left-column HUD
    # (``x=W*1%, y=H*9%, w=W*13.5%, h=H*54%``) which is what
    # rhythm.py uses when no ``--stick_*`` CLI flag is provided.
    # When the user drags / resizes the overlay on the Player panel,
    # the new fractions are saved here and forwarded to rhythm.py as
    # ``--stick_x0/y0/w/h <pixel>`` at render time
    # (pixels = fraction × output_width / output_height).
    stickman_location: dict = field(default_factory=lambda: {
        "x": 0.010,
        "y": 0.090,
        "w": 0.135,
        "h": 0.540,
    })

    @property
    def duration_sec(self) -> float:
        """Return segment duration in seconds."""
        return max(0.0, self.end_time_sec - self.start_time_sec)

