"""Queued rendering service that wraps src.rhythm in subprocess.

This service is the **final-export** path only.  Live preview was
moved fully in-process (see :mod:`src.live_renderer`) so this module
no longer carries any HLS / streaming / IPC plumbing — every job
encodes one MP4 and emits ``finished`` once.
"""

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
from src.bundle_paths import get_rhythm_command as _get_rhythm_command


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
    # Live preview no longer goes through this service — it runs
    # in-process via ``src.live_renderer``.  ``is_preview`` is kept
    # purely as a fast-quality hint for short throw-away renders that
    # the user might still want to kick off here (e.g. "render a
    # quick draft and watch it"), but Studio currently never sets it.
    is_preview: bool = False
    # Output resolution & frame-rate — forwarded directly to rhythm.py via
    # -W / -H / --fps.  If None, rhythm.py uses its own defaults (1920×1080
    # @ 30 fps), which is rarely what the user configured in Project Settings.
    output_width: Optional[int] = None
    output_height: Optional[int] = None
    output_fps: Optional[int] = None
    # Pre-trimmed WAV produced by AudioTrimService.  When set and the file
    # exists on disk, _run_job uses it directly as -i (no pydub re-trim).
    trimmed_audio_path: Optional[str] = None
    # Directory where trimmed WAVs should be saved (<project_dir>/temps/).
    # When a pre-trimmed file doesn't exist yet, the fallback trim is saved
    # here (not in AppData/Temp) so the file persists and can be re-used.
    project_temps_dir: Optional[str] = None
    # Explicit beat-event times (seconds, relative to the trimmed audio
    # window — i.e. ``t_local`` exactly as edited on the timeline).
    # When non-empty, ``_run_job`` forwards them to rhythm.py via
    # ``--beat_source array --beat_times <csv>`` so the final video
    # honours the user's timeline edits instead of re-running onset
    # detection (which can drift slightly from the saved positions).
    beat_times: list[float] = field(default_factory=list)
    # Stickman draw-box position+size as fractions of the rendered
    # frame (``{"x","y","w","h"}`` each in [0,1]).  Empty / None =
    # use rhythm.py's built-in default (left-column HUD).  Forwarded
    # to ``rhythm.py`` as ``--stick_x0/y0/w/h <pixel>`` after
    # multiplying by ``output_width`` / ``output_height``.  Resolution-
    # independent on purpose: the user's edit on a 1920x1080 project
    # is preserved when the project is later re-exported at 4K.
    stickman_location: dict = field(default_factory=dict)


