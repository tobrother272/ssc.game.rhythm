"""Beat-detection preview service.

Runs ``src.rhythm`` with ``--detect_only`` so the **same** beat detection
+ scheduler that produces the rendered video is used to preview where
blocks will appear in the timeline UI.  No video is rendered — the
rhythm script writes a tiny JSON of events and exits.

Output (per request):

* ``ready(segment_id, events)`` — list of ``(time_sec, kind)`` tuples,
  times are in **segment-local seconds** (i.e. relative to the trimmed
  audio's t=0; the timeline overlay adds the segment's start offset).
* ``failed(segment_id, message)``.

Runs on the Qt thread pool so the UI stays responsive.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from src.bundle_paths import get_rhythm_command as _get_rhythm_command


@dataclass
class BeatDetectJob:
    """All inputs needed to invoke ``rhythm.py --detect_only``.

    Mirrors the fields of :class:`RenderJob` that affect beat detection.
    Anything that does NOT affect detection (resolution, bloom, stickman,
    etc.) is intentionally omitted so cache keys stay stable across
    cosmetic edits.
    """
    segment_id: str
    audio_path: str
    duration_sec: float
    mode: str = "punch"
    beat_source: str = "onset"
    beat_sens: float = 0.7
    beat_min_gap: int = 4
    density: float = 0.5
    speed: float = 0.8
    fps: int = 30
    # Audio-amplitude floor (0..1) — passed to ``rhythm.py`` as
    # ``--beat_height_threshold`` so the rhythm core returns a
    # pre-filtered events list. Mirrors
    # :pyattr:`Segment.beat_height_threshold` and is what powers the
    # studio's red threshold slider over the waveform.
    beat_height_threshold: float = 0.0
    extra_args: List[str] = field(default_factory=list)

    def cache_key(self) -> tuple:
        """A hashable tuple uniquely identifying the detection inputs.

        We include the audio file's ``(st_size, st_mtime_ns)`` so a re-
        trim that overwrites the same path with NEW content invalidates
        the cache automatically.  Without this, the studio kept showing
        stale events after a segment drag/edit because ``audio_path``
        alone (always ``temps/audio_<segment_id>.mp3``) didn't change.
        Missing/inaccessible file falls back to a sentinel so callers
        still get a deterministic key (the worker will surface the
        actual "file not found" error).
        """
        try:
            st = Path(self.audio_path).stat()
            file_sig = (int(st.st_size), int(st.st_mtime_ns))
        except OSError:
            file_sig = (-1, -1)
        return (
            self.audio_path,
            file_sig,
            round(self.duration_sec, 3),
            self.mode,
            self.beat_source,
            round(self.beat_sens, 4),
            int(self.beat_min_gap),
            round(self.density, 4),
            round(self.speed, 4),
            int(self.fps),
            round(self.beat_height_threshold, 4),
            tuple(self.extra_args),
        )


def _build_command(job: BeatDetectJob, repo_root: Path, events_path: Path,
                   dummy_out: Path) -> list[str]:
    """Build the argv for the detection-only rhythm invocation."""
    cmd: list[str] = _get_rhythm_command(repo_root) + [
        "-i", job.audio_path,
        "-o", str(dummy_out),
        "--audio", "0",                # don't try to mux audio
        "--detect_only",
        "--export_events", str(events_path),
        "--fps", str(int(job.fps)),
        "--mode", job.mode,
        "--beat_source", job.beat_source,
        "--beat_sens", str(job.beat_sens),
        "--beat_min_gap", str(int(job.beat_min_gap)),
        "--density", str(job.density),
        "--speed", str(job.speed),
        "--beat_height_threshold", str(float(job.beat_height_threshold)),
        "-d", str(int(round(job.duration_sec))),
    ]
    cmd.extend(job.extra_args)
    return cmd


class _BeatDetectWorker(QRunnable):
    """Run the detection subprocess and emit results on completion."""

    def __init__(self, service: "BeatDetectService", job: BeatDetectJob,
                 repo_root: Path) -> None:
        super().__init__()
        self._service = service
        self._job = job
        self._repo_root = repo_root

    def run(self) -> None:
        job = self._job
        if not job.audio_path or not Path(job.audio_path).exists():
            self._service._on_failed(job, "audio file not found")
            return
        if job.duration_sec <= 0.05:
            self._service._on_failed(job, "duration must be > 0")
            return

        with tempfile.TemporaryDirectory(prefix="beatdetect_") as td:
            td_path = Path(td)
            events_path = td_path / "events.json"
            dummy_out = td_path / "dummy.mp4"
            cmd = _build_command(job, self._repo_root, events_path, dummy_out)
            print("[detect-only]", " ".join(f'"{c}"' if " " in c else c
                                             for c in cmd))
            # Force UTF-8 for the child Python's stdout/stderr so unicode
            # chars in rhythm.py prints (✔, →, …) don't blow up on the
            # Windows default cp1252 codec.
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["PYTHONUTF8"]       = "1"

            try:
                proc = subprocess.run(
                    cmd, cwd=str(self._repo_root), capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=120, env=child_env,
                )
            except subprocess.TimeoutExpired:
                self._service._on_failed(job, "detection timed out")
                return
            except Exception as exc:  # noqa: BLE001
                self._service._on_failed(job, f"spawn error: {exc}")
                return

            if proc.returncode != 0:
                output = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
                lines = [l.strip() for l in output.splitlines() if l.strip()]
                # Try to find the most informative error line instead of
                # blindly taking the last line (which is often a librosa /
                # numpy ``warnings.warn(`` call that fires on process startup
                # and is completely unrelated to the actual failure).
                _ERROR_PREFIXES = (
                    "ValueError", "RuntimeError", "TypeError",
                    "FileNotFoundError", "ImportError",
                    "[--mode]", "[--lanes]", "[color]", "Error:", "error:",
                    "Traceback",
                )
                msg = ""
                for line in reversed(lines):
                    if any(line.startswith(p) or p in line for p in _ERROR_PREFIXES):
                        msg = line
                        break
                if not msg:
                    # No recognisable error line — fall back to last non-
                    # warning, non-whitespace line so we at least show
                    # something useful.
                    _NOISE = ("warnings.warn", "UserWarning", "FutureWarning",
                              "DeprecationWarning", "site-packages")
                    for line in reversed(lines):
                        if not any(n in line for n in _NOISE):
                            msg = line
                            break
                if not msg:
                    msg = f"exit {proc.returncode}"
                self._service._on_failed(job, msg)
                return

            if not events_path.exists():
                self._service._on_failed(
                    job, "events JSON not written by rhythm.py"
                )
                return

            try:
                payload = json.loads(events_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                self._service._on_failed(job, f"events JSON parse: {exc}")
                return

            events: list[tuple[float, str, float]] = []
            raw_rows = payload.get("events", []) or []
            heights = (
                payload.get("meta", {}).get("event_heights") or []
            )
            for i, row in enumerate(raw_rows):
                if len(row) >= 2:
                    try:
                        h = float(heights[i]) if i < len(heights) else 1.0
                        h = max(0.0, min(1.0, h))
                        events.append(
                            (float(row[0]), str(row[1]), h)
                        )
                    except (TypeError, ValueError):
                        pass
            self._service._on_ready(job, events)


class BeatDetectService(QObject):
    """Async wrapper around ``rhythm.py --detect_only``."""

    # segment_id, events list of (time_sec, kind, height_0_1)
    ready  = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, project_root: str) -> None:
        super().__init__()
        self._pool = QThreadPool.globalInstance()
        # Cache keyed on detection inputs — avoids re-spawning rhythm.py
        # when the user just toggles UI fields that don't influence beats.
        self._cache: dict[tuple, list[tuple[float, str]]] = {}
        # Workspace root containing the ``src/`` package — used as cwd for
        # the subprocess and as the base for resolving ``src/rhythm.py``.
        self._repo_root = Path(project_root)
        # Track the latest job per segment so stale results from
        # super-seded requests are dropped.
        self._latest_job: dict[str, tuple] = {}

    def detect(self, job: BeatDetectJob) -> None:
        """Schedule a detection run, or short-circuit on a cache hit."""
        key = job.cache_key()
        self._latest_job[job.segment_id] = key
        cached = self._cache.get(key)
        if cached is not None:
            self.ready.emit(job.segment_id, list(cached))
            return
        self._pool.start(_BeatDetectWorker(self, job, self._repo_root))

    def clear_cache(self) -> None:
        self._cache.clear()

    # ── Internal callbacks (emitted from worker thread) ─────────────────
    def _on_ready(self, job: BeatDetectJob,
                  events: list[tuple[float, str]]) -> None:
        key = job.cache_key()
        # Drop stale results so only the most-recent request reaches the UI.
        if self._latest_job.get(job.segment_id) != key:
            return
        self._cache[key] = list(events)
        self.ready.emit(job.segment_id, events)

    def _on_failed(self, job: BeatDetectJob, message: str) -> None:
        if self._latest_job.get(job.segment_id) != job.cache_key():
            return
        self.failed.emit(job.segment_id, message)
