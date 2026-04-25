"""Audio waveform peak + RMS extraction service.

Computes two arrays per audio file and emits them to the UI:

1. ``peaks`` — list of ``(min, max)`` sample pairs at ``PEAKS_PER_SEC``
   buckets/sec. Kept for legacy compatibility (older project files).
2. ``rms``   — sequence of normalised ``[0..1]`` amplitudes derived via the
   **same** ``librosa.feature.rms`` recipe used by ``src/rhythm.py``
   (frame=1024 samples, hop=256 samples → ~10 ms tick at 44.1 kHz, then
   normalised by the 95-th percentile).  This is the array the timeline
   actually draws so the studio waveform looks identical to the
   ``BEAT DBG`` overlay rendered into the video.

Uses pydub + numpy for fast decode; runs on the Qt thread pool so the UI
stays responsive for long tracks.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from pydub import AudioSegment
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


# Peaks emitted as a flat list of (min, max) — kept only for legacy projects.
PeakPairs = List[Tuple[float, float]]
RmsArray  = List[float]


class _WaveformWorker(QRunnable):
    """Background worker that decodes audio and emits peaks + RMS."""

    def __init__(
        self,
        service: "WaveformService",
        audio_path: str,
        peaks_per_sec: int,
        rms_per_sec: int,
    ) -> None:
        super().__init__()
        self._service = service
        self._path = audio_path
        self._pps = peaks_per_sec
        self._rps = rms_per_sec

    def run(self) -> None:
        try:
            peaks, rms, duration_sec = _compute_waveform(
                self._path, self._pps, self._rps
            )
        except Exception as exc:  # noqa: BLE001 - surface any decode error to UI
            self._service.failed.emit(self._path, str(exc))
            return
        self._service.ready.emit(self._path, peaks, rms, duration_sec)


def _compute_rms_envelope(
    samples: np.ndarray,
    sr: int,
    rms_per_sec: int,
) -> np.ndarray:
    """Reproduce ``rhythm.py``'s LINE-DEBUG waveform 1:1.

    1. Compute RMS over **1024-sample** frames hopping every **256 samples**
       (the exact ``librosa.feature.rms(y, frame_length=1024, hop_length=256)``
       call used in ``rhythm.py``).
    2. Resample to ``rms_per_sec`` using nearest-neighbour lookup, identical
       to the per-video-frame resampler in the renderer.
    3. Normalise by the 95-th percentile and clip to ``[0, 1]``.
    """
    frame_length = 1024
    hop_length   = 256

    if samples.size == 0:
        return np.zeros(0, dtype=np.float32)

    # Pad short clips so we always get at least one full frame.
    if samples.size < frame_length:
        padded = np.zeros(frame_length, dtype=np.float32)
        padded[: samples.size] = samples
        samples = padded

    # Memory-efficient sliding-window RMS — equivalent to librosa.feature.rms.
    from numpy.lib.stride_tricks import sliding_window_view

    windows = sliding_window_view(samples, frame_length)[::hop_length]
    rms_native = np.sqrt(
        np.mean(windows.astype(np.float64) ** 2, axis=1)
    ).astype(np.float32)

    # Resample at fixed UI rate (rms_per_sec) using the same nearest-hop
    # lookup that rhythm.py uses to map video-frames -> rms hops.
    duration_sec = samples.size / float(sr)
    total_ticks  = max(1, int(round(duration_sec * rms_per_sec)))
    sec_per_hop  = hop_length / float(sr)
    out = np.zeros(total_ticks, dtype=np.float32)
    for f in range(total_ticks):
        t_f = f / float(rms_per_sec)
        ri = min(int(round(t_f / sec_per_hop)), rms_native.size - 1)
        ri = max(0, ri)
        out[f] = float(rms_native[ri])

    p95 = float(np.percentile(out, 95)) if out.size else 0.0
    if p95 > 1e-8:
        out = np.clip(out / p95, 0.0, 1.0)
    return out


def _compute_waveform(
    path: str,
    peaks_per_sec: int,
    rms_per_sec: int,
) -> Tuple[PeakPairs, RmsArray, float]:
    """Decode audio at ``path`` and return ``(peaks, rms, duration_sec)``.

    ``peaks`` retained for legacy serialisation; ``rms`` is what the UI
    actually draws (matches ``rhythm.py``'s LINE-DEBUG overlay).
    """
    if not Path(path).exists():
        raise FileNotFoundError(path)

    audio = AudioSegment.from_file(path)
    if audio.frame_count() <= 0:
        return [], [], 0.0

    samples = np.asarray(audio.get_array_of_samples(), dtype=np.float32)
    if audio.channels == 2 and samples.size % 2 == 0:
        samples = samples.reshape(-1, 2).mean(axis=1)

    max_val = float(1 << (8 * audio.sample_width - 1))
    if max_val > 0:
        samples = samples / max_val

    sr = int(audio.frame_rate)
    duration_sec = samples.size / float(sr)

    # ── Legacy peak buckets (kept for backward-compat with old projects). ──
    total_buckets = max(1, int(duration_sec * peaks_per_sec))
    bucket_size   = max(1, samples.size // total_buckets)
    usable = total_buckets * bucket_size
    trimmed = samples[:usable].reshape(total_buckets, bucket_size)
    mins = trimmed.min(axis=1).tolist()
    maxs = trimmed.max(axis=1).tolist()
    peaks: PeakPairs = list(zip(mins, maxs))

    # ── RMS envelope (the array we actually render). ─────────────────────
    rms = _compute_rms_envelope(samples, sr, rms_per_sec).tolist()

    return peaks, rms, duration_sec


class WaveformService(QObject):
    """Async waveform extractor with a tiny in-memory cache."""

    # audio_path, peaks, rms, duration_sec
    ready  = Signal(str, object, object, float)
    failed = Signal(str, str)

    # Legacy peaks resolution (kept for old projects).
    PEAKS_PER_SEC = 100
    # RMS resolution actually drawn — finer = sharper kicks visible.
    RMS_PER_SEC   = 100

    def __init__(self) -> None:
        super().__init__()
        self._pool = QThreadPool.globalInstance()
        self._cache: dict[str, tuple[PeakPairs, RmsArray, float]] = {}
        self._last_requested: Optional[str] = None

    def request(self, audio_path: str) -> None:
        """Queue a waveform computation (or emit cached result immediately)."""
        if not audio_path:
            return
        self._last_requested = audio_path
        cached = self._cache.get(audio_path)
        if cached is not None:
            peaks, rms, duration_sec = cached
            self.ready.emit(audio_path, peaks, rms, duration_sec)
            return
        self._pool.start(
            _WaveformWorker(
                self, audio_path, self.PEAKS_PER_SEC, self.RMS_PER_SEC
            )
        )

    def _store(
        self,
        audio_path: str,
        peaks: PeakPairs,
        rms: RmsArray,
        duration_sec: float,
    ) -> None:
        self._cache[audio_path] = (peaks, rms, duration_sec)

    def connect_cache(self) -> None:
        """Wire internal cache population once after construction."""
        self.ready.connect(self._store)