class RenderService(QObject):
    """Serial render queue for segment jobs."""

    progress = Signal(str, int)  # segment_id, 0..100
    finished = Signal(str, str)  # segment_id, output_path
    failed = Signal(str, str)    # segment_id, error_message
    # Emitted when the fallback trim saves a WAV to project_temps_dir so
    # MainWindow can store the path back on the segment and save the project.
    trimmed = Signal(str, str)   # segment_id, trimmed_wav_path

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
        project_temps_dir: Optional[str] = None,
    ) -> RenderJob:
        """Build render job object from segment state.

        When ``segment.beat_events`` is populated (Auto Gen Block result
        or manual timeline edits), the times are extracted into the
        job's ``beat_times`` list so :meth:`_run_job` can pass them as
        ``--beat_source array --beat_times`` and skip onset detection
        in the rhythm.py subprocess. The render then matches the
        timeline preview tick-for-tick.
        """
        dur = segment.duration_sec if segment.duration_sec and segment.duration_sec > 0 else None
        # Threshold from the waveform red line (0..1).  Beats whose
        # exported audio amplitude falls below the threshold are
        # filtered out here BEFORE we hand the list to rhythm.py, so
        # the rendered video matches what the user sees on the
        # timeline strip exactly.  Old projects without per-event
        # heights stored 1.0 during deserialise (see project_store)
        # so they always pass the threshold.
        thr = max(0.0, min(1.0, float(getattr(
            segment, "beat_height_threshold", 0.0
        ) or 0.0)))
        beat_times: list[float] = []
        for ev in (segment.beat_events or []):
            try:
                if isinstance(ev, (tuple, list)):
                    t = float(ev[0])
                    h = float(ev[2]) if len(ev) >= 3 else 1.0
                else:
                    t = float(ev)
                    h = 1.0
            except (TypeError, ValueError, IndexError):
                continue
            if h < thr - 1e-6:
                continue
            beat_times.append(t)
        # Stickman draw-box overrides — only forwarded when stickman
        # rendering is actually enabled in the segment's settings.
        # Otherwise rhythm.py's StickmanHUD never instantiates, so
        # the ``--stick_*`` flags would be silently ignored anyway,
        # and we keep the CLI line shorter for the easier-to-read
        # log lines.
        stick_loc: dict = {}
        rs = segment.render_settings or {}
        if bool(rs.get("stickman", True)):
            sl = getattr(segment, "stickman_location", None) or {}
            try:
                stick_loc = {
                    "x": float(sl.get("x", 0.010)),
                    "y": float(sl.get("y", 0.090)),
                    "w": float(sl.get("w", 0.135)),
                    "h": float(sl.get("h", 0.540)),
                }
            except (TypeError, ValueError):
                stick_loc = {}
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
            trimmed_audio_path=segment.trimmed_audio_path,
            project_temps_dir=project_temps_dir,
            beat_times=beat_times,
            stickman_location=stick_loc,
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
                if job.segment_id in self._cancelled:
                    self._cancelled.discard(job.segment_id)
                else:
                    self.finished.emit(job.segment_id, job.output_path)
            except Exception as exc:  # noqa: BLE001
                if job.segment_id in self._cancelled:
                    self._cancelled.discard(job.segment_id)
                    print(
                        f"[RenderService] Render cancelled for "
                        f"{job.segment_id} (user-triggered)",
                        flush=True,
                    )
                else:
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

        # --- Audio selection -----------------------------------------------
        # Priority:
        # 1. Pre-trimmed WAV from AudioTrimService (persisted next to project)
        #    → no pydub work at render time; the file is already lossless WAV
        #    with the exact [start, end] window the user configured.
        # 2. On-the-fly pydub trim (fallback: project not yet saved, or trim
        #    hasn't run yet) → same lossless WAV approach, written to a
        #    NamedTemporaryFile that is deleted in the finally block.
        audio_path = job.audio_path
        temp_audio: Optional[str] = None
        audio_was_trimmed = False  # set True whenever we cut the audio window
        try:
            pre_trimmed = (
                job.trimmed_audio_path
                and Path(job.trimmed_audio_path).exists()
            )
            if pre_trimmed:
                audio_path = job.trimmed_audio_path  # type: ignore[assignment]
                audio_was_trimmed = True
                print(
                    f"[RenderService] Using pre-trimmed audio: "
                    f"{Path(audio_path).name}",
                    flush=True,
                )
            else:
                needs_trim = (job.start_time_sec > 0.01) or (
                    job.duration_sec is not None and job.duration_sec > 0
                )
                if needs_trim:
                    from studio.core_bridge.audio_trim_service import trim_audio_ffmpeg

                    src_ext = Path(audio_path).suffix or ".mp3"
                    start_sec = job.start_time_sec
                    end_sec = (
                        start_sec + job.duration_sec
                        if job.duration_sec is not None
                        else start_sec + 99999.0  # effectively full remainder
                    )
                    if job.project_temps_dir:
                        out_dir = Path(job.project_temps_dir)
                        out_dir.mkdir(parents=True, exist_ok=True)
                        out_clip = out_dir / f"audio_{job.segment_id}{src_ext}"
                        trim_audio_ffmpeg(audio_path, start_sec, end_sec, out_clip)
                        audio_path = str(out_clip)
                        audio_was_trimmed = True
                        # Notify MainWindow so it stores the path on the segment.
                        self.trimmed.emit(job.segment_id, audio_path)
                        print(
                            f"[RenderService] Trim → {out_clip.name} "
                            f"({start_sec:.3f}s – {end_sec:.3f}s)",
                            flush=True,
                        )
                    else:
                        # Project not saved yet — fall back to system temp.
                        tmp = tempfile.NamedTemporaryFile(
                            suffix=src_ext, delete=False
                        )
                        tmp.close()
                        trim_audio_ffmpeg(audio_path, start_sec, end_sec, Path(tmp.name))
                        audio_path = tmp.name
                        temp_audio = tmp.name
                        audio_was_trimmed = True
                        print(
                            f"[RenderService] Trim → system temp {tmp.name} "
                            "(save project to use project-folder audio next time)",
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
            command = _get_rhythm_command(self._repo_root) + [
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

            # If the audio was trimmed, rhythm.py sees a clip starting at
            # t=0 with the exact duration — no -d needed.  Only pass -d
            # when the full source file is used and we want to cap duration.
            if not audio_was_trimmed and job.duration_sec is not None:
                command.extend(["-d", str(job.duration_sec)])

            command.extend(self._settings_to_args(settings))

            # ----------------------------------------------------------------
            # Beat-events override — when the segment has saved beat
            # events (from Auto Gen Block or hand-edited on the
            # timeline), feed them directly to rhythm.py via array
            # mode so the final video honours every drag/insert/
            # delete the user committed.  This is appended AFTER
            # ``_settings_to_args`` so our ``--beat_source array``
            # supersedes whatever ``beat_source`` the segment's
            # render_settings set (argparse keeps the last value
            # for repeated flags).
            #
            # Times are clipped to [0, duration_sec] when a duration
            # cap is known — saved events can fall outside the
            # current window if the user resized the segment after
            # editing.  rhythm.py's _parse_beat_times also clips on
            # its end, but trimming here keeps the CLI line shorter.
            # ----------------------------------------------------------------
            if job.beat_times:
                if job.duration_sec is not None and job.duration_sec > 0:
                    cap = float(job.duration_sec)
                    valid = [
                        t for t in job.beat_times
                        if 0.0 <= float(t) <= cap + 1e-3
                    ]
                else:
                    valid = [
                        float(t) for t in job.beat_times if float(t) >= 0.0
                    ]
                if valid:
                    times_csv = ",".join(f"{float(t):.6f}" for t in valid)
                    command.extend([
                        "--beat_source", "array",
                        "--beat_times", times_csv,
                    ])
                    print(
                        f"[RenderService] Using saved beat array: "
                        f"{len(valid)} event(s) "
                        f"(skipping onset detection)",
                        flush=True,
                    )

            # ----------------------------------------------------------------
            # Stickman draw-box override — when the user dragged/resized
            # the draggable box on the Player panel, the segment carries
            # a ``stickman_location`` dict (fractions of the rendered
            # frame).  Translate to pixels using the ACTUAL output
            # resolution that this job is rendering at (``-W`` / ``-H``
            # appended above) so a 0.135 width fraction becomes ~260 px
            # at 1920p, ~520 px at 3840p, etc.  Falls back to the
            # project's configured resolution and ultimately rhythm.py's
            # internal 1920x1080 default if none is supplied.  Each
            # ``--stick_*`` flag accepts -1 to mean "auto", so a missing
            # key in the dict cleanly defers to rhythm.py's left-column
            # HUD layout for that one component.
            # ----------------------------------------------------------------
            if job.stickman_location:
                w_px = int(job.output_width or 1920)
                h_px = int(job.output_height or 1080)
                if w_px > 0 and h_px > 0:
                    sl = job.stickman_location
                    sx = max(-1, int(round(float(sl.get("x", -1)) * w_px))) \
                        if sl.get("x", None) is not None else -1
                    sy = max(-1, int(round(float(sl.get("y", -1)) * h_px))) \
                        if sl.get("y", None) is not None else -1
                    sw = max(-1, int(round(float(sl.get("w", -1)) * w_px))) \
                        if sl.get("w", None) is not None else -1
                    sh = max(-1, int(round(float(sl.get("h", -1)) * h_px))) \
                        if sl.get("h", None) is not None else -1
                    command.extend([
                        "--stick_x0", str(sx),
                        "--stick_y0", str(sy),
                        "--stick_w",  str(sw),
                        "--stick_h",  str(sh),
                    ])
                    print(
                        f"[RenderService] Stickman box override: "
                        f"x0={sx} y0={sy} w={sw} h={sh} "
                        f"(frame {w_px}x{h_px})",
                        flush=True,
                    )

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
            # On Windows, spawning a console EXE (rhythm_worker.exe) from a
            # GUI process (SSCStudio.exe, console=False) causes Windows to
            # open a black CMD window.  CREATE_NO_WINDOW suppresses that
            # window and keeps stdout/stderr readable via PIPE.
            _creation_flags = 0
            if sys.platform == "win32":
                _creation_flags = subprocess.CREATE_NO_WINDOW

            proc = subprocess.Popen(
                command,
                cwd=self._repo_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr into stdout
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line-buffered
                creationflags=_creation_flags,
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
        "max_per_lane",
    })

    # Only the fields that are visible in the UI are forwarded to rhythm.py.
    # Hidden fields (travel, lanes, beat_min_gap, …) are kept in the project
    # file for future use but are intentionally NOT passed to the subprocess
    # so rhythm.py falls back to its own well-tested defaults — giving output
    # identical to a plain CLI run without those flags.
    # Mirrors SegmentConfigPanel._VISIBLE_FIELDS exactly (plus beat_source
    # which maps to the visible combo box in the UI).
    _ALLOWED_KEYS = frozenset({
        "beat_source",
        "beat_sens",
        "density",
        "speed",
        "max_per_lane",
        "floor_panels",
        "floor_panel_color",
        "floor_panel_blink",
        "floor_panel_image",
        "stickman",
        "line_zigzag",
        "side_rails",
        "rail_color",
        "rail_shape",
        "rail_height",
        "rail_offset_x",
        "rail_image",
        "rail_pulse",
        "rail_pulse_intensity",
        "floor_hit_frac",
        "horizon_frac",
        "floor_spread_frac",
        "far_spread_frac",
        "wall_floor_gap_frac",
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
            if key not in cls._ALLOWED_KEYS:
                continue
            # Skip Optional fields explicitly set to None — the CLI flag
            # is omitted so rhythm.py falls back to its own default (e.g.
            # line_zigzag=None means "Off", i.e. no zigzag pattern).
            if value is None:
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
