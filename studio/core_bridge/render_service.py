"""Queued rendering service that wraps src.rhythm in subprocess."""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from studio.models import Segment, build_settings


@dataclass
class RenderJob:
    """A queued segment render request."""

    segment_id: str
    mode: str
    audio_path: str
    output_path: str
    render_settings: dict = field(default_factory=dict)
    # Optional time-window: if start_time_sec > 0 the audio is trimmed with
    # pydub before being passed to src.rhythm so the render covers exactly
    # [start_time_sec, start_time_sec + duration_sec].
    start_time_sec: float = 0.0
    duration_sec: Optional[float] = None
    # Mark a job as a preview so the caller can auto-play on completion.
    is_preview: bool = False
    # Output resolution & frame-rate — forwarded directly to rhythm.py via
    # -W / -H / --fps.  If None, rhythm.py uses its own defaults (1920×1080
    # @ 30 fps), which is rarely what the user configured in Project Settings.
    output_width: Optional[int] = None
    output_height: Optional[int] = None
    output_fps: Optional[int] = None


class RenderService(QObject):
    """Serial render queue for segment jobs."""

    progress = Signal(str, int)  # segment_id, 0..100
    finished = Signal(str, str)  # segment_id, output_path
    failed = Signal(str, str)    # segment_id, error_message

    def __init__(
        self,
        repo_root: Path,
        token_provider: Optional[Callable[[], Optional[str]]] = None,
        url_provider: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        super().__init__()
        self._repo_root = repo_root
        # Callables resolved at job-run time so token rotation / re-login is
        # picked up automatically.  Either may be None — the subprocess will
        # then receive empty/missing values and src.rhythm's authourize_user
        # currently short-circuits to True regardless.
        self._token_provider = token_provider
        self._url_provider = url_provider
        self._queue: queue.Queue[RenderJob] = queue.Queue()
        self._cancelled: set[str] = set()
        self._worker = threading.Thread(target=self._run_loop, daemon=True)
        self._worker.start()

    def enqueue(self, job: RenderJob) -> None:
        """Put a render job in queue."""
        self._queue.put(job)

    def cancel(self, segment_id: str) -> None:
        """Mark queued/running segment as cancelled."""
        self._cancelled.add(segment_id)

    def build_job(
        self,
        segment: Segment,
        output_path: Path,
        *,
        output_width: Optional[int] = None,
        output_height: Optional[int] = None,
        output_fps: Optional[int] = None,
        is_preview: bool = False,
    ) -> RenderJob:
        """Build render job object from segment state."""
        dur = segment.duration_sec if segment.duration_sec and segment.duration_sec > 0 else None
        return RenderJob(
            segment_id=segment.id,
            mode=segment.mode,
            audio_path=segment.audio_path,
            output_path=str(output_path),
            render_settings=segment.render_settings or {},
            start_time_sec=segment.start_time_sec,
            duration_sec=dur,
            is_preview=is_preview,
            output_width=output_width,
            output_height=output_height,
            output_fps=output_fps,
        )

    def _run_loop(self) -> None:
        while True:
            job = self._queue.get()
            if job.segment_id in self._cancelled:
                self._cancelled.discard(job.segment_id)
                self._queue.task_done()
                continue
            try:
                self.progress.emit(job.segment_id, 1)
                self._run_job(job)
                self.progress.emit(job.segment_id, 100)
                self.finished.emit(job.segment_id, job.output_path)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(job.segment_id, str(exc))
            finally:
                self._queue.task_done()

    def _run_job(self, job: RenderJob) -> None:
        settings = build_settings(job.mode, job.render_settings).model_dump(
            mode="json", exclude_none=True
        )

        # `mode_list` is the multi-mode spec for combo renders.  In every
        # other case we MUST pass `job.mode` directly to --mode, otherwise
        # the BaseRenderSettings default (`mode_list=["punch"]`) leaks
        # through and turns a "line"/"dance"/"relax" segment into punch.
        if job.mode == "combo":
            mode_list = settings.get("mode_list") or [job.mode]
            if isinstance(mode_list, list) and mode_list:
                mode_arg = ",".join(str(m) for m in mode_list)
            else:
                mode_arg = str(mode_list) or "punch"
        else:
            mode_arg = job.mode

        # --- Audio trimming ------------------------------------------------
        # When the job covers only part of the audio (start_time_sec > 0 or
        # duration_sec is given) we trim the source with pydub first so
        # src.rhythm always sees a file that starts at t=0 and is exactly the
        # right length.  The temp file is cleaned up in the finally block.
        audio_path = job.audio_path
        temp_audio: Optional[str] = None
        try:
            needs_trim = (job.start_time_sec > 0.01) or (
                job.duration_sec is not None and job.duration_sec > 0
            )
            if needs_trim:
                from pydub import AudioSegment as _PydubAudio  # lazy import

                raw = _PydubAudio.from_file(audio_path)
                start_ms = int(job.start_time_sec * 1000)
                if job.duration_sec is not None:
                    end_ms = start_ms + int(job.duration_sec * 1000)
                else:
                    end_ms = len(raw)
                trimmed = raw[start_ms:end_ms]
                # Export as WAV (lossless PCM) so the beat/onset detector
                # in rhythm.py receives pristine audio data.  Re-encoding to
                # the lossy source format (e.g. MP3→MP3) applies a second
                # lossy-compression pass that smears the transients and
                # spectral peaks the detector relies on, causing missed or
                # mis-timed beats.  WAV is always decodable by rhythm.py and
                # avoids any quality loss while only being a few MB larger
                # for typical segment durations.
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp.close()
                trimmed.export(tmp.name, format="wav")
                audio_path = tmp.name
                temp_audio = tmp.name
                print(
                    f"[RenderService] Trimmed audio → WAV temp: {tmp.name} "
                    f"({start_ms}ms – {end_ms}ms)",
                    flush=True,
                )

            # --- Build subprocess command -----------------------------------
            # src.rhythm expects:
            #   -i / --input   <audio/video file>   (required)
            #   -o / --output  <output .mp4 path>   (required)
            #   -a / --audio   0 | 1                (mux source audio into output)
            #   --mode         <mode_arg>
            #   -d / --duration <seconds>           (optional cap)
            #   + all remaining render_settings flags
            # NOTE: invoke as a script ("python <path>/src/rhythm.py") rather
            # than as a module ("python -m src.rhythm") because rhythm.py does
            # `from authorization import authourize_user` (and `from stickman
            # import ...`), and those siblings live in src/.  Running as a
            # script puts src/ on sys.path automatically; -m would only put
            # the repo root on sys.path and the imports would fail with
            # "ModuleNotFoundError: No module named 'authorization'".
            rhythm_script = self._repo_root / "src" / "rhythm.py"
            command = [
                sys.executable, str(rhythm_script),
                "-i", audio_path,
                "-o", job.output_path,
                "--audio", "1",   # always mux audio so preview is watchable
                "--mode", mode_arg,
            ]
            # Pass project resolution & fps so rhythm.py renders at the exact
            # same dimensions the user configured in Project Settings — not at
            # the hard-coded 1920×1080 @ 30fps defaults in rhythm.py which
            # would differ from a CLI run that specifies -W -H --fps.
            if job.output_width:
                command.extend(["-W", str(job.output_width)])
            if job.output_height:
                command.extend(["-H", str(job.output_height)])
            if job.output_fps:
                command.extend(["--fps", str(job.output_fps)])

            # When we've already trimmed, src.rhythm sees the file from t=0,
            # so --duration == trimmed file length (redundant but safe).
            # When no trimming was needed but duration_sec is given, cap here.
            if not needs_trim and job.duration_sec is not None:
                command.extend(["-d", str(job.duration_sec)])

            # src.rhythm requires --token (otherwise it sys.exit(1) at startup).
            # Even though authourize_user() short-circuits to True, the
            # `if args.token:` gate must still see a non-empty value.
            token = self._token_provider() if self._token_provider else None
            if token:
                command.extend(["--token", token])
            else:
                # Fallback: any non-empty placeholder still satisfies the
                # gate.  We log a warning so the user knows they're not
                # authenticated and the network-side auth check (currently
                # disabled in authorization.py) would fail if re-enabled.
                command.extend(["--token", "studio_local"])
                print(
                    "[RenderService] Warning: no auth token available; passing "
                    "placeholder. Re-authenticate if rendering uploads to "
                    "the backend.",
                    file=sys.stderr, flush=True,
                )
            url = self._url_provider() if self._url_provider else None
            if url:
                command.extend(["--url", url])

            command.extend(self._settings_to_args(settings))

            # ----------------------------------------------------------------
            # Fast-preview overrides — applied ONLY when this is a Preview
            # render (is_preview=True).  We deliberately keep the gameplay
            # parameters (mode, beats, density, speed, lanes, …) identical
            # to a full render so the user is judging the SAME configuration
            # they will eventually export — only pixel cost is reduced.
            #
            #   * 960x540  (¼ pixel area of 1920x1080)
            #   * 24 fps   (20% fewer frames than 30)
            #   * bloom 0  (skips the most expensive post-process pass)
            #
            # In practice this turns a ~60s full render into ~12-18s.
            # IMPORTANT: appended AFTER _settings_to_args so argparse, which
            # takes the last value for repeated flags, sees these overrides
            # rather than the user's --bloom 1 from render_settings.
            # ----------------------------------------------------------------
            if job.is_preview:
                command.extend([
                    "-W", "960",
                    "-H", "540",
                    "--fps", "24",
                    "--bloom", "0",
                ])

            # Log the full command so the user can copy-paste it for
            # debugging.  We print:
            #   [RenderService] COMMAND
            #   python ... rhythm.py -i ... -o ... --mode ... <all flags>
            print(
                f"[RenderService] Starting render: segment={job.segment_id} "
                f"mode={job.mode} preview={job.is_preview}",
                flush=True,
            )
            print("[RenderService] COMMAND:", flush=True)
            print(" ".join(command), flush=True)
            print("[RenderService] ---", flush=True)

            # Force UTF-8 for both the subprocess's stdio and our decode of
            # captured output so unicode characters in src.rhythm's prints
            # (arrows, em-dashes, progress markers) don't trigger cp1252
            # UnicodeEncodeError on Windows.
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            # Disable Python's stdout buffering so we can stream progress
            # lines line-by-line as they're emitted (rhythm.py prints to
            # stdout without explicit flushing).
            env.setdefault("PYTHONUNBUFFERED", "1")

            # We Popen + read stdout line-by-line so we can:
            #   - parse "Progress: NN%" lines and forward them as the
            #     `progress` signal in real time;
            #   - keep a rolling tail of the last ~120 lines for diagnostics
            #     when the subprocess fails.
            proc = subprocess.Popen(
                command,
                cwd=self._repo_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr into stdout
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line-buffered
            )

            tail_lines: deque[str] = deque(maxlen=120)
            assert proc.stdout is not None  # for type-checkers
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                tail_lines.append(line)

                # Parse "Progress: NN%" emitted by rhythm.py every 10%.
                # Anchor on the literal "Progress:" prefix so we don't match
                # other "%" prints (combo counters, beat sens, etc.).
                m = self._PROGRESS_RE.search(line)
                if m:
                    try:
                        pct = int(m.group(1))
                    except (TypeError, ValueError):
                        pct = 0
                    # Clamp to (1, 99) — start (1) and end (100) are emitted
                    # by _run_loop itself so we don't pre-empt those.
                    pct = max(1, min(99, pct))
                    self.progress.emit(job.segment_id, pct)

            returncode = proc.wait()
            if returncode != 0:
                tail = "\n".join(tail_lines)[-1500:]
                print(
                    f"[RenderService] Subprocess failed ({returncode}).\n"
                    f"  cmd: {' '.join(command)}\n"
                    f"  tail:\n{tail}",
                    file=sys.stderr,
                    flush=True,
                )
                raise RuntimeError(
                    f"Render failed (exit {returncode}):\n{tail[-600:]}"
                )

        finally:
            if temp_audio:
                Path(temp_audio).unlink(missing_ok=True)

    # Matches "Progress: 30%" prints from src.rhythm.RhythmVisualizer
    # .process_video (emitted at every 10% boundary).
    _PROGRESS_RE = re.compile(r"Progress:\s*(\d{1,3})\s*%")

    # CLI flags in src/rhythm.py whose argparse action='store_true' — they
    # accept NO value, only their presence toggles them on.  Sending
    # "--mesh_wireframe 0" makes argparse abort with "unrecognized argument".
    _STORE_TRUE_KEYS = frozenset({"mesh_wireframe"})

    # CLI flags whose argparse type=int — Pydantic may dump them as floats
    # (e.g. "0.0"), which argparse rejects.  These are coerced to int strings.
    _INT_KEYS = frozenset({
        "beat_min_gap",
    })

    # CLI flags that don't exist in src/rhythm.py argparse (defensive — any
    # accidental settings key would otherwise surface as "unrecognized arg").
    _SKIP_KEYS = frozenset({"mode_list"})

    @classmethod
    def _settings_to_args(cls, settings: dict) -> list[str]:
        args: list[str] = []
        for key, value in settings.items():
            if key in cls._SKIP_KEYS:
                continue
            cli_key = f"--{key}"

            if key in cls._STORE_TRUE_KEYS:
                # Presence flag — only emit it when truthy, never with a value.
                if bool(value):
                    args.append(cli_key)
                continue

            if key in cls._INT_KEYS:
                try:
                    args.extend([cli_key, str(int(round(float(value))))])
                except (TypeError, ValueError):
                    args.extend([cli_key, str(value)])
                continue

            if isinstance(value, bool):
                args.extend([cli_key, "1" if value else "0"])
            elif isinstance(value, list):
                args.extend([cli_key, ",".join(str(v) for v in value)])
            else:
                args.extend([cli_key, str(value)])
        return args
