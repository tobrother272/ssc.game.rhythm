"""Background service that trims a segment's audio using FFmpeg stream-copy.

FFmpeg ``-c copy`` cuts the audio at the container level — no decoding,
no re-encoding, no quality change.  The output file keeps the original
codec and container (MP3 → MP3, AAC → AAC, FLAC → FLAC, …).

The trimmed file is saved to ``<project_dir>/temps/audio_<segment_id><ext>``
and used directly as the ``-i`` input when rendering, so rhythm.py always
works with the exact same audio data the user hears in any media player.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from src.bundle_paths import find_ffmpeg as _find_ffmpeg


def trim_audio_ffmpeg(
    audio_path: str,
    start_sec: float,
    end_sec: float,
    out_path: Path,
) -> None:
    """Cut *audio_path*[start_sec:end_sec] into *out_path* using FFmpeg.

    Uses ``-c copy`` (stream copy) — no decoding, no re-encoding.
    Output keeps the same codec/container as the source.
    ``out_path`` must have the **same extension** as *audio_path*
    (the caller is responsible for this).

    Raises
    ------
    subprocess.CalledProcessError
        If ffmpeg exits with a non-zero return code.
    FileNotFoundError
        If ffmpeg is not found on the system.
    """
    ffmpeg = _find_ffmpeg()
    duration_sec = max(0.001, end_sec - start_sec)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temporary file first, then atomically replace the
    # destination. This prevents readers (QMediaPlayer/ffmpeg probe) from
    # seeing a half-written MP3 while a trim job is in progress.
    tmp_out = out_path.with_name(
        f"{out_path.stem}.tmp-{uuid4().hex}{out_path.suffix}"
    )
    cmd = [
        ffmpeg,
        "-y",                          # overwrite without asking
        "-ss", f"{start_sec:.6f}",     # seek before input (fast)
        "-t",  f"{duration_sec:.6f}",  # duration (not end point)
        "-i",  audio_path,
        "-c",  "copy",                 # stream copy — no re-encode
        str(tmp_out),
    ]
    _creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creation_flags,
    )
    if result.returncode != 0:
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(
            f"ffmpeg trim failed (rc={result.returncode}):\n{result.stdout[-800:]}"
        )
    try:
        tmp_out.replace(out_path)
    except Exception:
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        raise


class _TrimWorker(QRunnable):
    def __init__(
        self,
        service: "AudioTrimService",
        segment_id: str,
        audio_path: str,
        start_sec: float,
        end_sec: float,
        out_path: Path,
    ) -> None:
        super().__init__()
        self._service = service
        self._segment_id = segment_id
        self._audio_path = audio_path
        self._start_sec = start_sec
        self._end_sec = end_sec
        self._out_path = out_path

    def run(self) -> None:
        try:
            trim_audio_ffmpeg(
                self._audio_path,
                self._start_sec,
                self._end_sec,
                self._out_path,
            )
        except Exception as exc:  # noqa: BLE001
            self._service.failed.emit(self._segment_id, str(exc))
            return
        self._service.ready.emit(self._segment_id, str(self._out_path))


class AudioTrimService(QObject):
    """Queue FFmpeg stream-copy trim jobs onto the Qt thread pool.

    Signals
    -------
    ready(segment_id, trimmed_audio_path)
        Emitted when the trimmed file has been written successfully.
    failed(segment_id, error_message)
        Emitted when trimming fails (ffmpeg not found, bad window, etc.).
    """

    ready  = Signal(str, str)   # segment_id, output_path
    failed = Signal(str, str)   # segment_id, error_message

    def trim(
        self,
        segment_id: str,
        audio_path: str,
        start_sec: float,
        end_sec: float,
        out_path: Path,
    ) -> None:
        """Schedule a background FFmpeg trim job.

        *out_path* should keep the same extension as *audio_path* so the
        container/codec stays identical (MP3 in → MP3 out, etc.).
        """
        if not audio_path or not Path(audio_path).exists():
            self.failed.emit(segment_id, f"Audio file not found: {audio_path}")
            return
        if end_sec <= start_sec:
            self.failed.emit(
                segment_id,
                f"Invalid trim window: start={start_sec:.3f}s end={end_sec:.3f}s",
            )
            return
        worker = _TrimWorker(self, segment_id, audio_path, start_sec, end_sec, out_path)
        QThreadPool.globalInstance().start(worker)
