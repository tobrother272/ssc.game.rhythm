"""Media item model for imported assets."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from uuid import uuid4


class MediaKind(str, Enum):
    """Supported media types in the library."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


@dataclass
class MediaItem:
    """Represents one imported media resource."""

    id: str = field(default_factory=lambda: str(uuid4()))
    kind: MediaKind = MediaKind.VIDEO
    source_path: str = ""
    display_name: str = ""
    duration_sec: Optional[float] = None
    thumbnail_path: Optional[str] = None
    imported_at: str = ""
    # Cached waveform peaks — list of [min, max] pairs, 100 buckets/sec.
    # Stored as list-of-lists for JSON serialisation (not tuple, which is
    # not directly JSON-serialisable).
    waveform_peaks: list = field(default_factory=list)
    waveform_peaks_per_sec: int = 100
    waveform_duration_sec: float = 0.0

