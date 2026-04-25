"""Audio waveform peak extraction service.

Computes downsampled (min, max) peak pairs per time-slice for an audio file and
emits them back to the UI so the timeline can draw a waveform preview.

Uses pydub + numpy for fast decode/downsampling; runs on the Qt thread pool so
the UI stays responsive for long tracks.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from pydub import AudioSegment
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


# Peaks emitted as a flat numpy array with shape (N, 2) == list of (min, max).
# Using (min, max) so we can draw a symmetric bar range per pixel column.
PeakPairs = List[Tuple[float, float]]


class _WaveformWorker(QRunnable):
    """Background worker that decodes audio and emits peaks."""

    def __init__(
        self,
        service: "WaveformService",
        audio_path: str,
        peaks_per_sec: int,
    ) -> None:
        super().__init__()
        self._service = service
        self._path = audio_path
        self._pps = peaks_per_sec

    def run(self) -> None:
        try:
            peaks, duration_sec = _compute_peaks(self._path, self._pps)
        except Exception as exc:  # noqa: BLE001 - surface any decode error to UI
            self._service.failed.emit(self._path, str(exc))
            return
        self._service.ready.emit(self._path, peaks, duration_sec)


def _compute_peaks(path: str, peaks_per_sec: int) -> Tuple[PeakPairs, float]:
    """Decode audio at `path` and return (peaks, duration_sec).

    Peaks is a list of (min, max) floats in range [-1.0, 1.0], one pair per
    bucket of `1/peaks_per_sec` seconds.
    """
    if not Path(path).exists():
        raise FileNotFoundError(path)

    audio = AudioSegment.from_file(path)
    if audio.frame_count() <= 0:
        return [], 0.0

    samples = np.asarray(audio.get_array_of_samples(), dtype=np.float32)
    if audio.channels == 2 and samples.size % 2 == 0:
        # Downmix stereo to mono by averaging interleaved samples.
        samples = samples.reshape(-1, 2).mean(axis=1)

    # Normalize to [-1, 1] using full int range of the sample_width.
    max_val = float(1 << (8 * audio.sample_width - 1))
    if max_val > 0:
        samples = samples / max_val

    duration_sec = len(samples) / float(audio.frame_rate)
    total_buckets = max(1, int(duration_sec * peaks_per_sec))
    bucket_size = max(1, len(samples) // total_buckets)
    usable = total_buckets * bucket_size
    trimmed = samples[:usable].reshape(total_buckets, bucket_size)
    mins = trimmed.min(axis=1).tolist()
    maxs = trimmed.max(axis=1).tolist()
    peaks: PeakPairs = list(zip(mins, maxs))
    return peaks, duration_sec


class WaveformService(QObject):
    """Async waveform peak extractor with a tiny in-memory cache."""

    ready = Signal(str, object, float)  # audio_path, peaks, duration_sec
    failed = Signal(str, str)  # audio_path, error

    # Resolution of the cached peak array — 100 buckets/sec is enough for most
    # timeline zoom levels (up to ~100 px/sec). Higher zoom just stretches.
    PEAKS_PER_SEC = 100

    def __init__(self) -> None:
        super().__init__()
        self._pool = QThreadPool.globalInstance()
        self._cache: dict[str, tuple[PeakPairs, float]] = {}
        self._last_requested: Optional[str] = None

    def request(self, audio_path: str) -> None:
        """Queue a peak computation (or emit cached result immediately)."""
        if not audio_path:
            return
        self._last_requested = audio_path
        cached = self._cache.get(audio_path)
        if cached is not None:
            peaks, duration_sec = cached
            self.ready.emit(audio_path, peaks, duration_sec)
            return
        self._pool.start(
            _WaveformWorker(self, audio_path, self.PEAKS_PER_SEC)
        )

    def _store(self, audio_path: str, peaks: PeakPairs, duration_sec: float) -> None:
        self._cache[audio_path] = (peaks, duration_sec)

    # The worker emits directly on our signals, but we also want to populate
    # the cache. We listen to our own signal for that.
    def connect_cache(self) -> None:
        """Wire internal cache population once after construction."""
        self.ready.connect(self._store)
