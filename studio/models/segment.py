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
    audio_offset_sec: Optional[float] = None   # None → legacy, use start_time_sec
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
    # Audio-amplitude threshold (0..1) for the timeline waveform's
    # red threshold line.  Beats whose ``height`` (the per-event audio
    # amplitude exported by ``rhythm.py --detect_only``) is below this
    # value are visually muted in the timeline AND excluded when the
    # segment is rendered (``render_service`` filters ``beat_events``
    # against this threshold before it builds ``--beat_times``).  A
    # value of 0.0 (default) keeps every detected beat — i.e. behaves
    # identically to the legacy "no-threshold" pipeline.  Persisted so
    # the user's tuning survives a project close/re-open.
    beat_height_threshold: float = 0.0
    # Minimum spacing (seconds) between two beat sticks emitted by the
    # *Gen by Chart* button.  When > 0, the panel's peak-detection pass
    # walks the chart-local maxima left-to-right and collapses any
    # cluster of peaks closer than this gap into a single stick at the
    # *highest-amplitude* peak of the cluster.  ``0.0`` disables merging
    # entirely — every detected peak emits its own stick.  Auto Gen
    # Block is unaffected (it has its own lane-spacing logic inside
    # rhythm.py).  Persisted so each segment can be tuned independently
    # and the value survives a project reopen.
    #
    # Default ``0.15 s`` (≈ 4 frames @ 30 fps) was chosen empirically
    # so a single drum hit no longer emits the 5–6-tick cluster the
    # user observed on real audio while still preserving fast rolls
    # and double hits.  Existing projects without this field saved on
    # disk fall back to ``0.0`` via :func:`project_store.load` so we
    # don't silently mutate already-tuned beat sticks.
    min_beat_spacing_sec: float = 0.15
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

