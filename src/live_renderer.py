"""In-process per-frame renderer for editor preview.

Why this exists
---------------
The Studio's "Preview" feature originally pushed every edit through a full
``ffmpeg`` HLS render → HTTP server → ``QMediaPlayer`` pipeline.  That gave
us 1–3 second latency per edit, race conditions on every restart, and a
constant stream of HTTP-404 ("segment not found") errors whenever the user
deleted a beat or tweaked density.

Real video editors (Premiere, DaVinci Resolve, CapCut, After Effects) don't
work that way.  They keep the timeline data in memory and **draw frames
on-demand** at the playhead position; audio plays via the OS mixer; the
viewport is a per-frame composite, not a video file.  Edits show up next
frame (≤ 33 ms at 30 fps) because no encoder lives in the loop.

``LiveFrameRenderer`` is that drawing layer for our app.  It owns:

  * one ``GameManager`` (from :mod:`rhythm`) plus its scheduled targets,
  * the camera + tunnel + particle/HUD decorations, and
  * the audio-derived buffers (RMS bass, wave columns) needed by the
    scheduler and the per-frame compose.

Public API:

  * ``render_at(t_sec)``     — render the closest frame to ``t_sec`` and
    return a ``numpy.ndarray`` of shape ``(H, W, 3)`` in BGR uint8.
    Backward seeks reset state and replay forward (no draw for skipped
    frames so scrubbing stays cheap).
  * ``update_beats(times)``  — swap in a new beat array and rebuild the
    schedule.  Cheap; sub-100 ms even on long segments.
  * ``update_mode(mode, *, show_stickman, stickman_box, show_floor_panels)``
    — rebuild the entire scene (cam/tunnel/HUDs) for a new mode and/or
    decoration toggles.  ``mode`` is the only required positional; pass
    any subset of the keyword args to also flip stickman visibility,
    move its draw-box, or toggle the lane-floor panels without spawning
    a new renderer.  Used by the editor when the user edits Sticky Man
    / Floor panels / mode in the segment config form while live
    preview is active.

Trade-offs vs. the final ffmpeg render
---------------------------------------
The live renderer reuses :mod:`rhythm`'s entity classes (``GameManager``,
``PerspectiveCamera``, ``TunnelRenderer``, ``StickmanHUD`` …) so the
on-screen layout is pixel-faithful for the supported mode set.  It
**deliberately omits**:

  * mesh/texture cube assets — preview always uses the default coloured
    cube + fist icon (loading textures takes ~50–100 ms which the user
    doesn't want to pay each time they hit Preview).
  * ``LINE_DEBUG`` overlay (timeline + waveform strip on top of the
    rendered frame) — debug-only, not needed in the editor preview.
  * ``--export_events`` JSON dump.
  * ``BEAT_HEIGHT_THRESHOLD`` filtering — by the time the user clicks
    Preview the beat array on the segment is already what they want
    rendered, so we feed it to the scheduler verbatim.

Source-of-truth for final-export visuals remains :func:`rhythm
.RhythmVisualizer.process_video`; this module is the editor's mirror.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import librosa
import numpy as np

# ``src/`` is **not** a Python package (no ``__init__.py``) — it's a flat
# folder of scripts that ``rhythm.py`` itself relies on for its sibling
# imports like ``from authorization import authourize_user``.  Adding an
# ``__init__.py`` would force every sibling import in rhythm.py to become
# an explicit relative import, which is more invasive than we want.
# Instead we make sure ``src/`` is on ``sys.path`` before doing the import
# so ``import rhythm`` resolves the script.  Idempotent: a second insertion
# of an already-present path is a no-op for sys.path lookups, and any
# duplicate gets de-duplicated implicitly by the import machinery.
_SRC_DIR = str(Path(__file__).resolve().parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import rhythm as _rhy  # noqa: E402  (sys.path setup must precede import)
from rhythm import (  # noqa: E402
    BASS_RANGE,
    CLR_BG,
    ComboHUD,
    DanceTarget,
    GameManager,
    HOP_LENGTH,
    LineTarget,
    N_LANES,
    N_LANES_DANCE,
    ParticleSystem,
    PerspectiveCamera,
    PunchTarget,
    RelaxTarget,
    SideRailRenderer,
    StickmanHUD,
    Target,
    TunnelRenderer,
    ViewportFrame,
    WallTarget,
    _FLOOR_SPREAD_BY_MODE,
    _parse_modes,
    _relax_camera_dy,
    _RELAX_BOB_WINDOW_F,
    detect_wave_columns,
    gpu_glow,
)


@dataclass
class _AudioFeatures:
    """Cached output of :func:`librosa` analysis for a clip.

    Recomputing onset_strength + magphase costs ~150–400 ms for a 30 s
    segment; we run it ONCE in ``__init__`` and hold the results across
    every ``render_at`` / ``update_beats`` / ``update_mode`` call so
    interactive edits stay snappy.
    """

    y: np.ndarray
    sr: int
    duration_sec: float
    bass_arr: np.ndarray
    wave_columns: list[dict]


class LiveFrameRenderer:
    """Composites BGR frames on-demand for the Studio editor preview.

    Construct one per Preview-mode toggle (per segment).  Reuse it across
    edits via ``update_beats`` / ``update_mode``; throw it away when the
    user toggles Preview off or switches segments.

    Parameters
    ----------
    audio_path:
        Path to the trimmed WAV / MP3 to play under the preview.  ``y, sr``
        are loaded with :func:`librosa.load` once and held in memory
        throughout the renderer's life.
    beat_times:
        List of beat-event times in **seconds, segment-local** (i.e. ``0.0``
        is the start of ``audio_path``, NOT the project timeline).  This is
        the same format the Studio's beat-edit UI uses.
    mode:
        Single-mode string (``"punch"`` / ``"dance"`` / ``"line"`` /
        ``"relax"``) or a comma-separated combo spec (``"punch,dance"``).
    fps, width, height:
        Preview frame dimensions.  Default 720p / 24 fps as picked by the
        user (low-CPU "draft" preview; the final render still uses the
        project's full resolution).
    bloom:
        Whether to run the post-process glow pass.  Default off for live
        preview because it adds 8–15 ms per frame which hurts headroom.
    show_stickman:
        Whether the left-column animated stickman is drawn.  Defaults
        ``True`` — same as ``RhythmVisualizer.SHOW_STICKMAN``.
    stickman_box:
        Optional ``(x0, y0, w, h)`` pixel box to override the stickman's
        default left-column placement.  Pass ``None`` to use defaults.
    show_floor_panels:
        Whether the lane-floor neon panels (the rectangular pads under
        each lane) are drawn.  Defaults ``True`` — matches
        ``process_video``'s default, and is hot-toggleable from the
        editor via :meth:`update_mode`.
    """

    def __init__(
        self,
        audio_path: str,
        *,
        beat_times: Optional[list[float]] = None,
        mode: str = "punch",
        fps: int = 24,
        width: int = 1280,
        height: int = 720,
        bloom: bool = False,
        show_stickman: bool = True,
        stickman_box: Optional[tuple[int, int, int, int]] = None,
        show_floor_panels: bool = True,
        floor_panel_color: Optional[str] = None,
        floor_panel_blink: bool = False,
        floor_panel_image: Optional[str] = None,
        show_side_rails: bool = False,
        rail_color: str = "#FF60FF",
        rail_shape: str = "chunky",
        rail_height: float = 0.14,
        rail_offset_x: float = 0.08,
        rail_image: Optional[str] = None,
        rail_pulse: str = "beat",
        rail_pulse_intensity: float = 0.6,
        floor_hit_frac: Optional[float] = None,
        horizon_frac: Optional[float] = None,
        floor_spread_frac: Optional[float] = None,
        far_spread_frac: Optional[float] = None,
        wall_floor_gap_frac: Optional[float] = None,
        cube_color_left: Optional[tuple[int, int, int]] = None,
        cube_color_right: Optional[tuple[int, int, int]] = None,
        panel_neon_color: Optional[tuple[int, int, int]] = None,
        max_per_lane: int = 3,
        block_speed: float = 1.0,
        beat_min_gap: int = 4,
        line_beats: int = 2,
        line_zigzag: str = "vertical",
        dance_pair_cycle: int = 4,
        punch_pair_cycle: int = 4,
        lane_filter: Optional[set[int]] = None,
    ) -> None:
        self._audio_path = audio_path
        self._beat_times = list(beat_times or [])
        self._mode_str = mode
        self._fps = int(fps)
        self._width = int(width)
        self._height = int(height)
        self._bloom = bool(bloom)
        self._show_stickman = bool(show_stickman)
        self._stickman_box = stickman_box
        self._show_floor_panels = bool(show_floor_panels)
        self._floor_panel_color = floor_panel_color or None
        self._floor_panel_blink = bool(floor_panel_blink)
        self._floor_panel_image = floor_panel_image or None
        self._show_side_rails = bool(show_side_rails)
        self._rail_color = str(rail_color)
        self._rail_shape = str(rail_shape)
        self._rail_height = float(rail_height)
        self._rail_offset_x = float(rail_offset_x)
        self._rail_image = rail_image or None
        self._rail_pulse = str(rail_pulse)
        self._rail_pulse_intensity = float(rail_pulse_intensity)
        self._floor_hit_frac      = float(floor_hit_frac)      if floor_hit_frac      is not None else None
        self._horizon_frac        = float(horizon_frac)        if horizon_frac        is not None else None
        self._floor_spread_frac   = float(floor_spread_frac)   if floor_spread_frac   is not None else None
        self._far_spread_frac     = float(far_spread_frac)     if far_spread_frac     is not None else None
        self._wall_floor_gap_frac = float(wall_floor_gap_frac) if wall_floor_gap_frac is not None else None
        self._cube_color_left = cube_color_left
        self._cube_color_right = cube_color_right
        self._panel_neon_color = panel_neon_color
        self._max_per_lane = max(1, int(max_per_lane))
        self._block_speed = max(0.05, float(block_speed))
        self._beat_min_gap = max(1, int(beat_min_gap))
        self._line_beats = max(1, int(line_beats))
        self._line_zigzag = line_zigzag
        self._dance_pair_cycle = int(dance_pair_cycle)
        self._punch_pair_cycle = int(punch_pair_cycle)
        self._lane_filter = lane_filter

        # Audio analysis (heavy — done once).
        self._audio = self._analyse_audio(audio_path)
        # Total frames clamped to the audio duration so render_at() never
        # walks past the end.
        self._total_frames = int(self._audio.duration_sec * self._fps)

        # Build the scene (cam/tunnel/HUDs/game/targets) for the initial
        # mode + beat list.  This populates self._cam, self._game, etc.
        self._build_scene()

        # ``_cur_fi == -1`` is the "fresh state, nothing rendered yet"
        # sentinel.  ``render_at`` will fast-forward to the requested
        # frame index from there.
        self._cur_fi: int = -1

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    @property
    def fps(self) -> int:
        """Configured preview frame-rate."""
        return self._fps

    @property
    def width(self) -> int:
        """Frame width in pixels."""
        return self._width

    @property
    def height(self) -> int:
        """Frame height in pixels."""
        return self._height

    @property
    def duration_sec(self) -> float:
        """Loaded audio's total duration in seconds."""
        return float(self._audio.duration_sec)

    def render_at(self, t_sec: float) -> np.ndarray:
        """Render the frame closest to ``t_sec`` and return a BGR ndarray.

        Time semantics
        --------------
        ``t_sec`` is segment-local — ``0.0`` is the first frame, the audio's
        end is ``duration_sec``.  Out-of-range values are clamped so the
        caller never has to worry about driving the renderer past its
        domain (e.g. when the QMediaPlayer briefly overshoots EndOfMedia).

        Backward-seek strategy
        ----------------------
        Game state is incremental — ``GameManager.update(fi)`` mutates
        target positions, kills hit cubes, etc., so we cannot just call
        it with ``fi`` smaller than the previous render.  When the caller
        asks for a frame **before** ``self._cur_fi`` we tear the scene
        down and rebuild it (cheap: tunnel/cam/HUD constructors are
        ~1 ms; the GameManager's pre_schedule re-runs in ~5–20 ms even
        on long segments).  Then we replay forward from frame 0 to the
        target index without drawing the intermediates so scrubbing is
        smooth.
        """
        if self._total_frames <= 0:
            return self._blank_frame()
        target_fi = max(
            0, min(self._total_frames - 1, int(round(float(t_sec) * self._fps)))
        )
        if target_fi < self._cur_fi:
            # Backward seek → rebuild the entire scene so the GameManager
            # state is back at frame 0.  pre_schedule's costs are
            # dominated by NumPy ops on the bass envelope; in practice
            # this round-trip is cheaper than running ``update`` in
            # reverse (which isn't supported anyway).
            self._build_scene()
            self._cur_fi = -1
        # Fast-forward state without drawing — particles + game advance
        # exactly the same way they would under the encoder loop, but we
        # skip the canvas compose for everything except the final frame.
        # Each ``game.update(fi)`` returns the list of targets that hit
        # the camera plane THIS frame; we apply their particle bursts
        # immediately so by the time we draw the target frame, the
        # particle ages match what the encoder would have produced.
        while self._cur_fi < target_fi:
            self._cur_fi += 1
            hits = self._game.update(self._cur_fi)
            self._apply_hits(hits, self._cur_fi)
            # Particles need to keep ticking even on skipped frames so a
            # forward-scrub through a hit lands with the same particle
            # state the encoder would produce.
            self._particles.update()
        return self._compose_frame(self._cur_fi)

    def update_beats(self, beat_times: list[float]) -> None:
        """Swap in a new beat array and rebuild the schedule.

        After this call, ``self._cur_fi`` is reset to ``-1`` so the next
        ``render_at`` reseeds from the new schedule.  Cheap — no audio
        re-analysis, no scene-decoration rebuild; only the
        ``GameManager`` is reconstructed.
        """
        self._beat_times = list(beat_times)
        # Re-derive frame indices from the new times.  ``_compute_beat_frames``
        # honours BEAT_SOURCE == 'array' semantics: clip to range, no
        # density filtering, no subdivision (the user's edits are
        # already exactly what they want).
        self._beat_frames = self._compute_beat_frames()
        # Particles + game must reset together — orphan particle bursts
        # from the OLD schedule would otherwise still be drawn on the
        # next render, looking like ghost hits at the old beat
        # positions.
        self._game = GameManager(self._cam, travel=self._travel)
        self._game.pre_schedule(
            self._beat_frames,
            self._audio.bass_arr,
            min_gap_frames=self._beat_min_gap,
            min_lane_gap=self._min_lane_gap,
            mode=self._modes_seq,
            lane_filter=self._lane_filter,
            dance_pair_cycle=self._dance_pair_cycle,
            punch_pair_cycle=self._punch_pair_cycle,
            line_beats=self._line_beats,
            beat_density=1.0,
            wave_columns=self._audio.wave_columns,
            line_zigzag=self._line_zigzag,
            beat_source="array",
        )
        self._particles = ParticleSystem()
        # Re-seed stickman events so the avatar's pose timeline tracks
        # the new beats; without this it would keep punching on the OLD
        # schedule.
        self._refresh_stickman_events()
        self._cur_fi = -1

    def update_mode(
        self,
        mode: str,
        *,
        show_stickman: Optional[bool] = None,
        stickman_box: Optional[tuple[int, int, int, int]] = None,
        show_floor_panels: Optional[bool] = None,
        floor_panel_color: Optional[str] = None,
        floor_panel_blink: Optional[bool] = None,
        floor_panel_image: Optional[str] = None,
        show_side_rails: Optional[bool] = None,
        rail_color: Optional[str] = None,
        rail_shape: Optional[str] = None,
        rail_height: Optional[float] = None,
        rail_offset_x: Optional[float] = None,
        rail_image: Optional[str] = None,
        rail_pulse: Optional[str] = None,
        rail_pulse_intensity: Optional[float] = None,
        max_per_lane: Optional[int] = None,
    ) -> None:
        """Switch gameplay mode (and optionally decor) then rebuild the scene.

        Heavier than ``update_beats`` because the camera lane count and
        viewport rails depend on the mode (dance has 4 panels, punch
        doesn't); we therefore re-create cam / tunnel / viewport / stick.
        Audio analysis is preserved.

        ``show_stickman`` / ``stickman_box`` / ``show_floor_panels`` /
        ``max_per_lane`` are OPTIONAL overrides — pass ``None`` (the
        default) to keep the current value.  Bundled into this single
        entrypoint because all of them decorate the SAME scene
        primitives that ``update_mode`` already rebuilds, so the cost
        is identical regardless of which subset changes.  Editor calls
        this with whatever subset of params the segment-config form
        actually mutated; preview hot-reloads complete in well under
        200 ms either way.

        ``max_per_lane`` resets ``_min_lane_gap`` (computed inside
        ``_build_scene``) so a tighter cap immediately drops more beats
        as "lane-stacked" while a looser cap pulls them back in — the
        scheduler diagnostic line in stdout reflects the change on the
        very next frame.
        """
        self._mode_str = mode
        if show_stickman is not None:
            self._show_stickman = bool(show_stickman)
        if stickman_box is not None:
            self._stickman_box = tuple(stickman_box)  # type: ignore[assignment]
        if show_floor_panels is not None:
            self._show_floor_panels = bool(show_floor_panels)
        if floor_panel_color is not None:
            self._floor_panel_color = floor_panel_color or None
        if floor_panel_blink is not None:
            self._floor_panel_blink = bool(floor_panel_blink)
        if floor_panel_image is not None:
            self._floor_panel_image = floor_panel_image or None
        if show_side_rails is not None:
            self._show_side_rails = bool(show_side_rails)
        if rail_color is not None:
            self._rail_color = str(rail_color)
        if rail_shape is not None:
            self._rail_shape = str(rail_shape)
        if rail_height is not None:
            self._rail_height = float(rail_height)
        if rail_offset_x is not None:
            self._rail_offset_x = float(rail_offset_x)
        if rail_image is not None:
            self._rail_image = rail_image or None
        if rail_pulse is not None:
            self._rail_pulse = str(rail_pulse)
        if rail_pulse_intensity is not None:
            self._rail_pulse_intensity = float(rail_pulse_intensity)
        if max_per_lane is not None:
            self._max_per_lane = max(1, int(max_per_lane))
        self._build_scene()
        self._cur_fi = -1

    def update_floor_wall(
        self,
        *,
        floor_hit_frac: Optional[float] = None,
        horizon_frac: Optional[float] = None,
        floor_spread_frac: Optional[float] = None,
        far_spread_frac: Optional[float] = None,
        wall_floor_gap_frac: Optional[float] = None,
    ) -> None:
        """Hot-update camera perspective and rebuild the scene.

        Pass ``None`` to keep the current override value unchanged.
        Pass a float to apply a new override.  The camera is rebuilt so
        the next ``render_at`` call reflects the new geometry.
        """
        changed = False
        if floor_hit_frac is not None:
            v = float(np.clip(floor_hit_frac, 0.70, 0.95))
            if v != self._floor_hit_frac:
                self._floor_hit_frac = v
                changed = True
        if horizon_frac is not None:
            v = float(np.clip(horizon_frac, 0.20, 0.60))
            if v != self._horizon_frac:
                self._horizon_frac = v
                changed = True
        if floor_spread_frac is not None:
            v = float(np.clip(floor_spread_frac, 0.20, 3.00))
            if v != self._floor_spread_frac:
                self._floor_spread_frac = v
                changed = True
        if far_spread_frac is not None:
            v = float(np.clip(far_spread_frac, 0.05, 3.00))
            if v != self._far_spread_frac:
                self._far_spread_frac = v
                changed = True
        if wall_floor_gap_frac is not None:
            v = float(np.clip(wall_floor_gap_frac, 0.00, 0.30))
            if v != self._wall_floor_gap_frac:
                self._wall_floor_gap_frac = v
                changed = True
        if changed:
            self._build_scene()
            self._cur_fi = -1

    def close(self) -> None:
        """Release references so Python can free large ndarrays sooner.

        Idempotent.  After ``close()`` further calls to ``render_at`` are
        undefined — the editor should drop its reference to the renderer.
        """
        # Setting attributes to None lets the GC reclaim them on the next
        # cycle even if some Qt object still holds the renderer in a
        # closure.
        self._game = None  # type: ignore[assignment]
        self._cam = None  # type: ignore[assignment]
        self._tunnel = None  # type: ignore[assignment]
        self._side_rail = None
        self._particles = None  # type: ignore[assignment]
        self._viewport = None  # type: ignore[assignment]
        self._combo = None  # type: ignore[assignment]
        self._stick = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------
    def _analyse_audio(self, audio_path: str) -> _AudioFeatures:
        """Load + extract per-frame buffers needed by the scheduler.

        Mirrors :meth:`rhythm.RhythmVisualizer.process_video` (audio-load
        + onset/spec + bass envelope + wave columns) but skips the
        ``librosa`` beat detection step entirely — Studio always supplies
        ``beat_times`` so we only need the audio-derived envelopes the
        scheduler reads (``bass_arr`` for wall-target spawn intensity and
        ``wave_columns`` for line-mode chain derivation + non-line snap-
        to-peak retiming).
        """
        y, sr = librosa.load(audio_path, mono=True)
        duration = len(y) / float(sr) if sr else 0.0
        total_frames = max(1, int(duration * self._fps))
        # Onset / spectrogram for bass envelope.  ``magphase`` returns the
        # magnitude spectrogram; we pick the lowest BASS_RANGE bins as a
        # proxy for low-frequency loudness, normalise to [0, 1] using the
        # global max, and resample to ``total_frames`` so wall targets
        # can read intensity by frame index in O(1).
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP_LENGTH)
        spec = librosa.stft(y, hop_length=HOP_LENGTH)
        spec_mag = librosa.magphase(spec)[0]
        bass_arr = np.zeros(total_frames, dtype=np.float32)
        bass_max = max(np.max(spec_mag[:BASS_RANGE]), 1e-6)
        if onset_env.size:
            for f in range(total_frames):
                oi = min(int(f * len(onset_env) / total_frames),
                         len(onset_env) - 1)
                bass_arr[f] = float(np.clip(
                    np.mean(spec_mag[:BASS_RANGE, oi]) / bass_max * 3,
                    0, 1,
                ))
        # Wave columns: rise → peak → descent triples on the RMS envelope,
        # used both for line-mode chain derivation AND for non-line modes
        # to snap each beat_frame to the nearest column rise (so visible
        # punches always land on a real audio peak instead of drifting
        # on the metronome grid).  Skipped re-computation when audio is
        # silent (empty y) so detect_wave_columns doesn't divide by zero.
        wave_columns: list[dict] = []
        if y.size:
            wave_columns = detect_wave_columns(
                y, sr, HOP_LENGTH, float(self._fps),
                min_gap_frames=max(2, self._beat_min_gap),
                prominence_pct=20.0,
                smooth_win=1,
            )
        return _AudioFeatures(
            y=y,
            sr=sr,
            duration_sec=duration,
            bass_arr=bass_arr,
            wave_columns=wave_columns,
        )

    def _compute_beat_frames(self) -> list[int]:
        """Convert ``self._beat_times`` (seconds) → unique frame indices.

        Honours the BEAT_SOURCE='array' contract: caller-supplied times
        are clipped to ``[0, total_frames)`` and de-duplicated, but
        density / subdivision / threshold filtering are NOT applied —
        the editor's beat array is the user's exact intent.
        """
        if not self._beat_times:
            return []
        beats = sorted(set(
            int(round(float(t) * self._fps)) for t in self._beat_times
        ))
        return [bf for bf in beats if 0 <= bf < self._total_frames]

    def _build_scene(self) -> None:
        """(Re-)create cam / tunnel / HUDs / game from current mode + beats.

        Idempotent — safe to call many times on the same renderer.  Used
        both at construction and from ``update_mode`` / backward-seek
        reset paths.
        """
        # Apply optional cube-color overrides.  These are class attributes
        # on ``Target`` so every PunchTarget / DanceTarget / LineTarget
        # instance picks them up automatically.
        Target.COLOR_LEFT = self._cube_color_left
        Target.COLOR_RIGHT = self._cube_color_right
        # Mode parsing — accept "punch", "punch,dance" combos, fall back
        # to ``punch`` on parse error to mirror process_video's
        # forgiving behaviour.
        try:
            modes_seq = _parse_modes(self._mode_str)
        except ValueError:
            modes_seq = ["punch"]
        if not modes_seq:
            modes_seq = ["punch"]
        self._modes_seq = modes_seq
        combo_mode = len(modes_seq) >= 2
        self._combo_mode = combo_mode
        # Scene-dressing primary mode picks lane count + floor spread.
        if "dance" in modes_seq:
            primary_mode = "dance"
        elif "punch" in modes_seq:
            primary_mode = "punch"
        else:  # solo line / relax both reuse punch dressing
            primary_mode = "punch"
        n_lanes_mode = N_LANES_DANCE if primary_mode == "dance" else N_LANES
        floor_spread = _FLOOR_SPREAD_BY_MODE.get(primary_mode, 0.50)
        cam_kwargs: dict = dict(n_lanes=n_lanes_mode,
                                floor_spread_frac=floor_spread)
        if self._floor_hit_frac is not None:
            cam_kwargs["hit_zone_frac"] = self._floor_hit_frac
        if self._horizon_frac is not None:
            cam_kwargs["horizon_frac"] = self._horizon_frac
        if self._floor_spread_frac is not None:
            cam_kwargs["floor_spread_frac"] = self._floor_spread_frac
        if self._far_spread_frac is not None:
            cam_kwargs["far_spread_frac"] = self._far_spread_frac
        if self._wall_floor_gap_frac is not None:
            cam_kwargs["wall_floor_gap_frac"] = self._wall_floor_gap_frac
        self._cam = PerspectiveCamera(self._width, self._height, **cam_kwargs)
        # Tunnel + decorative HUDs.  ``show_floor_panels`` is sourced
        # from the segment's render setting (default True) and is
        # hot-toggleable through :meth:`update_mode` so the user can
        # flip the floor pads on/off without restarting preview.
        # ``lane_tiles=True`` keeps the lane runway visible regardless
        # — it's essential for spatial orientation and not part of the
        # user-facing floor-panels toggle.
        self._tunnel = TunnelRenderer(
            self._cam,
            show_floor_panels=self._show_floor_panels,
            lane_tiles=True,
            floor_panel_color=self._floor_panel_color,
            floor_panel_blink=self._floor_panel_blink,
            floor_panel_image=self._floor_panel_image,
        )
        if self._show_side_rails:
            self._side_rail: Optional[SideRailRenderer] = SideRailRenderer(
                self._cam,
                color=self._rail_color,
                shape=self._rail_shape,
                height=self._rail_height,
                offset_x=self._rail_offset_x,
                image_path=self._rail_image,
                pulse=self._rail_pulse,
                pulse_intensity=self._rail_pulse_intensity,
            )
        else:
            self._side_rail = None
        self._particles = ParticleSystem()
        # Stickman action selection: combo runs use a dedicated
        # cross-mode pose library; solo runs match their mode's library
        # (line / relax have specialised libraries).  Disabled segments
        # produce ``stick = None`` so the per-frame compose can skip
        # the stickman draw entirely.
        if combo_mode:
            stick_action = "combo"
        elif modes_seq == ["line"]:
            stick_action = "line"
        elif modes_seq == ["relax"]:
            stick_action = "relax"
        else:
            stick_action = primary_mode
        if self._show_stickman:
            self._stick = StickmanHUD(
                self._cam,
                action=stick_action,
                box=self._stickman_box,
            )
        else:
            self._stick = None
        self._combo = ComboHUD(self._cam)
        self._viewport = ViewportFrame(
            self._cam,
            neon_color=self._panel_neon_color,
            mode=primary_mode,
        )

        # Beat frames (post-density on array source: density disabled).
        self._beat_frames = self._compute_beat_frames()
        # Auto-derive ``travel`` exactly the same way process_video does,
        # so the visual cadence matches the final render: travel = one
        # L↔R cycle of the median inter-beat gap, scaled by speed.  In
        # solo-relax mode (rare in editor preview) we do NOT apply the
        # 4× slowdown when source == 'array', matching process_video's
        # exception so user-placed beats render at the cadence they
        # asked for.
        solo_relax = (len(modes_seq) == 1 and modes_seq[0] == "relax")
        relax_slow_mult = 1.0  # array source → no auto-slowdown
        if (
            len(self._beat_frames) >= 2
            and _rhy.TARGET_TRAVEL_FRAMES < 0
        ):
            diffs = np.diff(self._beat_frames)
            base = int(round(np.median(diffs) * 2))
            travel = max(8, int(round(base / self._block_speed
                                       * relax_slow_mult)))
        else:
            travel = max(8, abs(_rhy.TARGET_TRAVEL_FRAMES))
        self._travel = travel
        # Per-lane visual spacing guard — same formula as process_video.
        if len(self._beat_frames) >= 2:
            base_cycle = int(round(np.median(np.diff(self._beat_frames)) * 2))
        else:
            base_cycle = 16
        self._min_lane_gap = max(
            1,
            travel // self._max_per_lane,
            base_cycle // 2,
        )

        # GameManager + schedule.  This is what populates ``game.targets``
        # with all the cubes / walls / line-chains / dance tiles that
        # the per-frame compose will then animate frame-by-frame.
        self._game = GameManager(self._cam, travel=travel)
        self._game.pre_schedule(
            self._beat_frames,
            self._audio.bass_arr,
            min_gap_frames=self._beat_min_gap,
            min_lane_gap=self._min_lane_gap,
            mode=modes_seq,
            lane_filter=self._lane_filter,
            dance_pair_cycle=self._dance_pair_cycle,
            punch_pair_cycle=self._punch_pair_cycle,
            line_beats=self._line_beats,
            beat_density=1.0,  # array source ignores density
            wave_columns=self._audio.wave_columns,
            line_zigzag=self._line_zigzag,
            beat_source="array",
        )
        # Stickman pose timeline derived from the actual scheduled
        # targets (so paired/double-hand / line-chain events map to
        # the right multi-cube pose).
        self._refresh_stickman_events()
        # Reset frame counter so render_at()'s catch-up loop replays
        # from frame 0 with the new schedule.
        self._cur_fi = -1

    def _refresh_stickman_events(self) -> None:
        """Build pose-sync events from ``self._game.targets`` and feed them
        to the stickman HUD.

        Mirrors the ``stick_events`` derivation block in
        :meth:`rhythm.RhythmVisualizer.process_video` (lines that
        translate each scheduled target's lane / kind into a pose tag
        like ``'L'`` / ``'R'`` / ``'JL'`` / ``'ZSLR'`` …).  Skipped when
        the segment has stickman rendering off (no HUD instance to
        feed).
        """
        if self._stick is None:
            return
        nL = max(2, int(self._cam.n_lanes))
        half = (nL - 1) / 2.0
        seen_paired_hits: set[int] = set()
        events: list[tuple] = []
        combo_mode = self._combo_mode
        for tg in self._game.targets:
            t_hit = tg.hit_frame / self._fps
            sustain = 0.0
            if isinstance(tg, WallTarget):
                kind = "W"
                lean_scale = 1.0
            elif isinstance(tg, RelaxTarget):
                kind = "JP" if tg.kind == "low" else "SQ"
                lean_scale = 1.0
                t_hit = tg.dodge_frame / self._fps
                hold_frames = tg.dodge_end_frame - tg.dodge_frame
                sustain = max(
                    _RELAX_BOB_WINDOW_F, int(hold_frames)
                ) / float(self._fps)
            elif isinstance(tg, LineTarget):
                is_horiz = (tg.zigzag == "horizontal")
                if is_horiz:
                    lean_scale_h = 0.55 + 1.05 * 1.0
                else:
                    side_tag_legacy = "L" if tg.is_left else "R"
                    if nL <= 2:
                        lean_scale_v = 1.0
                    else:
                        offset_norm = abs(tg.lane - half) / half
                        lean_scale_v = 0.55 + 1.05 * offset_norm
                n = tg.n_cubes
                for i in range(n):
                    t_i = tg.block_hit_frames[i] / self._fps
                    dur_i = (tg.block_shrink_dur[i]
                             if i < len(tg.block_shrink_dur) else tg._D)
                    per_sustain_i = max(1, int(dur_i)) / float(self._fps)
                    if is_horiz:
                        kind_i = "ZSLR" if (i % 2 == 0) else "ZSRL"
                        events.append((t_i, kind_i,
                                       lean_scale_h, per_sustain_i))
                    else:
                        side_tag = side_tag_legacy
                        vert = "D" if (i % 2 == 0) else "U"
                        events.append((t_i, "Z" + side_tag + vert,
                                       lean_scale_v, per_sustain_i))
                continue
            elif getattr(tg, "paired_side", None):
                if tg.hit_frame in seen_paired_hits:
                    continue
                seen_paired_hits.add(tg.hit_frame)
                ptag = tg.paired_side
                if ptag == "DL":
                    kind = "JL"
                elif ptag == "DR":
                    kind = "JR"
                elif ptag == "L":
                    kind = "LL"
                else:
                    kind = "RR"
                lean_scale = 1.0
            else:
                side_tag = "L" if tg.is_left else "R"
                if combo_mode:
                    prefix = "D" if isinstance(tg, DanceTarget) else "P"
                    kind = prefix + side_tag
                else:
                    kind = side_tag
                if nL <= 2:
                    lean_scale = 1.0
                else:
                    offset_norm = abs(tg.lane - half) / half
                    lean_scale = 0.55 + 1.05 * offset_norm
            if sustain > 0.0:
                events.append((t_hit, kind, lean_scale, sustain))
            else:
                events.append((t_hit, kind, lean_scale))
        self._stick.set_beat_events(events, self._fps)

    # ------------------------------------------------------------------
    # per-frame compose
    # ------------------------------------------------------------------
    def _apply_hits(self, hits: list, fi: int) -> None:
        """Spawn particle bursts + register combo for each hit at frame ``fi``.

        Mirrors the hit-processing branch inside
        :meth:`rhythm.RhythmVisualizer.process_video`'s render loop —
        per-target type the burst origin and intensity differ, and
        relax-mode dodges deliberately do **not** burst (avoiding the
        block reads as a success cue, exploding the block doesn't).
        """
        if not hits:
            return
        cam = self._cam
        for tg in hits:
            if isinstance(tg, RelaxTarget):
                continue
            line_horiz = (
                isinstance(tg, LineTarget)
                and getattr(tg, "zigzag", "vertical") == "horizontal"
            )
            if line_horiz and getattr(tg, "last_punched_i", -1) >= 0:
                lane_frac = (
                    0.0 if (tg.last_punched_i % 2 == 0)
                    else float(max(0, cam.n_lanes - 1))
                )
                x = int(cam.lane_x(lane_frac, 0.02))
            else:
                x = int(cam.lane_x(tg.lane, 0.02))
            if isinstance(tg, PunchTarget):
                if line_horiz:
                    proj = cam.project(
                        0.0, LineTarget.HORIZONTAL_WY,
                        cam.Z_NEAR + 0.01,
                    )
                    y = int(proj[1]) if proj else int(cam.air_y(0.02, 0.55))
                else:
                    y = int(cam.air_y(0.02, 0.55))
                count = 50
                self._viewport.trigger(1.0)
            elif isinstance(tg, DanceTarget):
                y = int(cam.floor_y(0.02)) - 6
                count = 40
                self._viewport.trigger(0.9)
            elif isinstance(tg, WallTarget):
                y = (int(cam.floor_y(0.02)) + int(cam.ceil_y(0.02))) // 2
                count = 55
                self._viewport.trigger(1.2)
            else:  # StepTarget / unknown
                y = int(cam.floor_y(0.02)) - 10
                count = 25
                self._viewport.trigger(0.55)
            self._particles.burst(x, y, tg.color, count)
            self._combo.register_hit(fi)

    def _blank_frame(self) -> np.ndarray:
        """Return a flat ``CLR_BG`` frame.  Used when audio is empty."""
        return np.full((self._height, self._width, 3),
                       CLR_BG, dtype=np.uint8)

    def _compose_frame(self, fi: int) -> np.ndarray:
        """Render one frame at index ``fi``.

        Mirrors the per-frame body of
        :meth:`rhythm.RhythmVisualizer.process_video` (between
        ``game.update`` and the encoder write).  The IPC drain, encoder
        write and progress logging are skipped — the live renderer's
        only output is the returned ndarray.
        """
        # ``game.update`` is called by ``render_at`` before the compose
        # so this method is pure-visual: it READS state and writes a
        # canvas, no side effects on game/particles aside from the
        # particle redraw bookkeeping inside ``particles.draw``.
        # Hits emitted from the most recent ``update`` are stored on
        # the GameManager's ``_pending_hits``; we re-resolve them here
        # so particle bursts spawn at the right pixel position.
        canvas = np.full((self._height, self._width, 3),
                         CLR_BG, dtype=np.uint8)
        # 1. tunnel walls + floor grid
        canvas = self._tunnel.draw(canvas, fi)
        # 1b. side rails
        if self._side_rail is not None:
            _bass = float(self._audio.bass_arr[fi]) if fi < len(self._audio.bass_arr) else 0.0
            self._side_rail.draw(canvas, fi, bass_val=_bass, hit=False)
        # 2. targets (back to front so close cubes occlude far ones)
        for tg in self._game.alive_sorted(fi):
            canvas = tg.draw(canvas, self._cam, fi)
        # 3. particle update + draw.  Bursts for hits at this frame
        # were already triggered inside ``render_at``'s fast-forward
        # loop (see ``_apply_hits``); here we only DRAW the current
        # particle state.
        self._particles.draw(canvas)
        # 5. bloom pass — skipped by default (live preview prioritises
        # frame headroom over post-fx parity with the final render).
        if self._bloom:
            canvas = gpu_glow(canvas, sigma=9.0, gain=0.32)
        # 6. relax-mode camera bob — vertical post-shift to sell the
        # jump / squat dodge.  Same logic as process_video.
        if "relax" in self._modes_seq:
            bob_dy = _relax_camera_dy(self._game.targets, fi, self._height)
            if bob_dy != 0:
                M = np.float32([[1, 0, 0], [0, 1, bob_dy]])
                canvas = cv2.warpAffine(
                    canvas, M, (self._width, self._height),
                    borderValue=(0, 0, 0),
                )
        # 7. HUDs (drawn last, above bloom & camera bob)
        self._viewport.update()
        self._viewport.draw(canvas)
        if self._stick is not None:
            self._stick.draw(canvas, fi)
        self._combo.draw(canvas, fi)
        return canvas
