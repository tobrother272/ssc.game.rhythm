"""
rhythm.py — Rhythm-game tunnel visualization (Beat Saber / Muse Dash style).

Scene:
  - First-person 3D perspective tunnel with neon walls receding to vanishing point
  - 4 lane hit-zone at the bottom
  - 3 target types flying from the vanishing point toward the camera:
        step   — floor squares with foot icon  (green = left, red = right)
        punch  — air cubes with motion tail     (green = left, red = right)
        wall   — laser stripe obstacle across 1-2 lanes
  - Hit VFX: particle burst + rating text pop-up (GOOD/GREAT/PERFECT)
  - Left  HUD: stickman doing walk/punch animation
  - Right HUD: COMBO counter + latest rating

Targets spawn from audio onsets and auto-hit when they reach the hit zone.
"""

import numpy as np
import librosa
import cv2
import math
import random
import argparse
import platform
import time
import sys
import subprocess
import shlex
from pathlib import Path
import ffmpeg

# ── GPU acceleration (CuPy + cuFFT) ──────────────────────────────────────────
try:
    import cupy as cp
    cp.array([1])
    _t = cp.zeros((4, 4), dtype=cp.complex64)
    cp.fft.fft2(_t)
    _CUPY = True
    print("[GPU] CuPy + cuFFT acceleration enabled")
except Exception:
    _CUPY = False
    print("[CPU] CuPy/cuFFT not available – falling back to CPU")


def _ks2s(k: int) -> float:
    return max(0.3 * ((k - 1) * 0.5 - 1) + 0.8, 0.01)


_KERNEL_CACHE: dict = {}


def _get_kernel_fft(sigma_y: float, sigma_x: float, H: int, W: int):
    key = (round(sigma_y, 3), round(sigma_x, 3), H, W)
    if key not in _KERNEL_CACHE:
        ky = min((int(6 * sigma_y + 1) | 1), H if H % 2 == 1 else H - 1)
        kx = min((int(6 * sigma_x + 1) | 1), W if W % 2 == 1 else W - 1)
        y = np.arange(-(ky // 2), ky // 2 + 1, dtype=np.float32)
        x = np.arange(-(kx // 2), kx // 2 + 1, dtype=np.float32)
        gy = np.exp(-0.5 * (y / sigma_y) ** 2); gy /= gy.sum()
        gx = np.exp(-0.5 * (x / sigma_x) ** 2); gx /= gx.sum()
        k2d = np.outer(gy, gx).astype(np.float32)
        pad = np.zeros((H, W), dtype=np.float32)
        pad[:k2d.shape[0], :k2d.shape[1]] = k2d
        pad = np.roll(np.roll(pad, -(k2d.shape[0] // 2), axis=0),
                      -(k2d.shape[1] // 2), axis=1)
        k_gpu = cp.asarray(pad, dtype=cp.complex64)
        _KERNEL_CACHE[key] = cp.fft.fft2(k_gpu)
    return _KERNEL_CACHE[key]


def _fft_blur_gpu(arr_gpu, sigma_y: float, sigma_x: float):
    H, W = arr_gpu.shape[:2]
    k_fft = _get_kernel_fft(sigma_y, sigma_x, H, W)
    if arr_gpu.ndim == 3:
        channels = []
        for c in range(arr_gpu.shape[2]):
            ch = arr_gpu[:, :, c].astype(cp.complex64)
            channels.append(cp.real(cp.fft.ifft2(cp.fft.fft2(ch) * k_fft)).astype(cp.float32))
        return cp.stack(channels, axis=2)
    ch = arr_gpu.astype(cp.complex64)
    return cp.real(cp.fft.ifft2(cp.fft.fft2(ch) * k_fft)).astype(cp.float32)


def gpu_glow(canvas: np.ndarray, sigma: float = 18.0, gain: float = 0.55) -> np.ndarray:
    """Additive bloom glow for whole frame (screen-space bloom)."""
    if not _CUPY:
        ks = max(3, int(6 * sigma + 1) | 1)
        blurred = cv2.GaussianBlur(canvas, (ks, ks), sigma)
        return cv2.addWeighted(canvas, 1.0, blurred, gain, 0)
    g = cp.asarray(canvas, dtype=cp.float32)
    blurred = _fft_blur_gpu(g, sigma, sigma)
    return cp.asnumpy(cp.clip(g + blurred * gain, 0, 255)).astype(np.uint8)


# ── Constants ─────────────────────────────────────────────────────────────────
FPS = 30         # matches reference video, smoother than 24
HOP_LENGTH = 512
BASS_RANGE = 20
IS_MAC = platform.system() == 'Darwin'

# colors BGR
CLR_BG         = (6, 4, 10)
CLR_NEON_CYAN  = (255, 200, 0)
CLR_NEON_PINK  = (230, 50, 255)
CLR_NEON_LIME  = (80, 255, 120)
CLR_NEON_PURPLE= (255, 40, 170)
CLR_GREEN      = (80, 230, 80)
CLR_RED        = (40, 60, 240)
CLR_WHITE      = (250, 250, 250)
CLR_LANE_EDGE  = (160, 140, 90)
CLR_WALL_PINK  = (200, 40, 255)


def _parse_color(s: str | None) -> tuple[int, int, int] | None:
    """Parse a user-supplied color string → BGR tuple for OpenCV.

    Accepted formats (case-insensitive, whitespace tolerant):
      - '#RRGGBB'  or  'RRGGBB'        (hex)
      - 'R,G,B'                        (decimal 0-255, RGB order)

    Returns None if `s` is None / empty.
    Raises ValueError on malformed input.
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    # Hex form
    if s.startswith('#'):
        s = s[1:]
    if len(s) == 6 and all(c in '0123456789abcdefABCDEF' for c in s):
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (b, g, r)      # BGR for OpenCV
    # Comma form
    if ',' in s:
        parts = [p.strip() for p in s.split(',')]
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            r, g, b = (max(0, min(255, int(p))) for p in parts)
            return (b, g, r)
    raise ValueError(
        f"Invalid color '{s}'. Use '#RRGGBB', 'RRGGBB', or 'R,G,B'.")


# game tuning
# Both 'punch' and 'dance' modes now use 4 lanes with strict L↔R body-side
# alternation and sub-lane cycling (see GameManager.pre_schedule).  The
# legacy alias N_LANES_DANCE is kept for backwards-compatible imports.
N_LANES             = 4
N_LANES_DANCE       = 4
# Per-mode floor spread: picks a `floor_spread_frac` so lane 0 and lane N-1
# visually sit under the outermost viewport panels in dance mode.  Viewport
# panels span ±0.368*W × factor at bottom with centers at ±0.276*W (outer)
# and ±0.092*W (inner) → dance needs `lane_half_spread = 0.276*W` →
# `floor_spread_frac = 0.552`.  Punch keeps a slightly tighter spread since
# it doesn't render the 4 viewport panels and has smaller air cubes.
_FLOOR_SPREAD_BY_MODE = {
    'punch': 0.50,
    'dance': 0.552,
}


# Camera bob configuration for relax-mode jump / squat dodges.
# The bob is purely a post-render Y translation of the canvas so the
# HUDs (stickman, combo, debug overlay) can sit on top of the moved
# scene without themselves moving.  The envelope has three phases:
#   • ramp-up   : WINDOW_F frames easing in to the peak at dodge_frame
#   • hold      : peak held until the obstacle has fully left the view
#                 (i.e. hit_frame + travel_f, matching RelaxTarget.is_
#                 dead) so the camera stays dipped / raised while the
#                 stickman is still crouched / airborne
#   • ramp-down : WINDOW_F frames easing back to 0 as the block dies
# This matches the stickman's pose-sustain window, avoiding the jarring
# case where the camera springs back while the avatar is still crouched.
_RELAX_BOB_WINDOW_F     = 8       # ramp-in / ramp-out length (frames)
_RELAX_BOB_HEIGHT_FRAC  = 0.08    # peak offset as a fraction of HEIGHT


def _relax_camera_dy(targets, cur_frame: int, height: int) -> int:
    """Compute the vertical canvas translation for relax-mode camera bob.

    Iterates every live RelaxTarget in its active window and accumulates
    the trapezoidal envelope described above.  LOW slabs (ground
    obstacle → the player JUMPS over them) shift the canvas DOWN
    (positive dy) so the scene appears lower on screen — the classic
    "camera rising as feet leave the ground" effect.  HIGH bars
    (hanging obstacle → the player DUCKS under them) shift the canvas
    UP (negative dy) so the scene appears higher — the "camera dropping
    as head lowers" effect.
    """
    peak_px = int(height * _RELAX_BOB_HEIGHT_FRAC)
    if peak_px <= 0:
        return 0
    total = 0.0
    W = _RELAX_BOB_WINDOW_F
    for t in targets:
        if not isinstance(t, RelaxTarget):
            continue
        if t.kind == 'middle':
            continue
        dodge_f  = t.dodge_frame
        hold_end = t.dodge_end_frame    # end of the sustained pose
        start_f  = dodge_f - W
        if cur_frame < start_f or cur_frame > hold_end:
            continue
        if cur_frame <= dodge_f:
            k = 1.0 - (dodge_f - cur_frame) / float(W)
        elif cur_frame >= hold_end - W:
            k = (hold_end - cur_frame) / float(W)
        else:
            k = 1.0
        k = max(0.0, min(1.0, k))
        if t.kind == 'low':        # JUMP  → canvas drops
            total += peak_px * k
        else:                      # SQUAT → canvas rises
            total -= peak_px * k
    return int(total)


def _parse_modes(spec: str | None) -> list[str]:
    """Parse the ``--mode`` CLI value into a list of gameplay sub-modes.

    Single value stays as-is: ``"punch"`` → ``['punch']``.  Comma-separated
    spec activates **combo mode** where beats alternate cyclically between
    the listed sub-modes: ``"punch,dance"`` → ``['punch', 'dance']`` =
    beat 0 spawns a PunchTarget, beat 1 spawns a DanceTarget, beat 2
    PunchTarget again, ...  Whitespace is trimmed; empty segments ignored.
    Raises ``ValueError`` on unknown sub-modes so the CLI can surface it.
    """
    if not spec:
        return ['punch']
    parts = [m.strip().lower() for m in str(spec).split(',') if m.strip()]
    if not parts:
        return ['punch']
    valid = {'punch', 'dance', 'line', 'relax'}
    bad = [p for p in parts if p not in valid]
    if bad:
        raise ValueError(
            f"Unknown mode(s) {bad}; allowed: 'punch', 'dance', 'line', "
            f"'relax', or comma-combined e.g. 'punch,dance,relax'.")
    return parts


def _parse_lanes(spec: str | None, n_lanes: int) -> set[int] | None:
    """Parse a CLI lane-filter string like "1,2" / "1,4" / "1,2,3,4" into a
    set of 0-based lane indices.

    Accepts 1-based indices (human-friendly, matches how the user thinks
    about lanes in the UI) and converts them to the 0-based indices the
    scheduler uses internally.  Supports:
      - comma-separated list: "1,2,4"
      - range notation:       "1-3"  →  {0, 1, 2}
      - mixed:                "1,3-4"
      - empty / None / "all": returns None (= no filter, all lanes active)

    Invalid entries (non-numeric or out-of-range) raise ValueError with a
    clear diagnostic so the caller can surface it via CLI exit.
    """
    if spec is None:
        return None
    s = spec.strip().lower()
    if not s or s in ('all', '*', '0'):
        return None
    out: set[int] = set()
    for tok in s.split(','):
        tok = tok.strip()
        if not tok:
            continue
        if '-' in tok:
            a, _, b = tok.partition('-')
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise ValueError(f"Invalid lane range '{tok}' in --lanes.")
            if lo > hi:
                lo, hi = hi, lo
            for v in range(lo, hi + 1):
                out.add(v - 1)
        else:
            try:
                out.add(int(tok) - 1)
            except ValueError:
                raise ValueError(f"Invalid lane index '{tok}' in --lanes.")
    for v in out:
        if v < 0 or v >= n_lanes:
            raise ValueError(
                f"Lane {v + 1} out of range — valid lanes are 1..{n_lanes}.")
    return out if out else None


def _parse_beat_times(args) -> list[float]:
    """Resolve ``--beat_times`` / ``--beats_file`` into a sorted/deduped list.

    Returns an empty list when ``--beat_source`` is anything other than
    ``array`` (other sources don't consume this field).  Otherwise:

      * Exactly one of ``--beat_times`` / ``--beats_file`` MUST be set.
      * ``--beat_times`` is comma-separated seconds, e.g.
        ``"1.20, 1.85, 2.4"`` (whitespace tolerated).
      * ``--beats_file`` is a JSON file containing a flat list of numbers,
        e.g. ``[1.20, 1.85, 2.4]`` — no other schema is supported by
        design (one canonical format keeps this simple).
      * Negative values are rejected (a beat at t<0 makes no sense and is
        almost certainly a bug in the caller).  Out-of-range values
        ``>= duration`` are silently clipped later inside the visualiser
        (we don't know the duration here yet).
      * Result is sorted ascending and de-duplicated to within 0.5 ms so
        floating-point round-trips through JSON don't double-fire blocks.

    Raises
    ------
    SystemExit
        Via ``sys.exit(1)`` with a helpful CLI message when the inputs
        are inconsistent (both/none provided in array mode, malformed
        JSON, non-numeric tokens, empty list, …).  We exit instead of
        raising because this runs from ``main()`` where the caller has
        no way to recover.
    """
    if args.beat_source != 'array':
        if args.beat_times is not None or args.beats_file is not None:
            print("[--beat_times/--beats_file] ignored "
                  "(only used when --beat_source=array).")
        return []

    if (args.beat_times is None) == (args.beats_file is None):
        if args.beat_times is None:
            print("[--beat_source=array] requires either --beat_times "
                  "(inline list) or --beats_file (JSON path).")
        else:
            print("[--beat_source=array] supply EITHER --beat_times OR "
                  "--beats_file, not both.")
        sys.exit(1)

    import json

    raw: list[float] = []
    if args.beat_times is not None:
        for tok in str(args.beat_times).split(','):
            tok = tok.strip()
            if not tok:
                continue
            try:
                raw.append(float(tok))
            except ValueError:
                print(f"[--beat_times] '{tok}' is not a number.")
                sys.exit(1)
    else:
        try:
            with open(args.beats_file, 'r', encoding='utf-8') as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[--beats_file] cannot read '{args.beats_file}': {exc}")
            sys.exit(1)
        if not isinstance(payload, list):
            print(f"[--beats_file] expected a JSON array of numbers, "
                  f"got {type(payload).__name__}.")
            sys.exit(1)
        for i, v in enumerate(payload):
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                print(f"[--beats_file] entry #{i} is not a number: {v!r}")
                sys.exit(1)
            raw.append(float(v))

    if not raw:
        print("[--beat_source=array] beat list is empty.")
        sys.exit(1)

    for v in raw:
        if v < 0.0:
            print(f"[--beat_source=array] negative time {v} not allowed.")
            sys.exit(1)

    raw.sort()
    deduped: list[float] = []
    for v in raw:
        if not deduped or (v - deduped[-1]) > 5e-4:  # 0.5 ms
            deduped.append(v)
    if len(deduped) != len(raw):
        print(f"[--beat_source=array] dropped {len(raw) - len(deduped)} "
              f"near-duplicate time(s) (within 0.5 ms).")
    return deduped


TARGET_TRAVEL_FRAMES = 40    # how many frames a target takes to cross from z=1 → z=0
SPAWN_COOLDOWN       = 3
ONSET_SPAWN_THRESH   = 0.35
WALL_SPAWN_PROB      = 0.0   # punch mode no longer spawns full-tunnel walls
HIT_Z_THRESHOLD      = 0.08  # z below this is considered "at hit zone"


# ── PerspectiveCamera (with 3D projection) ────────────────────────────────────
class PerspectiveCamera:
    """Combined 2D-UI + 3D-world camera.

    2-D interface (legacy, used by HUD / flashes):
      lane_x(lane, z_norm), floor_y(z_norm), air_y(z_norm), wall_x(side, z_norm)
      – z_norm ∈ [0,1]: 0 = hit zone, 1 = vanishing point.

    3-D interface (new, used for cubes + floor panels):
      project(wx, wy, wz) → (sx, sy, depth_scale)
      lane_world_x(lane), z_from_norm(z_norm)
      – camera at origin, looking +Z; +X right, +Y down (image convention).
    """

    def __init__(self, W: int, H: int,
                 n_lanes: int = N_LANES,
                 horizon_frac: float = 0.45,
                 hit_zone_frac: float = 0.86,
                 floor_spread_frac: float = 0.70,
                 far_spread_frac: float | None = None,
                 wall_floor_gap_frac: float | None = None,
                 fov_deg: float = 55.0):
        self.W = W
        self.H = H
        self.cx = W // 2
        self.cy_v = int(H * horizon_frac)          # horizon / vanishing y
        self.y_hit = int(H * hit_zone_frac)        # floor at camera
        self.lane_half_spread = int(W * floor_spread_frac * 0.5)
        self.n_lanes = n_lanes
        # Lane x-positions at the bottom (z=0)
        step = (2 * self.lane_half_spread) / (n_lanes - 1) if n_lanes > 1 else 0
        self.lane_x_bottom = [self.cx - self.lane_half_spread + i * step
                              for i in range(n_lanes)]

        # --- 3D world calibration --------------------------------------
        # Pinhole projection: sx = cx + fx * wx / wz
        self.fov_deg = fov_deg
        self.fx = W / 2 / math.tan(math.radians(fov_deg) / 2)
        self.fy = self.fx                              # square pixels
        # Image projection center aligned with horizon to match tunnel look
        self.cx_pix = W / 2
        self.cy_pix = float(self.cy_v)

        # World distances (arbitrary "meters"). Near = hit zone, Far = horizon.
        self.Z_NEAR = 2.5
        self.Z_FAR  = 28.0
        # Solve LANE_WORLD_X so that at Z_NEAR the projection hits the existing
        # lane_x_bottom pixel offset (= 2D/3D are visually consistent).
        self.LANE_WORLD_X = self.lane_half_spread * self.Z_NEAR / self.fx
        # Cubes float near eye-level (slightly below for "punch height" feel)
        self.AIR_WORLD_Y = 0.05
        # Floor y-world s.t. at Z_NEAR floor_y projects near y_hit
        self.FLOOR_WORLD_Y = (
            (self.y_hit - self.cy_pix) * self.Z_NEAR / self.fy)
        # Tunnel-wall x at bottom of frame (~0.52*W each side)
        self.WALL_WORLD_X = (self.W * 0.52) * self.Z_NEAR / self.fx
        # Wall bottom Y in world space.
        # When `wall_floor_gap_frac` is provided (including 0.0) the editor
        # is taking over the wall-floor relationship:
        #   gap_pixels   = wall_floor_gap_frac * H
        #   wall_screen_y = y_hit - gap_pixels
        #   WALL_BOTTOM_WORLD_Y = (wall_screen_y - cy_pix) * Z_NEAR / fy
        # gap=0 ⇒ wall bottom on the floor; gap>0 ⇒ wall lifted above floor.
        # When None ⇒ legacy behaviour (renderers fall back to default).
        if wall_floor_gap_frac is not None:
            gap_pix = float(wall_floor_gap_frac) * H
            wall_screen_y = self.y_hit - gap_pix
            self.WALL_BOTTOM_WORLD_Y: float | None = (
                (wall_screen_y - self.cy_pix) * self.Z_NEAR / self.fy)
        else:
            self.WALL_BOTTOM_WORLD_Y = None

        # Independent far-end spread: when set, the X projection is
        # linearly interpolated between 1.0 (at Z_NEAR) and the
        # far_spread/near_spread ratio (at Z_FAR).  This lets the user
        # set how wide/narrow the tunnel appears at the horizon
        # independently from the near (floor) width.
        if far_spread_frac is not None and floor_spread_frac > 0:
            self._x_stretch_far = float(far_spread_frac) / float(floor_spread_frac)
        else:
            self._x_stretch_far = None  # standard perspective, no stretch

    # ---------- 3D projection ----------
    def project(self, wx: float, wy: float, wz: float):
        """Project world point → (sx, sy, depth_scale). None if behind cam."""
        if wz <= 0.05:
            return None
        # Apply depth-based x stretch when far_spread_frac was provided.
        # t=0 at Z_NEAR (stretch=1.0) → t=1 at Z_FAR (stretch=_x_stretch_far).
        if self._x_stretch_far is not None:
            t = max(0.0, min(1.0, (wz - self.Z_NEAR) / (self.Z_FAR - self.Z_NEAR)))
            wx = wx * (1.0 + t * (self._x_stretch_far - 1.0))
        sx = self.cx_pix + self.fx * wx / wz
        sy = self.cy_pix + self.fy * wy / wz
        return (sx, sy, 1.0 / wz)

    def lane_world_x(self, lane) -> float:
        """World-X for a (possibly fractional) lane index.

        lane 0 → -LANE_WORLD_X,  lane 1 → +LANE_WORLD_X  (for N_LANES=2).
        Fractional lanes interpolate linearly between extremes.
        """
        if self.n_lanes <= 1:
            return 0.0
        t = float(lane) / (self.n_lanes - 1)           # 0..1
        return -self.LANE_WORLD_X + 2 * self.LANE_WORLD_X * t

    # Global mapping mode: 'linear' (block travels at constant world-speed,
    # stays tiny at distance for most of the travel and only zooms in near
    # the end – matches the reference video) or 'inv' (legacy 1/z mapping
    # which grows on screen at uniform rate).
    DEPTH_MODE = 'linear'

    def z_from_norm(self, z_norm: float) -> float:
        """Normalized depth (0=near,1=far) → world z."""
        z_norm = max(0.0, min(1.0, z_norm))
        if self.DEPTH_MODE == 'inv':
            inv_near = 1.0 / self.Z_NEAR
            inv_far  = 1.0 / self.Z_FAR
            inv_z    = inv_far + (inv_near - inv_far) * (1 - z_norm)
            return 1.0 / inv_z
        # linear in world-z: block moves toward camera at constant world
        # speed.  Perspective naturally produces the slow-far + zoom-near
        # feel of the reference.
        return self.Z_NEAR + z_norm * (self.Z_FAR - self.Z_NEAR)

    def lane_x(self, lane: float, z: float) -> float:
        """X-coord for a (possibly fractional) lane at depth z."""
        lane_i = int(round(lane))
        if 0 <= lane_i < self.n_lanes:
            bot_x = self.lane_x_bottom[lane_i]
        else:
            # extrapolate for fractional / out-of-range lanes
            step = (self.lane_x_bottom[-1] - self.lane_x_bottom[0]) / (self.n_lanes - 1)
            bot_x = self.lane_x_bottom[0] + lane * step
        converge = (1 - z) ** 1.0
        return self.cx + (bot_x - self.cx) * converge

    def floor_y(self, z: float) -> float:
        """Y-coord on the floor plane (y_world = 0) at depth z."""
        t = (1 - z) ** 1.6
        return self.cy_v + t * (self.y_hit - self.cy_v)

    def air_y(self, z: float, height_frac: float = 0.45) -> float:
        """Y-coord for a floating object at world height (above floor).

        height_frac is how far up from horizon the object appears at z=0.
        """
        fy = self.floor_y(z)
        # offset from floor up to horizon; object sits `height_frac` way up
        offset = (fy - self.cy_v) * height_frac
        return fy - offset

    def scale(self, z: float) -> float:
        """Pixel scale factor for an object at depth z (1.0 at z=0)."""
        return max(0.02, (1 - z) ** 1.3)

    # ---- tunnel wall points ----
    def wall_x(self, side: int, z: float) -> float:
        """X of the tunnel wall at depth z. side=-1 left, +1 right."""
        edge_bottom_x = self.cx + side * int(self.W * 0.52)
        converge = (1 - z) ** 1.0
        return self.cx + (edge_bottom_x - self.cx) * converge

    def ceil_y(self, z: float) -> float:
        """Top-of-wall y at depth z (mirror of floor above horizon)."""
        t = (1 - z) ** 1.6
        top = int(self.H * 0.10)
        return self.cy_v - t * (self.cy_v - top)


# ── SegmentBackgroundLayer ───────────────────────────────────────────────────
class SegmentBackgroundLayer:
    """Background fill layer drawn under all segment elements."""

    def __init__(
        self,
        width: int,
        height: int,
        *,
        bg_type: str = "solid",
        color: str = "#000000",
        image_path: str | None = None,
        video_path: str | None = None,
        fps: float = 30.0,
    ) -> None:
        self._w = int(width)
        self._h = int(height)
        self._fps = max(1e-6, float(fps))
        t = str(bg_type or "solid").strip().lower()
        self._type = t if t in {"solid", "image", "video"} else "solid"
        self._solid_bgr = _hex_to_bgr(color, default=CLR_BG)
        self._image: np.ndarray | None = None
        self._cap: cv2.VideoCapture | None = None
        self._video_fps = self._fps
        self._video_frames = 0
        self._last_src_idx = -1
        self._last_frame: np.ndarray | None = None

        if self._type == "image" and image_path:
            try:
                img = cv2.imread(str(image_path))
                if img is not None:
                    self._image = cv2.resize(
                        img, (self._w, self._h), interpolation=cv2.INTER_AREA
                    )
            except Exception:
                self._image = None
        elif self._type == "video" and video_path:
            try:
                cap = cv2.VideoCapture(str(video_path))
                if cap is not None and cap.isOpened():
                    self._cap = cap
                    vf = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                    if vf > 1e-3:
                        self._video_fps = vf
                    self._video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                else:
                    if cap is not None:
                        cap.release()
            except Exception:
                self._cap = None

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _solid_frame(self) -> np.ndarray:
        return np.full((self._h, self._w, 3), self._solid_bgr, dtype=np.uint8)

    def frame(self, frame_idx: int) -> np.ndarray:
        if self._type == "image" and self._image is not None:
            return self._image.copy()
        if self._type == "video" and self._cap is not None:
            src_idx = int(round((float(frame_idx) / self._fps) * self._video_fps))
            if self._video_frames > 0:
                src_idx = max(0, min(self._video_frames - 1, src_idx))
            if src_idx != self._last_src_idx:
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, float(src_idx))
                ok, frm = self._cap.read()
                if ok and frm is not None:
                    if frm.shape[1] != self._w or frm.shape[0] != self._h:
                        frm = cv2.resize(
                            frm, (self._w, self._h), interpolation=cv2.INTER_LINEAR
                        )
                    self._last_frame = frm
                    self._last_src_idx = src_idx
            if self._last_frame is not None:
                return self._last_frame.copy()
        return self._solid_frame()


# ── TunnelRenderer ────────────────────────────────────────────────────────────
class TunnelRenderer:
    """Draws the receding 3D tunnel: floor grid + side walls with neon strips."""

    def __init__(self, cam: PerspectiveCamera, show_floor_panels: bool = True,
                 lane_tiles: bool = False,
                 floor_panel_color: str | None = None,
                 floor_panel_opacity: float = 1.0,
                 floor_panel_blink: bool = False,
                 floor_panel_image: str | None = None,
                 floor_full_static_image: bool = False,
                 floor_layout: str = 'auto',
                 floor_bg_color: str | None = None,
                 floor_bg_opacity: float = 1.0,
                 chevron_color: str = '#FFD700',
                 chevron_scroll: bool = True,
                 chevron_blink: bool = False,
                 chevron_width_frac: float = 0.45,
                 chevron_count: int = 6):
        """
        `lane_tiles`: when True, floor panels are drawn directly UNDER each
        lane (one column of tiles per lane, derived from `cam.lane_world_x`).
        Used by dance mode so the 4 lanes have visible "runway" tiles.
        When False (default), uses the original 2 columns flanking the
        center — matches the original punch-mode look.

        `floor_panel_color`: optional hex string ("#RRGGBB") to override the
        default grey neon color of floor tiles.
        `floor_panel_blink`: when True tiles flash on/off each beat (derived
        from a fixed half-second period so the preview matches the render).
        `floor_panel_image`: optional image path; when set the image is
        perspective-warped onto each tile instead of the flat grey fill.
        `floor_full_static_image`: when True AND `floor_panel_image` is set,
        the image is stretched (perspective-warped) onto the FULL floor
        trapezoid as one static graphic — chevrons, tiles, BG color, blink
        and opacity are all bypassed.  Has no effect when no image is loaded.
        `floor_layout`: 'auto' (legacy lane_tiles / 2-column) or
        'chevron_strip' (single centre column of >>>-arrow shapes).
        `floor_bg_color`: hex "#RRGGBB" solid trapezoid drawn UNDER tiles;
        None = transparent (default canvas black).
        `chevron_*`: colour / animation / geometry of the chevron strip.
        """
        self.cam = cam
        self.show_floor_panels = show_floor_panels
        self.lane_tiles = lane_tiles
        self.floor_panel_color = floor_panel_color
        self.floor_panel_opacity = float(max(0.0, min(1.0, floor_panel_opacity)))
        self.floor_panel_blink = floor_panel_blink
        self.floor_layout = str(floor_layout)
        self.floor_bg_color = floor_bg_color or None
        self.floor_bg_opacity = float(max(0.0, min(1.0, floor_bg_opacity)))
        self.chevron_color = str(chevron_color)
        self.chevron_scroll = bool(chevron_scroll)
        self.chevron_blink = bool(chevron_blink)
        self.chevron_width_frac = float(max(0.1, min(2.0, chevron_width_frac)))
        self.chevron_count = int(max(3, min(12, chevron_count)))
        self.floor_full_static_image = bool(floor_full_static_image)
        # Pre-load the tile image (BGR) so we don't re-read on every frame.
        self._tile_img: "np.ndarray | None" = None
        if floor_panel_image:
            try:
                img = cv2.imread(floor_panel_image)
                if img is not None:
                    self._tile_img = img
            except Exception:
                pass

    def draw(self, canvas: np.ndarray, frame: int) -> np.ndarray:
        """Dark 3D tunnel with floor panels receding toward the horizon.

        Uses the camera's 3D projection so floor tiles are true trapezoid
        perspective (not hand-faked), matching the CapCut reference.

        Z-order (bottom to top):
          1. Floor BG trapezoid (solid color, optional).
          2. Floor tiles / chevron strip (show_floor_panels gate).
          3. Horizon glow line (atmospherics).
        """
        cam = self.cam

        # ── Phase 0: Full static image short-circuit ───────────────────
        # When enabled with an image loaded, render JUST the image stretched
        # onto the whole floor trapezoid and skip every other floor effect.
        if self.floor_full_static_image and self._tile_img is not None:
            self._draw_floor_full_static(canvas)
            y_hz = self._runway_horizon_y()
            cv2.line(canvas, (0, y_hz), (cam.W, y_hz), (70, 60, 80), 1,
                     lineType=cv2.LINE_AA)
            return canvas

        # ── Phase A: Floor BG trapezoid (solid color under tiles) ──────
        if self.floor_bg_color:
            self._draw_floor_bg(canvas)

        # ── Phase B: Floor tiles or chevron strip ───────────────────────
        if self.show_floor_panels:
            if self.floor_layout == 'chevron_strip':
                self._draw_chevron_strip(canvas, frame)
            else:
                self._draw_floor_tiles_legacy(canvas, frame)

        # ── Phase C: Faint horizon / runway glow line (atmospherics) ───
        y_hz = self._runway_horizon_y()
        cv2.line(canvas, (0, y_hz), (cam.W, y_hz), (70, 60, 80), 1,
                 lineType=cv2.LINE_AA)

        return canvas

    # ── helpers ──────────────────────────────────────────────────────────

    def _runway_horizon_y(self) -> int:
        """Y of the runway far edge; keeps glow line aligned to floor."""
        cam = self.cam
        if cam.n_lanes >= 2:
            outer_left = cam.lane_world_x(0)
            outer_right = cam.lane_world_x(cam.n_lanes - 1)
            half_step = abs(outer_right - outer_left) / max(1, cam.n_lanes - 1) * 0.5
        else:
            outer_left, outer_right, half_step = -0.95, +0.95, 0.5
        x_left = outer_left - half_step
        x_right = outer_right + half_step
        p_l = cam.project(x_left, cam.FLOOR_WORLD_Y, cam.Z_FAR)
        p_r = cam.project(x_right, cam.FLOOR_WORLD_Y, cam.Z_FAR)
        if p_l is None or p_r is None:
            return int(cam.cy_pix + 2)
        return int(round((float(p_l[1]) + float(p_r[1])) * 0.5))

    def _draw_floor_bg(self, canvas: np.ndarray) -> None:
        """Fill a (possibly translucent) trapezoid covering the full runway."""
        cam = self.cam
        bgr = _hex_to_bgr(self.floor_bg_color, default=(140, 26, 90))

        if cam.n_lanes >= 2:
            outer_left  = cam.lane_world_x(0)
            outer_right = cam.lane_world_x(cam.n_lanes - 1)
            half_step   = abs(outer_right - outer_left) / max(1, cam.n_lanes - 1) * 0.5
        else:
            outer_left, outer_right, half_step = -0.95, +0.95, 0.5
        x_left  = outer_left  - half_step
        x_right = outer_right + half_step

        corners_w = [
            (x_left,  cam.FLOOR_WORLD_Y, cam.Z_NEAR),
            (x_right, cam.FLOOR_WORLD_Y, cam.Z_NEAR),
            (x_right, cam.FLOOR_WORLD_Y, cam.Z_FAR),
            (x_left,  cam.FLOOR_WORLD_Y, cam.Z_FAR),
        ]
        proj = [cam.project(*c) for c in corners_w]
        if any(p is None for p in proj):
            return
        poly = np.array(
            [(int(round(p[0])), int(round(p[1]))) for p in proj],
            dtype=np.int32,
        )
        opacity = self.floor_bg_opacity
        if opacity >= 1.0:
            cv2.fillConvexPoly(canvas, poly, bgr, lineType=cv2.LINE_AA)
        elif opacity > 0.0:
            overlay = canvas.copy()
            cv2.fillConvexPoly(overlay, poly, bgr, lineType=cv2.LINE_AA)
            cv2.addWeighted(overlay, opacity, canvas, 1.0 - opacity, 0, canvas)

    def _draw_floor_full_static(self, canvas: np.ndarray) -> None:
        """Stretch ``self._tile_img`` to the full runway trapezoid (one shot)."""
        cam = self.cam
        if self._tile_img is None:
            return

        if cam.n_lanes >= 2:
            outer_left  = cam.lane_world_x(0)
            outer_right = cam.lane_world_x(cam.n_lanes - 1)
            half_step   = abs(outer_right - outer_left) / max(1, cam.n_lanes - 1) * 0.5
        else:
            outer_left, outer_right, half_step = -0.95, +0.95, 0.5
        x_left  = outer_left  - half_step
        x_right = outer_right + half_step

        corners_w = [
            (x_left,  cam.FLOOR_WORLD_Y, cam.Z_NEAR),   # near-left
            (x_right, cam.FLOOR_WORLD_Y, cam.Z_NEAR),   # near-right
            (x_right, cam.FLOOR_WORLD_Y, cam.Z_FAR),    # far-right
            (x_left,  cam.FLOOR_WORLD_Y, cam.Z_FAR),    # far-left
        ]
        proj = [cam.project(*c) for c in corners_w]
        if any(p is None for p in proj):
            return
        dst_pts = np.array(
            [(float(p[0]), float(p[1])) for p in proj],
            dtype=np.float32,
        )

        ih, iw = self._tile_img.shape[:2]
        # Source corners ordered to match dst: TL, TR, BR, BL.
        # Image y=0 is "top" / far end of the floor; y=ih is "bottom" / near.
        src_pts = np.array(
            [[0,   ih], [iw, ih], [iw, 0], [0,  0]],
            dtype=np.float32,
        )
        M = cv2.getPerspectiveTransform(src_pts, dst_pts)
        warped = cv2.warpPerspective(
            self._tile_img, M, (canvas.shape[1], canvas.shape[0])
        )
        poly_int = dst_pts.astype(np.int32)
        mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
        cv2.fillConvexPoly(mask, poly_int, 255)
        canvas[mask > 0] = warped[mask > 0]

    def _draw_chevron_strip(self, canvas: np.ndarray, frame: int) -> None:
        """Draw a single column of >>>-arrows down the centre of the runway."""
        cam = self.cam

        if self.chevron_blink and (frame // 15) % 2 == 1:
            return

        bgr = _hex_to_bgr(self.chevron_color, default=(0, 215, 255))

        spacing = (cam.Z_FAR - cam.Z_NEAR) / max(1, self.chevron_count)
        z_slots = [cam.Z_NEAR + i * spacing for i in range(self.chevron_count)]
        scroll  = ((frame * 0.30) % spacing) if self.chevron_scroll else 0.0

        if cam.n_lanes >= 2:
            spread = abs(cam.lane_world_x(cam.n_lanes - 1) - cam.lane_world_x(0))
        else:
            spread = 1.9
        half_w    = spread * self.chevron_width_frac * 0.5
        arrow_len = 1.2

        polys: list[tuple[float, "np.ndarray", float]] = []
        for z_c in z_slots:
            wz = z_c - scroll
            if wz <= cam.Z_NEAR + 0.05:
                continue
            z_tip  = wz - arrow_len * 0.5
            z_base = wz + arrow_len * 0.5
            z_mid  = wz
            # Clamp the near-end vertices so they never project outside
            # (below) the floor-background trapezoid boundary at Z_NEAR.
            _z_floor = cam.Z_NEAR + 0.05
            z_tip_c       = max(_z_floor, z_tip)
            z_tip_inner_c = max(_z_floor, z_tip + 0.45)
            # 8-vertex notched chevron (outer ring CCW then inner ring CW)
            corners_w = [
                (-half_w,        cam.FLOOR_WORLD_Y, z_base),
                (-half_w * 0.55, cam.FLOOR_WORLD_Y, z_mid),
                (0.0,            cam.FLOOR_WORLD_Y, z_tip_c),
                (+half_w * 0.55, cam.FLOOR_WORLD_Y, z_mid),
                (+half_w,        cam.FLOOR_WORLD_Y, z_base),
                (+half_w * 0.65, cam.FLOOR_WORLD_Y, z_base + 0.05),
                (0.0,            cam.FLOOR_WORLD_Y, z_tip_inner_c),
                (-half_w * 0.65, cam.FLOOR_WORLD_Y, z_base + 0.05),
            ]
            proj = [cam.project(*c) for c in corners_w]
            if any(p is None for p in proj):
                continue
            depth_factor = max(0.15, min(1.0, 5.0 / wz))
            pts = np.array(
                [(int(round(p[0])), int(round(p[1]))) for p in proj],
                dtype=np.int32,
            )
            polys.append((wz, pts, depth_factor))

        polys.sort(key=lambda t: -t[0])   # far first

        for _, pts, df in polys:
            fill = tuple(int(c * (0.35 + 0.65 * df)) for c in bgr)
            cv2.fillPoly(canvas, [pts], fill, lineType=cv2.LINE_AA)
            rim = tuple(int(min(255, c * (0.55 + 0.85 * df))) for c in bgr)
            cv2.polylines(canvas, [pts], True, rim, 1, lineType=cv2.LINE_AA)

    def _draw_floor_tiles_legacy(self, canvas: np.ndarray, frame: int) -> None:
        """Original floor-tile rendering (lane_tiles or 2-column). Pixel-identical."""
        cam = self.cam

        # -- Blink: hide tiles on odd half-seconds --------------------
        if self.floor_panel_blink and (frame // 15) % 2 == 1:
            return

        # -- Floor panels (receding rows of grey tiles) ----------------
        # `lane_tiles` mode: one tile column per lane (dance mode, 4
        # lanes under the 4 viewport panels).  Legacy mode: two columns
        # flanking center (punch mode).
        tile_len  = 1.6                        # length along z
        if self.lane_tiles and cam.n_lanes >= 2:
            x_centers = tuple(cam.lane_world_x(i)
                              for i in range(cam.n_lanes))
            # Tile half-width = 80% of lane spacing so adjacent rows
            # almost touch but keep a visible neon seam between them.
            step = abs(cam.lane_world_x(1) - cam.lane_world_x(0))
            tile_w = max(0.25, step * 0.80)
        else:
            x_centers = (-0.95, +0.95)
            tile_w    = 0.55
        z_slots   = [3.0, 5.5, 8.5, 12.5, 17.5]
        scroll    = (frame * 0.30) % (z_slots[1] - z_slots[0])

        # Resolve the glow color: custom hex or neutral grey default.
        if self.floor_panel_color:
            try:
                hx = self.floor_panel_color.lstrip("#")
                r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
                floor_glow_color = np.array([b, g, r], dtype=np.float32)
            except Exception:
                floor_glow_color = np.array([170, 175, 180], dtype=np.float32)
        else:
            # Neutral grey neon for the floor panels (same hue as the tile
            # fill, just brighter) – keeps the ground reading as ground while
            # the punch cubes own the saturated green/red accent colors.
            floor_glow_color = np.array([170, 175, 180], dtype=np.float32)

        floor_polys = []
        for lane_i, xc in enumerate(x_centers):
            for z_c in z_slots:
                wz = z_c - scroll
                if wz < cam.Z_NEAR + 0.2:      # don't clip front edge
                    continue
                corners = [
                    (xc - tile_w / 2, cam.FLOOR_WORLD_Y, wz - tile_len / 2),
                    (xc + tile_w / 2, cam.FLOOR_WORLD_Y, wz - tile_len / 2),
                    (xc + tile_w / 2, cam.FLOOR_WORLD_Y, wz + tile_len / 2),
                    (xc - tile_w / 2, cam.FLOOR_WORLD_Y, wz + tile_len / 2),
                ]
                proj = [cam.project(*c) for c in corners]
                if any(p is None for p in proj):
                    continue
                depth_factor = max(0.08, min(1.0, 5.0 / wz))
                poly = np.array([(int(p[0]), int(p[1])) for p in proj],
                                dtype=np.int32)
                floor_polys.append((wz, poly, depth_factor, lane_i))

        floor_polys.sort(key=lambda t: -t[0])  # far first

        # Pass 1: fill each tile — image-warp or flat color.
        if self._tile_img is not None:
            # Perspective-warp the source image onto each tile poly.
            ih, iw = self._tile_img.shape[:2]
            src_pts = np.array([[0, 0], [iw, 0], [iw, ih], [0, ih]],
                               dtype=np.float32)
            for _, poly, df, _ in floor_polys:
                dst_pts = poly.astype(np.float32)
                M = cv2.getPerspectiveTransform(src_pts, dst_pts)
                warped = cv2.warpPerspective(
                    self._tile_img, M, (canvas.shape[1], canvas.shape[0])
                )
                # Blend warped image into canvas only inside tile poly.
                mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
                cv2.fillPoly(mask, [poly], 255)
                alpha = min(1.0, 0.25 + 0.75 * df) * self.floor_panel_opacity
                canvas[mask > 0] = cv2.addWeighted(
                    canvas, 1.0 - alpha, warped, alpha, 0
                )[mask > 0]
        else:
            for _, poly, df, _ in floor_polys:
                if self.floor_panel_color:
                    # Tint with custom color, scale by depth.
                    fill = tuple(int(c * df * 0.4) for c in
                                 (int(floor_glow_color[0]),
                                  int(floor_glow_color[1]),
                                  int(floor_glow_color[2])))
                else:
                    base = int(45 * df)
                    fill = (base, base + 2, base + 2)
                if self.floor_panel_opacity >= 1.0:
                    cv2.fillPoly(canvas, [poly], fill, lineType=cv2.LINE_AA)
                elif self.floor_panel_opacity > 0.0:
                    overlay = canvas.copy()
                    cv2.fillPoly(overlay, [poly], fill, lineType=cv2.LINE_AA)
                    cv2.addWeighted(
                        overlay,
                        self.floor_panel_opacity,
                        canvas,
                        1.0 - self.floor_panel_opacity,
                        0,
                        canvas,
                    )

        # Pass 2: rim outline.
        if self.lane_tiles:
            for _, poly, df, _ in floor_polys:
                c = int((60 + 60 * df) * self.floor_panel_opacity)
                cv2.polylines(canvas, [poly], True, (c, c, c), 1,
                              lineType=cv2.LINE_AA)
        else:
            for _, poly, df, _ in floor_polys:
                glow = floor_glow_color * (0.40 + 0.60 * df) * self.floor_panel_opacity
                thickness = max(1, int(round(1 + df * 1.2)))
                _draw_neon_edges(canvas, [poly], glow, thickness)

    def draw_hit_zone(self, canvas: np.ndarray) -> np.ndarray:
        cam = self.cam
        z = 0.02
        y = int(cam.floor_y(z))
        y_back = int(cam.floor_y(0.12))
        for i in range(cam.n_lanes):
            l_x0 = int(cam.lane_x(i - 0.5, z))
            l_x1 = int(cam.lane_x(i + 0.5, z))
            l_xb0 = int(cam.lane_x(i - 0.5, 0.12))
            l_xb1 = int(cam.lane_x(i + 0.5, 0.12))
            poly = np.array([(l_x0, y), (l_x1, y),
                             (l_xb1, y_back), (l_xb0, y_back)], dtype=np.int32)
            # translucent fill
            overlay = canvas.copy()
            cv2.fillPoly(overlay, [poly], (50, 40, 15))
            canvas = cv2.addWeighted(overlay, 0.55, canvas, 0.45, 0)
            cv2.polylines(canvas, [poly], True, CLR_LANE_EDGE, 2, lineType=cv2.LINE_AA)
        return canvas


# ── Target base + subclasses ─────────────────────────────────────────────────
class Target:
    """Base flying target. Subclasses define visual + hit y-placement."""

    HIT_DEPTH = 0.0

    # Class-level color overrides (BGR tuples). None = use built-in defaults
    # (CLR_GREEN for left, CLR_RED for right).
    COLOR_LEFT:  tuple | None = None
    COLOR_RIGHT: tuple | None = None

    def __init__(self, spawn_frame: int, hit_frame: int, lane: int,
                 is_left: bool):
        self.spawn_frame = spawn_frame
        self.hit_frame   = hit_frame
        self.lane        = lane
        self.is_left     = is_left
        if is_left:
            self.color = self.COLOR_LEFT if self.COLOR_LEFT is not None else CLR_GREEN
        else:
            self.color = self.COLOR_RIGHT if self.COLOR_RIGHT is not None else CLR_RED
        self.state       = 'flying'   # 'flying' | 'hit' | 'dead'
        self.hit_exec_f  = -1

    def depth(self, cur_frame: int) -> float:
        if cur_frame <= self.spawn_frame:
            return 1.0
        if cur_frame >= self.hit_frame:
            return 0.0
        p = (cur_frame - self.spawn_frame) / (self.hit_frame - self.spawn_frame)
        return 1.0 - p

    def check_hit(self, cur_frame: int) -> bool:
        """If reached hit zone and still flying → mark hit (return True)."""
        if self.state != 'flying':
            return False
        if cur_frame >= self.hit_frame:
            self.state = 'hit'
            self.hit_exec_f = cur_frame
            return True
        return False

    def is_dead(self, cur_frame: int) -> bool:
        return self.state in ('hit', 'dead') and cur_frame - self.hit_exec_f > 2

    def draw(self, canvas: np.ndarray, cam: PerspectiveCamera, cur_frame: int):
        raise NotImplementedError


class StepTarget(Target):
    """Floor-slab target. Steps with left/right foot indicator."""

    def draw(self, canvas, cam, cur_frame):
        if self.state != 'flying':
            return canvas
        z = self.depth(cur_frame)
        x_c = int(cam.lane_x(self.lane, z))
        y_c = int(cam.floor_y(z))
        s = cam.scale(z)
        w = int(cam.W * 0.12 * s)
        h = int(cam.H * 0.05 * s)
        # slab rectangle (slight perspective tilt)
        z_back = min(1.0, z + 0.04)
        x_b = int(cam.lane_x(self.lane, z_back))
        y_b = int(cam.floor_y(z_back))
        w_b = int(cam.W * 0.10 * cam.scale(z_back))

        poly = np.array([
            (x_c - w // 2, y_c),
            (x_c + w // 2, y_c),
            (x_b + w_b // 2, y_b),
            (x_b - w_b // 2, y_b),
        ], dtype=np.int32)
        cv2.fillPoly(canvas, [poly], self.color)
        cv2.polylines(canvas, [poly], True, CLR_WHITE, max(1, int(2 * s)),
                      lineType=cv2.LINE_AA)
        # foot icon (L/R letter)
        if s > 0.25:
            letter = 'L' if self.is_left else 'R'
            font_scale = 0.7 * s
            (tw, th), _ = cv2.getTextSize(letter, cv2.FONT_HERSHEY_DUPLEX,
                                          font_scale, 2)
            cv2.putText(canvas, letter, (x_c - tw // 2, y_c - h // 4 + th // 2),
                        cv2.FONT_HERSHEY_DUPLEX, font_scale, CLR_WHITE,
                        max(1, int(2 * s)), lineType=cv2.LINE_AA)
        return canvas


# ── 3D cube helper ───────────────────────────────────────────────────────────
# Vertex / face definitions of a unit cube centered at origin:
#
#     Y axis (down in image)
#     │
#     │     4───────5       (Z = +s, far face)
#     │    ╱│      ╱│
#     │   0─┼─────1 │       (Z = -s, near face)
#     │   │ 7─────┼─6
#     │   │╱      │╱
#     │   3───────2
#     └─────────────→ X
#
_CUBE_VERTS = np.array([
    [-1, -1, -1], [ 1, -1, -1], [ 1,  1, -1], [-1,  1, -1],   # near
    [-1, -1,  1], [ 1, -1,  1], [ 1,  1,  1], [-1,  1,  1],   # far
], dtype=np.float32)

# (vertex_indices, shade_factor, name)
_CUBE_FACES = [
    ([0, 1, 2, 3], 1.00, 'front'),   # z-
    ([5, 4, 7, 6], 0.45, 'back'),    # z+
    ([4, 0, 3, 7], 0.72, 'left'),    # x-
    ([1, 5, 6, 2], 0.72, 'right'),   # x+
    ([4, 5, 1, 0], 0.85, 'top'),     # y-
    ([3, 2, 6, 7], 0.55, 'bottom'),  # y+
]
_CUBE_EDGES = [(0,1),(1,2),(2,3),(3,0),
               (4,5),(5,6),(6,7),(7,4),
               (0,4),(1,5),(2,6),(3,7)]


# ── 3-D mesh loader + renderer ───────────────────────────────────────────────
# Supports .obj / .glb / .gltf / .stl / .ply / .dae via the trimesh library.
# Model is normalized to fit inside a unit cube centered at origin so that
# CUBE_HALF controls visual size regardless of source scale.
_MESH_CACHE: dict = {}


def _load_mesh(path: str):
    """Load a 3-D model and return (vertices Nx3, faces Mx3, face_normals Mx3,
    face_colors Mx3 float 0-1). Cached by path.
    """
    if path in _MESH_CACHE:
        return _MESH_CACHE[path]

    try:
        import trimesh            # lazy import
    except ImportError as e:
        raise RuntimeError(
            "The --cube_model option requires the 'trimesh' package. "
            "Install with: pip install trimesh") from e

    try:
        scene = trimesh.load(path, force='mesh', process=True)
    except Exception as e:
        raise RuntimeError(f"Could not load mesh: {path}: {e}") from e
    if scene is None or not hasattr(scene, 'vertices'):
        raise RuntimeError(f"File does not contain a mesh: {path}")

    mesh = scene
    # Normalize: center + scale so the bounding-box diagonal is ~1 unit
    bounds = mesh.bounds
    center = (bounds[0] + bounds[1]) / 2.0
    extents = bounds[1] - bounds[0]
    diag = float(np.linalg.norm(extents)) or 1.0
    verts = (mesh.vertices - center) / diag          # in [-0.5, +0.5]-ish

    faces = np.asarray(mesh.faces, dtype=np.int32)
    # Triangle face normals
    v0 = verts[faces[:, 0]]
    v1 = verts[faces[:, 1]]
    v2 = verts[faces[:, 2]]
    normals = np.cross(v1 - v0, v2 - v0)
    nlen = np.linalg.norm(normals, axis=1, keepdims=True) + 1e-9
    normals = normals / nlen

    # Per-face color. Trimesh assigns a default grey placeholder even when the
    # source file has no colors, so we only adopt baked colors when the visual
    # kind is explicitly 'vertex' or 'face' – otherwise we let the caller's
    # base_color tint the mesh.
    face_colors = None
    try:
        kind = getattr(mesh.visual, 'kind', None)
        if kind in ('vertex', 'face'):
            vc = np.asarray(mesh.visual.vertex_colors,
                            dtype=np.float32) / 255.0
            if vc.shape[1] >= 3:
                face_colors = (vc[faces[:, 0], :3] +
                               vc[faces[:, 1], :3] +
                               vc[faces[:, 2], :3]) / 3.0
                # trimesh uses RGB; our canvas is BGR
                face_colors = face_colors[:, ::-1].copy()
    except Exception:
        face_colors = None

    _MESH_CACHE[path] = (verts.astype(np.float32),
                         faces,
                         normals.astype(np.float32),
                         face_colors)
    print(f"[cube_model] Loaded {path}: {len(verts)} verts, {len(faces)} tris")
    return _MESH_CACHE[path]


def draw_mesh_3d(canvas: np.ndarray, cam: PerspectiveCamera,
                 mesh_data: tuple, pos_world: tuple, half: float,
                 base_color: tuple, rim=None) -> None:
    """Software-rasterize a triangle mesh: transform verts into world space,
    project to screen, back-face cull, depth-sort tris, fill with Lambert
    shading (per-face normal · view direction).
    """
    verts, faces, normals, face_colors = mesh_data
    cx_w, cy_w, cz_w = pos_world

    # World positions (scale by half-size, translate to pos)
    world_v = verts * (half * 2.0) + np.array([cx_w, cy_w, cz_w],
                                              dtype=np.float32)

    # Project all vertices at once
    wz = world_v[:, 2]
    in_front = wz > 0.1
    # Safe invert
    inv_z = np.where(in_front, 1.0 / np.maximum(wz, 0.01), 0.0)
    sx = cam.cx_pix + cam.fx * world_v[:, 0] * inv_z
    sy = cam.cy_pix + cam.fy * world_v[:, 1] * inv_z

    # Each face -> (avg_z, shade, color, 3 screen pts)
    f_v0 = world_v[faces[:, 0]]
    f_v1 = world_v[faces[:, 1]]
    f_v2 = world_v[faces[:, 2]]
    avg_z = (f_v0[:, 2] + f_v1[:, 2] + f_v2[:, 2]) / 3.0

    # Backface culling: face normal · view_dir (view dir ≈ face_center normalised)
    face_center = (f_v0 + f_v1 + f_v2) / 3.0
    view_dir = face_center / (np.linalg.norm(face_center, axis=1, keepdims=True)
                              + 1e-9)
    front_facing = np.einsum('ij,ij->i', normals, view_dir) < 0

    # Skip tris with any vertex behind camera
    all_front = (in_front[faces[:, 0]] &
                 in_front[faces[:, 1]] &
                 in_front[faces[:, 2]])

    # Lambert shading: headlamp that travels into the scene (+z), slightly
    # angled. A front-facing surface has outward normal pointing back toward
    # the camera (-z), so dot(N, -L) is positive when illuminated.
    light_dir = np.array([0.2, -0.3, 1.0], dtype=np.float32)   # forward/down
    light_dir /= np.linalg.norm(light_dir)
    lambert = np.clip(-np.einsum('ij,j->i', normals, light_dir), 0.0, 1.0)
    shade = 0.30 + 0.70 * lambert           # ambient 0.3 + diffuse 0.7

    valid = all_front & front_facing
    if not np.any(valid):
        return

    # Build draw list and sort by depth (far first for painter's algo)
    draw_order = np.argsort(-avg_z[valid])
    valid_idx = np.nonzero(valid)[0][draw_order]

    base_bgr = np.array(base_color, dtype=np.float32)

    pts_all_x = sx
    pts_all_y = sy

    for fi in valid_idx:
        i0, i1, i2 = faces[fi]
        tri = np.array([
            [pts_all_x[i0], pts_all_y[i0]],
            [pts_all_x[i1], pts_all_y[i1]],
            [pts_all_x[i2], pts_all_y[i2]],
        ], dtype=np.int32)
        s = float(shade[fi])
        if face_colors is not None:
            col_base = face_colors[fi] * 255.0
        else:
            col_base = base_bgr
        col = tuple(int(c * s) for c in col_base)
        cv2.fillConvexPoly(canvas, tri, col, lineType=cv2.LINE_AA)
        if rim is not None:
            cv2.polylines(canvas, [tri], True, rim, 1, lineType=cv2.LINE_AA)


# Cache: path -> (bgr uint8, alpha float32)
_IMG_TEX_CACHE: dict = {}


def _load_cube_texture(path: str):
    """Load cube texture with optional alpha channel. Cached by path."""
    if path in _IMG_TEX_CACHE:
        return _IMG_TEX_CACHE[path]
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Cube image not found: {path}")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        bgr = img[:, :, :3].copy()
        a   = img[:, :, 3].astype(np.float32) / 255.0
    else:
        bgr = img
        a   = np.ones(img.shape[:2], dtype=np.float32)
    _IMG_TEX_CACHE[path] = (bgr, a)
    print(f"[cube_image] Loaded texture {path}  shape={bgr.shape}")
    return bgr, a


def draw_cube_3d_textured(canvas: np.ndarray, cam: PerspectiveCamera,
                          pos_world: tuple, half: float,
                          texture_bgr: np.ndarray, texture_alpha: np.ndarray,
                          rim=CLR_WHITE,
                          half_xyz: tuple | None = None) -> dict | None:
    """Textured 3D cube – warps the given image onto each of the 6 faces
    with per-face shading. Other behaviour (depth-sort / edges) matches
    draw_cube_3d().

    `half_xyz` (optional): per-axis half-extents (hx, hy, hz). When given,
    the box becomes non-uniform (used by the flat "dance" slabs). When
    omitted, falls back to uniform `half` on all axes.
    """
    cx_w, cy_w, cz_w = pos_world
    if half_xyz is None:
        scale_vec = np.array([half, half, half], dtype=np.float32)
    else:
        scale_vec = np.asarray(half_xyz, dtype=np.float32)
    verts_world = _CUBE_VERTS * scale_vec + np.array([cx_w, cy_w, cz_w])
    pts_2d = []
    for v in verts_world:
        p = cam.project(float(v[0]), float(v[1]), float(v[2]))
        if p is None:
            return None
        pts_2d.append((p[0], p[1]))

    # Source texture corners (TL, TR, BR, BL)
    H_img, W_img = texture_bgr.shape[:2]
    src_pts = np.array([[0, 0], [W_img - 1, 0],
                        [W_img - 1, H_img - 1], [0, H_img - 1]],
                       dtype=np.float32)

    # Depth-sort faces (painter's algorithm: far first)
    face_data = []
    for idxs, shade, name in _CUBE_FACES:
        avg_z = float(np.mean([verts_world[i][2] for i in idxs]))
        face_data.append((avg_z, idxs, shade, name))
    face_data.sort(key=lambda t: -t[0])

    H_img_c, W_img_c = canvas.shape[:2]

    for _, idxs, shade, _ in face_data:
        dst_pts = np.array([pts_2d[i] for i in idxs], dtype=np.float32)
        x_min, y_min = dst_pts.min(axis=0)
        x_max, y_max = dst_pts.max(axis=0)
        if x_max < 0 or y_max < 0 or x_min >= W_img_c or y_min >= H_img_c:
            continue
        x0 = max(0, int(math.floor(x_min)))
        y0 = max(0, int(math.floor(y_min)))
        x1 = min(W_img_c, int(math.ceil(x_max)))
        y1 = min(H_img_c, int(math.ceil(y_max)))
        w_roi = x1 - x0
        h_roi = y1 - y0
        if w_roi <= 1 or h_roi <= 1:
            continue

        dst_roi = dst_pts - np.array([x0, y0], dtype=np.float32)
        try:
            M = cv2.getPerspectiveTransform(src_pts, dst_roi)
        except cv2.error:
            continue
        warped_rgb = cv2.warpPerspective(texture_bgr, M, (w_roi, h_roi))
        warped_a   = cv2.warpPerspective(texture_alpha, M, (w_roi, h_roi))

        # Apply face shading (multiply RGB, keep alpha)
        if shade != 1.0:
            warped_rgb = (warped_rgb.astype(np.float32) * shade
                          ).clip(0, 255).astype(np.uint8)

        a = warped_a[..., None]
        roi = canvas[y0:y1, x0:x1].astype(np.float32)
        blended = roi * (1.0 - a) + warped_rgb.astype(np.float32) * a
        canvas[y0:y1, x0:x1] = blended.clip(0, 255).astype(np.uint8)

    # Crisp white edges on top
    for a_i, b_i in _CUBE_EDGES:
        cv2.line(canvas,
                 (int(pts_2d[a_i][0]), int(pts_2d[a_i][1])),
                 (int(pts_2d[b_i][0]), int(pts_2d[b_i][1])),
                 rim, 1, lineType=cv2.LINE_AA)

    # Near-face metadata (for optional overlay)
    near_ids = _CUBE_FACES[0][0]
    nx = int(np.mean([pts_2d[i][0] for i in near_ids]))
    ny = int(np.mean([pts_2d[i][1] for i in near_ids]))
    nw = int(abs(pts_2d[near_ids[1]][0] - pts_2d[near_ids[0]][0]))
    return {'cx': nx, 'cy': ny, 'size': nw, 'pts': pts_2d}


# Outward face normals (matches _CUBE_FACES order).
_CUBE_FACE_NORMALS = np.array([
    [ 0,  0, -1],   # front   (−Z)
    [ 0,  0,  1],   # back    (+Z)
    [-1,  0,  0],   # left    (−X)
    [ 1,  0,  0],   # right   (+X)
    [ 0, -1,  0],   # top     (−Y, world-up)
    [ 0,  1,  0],   # bottom  (+Y, world-down)
], dtype=np.float32)


def _round_poly(pts: np.ndarray, radius: float, steps: int = 6) -> np.ndarray:
    """Return a rounded-corner version of a convex polygon.

    Each corner is replaced by a short quadratic-bezier arc using the two
    neighboring edges as tangent directions and the original corner as the
    control point (the classic "chamfer into a fillet" construction).

    pts     : (N,2) float array, vertices in polygon order.
    radius  : distance (pixels) to trim back along each incident edge; the
              actual radius is clamped to 0.48 * shorter adjacent edge.
    steps   : samples per arc (higher = smoother).
    """
    pts = np.asarray(pts, dtype=np.float32)
    N = len(pts)
    if N < 3 or radius <= 0.5:
        return pts.copy()
    ts = np.linspace(0.0, 1.0, steps, dtype=np.float32)
    out = []
    for i in range(N):
        prev = pts[(i - 1) % N]
        curr = pts[i]
        nxt  = pts[(i + 1) % N]
        e1 = prev - curr
        e2 = nxt  - curr
        l1 = float(np.linalg.norm(e1))
        l2 = float(np.linalg.norm(e2))
        if l1 < 1e-3 or l2 < 1e-3:
            out.append(curr); continue
        r = min(radius, l1 * 0.48, l2 * 0.48)
        if r <= 0.5:
            out.append(curr); continue
        u1 = e1 / l1
        u2 = e2 / l2
        A = curr + u1 * r
        B = curr + u2 * r
        for t in ts:
            mt = 1.0 - t
            out.append(mt * mt * A + 2.0 * mt * t * curr + t * t * B)
    return np.asarray(out, dtype=np.float32)


def _hex_to_bgr(hex_str: str,
                default: tuple[int, int, int] = (255, 0, 255)
                ) -> tuple[int, int, int]:
    """Parse a '#RRGGBB' hex string and return (B, G, R) for OpenCV.

    Returns *default* (magenta) on any parse failure so callers never crash
    on bad user input.
    """
    try:
        s = (hex_str or "").lstrip('#')
        if len(s) != 6:
            return default
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return (b, g, r)
    except (ValueError, AttributeError):
        return default


def _draw_neon_edges(canvas: np.ndarray,
                     polys: list,
                     base_color: np.ndarray,
                     core_thick: int) -> None:
    """Draw neon-tube style outlines around the given closed polygons.

    1. A soft wide halo in the saturated cube color (screen-blended so it
       glows on top of the faces without a hard white contour).
    2. A thin bright core pulled toward white so the edge still reads as
       a crisp line at any scale – same hue as the cube so it doesn't look
       like a painted-on outline.
    """
    if not polys or core_thick < 1:
        return

    H_img, W_img = canvas.shape[:2]
    all_pts = np.concatenate(polys, axis=0)
    xs = all_pts[:, 0]
    ys = all_pts[:, 1]
    pad = max(6, core_thick * 6)
    x0 = max(0, int(xs.min()) - pad)
    y0 = max(0, int(ys.min()) - pad)
    x1 = min(W_img, int(xs.max()) + pad + 1)
    y1 = min(H_img, int(ys.max()) + pad + 1)
    if x1 <= x0 or y1 <= y0:
        return

    crop = canvas[y0:y1, x0:x1]

    glow_col = tuple(int(min(255.0, c * 1.15 + 25)) for c in base_color)
    wide = max(3, core_thick * 4)

    overlay = np.zeros_like(crop)
    for poly in polys:
        shifted = poly - np.array([x0, y0], dtype=poly.dtype)
        cv2.polylines(overlay, [shifted], True, glow_col,
                      wide, lineType=cv2.LINE_AA)
    k = (wide * 2) | 1
    overlay = cv2.GaussianBlur(overlay, (k, k), 0)
    np.maximum(crop, overlay, out=crop)


def draw_cube_3d(canvas: np.ndarray, cam: PerspectiveCamera,
                 pos_world: tuple, half: float,
                 color: tuple, rim=None,
                 yaw: float = 0.0,
                 corner_radius: float = 0.15,
                 half_xyz: tuple | None = None,
                 front_only: bool = False) -> dict | None:
    """Render a shaded 3-D cube centered at pos_world with edge half-length.

    Uses a proper light model (Lambert + ambient) on the true face normals so
    the block reads as solid geometry rather than a flat square.  `yaw` rotates
    the cube around world-Y (image-up axis); a small non-zero yaw exposes the
    side face and makes the cube look obviously three-dimensional.

    `half_xyz` (optional): per-axis half-extents (hx, hy, hz) to render a
    non-uniform box (used by the flat "dance" slabs).  Face normals are
    recomputed from the scaled-and-rotated geometry so lighting stays
    correct.  When omitted, falls back to a uniform `half` cube.

    `front_only` (optional): when True, only the near face (the one facing the
    camera) is drawn — side/top faces are suppressed.  Use for chain tail cubes
    so they appear as a seamless flat strip without visible block edges.

    Returns a dict with the projected near-face position and screen size (for
    icon overlays) or None if the cube is off-screen / behind the camera.
    """
    cx_w, cy_w, cz_w = pos_world

    # Rotate cube vertices + face normals around world-Y axis.
    cs, sn = math.cos(yaw), math.sin(yaw)
    R = np.array([[ cs, 0.0,  sn],
                  [0.0, 1.0, 0.0],
                  [-sn, 0.0,  cs]], dtype=np.float32)
    if half_xyz is None:
        scale_vec = np.array([half, half, half], dtype=np.float32)
    else:
        scale_vec = np.asarray(half_xyz, dtype=np.float32)
    verts_local = _CUBE_VERTS * scale_vec
    verts_rot   = verts_local @ R.T
    verts_world = verts_rot + np.array([cx_w, cy_w, cz_w], dtype=np.float32)
    # For non-uniform scaling we must reconstruct normals from the actual
    # scaled-and-rotated face vertices (otherwise lighting + back-face cull
    # use the wrong directions when hx ≠ hy ≠ hz).
    if half_xyz is None:
        normals_rot = _CUBE_FACE_NORMALS @ R.T
    else:
        normals_rot = np.zeros_like(_CUBE_FACE_NORMALS)
        for fi, (idxs, _s, _n) in enumerate(_CUBE_FACES):
            fv = verts_rot[idxs]
            e1 = fv[1] - fv[0]
            e2 = fv[2] - fv[0]
            nrm = np.cross(e1, e2)
            ln = float(np.linalg.norm(nrm))
            normals_rot[fi] = nrm / ln if ln > 1e-6 else _CUBE_FACE_NORMALS[fi]

    # Project every vertex.
    pts_2d: list[tuple[int, int]] = []
    for v in verts_world:
        p = cam.project(float(v[0]), float(v[1]), float(v[2]))
        if p is None:
            return None
        pts_2d.append((int(p[0]), int(p[1])))

    # Directional light: headlamp from the camera with a small upper-right
    # key so the front face stays brightest while the side and top are
    # clearly darker (mandatory for the "front panel with icon" read).
    L = np.array([0.18, 0.30, 0.95], dtype=np.float32)
    L /= np.linalg.norm(L)
    neg_L = -L

    # Collect visible faces (back-face cull using view direction).
    base = np.array(color, dtype=np.float32)
    face_data = []
    for fi, (idxs, _stub_shade, name) in enumerate(_CUBE_FACES):
        face_verts = verts_world[idxs]
        center = face_verts.mean(axis=0)
        view = center / (np.linalg.norm(center) + 1e-9)
        facing = float(np.dot(normals_rot[fi], view))
        if facing >= -0.02:                     # normal pointing away
            continue
        lamb = max(0.0, float(np.dot(normals_rot[fi], neg_L)))
        shade = 0.32 + 0.72 * lamb              # ambient + diffuse
        face_data.append((float(center[2]), idxs, shade, name, fi))

    if not face_data:
        return None

    face_data.sort(key=lambda t: -t[0])             # back to front

    # When front_only is requested, discard every face except the one
    # nearest the camera (smallest centre-z).  This suppresses visible
    # side/top edges so a chain of tail cubes looks like a seamless flat
    # strip rather than a row of individual 3-D blocks.
    if front_only:
        face_data = [min(face_data, key=lambda t: t[0])]

    # ── outer silhouette: convex hull of every visible-face vertex in 2-D.
    # Rounding the silhouette (instead of each face independently) keeps the
    # cube outline fully continuous – no triangular gaps where three faces
    # meet at a cube corner.
    visible_vert_ids = sorted({i for _, idxs, _, _, _ in face_data
                               for i in idxs})
    sil_pts = np.array([pts_2d[i] for i in visible_vert_ids],
                       dtype=np.int32)
    hull = cv2.convexHull(sil_pts, clockwise=True, returnPoints=True)
    sil_poly = hull.reshape(-1, 2).astype(np.float32)
    if len(sil_poly) < 3:
        return None

    sil_edges = [float(np.linalg.norm(sil_poly[(k + 1) % len(sil_poly)]
                                     - sil_poly[k]))
                 for k in range(len(sil_poly))]
    sil_r_px = min(sil_edges) * corner_radius
    rounded_sil = (_round_poly(sil_poly, sil_r_px, steps=8)
                   if sil_r_px > 1.0 else sil_poly).astype(np.int32)

    # Base silhouette fill = the darkest shade.  Acts as a "back wall" so
    # any tiny sub-pixel gap between two face fills shows a matching-hue
    # panel instead of the black background.
    darkest = min(fd[2] for fd in face_data)
    sil_col = tuple(int(min(255.0, c * darkest * 0.85)) for c in base)
    cv2.fillPoly(canvas, [rounded_sil], sil_col, lineType=cv2.LINE_AA)

    # Each visible face painted on top with its own shade (straight quads
    # – their shared edges coincide with neighbors' edges exactly).
    face_polys_int = []
    for _, idxs, shade, _, _ in face_data:
        poly = np.array([pts_2d[i] for i in idxs], dtype=np.int32)
        face_polys_int.append((poly, shade, idxs))
        col = tuple(int(min(255.0, c * shade)) for c in base)
        cv2.fillPoly(canvas, [poly], col, lineType=cv2.LINE_AA)

    # Inner glowing core on the NEAR (front) face – this is the "screen"
    # that hosts the fist icon.  Rounded with a matching radius so it
    # visually rhymes with the silhouette curvature.
    near_face = min(face_data, key=lambda t: t[0])     # smallest z
    near_idxs = near_face[1]
    near_raw = np.array([pts_2d[i] for i in near_idxs], dtype=np.float32)
    center2 = near_raw.mean(axis=0)
    inset_raw = (near_raw - center2) * 0.55 + center2
    inset_edges = [float(np.linalg.norm(inset_raw[(k + 1) % 4] - inset_raw[k]))
                   for k in range(4)]
    inset_r = min(inset_edges) * min(0.30, corner_radius * 1.5)
    inset = (_round_poly(inset_raw, inset_r, steps=7)
             if inset_r > 1.0 else inset_raw).astype(np.int32)
    bright = tuple(int(min(255.0, c * 1.30 + 50)) for c in base)
    cv2.fillPoly(canvas, [inset], bright, lineType=cv2.LINE_AA)

    # Neon halo + crisp core follow the ROUNDED SILHOUETTE only → one
    # clean glow around the whole block (no double-lines at interior
    # seams, no corner gaps).
    core_thick = max(1, int(round(half * cam.fx / max(0.3, cz_w) * 0.025)))
    _draw_neon_edges(canvas, [rounded_sil], base, core_thick)

    # Near-face metadata – pick the visible face whose center is closest
    # to the camera; that is the face the icon should sit on.
    near_face = min(face_data, key=lambda t: t[0])
    near_ids = near_face[1]
    nx = int(np.mean([pts_2d[i][0] for i in near_ids]))
    ny = int(np.mean([pts_2d[i][1] for i in near_ids]))
    nw = int(max(
        abs(pts_2d[near_ids[1]][0] - pts_2d[near_ids[0]][0]),
        abs(pts_2d[near_ids[2]][1] - pts_2d[near_ids[1]][1]),
    ))
    return {'cx': nx, 'cy': ny, 'size': nw, 'pts': pts_2d}


def _draw_fist_icon(canvas: np.ndarray, cx: int, cy: int, size: int,
                    color=CLR_WHITE):
    """Stylized closed-fist icon (knuckles + palm) centered at (cx,cy).

    size = total width of the icon in pixels. Designed to be crisp at any scale.
    """
    if size < 6:
        return
    # Palm: a rounded square body
    s = size
    pw, ph = int(s * 0.78), int(s * 0.58)
    px0, py0 = cx - pw // 2, cy - ph // 2 + int(s * 0.08)
    px1, py1 = cx + pw // 2, cy + ph // 2 + int(s * 0.08)
    cv2.rectangle(canvas, (px0, py0), (px1, py1), color, -1,
                  lineType=cv2.LINE_AA)
    # Knuckles: 4 small vertical bumps on top of palm
    k_w = max(2, int(s * 0.15))
    k_h = max(3, int(s * 0.22))
    gap = max(1, int(s * 0.03))
    total_k = 4 * k_w + 3 * gap
    kx0 = cx - total_k // 2
    ky0 = py0 - k_h
    for i in range(4):
        x0 = kx0 + i * (k_w + gap)
        cv2.rectangle(canvas, (x0, ky0), (x0 + k_w, py0 + 2),
                      color, -1, lineType=cv2.LINE_AA)
    # Thumb bump on the left
    tw, th = max(2, int(s * 0.18)), max(3, int(s * 0.30))
    cv2.rectangle(canvas, (px0 - tw, cy - th // 2),
                  (px0 + 2, cy + th // 2),
                  color, -1, lineType=cv2.LINE_AA)


class PunchTarget(Target):
    """3-D neon cube with fist icon, or a textured 3-D cube when an image
    is supplied. Left = green, right = red (if untextured).
    """

    # Cube half-size in world units.  Previously 0.22 when punch ran in a
    # 2-lane layout; now trimmed to 0.154 (≈70% of the old size) so the
    # cubes stay visually comfortable in the 4-lane layout and don't
    # crowd adjacent lanes.
    CUBE_HALF = 0.154
    # Corner rounding for the default neon cubes (fraction of the shortest
    # projected face-edge; 0 = sharp corners, ~0.45 = pill-shaped).
    CORNER_RADIUS: float = 0.18
    # Populated by RhythmVisualizer before render.
    TEXTURE_LEFT:  tuple | None = None
    TEXTURE_RIGHT: tuple | None = None
    # Optional 3-D mesh data (verts, faces, normals, face_colors)
    MESH_LEFT:     tuple | None = None
    MESH_RIGHT:    tuple | None = None
    MESH_WIREFRAME: bool = False

    # Visual flight bounds.  Independent of cam.Z_FAR / cam.Z_NEAR (which
    # govern game logic): cube travels visually from Z_VIS_FAR (horizon,
    # eye level) down to Z_VIS_NEAR — the "hit line" = back-edge of the
    # foreground hit panels = ``cam.Z_NEAR`` (2.5).  At this depth the
    # cube's centre lands flush on the red hit-line (where the 4 punch
    # panels meet the floor grid) and detonates THERE — instead of zooming
    # past Z_NEAR and exploding centimetres from the lens.
    Z_VIS_FAR:  float = 28.0
    Z_VIS_NEAR: float = 2.5
    # Trajectory descends from horizon (eye level) to WY_HIT (cube fully
    # below eye → top face visible, sits inside the bottom hit panel).
    WY_SPAWN: float = 0.0
    WY_HIT:   float = 0.25
    # Cube ARRIVES at the panel this many frames BEFORE ``hit_frame`` and
    # holds there until ``hit_frame``.  At 30 fps, 2 frames ≈ 67 ms — long
    # enough for the eye + irregular playback sampling to register the
    # arrival.  The hit (stickman strike + VFX + sound) still fires on the
    # exact ``hit_frame`` (= UI beat-stick).  See ``check_hit`` for the
    # state machine that turns this into "vanish exactly on the beat".
    HIT_ARRIVAL_OFFSET: int = 2

    def depth(self, cur_frame: int) -> float:
        """Override base depth so cube reaches panel HIT_ARRIVAL_OFFSET
        frames early and stays there until ``hit_frame``.

        Effect: from ``hit_frame - HIT_ARRIVAL_OFFSET`` onwards depth=0
        (cube fully at the hit panel).  Earlier than that, linear travel
        from horizon to panel — same speed as before, just the final
        ``HIT_ARRIVAL_OFFSET`` frames are spent locked at depth=0 instead
        of finishing the last few % of the journey.  Practically the
        cube looks identical (those last 2 frames cover only ~5% of
        ``Z_VIS_FAR-Z_VIS_NEAR``) but its arrival is now perceptible.
        """
        if cur_frame <= self.spawn_frame:
            return 1.0
        arrival_frame = self.hit_frame - self.HIT_ARRIVAL_OFFSET
        if cur_frame >= arrival_frame:
            return 0.0
        denom = max(1, arrival_frame - self.spawn_frame)
        p = (cur_frame - self.spawn_frame) / denom
        return 1.0 - p

    def check_hit(self, cur_frame: int) -> bool:
        """Hit fires AT ``hit_frame`` (= beat moment = stickman strike).

        Combined with the ``depth`` override above, the cube is:
          • flying through tunnel       on frames < hit - HIT_ARRIVAL_OFFSET
          • locked at panel (depth=0)   on frames in
                                        [hit - HIT_ARRIVAL_OFFSET, hit_frame]
          • vanished                    on frames > hit_frame

        We keep ``state='flying'`` THROUGH ``hit_frame`` itself so the
        painter still draws the cube at the panel for that frame — that
        way the visual "punch + dissolve" lands exactly on the audio
        beat / UI beat-stick instead of one frame early.
        """
        if self.state != 'flying':
            return False
        if cur_frame < self.hit_frame:
            return False
        if self.hit_exec_f < 0:
            self.hit_exec_f = cur_frame
            return True
        self.state = 'hit'
        return False

    def draw(self, canvas, cam, cur_frame):
        if self.state != 'flying':
            return canvas
        z_norm = self.depth(cur_frame)   # 1 at spawn, 0 at hit
        wx = cam.lane_world_x(self.lane)
        # Linear interpolation through 3D world space — straight-line
        # trajectory from (lane_x, 0, Z_VIS_FAR) to (lane_x, WY_HIT, Z_VIS_NEAR)
        wz = self.Z_VIS_NEAR + z_norm * (self.Z_VIS_FAR - self.Z_VIS_NEAR)
        wy = self.WY_SPAWN + (self.WY_HIT - self.WY_SPAWN) * (1.0 - z_norm)

        mesh = self.MESH_LEFT if self.is_left else self.MESH_RIGHT
        tex  = self.TEXTURE_LEFT if self.is_left else self.TEXTURE_RIGHT

        if mesh is not None:
            draw_mesh_3d(canvas, cam, mesh, (wx, wy, wz), self.CUBE_HALF,
                         base_color=self.color,
                         rim=CLR_WHITE if self.MESH_WIREFRAME else None)
            return canvas
        if tex is not None:
            draw_cube_3d_textured(canvas, cam, (wx, wy, wz), self.CUBE_HALF,
                                  tex[0], tex[1], rim=None)
            return canvas

        # Perfect dice: uniform half on all axes, yaw = 0 (axis-aligned).
        # 3 faces emerge naturally from perspective at hit zone:
        #   • FRONT — always visible
        #   • TOP   — WY_HIT > CUBE_HALF → cube fully below eye line → camera looks down
        #   • SIDE  — lane is off-centre → camera sees inner face
        cube_info = draw_cube_3d(canvas, cam, (wx, wy, wz), self.CUBE_HALF,
                                 color=self.color, yaw=0.0,
                                 corner_radius=self.CORNER_RADIUS)
        if cube_info is not None and cube_info['size'] >= 22:
            _draw_fist_icon(canvas, cube_info['cx'], cube_info['cy'],
                            int(cube_info['size'] * 0.58), CLR_WHITE)
        return canvas


def detect_wave_columns(y: np.ndarray, sr: int, hop_length: int,
                        fps: float,
                        min_gap_frames: int = 4,
                        prominence_pct: float = 20.0,
                        smooth_win: int = 1) -> list[dict]:
    """Extract "wave columns" from an RMS envelope.

    Each column describes one perceived attack+sustain in the audio and
    is used by line-mode as the SINGLE source of truth for both:

        1. WHEN a block arrives at the punch zone → ``rise_f`` = frame
           where the RMS envelope starts rising (local minimum just
           before the peak).
        2. HOW LONG the block takes to shrink away → ``end_f - rise_f``
           where ``end_f`` is the midpoint of the descent from the peak
           to the next local minimum (the "average" falling-off point).

    Returned fields (all frame indices are VIDEO frames at ``fps``):
        rise_f  : rise-start frame  (block arrives / spawns shrink)
        peak_f  : peak frame        (loudest point of the column)
        end_f   : descent-midpoint  (block is fully gone by here)
        height  : peak amplitude    (used to rank strongest columns)
    """
    try:
        from scipy.signal import find_peaks
    except Exception:
        return []

    if y is None or len(y) == 0 or sr <= 0 or fps <= 0:
        return []

    # Use an INTERNAL finer envelope — independent of the rhythm
    # feature hop — so transients stay crisp even when the caller
    # loaded audio at sr=22050 and hop=512.
    local_hop   = max(128, min(int(hop_length), 256))
    local_frame = max(512, local_hop * 4)
    rms = librosa.feature.rms(y=y, frame_length=local_frame,
                              hop_length=local_hop)[0]
    n = len(rms)
    if n < 3:
        return []

    # Light moving-average smoothing to suppress micro-peaks.
    k = max(1, int(smooth_win))
    if k > 1:
        kernel = np.ones(k, dtype=np.float32) / float(k)
        rms_s = np.convolve(rms, kernel, mode='same')
    else:
        rms_s = rms.astype(np.float32, copy=True)

    sec_per_hop = local_hop / float(sr)
    sec_per_vf = 1.0 / float(fps)
    # Minimum inter-peak distance in RMS hop units.
    min_dist_hops = max(1, int(round(min_gap_frames * sec_per_vf
                                     / max(1e-9, sec_per_hop))))
    prom = max(1e-6, float(np.percentile(rms_s, prominence_pct)))

    peaks, _props = find_peaks(rms_s, distance=min_dist_hops,
                               prominence=prom)
    if len(peaks) == 0:
        return []

    def hop_to_frame(h: int) -> int:
        return int(round(h * sec_per_hop * fps))

    columns: list[dict] = []
    for p in peaks:
        p = int(p)
        # Rise start = scan left while envelope was rising toward p.
        left = p
        while left > 0 and rms_s[left - 1] < rms_s[left]:
            left -= 1
        # Right minimum = scan right while envelope is descending.
        right = p
        while right < n - 1 and rms_s[right + 1] < rms_s[right]:
            right += 1
        # Column "end" = midpoint of the descent (average falling-off
        # point, as the user described).
        end_idx = (p + right) // 2
        columns.append({
            'rise_f': hop_to_frame(left),
            'peak_f': hop_to_frame(p),
            'end_f':  hop_to_frame(end_idx),
            'height': float(rms[p]),
        })

    # Enforce strict time ordering + non-overlap of (rise_f, end_f)
    # windows (columns with identical rise due to rounding collapse
    # into the stronger one).
    columns.sort(key=lambda c: c['rise_f'])
    dedup: list[dict] = []
    for c in columns:
        if dedup and c['rise_f'] <= dedup[-1]['rise_f']:
            if c['height'] > dedup[-1]['height']:
                dedup[-1] = c
            continue
        dedup.append(c)
    return dedup


class LineTarget(PunchTarget):
    """Chain-of-cubes hold-note (mode='line').

    Renders N consecutive punch cubes spaced ``CHAIN_D`` frames apart
    along the same lane.  Because perspective is linear, the ratio
    "cube-diameter / inter-cube-gap" is constant at every Z, so if the
    cubes look touching when they are far away they look touching when
    they are close — the whole chain moves as a seamless "snake of
    blocks" toward the camera.

    The cube half-extent in Z (``hz``) is sized automatically to make
    adjacent cubes end-to-end touching:

        velocity = (Z_FAR − Z_NEAR) / travel_frames   [wu/frame]
        D_z      = CHAIN_D × velocity                  [wu]
        hz       = D_z / 2                             [wu]

    So every cube fills its share of the lane exactly, no gaps, no
    overlap.

    Game mechanics:

    - Head cube (i = 0) arrives at ``hit_frame``, fires the stickman
      ``ZL``/``ZR`` event once, stickman holds the punch pose for
      ``hold_frames / FPS`` seconds.
    - Tail cubes (i = 1..N−1) arrive at ``hit_frame + i × CHAIN_D``,
      auto-vanish without triggering separate events.
    - Lane stays busy until the last tail cube passes (managed by the
      scheduler via ``line_busy_until``).
    - ``is_dead`` → True after the last tail cube has cleared the hit
      zone (``cur_frame > last_hit_frame + 3``).

    ``CHAIN_D`` (default 2 frames) controls visual density:

    - ``CHAIN_D = 1`` → cubes touching (D_z ≈ cube diameter), densest
    - ``CHAIN_D = 2`` → slight inter-cube gap, N ≈ hold_frames / 2
    - ``CHAIN_D = 3`` → more visible "brick" separation
    """

    # Legacy default; no longer used at runtime.  The actual per-block
    # spacing self._D is now derived in __init__ from hold_frames /
    # (n_cubes-1) so the chain lines up with the song's beat grid
    # (n_cubes = line_beats → 1 block per beat).  Kept as a class
    # attribute for any external code that still inspects it.
    CHAIN_D: int = 10
    CORNER_RADIUS: float = 0.12
    # World-Z where the head cube centre freezes.
    # Set so front_z = HEAD_Z_CLAMP − hz ≈ Z_NEAR → fills ~20 % of frame.
    HEAD_Z_CLAMP: float = 4.1
    # Legacy stubs — kept so external callers that inspect these
    # attributes do not crash (values are no longer used by draw()).
    LENGTH_MULT: float = 2.8
    THICKNESS_MULT: float = 0.92

    # World-Y used for horizontal-zigzag chains.  More negative = higher
    # above the horizon (camera tilts upward to see the chain).  Set
    # so that at Z_NEAR the block sits in the top ~⅓ of the frame and
    # at Z_FAR it grazes the horizon, giving a clear perspective view
    # of the whole 4-block chain with no block occluding another.
    HORIZONTAL_WY: float = -0.30

    def __init__(self, spawn_frame: int, hit_frame: int, lane: int,
                 is_left: bool, hold_frames: int,
                 line_beats: int = 2,
                 block_hit_frames: list[int] | None = None,
                 block_shrink_frames: list[int] | None = None,
                 zigzag: str = 'vertical'):
        super().__init__(spawn_frame, hit_frame, lane, is_left)
        self.hold_frames = max(1, int(hold_frames))
        self.line_beats  = max(1, int(line_beats))
        # Zigzag axis for this chain: 'vertical' (legacy saw-tooth inside
        # a single lane) or 'horizontal' (chain spans lane 0 <-> lane n-1,
        # each block alternates direction).  Stored per-target so mixed
        # runs are possible even if current CLI forces a single value.
        self.zigzag = (zigzag or 'vertical').lower().strip()
        if self.zigzag not in ('vertical', 'horizontal'):
            self.zigzag = 'vertical'
        # Prefer the caller-supplied block_hit_frames as the source of
        # truth for n_cubes (waveform-driven scheduler produces one
        # block per RMS column — could be any number 2..8).  Fallback
        # to 8th-note subdivision when no explicit per-block frames
        # are given.
        if block_hit_frames is not None and len(block_hit_frames) >= 2:
            self.n_cubes = min(8, max(2, int(len(block_hit_frames))))
            _bh = sorted(int(v) for v in block_hit_frames[:self.n_cubes])
            for i in range(1, len(_bh)):
                if _bh[i] <= _bh[i - 1]:
                    _bh[i] = _bh[i - 1] + 1
            self.block_hit_frames = _bh
        else:
            self.n_cubes = min(8, max(2, 2 * self.line_beats))
            D = max(1, int(round(self.hold_frames /
                                 max(1, self.n_cubes - 1))))
            self.block_hit_frames = [self.hit_frame + i * D
                                     for i in range(self.n_cubes)]

        # ── Time-anchored block geometry ──────────────────────────────────
        # Each block spans a TIME interval [front_f, back_f]:
        #   • front_f = block_hit_frames[i]   (when front face reaches punch)
        #   • back_f  = block_hit_frames[i+1] (when back face reaches punch,
        #                                     i.e. when next block arrives)
        # For the LAST block there is no "next block" so we fall back to
        # the wave-column width (block_shrink_frames[-1]) or, if absent,
        # the median intra-block gap.  Because consecutive blocks share
        # the EXACT SAME Z at the junction (block i's back and block
        # i+1's front both map to z_from_norm((t_{i+1}-cur)/travel)),
        # the chain is visually seamless at every frame — blocks slide
        # into the punch plane end-to-end with zero gap.
        if len(self.block_hit_frames) >= 2:
            _diffs = np.diff(np.asarray(self.block_hit_frames, dtype=np.int64))
            _med = max(1, int(np.median(_diffs)))
        else:
            _med = max(1, int(round(self.hold_frames)))

        # Width of the LAST block in frames (its shrink duration).  Use
        # the caller-supplied waveform width if available, otherwise the
        # median gap so the chain tail has a natural length.
        if (block_shrink_frames is not None
                and len(block_shrink_frames) >= self.n_cubes):
            last_width = max(1, int(block_shrink_frames[self.n_cubes - 1]))
        else:
            last_width = _med

        self.block_back_frames: list[int] = []
        for i in range(self.n_cubes):
            if i < self.n_cubes - 1:
                back_f = int(self.block_hit_frames[i + 1])
            else:
                back_f = int(self.block_hit_frames[i] + last_width)
            if back_f <= self.block_hit_frames[i]:
                back_f = self.block_hit_frames[i] + 1
            self.block_back_frames.append(back_f)

        # Shrink duration = back arrival − front arrival.  For middle
        # blocks this equals the gap to the next block, so the block
        # is already fully compressed by the time its successor lands
        # at the punch plane (no visual overlap, no visible gap).
        self.block_shrink_dur = [
            max(1, int(self.block_back_frames[i] - self.block_hit_frames[i]))
            for i in range(self.n_cubes)
        ]

        # Baseline _D kept for any legacy inspector; use the median of
        # per-block durations so "chain life" heuristics still work.
        self._D = max(1, int(np.median(np.asarray(self.block_shrink_dur,
                                                  dtype=np.int64))))

        # Tracks which block indices have already fired a hit event —
        # each block in a chain should pulse the viewport shake + burst
        # its own particles + tick the combo counter as it lands.
        self._punched: set[int] = set()
        # Index of the block whose hit frame was consumed most recently
        # by check_hit().  The main-loop VFX block uses this to place
        # the particle burst at the correct lane for horizontal-zigzag
        # chains, where each block lands on a different X position.
        self.last_punched_i: int = -1
        # hit_frame      = frame when the HEAD cube reaches the hit zone
        #                  and FREEZES (stops moving).
        # final_hit_frame= frame when the HEAD cube is punched LAST,
        #                  after all tail cubes have passed through.
        # New hit order: head (i=0) first, then tails sequentially.
        # final_hit_frame = frame when the LAST tail cube clears.
        self.freeze_frame     = self.hit_frame          # kept for compat; unused
        self.hit_frame        = int(self.block_hit_frames[0])
        self.final_hit_frame  = int(self.block_hit_frames[-1])

    # ── lifecycle --------------------------------------------------
    def check_hit(self, cur_frame: int) -> bool:
        """Fire once per block as it lands on the punch plane.

        Each block in the chain is a real "punch": we want the viewport
        neon panels to shake, particles to burst and the combo counter
        to tick for every one of them, not only the last block.  The
        lifecycle (``is_dead`` / drawing) is still driven by the final
        block's back-face arrival, so the target object stays alive for
        the whole chain even though ``check_hit`` fires several times.
        """
        if self.state != 'flying':
            return False
        for i in range(self.n_cubes):
            if i in self._punched:
                continue
            if cur_frame >= self.block_hit_frames[i]:
                self._punched.add(i)
                self.last_punched_i = i
                # Only stamp hit_exec_f on the LAST block — that's the
                # frame the chain is considered "punched out" and starts
                # its final-block shrink countdown inside is_dead.
                if i == self.n_cubes - 1:
                    self.hit_exec_f = cur_frame
                return True
        return False

    def is_dead(self, cur_frame: int) -> bool:
        if self.hit_exec_f < 0:
            return False
        # Chain ends when the LAST block's back face has reached the
        # punch plane (that's when the final block fully collapses).
        last_back = (self.block_back_frames[-1]
                     if self.block_back_frames
                     else self.final_hit_frame + self._D)
        if cur_frame > last_back + 1:
            self.state = 'hit'
            return True
        return False

    def depth(self, cur_frame: int) -> float:
        """Head-cube depth — used by the painter-sort in the main loop."""
        return super().depth(cur_frame)

    # ── rendering --------------------------------------------------
    def draw(self, canvas: np.ndarray, cam: PerspectiveCamera,
             cur_frame: int):
        if self.state == 'dead':
            return canvas

        travel = max(1, self.hit_frame - self.spawn_frame)

        hx = PunchTarget.CUBE_HALF
        yaw = 0.18 if self.is_left else -0.18
        # Horizontal-zigzag chains live higher in world-Y so the camera
        # tilts upward and the viewer can see through to the rear blocks
        # of the chain instead of having them occluded by the nearest
        # one.  Vertical-zigzag keeps the legacy AIR_WORLD_Y.
        wy_center = (LineTarget.HORIZONTAL_WY
                     if self.zigzag == 'horizontal'
                     else cam.AIR_WORLD_Y)
        wx_lane   = cam.lane_world_x(self.lane)

        freeze_frame    = self.freeze_frame
        final_hit_frame = self.final_hit_frame

        # ── Zigzag-axis-aware segment centres ─────────────────────────────
        # Two modes:
        #   vertical (legacy): single lane, blocks saw-tooth up/down in Y.
        #   horizontal      : single Y (AIR_WORLD_Y), blocks sweep between
        #                      lane 0 and lane n_lanes-1 in X.  Each junction
        #                      _seg_wx(k) pins the shared (X) coordinate of
        #                      block k-1's back and block k's front, so the
        #                      chain stays seamless even with X-tilt.
        if self.zigzag == 'horizontal':
            _lane_left_x  = cam.lane_world_x(0)
            _lane_right_x = cam.lane_world_x(max(0, cam.n_lanes - 1))

            def _seg_wx(idx: int) -> float:
                return _lane_left_x if (idx % 2 == 0) else _lane_right_x

            def _seg_wy(idx: int) -> float:
                return wy_center
        else:
            def _seg_wx(idx: int) -> float:
                return wx_lane

            def _seg_wy(idx: int) -> float:
                """Y centre for segment idx: even=below-horizon, odd=above."""
                return (wy_center + hx) if (idx % 2 == 0) else (wy_center - hx)

        # ── Time-anchored geometry ────────────────────────────────────────
        # Block i's front face arrives at the punch plane at frame
        # `block_hit_frames[i]` and its back face arrives at
        # `block_back_frames[i]` (= next block's rise_f, or own column
        # width for the last block).  At any time `cur`:
        #
        #     z_at(t_target) = cam.z_from_norm( (t_target - cur) / travel )
        #
        # gives the world-Z of a point that is DUE to reach the punch
        # plane at frame t_target.  So block i's FRONT is at
        # z_at(front_f) and its BACK at z_at(back_f).  Because block i+1
        # uses the same `t_back[i]` as its own `t_front[i+1]`, the two
        # blocks always share Z at that instant → seamless chain.
        def _z_at(t_target: int) -> float:
            dn = (t_target - cur_frame) / float(travel)
            dn = min(1.0, max(0.0, dn))
            return cam.z_from_norm(dn)

        # Format: (fz, bz, wx_f, wx_b, wy_f, wy_b, is_neon, cube_i, shrink_t)
        segs = []
        neon_i: int | None = None

        # Pre-pass: is any block currently shrinking (front past punch,
        # back still in front of punch)?  If so, no other block may
        # claim the neon highlight — only one bright "hero" at a time.
        has_shrink = False
        for k in range(self.n_cubes):
            t_fk = self.block_hit_frames[k]
            t_bk = self.block_back_frames[k]
            if t_fk <= cur_frame < t_bk:
                has_shrink = True
                break

        for i in range(self.n_cubes):
            wy_f = _seg_wy(i)
            wy_b = _seg_wy(i + 1)   # back Y always = next block's front Y
            wx_f = _seg_wx(i)
            wx_b = _seg_wx(i + 1)   # back X always = next block's front X

            t_front = self.block_hit_frames[i]
            t_back  = self.block_back_frames[i]
            D_i     = max(1, t_back - t_front)

            # Block fully gone once its back has reached the punch plane.
            if cur_frame >= t_back:
                continue

            # ── Back face: TAIL IS THE ANCHOR ────────────────────────────
            # The back face is kept on its natural velocity the entire
            # time — both during approach and during the shrink phase.
            # This is the junction point between block i and block i+1
            # (they share the exact same z = z_from_norm((t_back - cur)
            # / travel) every frame), so letting the tail keep flying
            # guarantees the junction never drifts in either Z or Y.
            bz = _z_at(t_back)

            # Shrink progress [0..1] — 0 while approaching, then ramps
            # up as the front face collapses onto the (still moving)
            # back face.
            if cur_frame >= t_front:
                shrink_t = min(1.0, max(0.0,
                                        (cur_frame - t_front) / float(D_i)))
            else:
                shrink_t = 0.0

            # ── Front face: FRONT IS THE ONE THAT SHRINKS ────────────────
            # Approach phase: front slides in at natural velocity.
            # Shrink phase : front retreats from the punch plane back
            #                toward the (moving) tail, so the block
            #                collapses *from front to back*.  At
            #                shrink_t = 0 it sits on the punch plane
            #                (brief hit feedback), at shrink_t = 1 it
            #                merges with the back face.
            if cur_frame < t_front:
                z_norm_f = (t_front - cur_frame) / float(travel)
                if z_norm_f > 1.0:
                    # Block's front hasn't entered the frustum yet.
                    continue
                fz = cam.z_from_norm(z_norm_f)
            else:
                fz_anchor = cam.Z_NEAR + 0.01
                fz = fz_anchor + (bz - fz_anchor) * shrink_t

            if bz <= fz + 0.005:
                continue

            # Y / X interpolation follows the SAME direction as Z:
            #   • Back (wx_b / wy_b) stays anchored at its junction coord
            #     so it lines up with the next block's front throughout
            #     the shrink (no side-drift at the junction).
            #   • Front (wx_f / wy_f) glides toward that same junction,
            #     so the front corner merges into the junction point as
            #     the block collapses front-to-back.
            #
            # In vertical mode wx_f == wx_b (== wx_lane) so the X glide
            # is a no-op; in horizontal mode wy_f == wy_b (== AIR_WORLD_Y)
            # so the Y glide is a no-op.  Same formula works for both.
            wy_f_now = wy_f + (wy_b - wy_f) * shrink_t
            wy_b_now = wy_b
            wx_f_now = wx_f + (wx_b - wx_f) * shrink_t
            wx_b_now = wx_b

            # Neon only on APPROACHING blocks, and only when nothing is
            # currently shrinking.  This keeps a single "hero" highlight
            # on the front-most approaching block until it lands.
            is_neon = False
            if cur_frame < t_front and not has_shrink and neon_i is None:
                neon_i = i
                is_neon = True

            segs.append((fz, bz, wx_f_now, wx_b_now,
                         wy_f_now, wy_b_now, is_neon, i, shrink_t))

        if not segs:
            return canvas

        segs.sort(key=lambda s: s[0], reverse=True)   # back-to-front

        col     = self.color
        rim_col = tuple(min(255, int(c * 1.8)) for c in col)

        def _pt(x, y, z):
            p = cam.project(x, y, z)
            return (int(p[0]), int(p[1])) if p else None

        # Bo góc nhẹ cho từng face — radius = 8% cạnh ngắn nhất (min 1 px).
        # Tạo cảm giác mềm mại, không sắc cạnh mà vẫn giữ hình dạng quad.
        def _fill_rounded(quad_pts, color):
            pts_f = np.asarray(quad_pts, dtype=np.float32)
            edges = [float(np.linalg.norm(pts_f[(k + 1) % len(pts_f)] -
                                          pts_f[k]))
                     for k in range(len(pts_f))]
            r = max(1.0, min(edges) * 0.08)
            rp = (_round_poly(pts_f, r, steps=5)
                  if r > 1.0 else pts_f).astype(np.int32)
            cv2.fillPoly(canvas, [rp], color, lineType=cv2.LINE_AA)

        for fz, bz, wx_f, wx_b, wy_f, wy_b, is_neon, cube_i, shrink_t in segs:
            # Front face corners (centred at (wx_f, wy_f))
            fTL = _pt(wx_f - hx, wy_f + hx, fz)
            fTR = _pt(wx_f + hx, wy_f + hx, fz)
            fBR = _pt(wx_f + hx, wy_f - hx, fz)
            fBL = _pt(wx_f - hx, wy_f - hx, fz)
            if None in (fTL, fTR, fBR, fBL):
                continue

            # Back face corners (centred at (wx_b, wy_b) — tilt may offset
            # in either X (horizontal mode) or Y (vertical mode))
            bTL = _pt(wx_b - hx, wy_b + hx, bz)
            bTR = _pt(wx_b + hx, wy_b + hx, bz)
            bBR = _pt(wx_b + hx, wy_b - hx, bz)
            bBL = _pt(wx_b - hx, wy_b - hx, bz)

            fade = 1.0 - shrink_t * 0.6

            if is_neon:
                s_bright = 0.82 * fade
                f_bright = 1.00 * fade
                edge_col = tuple(int(c * fade) for c in rim_col)
                side_lw  = 2
                front_lw = 3
            elif shrink_t > 0:
                # Khối đã được punch: mặt trước giữ neon 100%, cả 3 mặt (front +
                # side + cap) bắt đầu bằng style neon đầy đủ (khớp với pha
                # approach-neon), phần thân (side/cap) tắt dần theo body_fade
                # khi chiều sâu rút về 0; mặt trước sáng tối đa cho đến khi
                # chiều sâu bằng 0 thì mới biến mất.
                body_fade = 1.0 - shrink_t
                s_bright  = 0.82 * body_fade
                f_bright  = 1.00
                edge_col  = rim_col
                side_lw   = 2
                front_lw  = 3
            else:
                s_bright = 0.18 * fade
                f_bright = 0.22 * fade
                edge_col = tuple(int(c * 0.55 * fade) for c in rim_col)
                side_lw  = 1
                front_lw = 1

            # ── SIDE / CAP FACES ──────────────────────────────────────────
            # The SIDE face is the one that connects the front face to
            # the back face along the TILT axis (and therefore shows the
            # slant most clearly from camera).  In vertical mode the
            # block tilts in Y, so the visible "side" is the LEFT/RIGHT
            # wall and its selection is driven by the chain's lane side
            # (is_left) just like the legacy code.  In horizontal mode
            # the block tilts in X, so the visible "side" is still a
            # LEFT/RIGHT wall but it's now naturally skewed in X; we
            # pick whichever wall faces the camera based on the tilt
            # direction (wx_b > wx_f → RIGHT wall is "outer" and thus
            # visible, wx_b < wx_f → LEFT wall).
            if self.zigzag == 'horizontal':
                pick_right = (wx_b >= wx_f)
            else:
                pick_right = self.is_left
            if pick_right:
                sf = (fTR, fBR, bBR, bTR)
            else:
                sf = (fTL, fBL, bBL, bTL)

            if all(p is not None for p in sf):
                side_col = tuple(int(c * s_bright) for c in col)
                _fill_rounded(sf, side_col)

            # ── CAP FACE (top/bottom) ─────────────────────────────────────
            # The horizontal face that "caps" the slant is only visible
            # when the block actually tilts in Y (vertical-zigzag mode).
            # In horizontal mode wy_f == wy_b so this would be a thin
            # edge-on sliver — skip it and let the SIDE face alone carry
            # the tilt cue (mirrors how vertical mode lets the CAP carry
            # it).
            if self.zigzag != 'horizontal':
                horiz_bright = (s_bright + f_bright) * 0.5
                horiz_col    = tuple(int(c * horiz_bright) for c in col)
                if wy_b < wy_f:
                    # TOP face: upper edges of front and back faces
                    h0 = _pt(wx_f - hx, wy_f - hx, fz)
                    h1 = _pt(wx_f + hx, wy_f - hx, fz)
                    h2 = _pt(wx_b + hx, wy_b - hx, bz)
                    h3 = _pt(wx_b - hx, wy_b - hx, bz)
                else:
                    # BOTTOM face: lower edges of front and back faces
                    h0 = _pt(wx_f - hx, wy_f + hx, fz)
                    h1 = _pt(wx_f + hx, wy_f + hx, fz)
                    h2 = _pt(wx_b + hx, wy_b + hx, bz)
                    h3 = _pt(wx_b - hx, wy_b + hx, bz)
                if all(p is not None for p in (h0, h1, h2, h3)):
                    _fill_rounded((h0, h1, h2, h3), horiz_col)

            # ── FRONT FACE ────────────────────────────────────────────────
            fill_col = tuple(int(c * f_bright) for c in col)
            _fill_rounded((fTL, fTR, fBR, fBL), fill_col)

            cx_s   = int((fTL[0] + fBR[0]) / 2)
            cy_s   = int((fTL[1] + fBR[1]) / 2)
            size_s = abs(fTR[0] - fTL[0])
            if size_s >= 10:
                _draw_fist_icon(canvas, cx_s, cy_s,
                                int(size_s * 0.54), CLR_WHITE)

        return canvas


class DanceTarget(Target):
    """Flat glowing floor tile – a "stomp pad" the stickman lands on.

    Rendered as a quad lying flush on the floor plane (not a thin 3D box):
    the 4 corners are projected through the perspective camera and filled
    as a solid glowing trapezoid with a neon rim and a dark footprint
    icon in the center.  This reads like the green floor pads in the
    CapCut reference ("miếng gạch phát sáng"), not a thin levitating
    cube.

    The legacy HALF_Y is kept so external code that still wants a thin
    slab (e.g. imported meshes) can opt in, but the default draw path is
    now the flat-tile renderer.
    """

    # Half-extents (world units): X = lane width, Z = tile length along the
    # floor (front→back depth).  Y is unused by the flat-tile path but kept
    # for the legacy mesh fallback.
    HALF_X: float = 0.20
    HALF_Y: float = 0.02
    HALF_Z: float = 0.32
    CORNER_RADIUS: float = 0.18
    # Shared with PunchTarget (assigned by RhythmVisualizer before render).
    # If MESH_* is set, we still defer to the legacy 3D mesh renderer so
    # people importing a custom asset get the asset, not the flat pad.
    TEXTURE_LEFT:  tuple | None = None
    TEXTURE_RIGHT: tuple | None = None
    MESH_LEFT:     tuple | None = None
    MESH_RIGHT:    tuple | None = None
    MESH_WIREFRAME: bool = False

    def draw(self, canvas, cam, cur_frame):
        if self.state != 'flying':
            return canvas
        z_norm = self.depth(cur_frame)
        wz = cam.z_from_norm(z_norm)
        wx = cam.lane_world_x(self.lane)
        fy = cam.FLOOR_WORLD_Y

        mesh = self.MESH_LEFT if self.is_left else self.MESH_RIGHT
        if mesh is not None:
            # Custom asset override → keep the legacy 3D mesh path flush
            # with the floor so external meshes still work as stomp pads.
            draw_mesh_3d(canvas, cam, mesh,
                         (wx, fy - self.HALF_Z * 0.5, wz),
                         self.HALF_Z,
                         base_color=self.color,
                         rim=CLR_WHITE if self.MESH_WIREFRAME else None)
            return canvas

        self._draw_flat_tile(canvas, cam, wx, fy, wz, z_norm)
        return canvas

    # ------------------------------------------------------------------
    def _draw_flat_tile(self, canvas, cam, wx: float, fy: float,
                        wz: float, z_norm: float):
        """Paint a glowing quad on the floor plane with a footprint icon."""
        HX, HZ = self.HALF_X, self.HALF_Z
        # Tile corners in world space (all on the floor plane y=fy).
        # Order: front-left, front-right, back-right, back-left (= closer
        # to camera first, so the trapezoid has the wide edge at the bottom
        # of the screen).
        corners_w = (
            (wx - HX, fy, wz - HZ),
            (wx + HX, fy, wz - HZ),
            (wx + HX, fy, wz + HZ),
            (wx - HX, fy, wz + HZ),
        )
        proj = [cam.project(*p) for p in corners_w]
        if any(p is None for p in proj):
            return
        pts = np.array(
            [(int(round(p[0])), int(round(p[1]))) for p in proj],
            dtype=np.int32)

        H, W = canvas.shape[:2]
        # Viewport-clip sanity: reject tiles that ended up fully offscreen
        # (projects can blow up for tiles right at the near plane).
        xs, ys = pts[:, 0], pts[:, 1]
        if xs.max() < 0 or xs.min() >= W or ys.max() < 0 or ys.min() >= H:
            return

        # Proximity gain: closer tiles glow brighter, far ones dim toward
        # the tunnel floor so they don't fight the foreground for attention.
        depth_gain = 0.35 + 0.65 * (1.0 - z_norm)

        base = tuple(int(c) for c in self.color)          # lane BGR
        # Neon "hot" version of the lane color (lerp toward white).
        neon = tuple(int(min(255, c * 0.30 + 255 * 0.70)) for c in base)
        # Dark outline version for the back of the rim.
        dark = tuple(int(c * 0.35) for c in base)

        # 1) Outer soft glow — two passes at different widths, alpha-blended
        #    so the tile "bleeds" onto the surrounding tunnel floor.
        glow_col = tuple(int(c * depth_gain) for c in neon)
        for gw, base_a in ((14, 0.22), (7, 0.38)):
            a = base_a * depth_gain
            if a < 0.02:
                continue
            overlay = canvas.copy()
            cv2.polylines(overlay, [pts], True, glow_col,
                          gw, lineType=cv2.LINE_AA)
            cv2.addWeighted(overlay, a, canvas, 1.0 - a, 0, dst=canvas)

        # 2) Main tile fill — translucent bright color so the tunnel floor
        #    still shows a faint grid through the pad (reference look).
        mask = np.zeros((H, W), dtype=np.uint8)
        cv2.fillConvexPoly(mask, pts, 255)
        idx = mask > 0
        if not idx.any():
            return
        fill_col = np.array(
            [c * depth_gain for c in base], dtype=np.float32)
        fill_a = 0.55 + 0.30 * depth_gain
        canvas[idx] = (canvas[idx].astype(np.float32) * (1.0 - fill_a)
                       + fill_col * fill_a).astype(np.uint8)

        # 3) Inner "hot" highlight — a shrunken trapezoid biased toward the
        #    FRONT edge (closer to camera) so the tile reads as a lit-up
        #    pad instead of a flat sticker.
        cx_px = float(pts[:, 0].mean())
        cy_px = float(pts[:, 1].mean())
        front_cx = (pts[0][0] + pts[1][0]) * 0.5
        front_cy = (pts[0][1] + pts[1][1]) * 0.5
        # Shift the shrink center 25% toward the front edge.
        sc_x = cx_px * 0.75 + front_cx * 0.25
        sc_y = cy_px * 0.75 + front_cy * 0.25
        shrink = 0.65
        inner = np.array([
            (int(round(sc_x + (p[0] - sc_x) * shrink)),
             int(round(sc_y + (p[1] - sc_y) * shrink)))
            for p in pts
        ], dtype=np.int32)
        inner_mask = np.zeros((H, W), dtype=np.uint8)
        cv2.fillConvexPoly(inner_mask, inner, 255)
        idx2 = inner_mask > 0
        if idx2.any():
            hot = np.array(
                [min(255, c * 0.40 + 255 * 0.55 * depth_gain) for c in base],
                dtype=np.float32)
            hot_a = 0.35 * depth_gain
            canvas[idx2] = (canvas[idx2].astype(np.float32) * (1.0 - hot_a)
                            + hot * hot_a).astype(np.uint8)

        # 4) Crisp neon rim so the tile edges are unambiguous (the
        #    reference video has a sharp bright border around each pad).
        rim_col = tuple(int(min(255, c * 0.20 + 255 * 0.80 * depth_gain))
                        for c in base)
        rim_w = max(2, int(round(3 * depth_gain + 1)))
        cv2.polylines(canvas, [pts], True, rim_col, rim_w,
                      lineType=cv2.LINE_AA)
        # Darker inner shadow line just below the neon rim — sells the
        # "recessed into the floor" look.
        cv2.polylines(canvas, [pts], True, dark, 1, lineType=cv2.LINE_AA)

        # 5) Footprint / stomp icon in the middle of the tile.
        self._draw_stomp_icon(canvas, pts, depth_gain)

    # ------------------------------------------------------------------
    @staticmethod
    def _draw_stomp_icon(canvas: np.ndarray, pts: np.ndarray,
                         depth_gain: float):
        """Dark "foot" silhouette centered on the tile — heel + toes."""
        fl, fr, br, bl = pts[0], pts[1], pts[2], pts[3]
        front_c = np.array([(fl[0] + fr[0]) * 0.5,
                            (fl[1] + fr[1]) * 0.5])
        back_c  = np.array([(bl[0] + br[0]) * 0.5,
                            (bl[1] + br[1]) * 0.5])
        tile_len = float(np.linalg.norm(front_c - back_c))
        front_w  = float(np.linalg.norm(fr - fl))
        # Skip when the tile is too small on screen: drawing an icon on a
        # <20px pad just produces a noisy blob.
        if min(tile_len, front_w) < 26:
            return

        cx = int((fl[0] + fr[0] + br[0] + bl[0]) * 0.25)
        cy = int((fl[1] + fr[1] + br[1] + bl[1]) * 0.25)
        # "Forward" = from back-center toward front-center (i.e. toward the
        # camera). cv2.ellipse measures angles in degrees from +X clockwise,
        # which matches image-coord math directly.
        fwd = front_c - back_c
        ang = math.degrees(math.atan2(fwd[1], fwd[0]))
        fw_u = fwd / (np.linalg.norm(fwd) + 1e-6)

        icon_col = (18, 18, 18)   # near-black → strong contrast vs. neon

        # Heel ellipse: centered slightly backward from tile center.
        heel_off = tile_len * 0.10
        heel_cx = int(cx - fw_u[0] * heel_off)
        heel_cy = int(cy - fw_u[1] * heel_off)
        heel_rx = max(3, int(tile_len * 0.22))   # along fwd
        heel_ry = max(3, int(front_w * 0.20))    # across fwd
        cv2.ellipse(canvas, (heel_cx, heel_cy),
                    (heel_rx, heel_ry), ang, 0, 360,
                    icon_col, -1, cv2.LINE_AA)

        # Toe pad: smaller ellipse forward of the heel, with a visible gap.
        toe_off = tile_len * 0.22
        toe_cx = int(cx + fw_u[0] * toe_off)
        toe_cy = int(cy + fw_u[1] * toe_off)
        toe_rx = max(2, int(tile_len * 0.10))
        toe_ry = max(2, int(front_w * 0.15))
        cv2.ellipse(canvas, (toe_cx, toe_cy),
                    (toe_rx, toe_ry), ang, 0, 360,
                    icon_col, -1, cv2.LINE_AA)


def _rounded_rect_points(x1: int, y1: int, x2: int, y2: int,
                         r: int, n: int = 8) -> np.ndarray:
    """CCW polygon that approximates a rounded rectangle.

    Corners are replaced with `n`-segment quarter arcs of radius `r`.
    `r` is clamped to at most half of the shorter side so the arcs
    never cross.  Used by ``RelaxTarget._draw_low`` to soften the
    front face of ground slabs.
    """
    r = max(0, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    if r <= 0:
        return np.array([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], np.int32)
    pts: list[tuple[int, int]] = []
    # top-left arc: 180° → 270°
    cx, cy = x1 + r, y1 + r
    for i in range(n + 1):
        a = np.pi + i * (np.pi / 2) / n
        pts.append((int(cx + r * np.cos(a)), int(cy + r * np.sin(a))))
    # top-right arc: 270° → 360°
    cx, cy = x2 - r, y1 + r
    for i in range(n + 1):
        a = -np.pi / 2 + i * (np.pi / 2) / n
        pts.append((int(cx + r * np.cos(a)), int(cy + r * np.sin(a))))
    # bottom-right arc: 0° → 90°
    cx, cy = x2 - r, y2 - r
    for i in range(n + 1):
        a = 0 + i * (np.pi / 2) / n
        pts.append((int(cx + r * np.cos(a)), int(cy + r * np.sin(a))))
    # bottom-left arc: 90° → 180°
    cx, cy = x1 + r, y2 - r
    for i in range(n + 1):
        a = np.pi / 2 + i * (np.pi / 2) / n
        pts.append((int(cx + r * np.cos(a)), int(cy + r * np.sin(a))))
    return np.array(pts, np.int32)


class RelaxTarget(Target):
    """Full-tunnel obstacle for the *relax* mode.

    Two visual variants chosen per-spawn:

      kind='low'  : a LOW, wide slab lying on the ground that spans the
                    full tunnel width.  The player has to JUMP over it —
                    camera pops upward and the stickman performs a LEAP.

      kind='high' : a FLOATING horizontal bar suspended at head height.
                    The player has to DUCK under it — camera dips down
                    and the stickman performs a SQUAT.

    The obstacle does not occupy a specific lane — it's a global beat
    trigger — so the scheduler skips all lane-stacking logic for
    `relax` mode and the hit is automatic (the player-avatar dodges
    it; the game never "misses").
    """

    HIT_DEPTH = 0.0
    _tex_cache: dict[str, np.ndarray] = {}
    _hole_mask_cache: dict[str, np.ndarray] = {}

    # Visual tuning ────────────────────────────────────────────────────
    LOW_HEIGHT_FRAC  = 0.07   # fraction of tunnel height (hit-plane)
    # HIGH (floating) bar — anchored to the horizon line with the same
    # (1-z)^1.6 perspective envelope as floor_y/ceil_y so the bar
    # grows naturally from the vanishing point toward the camera.
    # The bar is intentionally tall (≈35% of viewport height) so that
    # at the hit plane it fills the whole upper half of the screen —
    # a clear "wall you must duck under" silhouette.  HIGH_HORIZON_
    # OFFSET_FRAC places the bar's CENTRE a fixed fraction of H above
    # the horizon.
    HIGH_HORIZON_OFFSET_FRAC = 0.26   # bar centre above horizon @ z=0
    HIGH_HEIGHT_FRAC         = 0.35   # bar height           @ z=0
    MIDDLE_HORIZON_OFFSET_FRAC = 0.20
    HOLE_DEFAULT_WIDTH_FRAC = 0.18
    HOLE_DEFAULT_HEIGHT_FRAC = 0.55

    # Motion profile (two-phase piecewise) ────────────────────────────
    # The block's spawn→hit travel is split into TWO distinct phases
    # with different world-speeds, per user spec:
    #   "70% quãng đường đầu tiên chạy chậm từ từ.
    #    30% còn lại thì vút nhanh."
    #
    #   • Phase 1  (drift):  covers the first PHASE_SPLIT_D fraction
    #                        of z-distance (z from 1.0 to 1-D) at
    #                        low velocity — the block glides lazily
    #                        through the far field at the horizon.
    #   • Phase 2  (vút):    covers the remaining (1-D) of z-distance
    #                        at PHASE_SPEED_RATIO × the phase-1 speed,
    #                        so the block snaps into the hit plane.
    #
    # With D=0.70 and ratio=4 the time split works out to ~90.3% of
    # the travel window in Phase 1 and only ~9.7% in Phase 2 — the
    # block spends the vast majority of its travel drifting slowly,
    # then zooms through the last 30% of distance in ~0.6s (at
    # travel=180f / 30fps).
    #
    # Pass-by (p_lin > 1) carries Phase-2 velocity forward so the
    # block exits the viewport decisively.
    PHASE_SPLIT_D     = 0.70   # fraction of z-distance in Phase 1
    PHASE_SPEED_RATIO = 12.0   # Phase-2 world-speed / Phase-1 speed

    # Dodge timing ─────────────────────────────────────────────────────
    # DODGE_OFFSET_{LOW,HIGH}:  where the stickman fires its pose
    #     relative to hit_frame, expressed as a SIGNED fraction of
    #     travel_f.
    #       NEGATIVE → dodge_frame is BEFORE hit_frame (anticipate).
    #       POSITIVE → dodge_frame is AFTER  hit_frame (react).
    #
    #     LOW (ground slab, JUMP):  +0.01 × travel
    #         With PHASE_SPEED_RATIO=12 the block rockets through the
    #         last 30 % of z-distance in only ~3.4 % of travel-time
    #         (≈6 frames at travel=180).  A negative lead-in therefore
    #         fires the jump when the slab is still at z≈0.30 — still
    #         visually mid-tunnel.  Using a small POSITIVE offset means
    #         the jump fires ~2 frames AFTER the slab crosses z=0, i.e.
    #         when it is right at the hit-zone edge (user: "sát mép
    #         vùng hít thì mới bắt đầu nhảy").
    #
    #     HIGH (overhead bar, SQUAT):  +0.064 × travel
    #         Per the reference video (and user: "đối với block treo
    #         thì khi block này khuất 1/3 rồi thì mới ngồi"), the
    #         squat only reads well once the bar has ALREADY started
    #         to pass overhead and the top ~1/3 of the bar has exited
    #         the top of the viewport.  From the `(1-z)^2.0` pass-by
    #         anchor we get exactly 1/3 occlusion at p_lin ≈ 1.063,
    #         i.e. 11-12 frames AFTER hit_frame at travel=180.
    #
    # DODGE_HOLD_FRAC: how long AFTER dodge_frame the stickman holds
    #     the pose before recovering to RELAX_STAND.  Must be large
    #     enough to cover the block's transit past the camera plane
    #     but short enough that the next beat's dodge_frame leaves
    #     room for the stickman to stand (otherwise consecutive SQ
    #     waypoints chain together and the avatar never returns to
    #     neutral — user: "sau khi thực hiện hành động nhảy, ngồi
    #     xong thì phải trở về vị trí ban đầu").  0.25 × travel ≈
    #     1.5 s at travel=180 leaves ~0.5 s recovery room on the
    #     default 2 s cadence.
    DODGE_OFFSET_LOW  = +0.01   # fire just after z=0 — block is right at
                                # the hit-zone edge before the jump starts
    DODGE_OFFSET_HIGH = +0.064
    DODGE_OFFSET_MIDDLE = +0.0
    DODGE_HOLD_FRAC   = 0.04   # hold briefly then snap back to RELAX_STAND

    # Dodge timing ─────────────────────────────────────────────────────
    # Fire the stickman squat/leap pose + camera bob exactly at the
    # transition from Phase 1 → Phase 2.  That gives the player a
    # single clear cue ("the block is about to snap in!") right when
    # visual motion explodes, and the pose tween has just enough time
    # to settle before the block hits the hit plane.  We compute this
    # dynamically from PHASE_SPLIT_D / PHASE_SPEED_RATIO so the
    # timing automatically stays glued to the "vút" moment no matter
    # how the phase split is tuned.

    def __init__(self, spawn_frame: int, hit_frame: int,
                 kind: str = 'low',
                 wait_frames: int = 0):
        # Centre lane + is_left=False: lane index is irrelevant here
        # because the slab always spans the whole width, but Target's
        # base __init__ needs *something*, so we pass centre.
        super().__init__(spawn_frame, hit_frame, lane=0, is_left=False)
        if kind not in ('low', 'high', 'middle'):
            kind = 'low'
        self.kind = kind
        self.color = CLR_WALL_PINK
        self.texture_path: str | None = None
        self.hole_mask_path: str | None = None
        self.wait_frames = max(0, int(wait_frames))

    # ---------------- lifecycle ----------------
    #
    # Relax obstacles do NOT "hit and vanish" at the hit plane — they
    # fly PAST the camera and only disappear once they've drifted off
    # the viewport.  We achieve that by:
    #
    #   • depth()     → allow z to continue into negative territory
    #                   past z=0 (behind the camera); floor_y / ceil_y
    #                   already extrapolate smoothly so the slab slides
    #                   off the bottom (low) or top (high) edge.
    #
    #   • check_hit() → fire EXACTLY ONCE at hit_frame so the stickman
    #                   pose-pulse + camera-bob pipeline still sees a
    #                   single JP/SQ event, but we KEEP state='flying'
    #                   so the painter's-algorithm renderer keeps
    #                   drawing the obstacle.
    #
    #   • is_dead()   → time-based: kill the target once it has
    #                   travelled a full "spawn→hit" duration PAST the
    #                   hit plane (i.e. z ≈ -1.0).  By that point LOW
    #                   slabs have fallen well below y=H and HIGH bars
    #                   have risen above y=0, so the block is fully
    #                   off-screen regardless of camera-bob offset.

    # Time-split derived from D / ratio.  Cache as class-level for
    # speed but compute once — cheap and keeps the derivation visible.
    @classmethod
    def _phase_split_t(cls) -> float:
        D = cls.PHASE_SPLIT_D
        return D / (D + (1.0 - D) / cls.PHASE_SPEED_RATIO)

    @property
    def move_start_frame(self) -> int:
        return self.spawn_frame + self.wait_frames

    def depth(self, cur_frame: int) -> float:
        move_start = self.move_start_frame
        # Hold at the far horizon while waiting.
        if cur_frame <= move_start:
            return 1.0
        # Movement duration stays unchanged (configured travel), so it is
        # measured from move_start -> hit_frame.
        travel_f = max(1, self.hit_frame - move_start)
        p_lin = (cur_frame - move_start) / travel_f

        # ── MIDDLE blocks: visual-linear approach ────────────────────────
        # Middle is a wall to dodge through — it should grow on screen
        # at a roughly CONSTANT rate so the player can read its
        # approach.  Using inverse-Z (1/wz linear in time) makes the
        # block's screen-space size scale linearly: at p_lin=0.5 it's
        # already roughly half-size, at p_lin=1 it's at the hit plane.
        # Pass-by carries the same inverse velocity so it leaves the
        # viewport without lingering.
        if self.kind == 'middle':
            # Two-phase z_norm linear motion (per user spec):
            #   • Phase 1 (drift):  t ∈ [0, 2/3], z: 1.0 → 0.2
            #     covers 80 % of the z-distance in the first 2/3 of travel.
            #     Block enters at the FAR horizon (start of floor) and
            #     drifts forward — slow visual change because perspective
            #     compresses the far field.
            #   • Phase 2 (approach): t ∈ [2/3, 1], z: 0.2 → 0
            #     covers the last 20 % of z in 1/3 of time — visually
            #     this is the "rush to camera" because the same z-step
            #     near the camera produces a much larger screen-scale
            #     change.
            # Velocities are CONTINUOUS in z (linear pieces), so the
            # block moves smoothly without "jumping".
            T_M = 2.0 / 3.0
            D_M = 0.8                              # phase-1 z-distance fraction
            z_split = 1.0 - D_M                    # = 0.2 — z at hand-off
            if p_lin <= T_M:
                z = 1.0 - D_M * (p_lin / T_M)
            elif p_lin <= 1.0:
                z = z_split * (1.0 - (p_lin - T_M) / (1.0 - T_M))
            else:
                # Pass-by: continue at the phase-2 z-velocity.  At z<-0.1
                # cam.project() returns None (behind camera) so the wall
                # disappears almost immediately after p=1.
                v2 = z_split / max(1e-6, 1.0 - T_M)
                z = -v2 * (p_lin - 1.0)
            return max(-1.2, z)

        # ── LOW / HIGH: two-phase "70% chậm + 30% vút" ──────────────────
        D = self.PHASE_SPLIT_D
        T = self._phase_split_t()
        z_split = 1.0 - D                  # z at the Phase 1 → 2 hand-off
        if p_lin <= T:
            # Phase 1 (drift): z: 1.0 → z_split over t: [0, T]
            z = 1.0 - D * (p_lin / T)
        elif p_lin <= 1.0:
            # Phase 2 (vút): z: z_split → 0 over t: [T, 1]
            z = z_split * (1.0 - (p_lin - T) / (1.0 - T))
        else:
            # Pass-by: carry Phase-2 velocity forward so the block
            # exits the viewport at the same sharp speed.  Phase-2
            # world-velocity in z-units per unit of normalised time
            # is z_split / (1 - T).
            v2 = z_split / (1.0 - T)
            z = -v2 * (p_lin - 1.0)
        # Clamp so numerical blowup in (1-z)**k stays bounded.
        return max(-1.2, z)

    @property
    def dodge_frame(self) -> int:
        """Frame at which the stickman SQUAT / JUMP and camera bob fire.

        Offset from ``hit_frame`` by ``DODGE_OFFSET_{LOW,HIGH} ×
        travel_f`` — negative for LOW (anticipate the jump) and
        positive for HIGH (wait until the bar has started to obscure
        overhead, then squat).  This matches the reference motion
        where a jump must precede the slab but a duck is delayed
        until the bar is visibly passing above.
        """
        travel_f = max(1, self.hit_frame - self.move_start_frame)
        if self.kind == 'low':
            offset_frac = self.DODGE_OFFSET_LOW
        elif self.kind == 'high':
            offset_frac = self.DODGE_OFFSET_HIGH
        else:
            offset_frac = self.DODGE_OFFSET_MIDDLE
        return self.hit_frame + int(round(travel_f * offset_frac))

    @property
    def dodge_end_frame(self) -> int:
        """Frame at which the stickman returns to RELAX_STAND and the
        camera bob finishes its ramp-down.  Set to ``dodge_frame +
        DODGE_HOLD_FRAC * travel_f`` so the dodge pose covers the
        short "vút" phase plus a bit of pass-by but releases long
        before the next beat's dodge_frame (on a typical 2 s cadence
        at travel=180 this leaves ≈0.5 s of recovery room).
        """
        travel_f = max(1, self.hit_frame - self.move_start_frame)
        return self.dodge_frame + int(round(travel_f * self.DODGE_HOLD_FRAC))

    def check_hit(self, cur_frame: int) -> bool:
        if cur_frame < self.hit_frame:
            return False
        if self.hit_exec_f >= 0:
            return False
        self.hit_exec_f = cur_frame
        return True

    def is_dead(self, cur_frame: int) -> bool:
        if self.hit_exec_f < 0:
            return False
        # Kill the target once the depth has hit its pass-by clamp
        # (z = -1.2).  Past that point depth() returns a constant, so
        # the block is visually frozen — LOW is well below the viewport,
        # HIGH has shrunk to the top margin — and keeping it alive just
        # wastes the painter's-algorithm sort.  We derive the exit
        # frame analytically from the Phase-2 world-velocity:
        #     v2 = z_split / (1 - T)
        #     Δframes = travel_f · 1.2 / v2
        travel_f = max(1, self.hit_frame - self.move_start_frame)
        T = self._phase_split_t()
        z_split = 1.0 - self.PHASE_SPLIT_D
        v2 = z_split / max(1e-6, 1.0 - T)
        exit_pad = int(round(travel_f * 1.2 / v2))
        return cur_frame - self.hit_frame > exit_pad

    # ---------------- helpers ----------------
    def _span_x(self, cam: "PerspectiveCamera", z: float):
        """Pink slab spans the full lane runway (outer lanes edge-to-edge).

        We deliberately use the LANE boundaries — not the tunnel walls —
        so the bar stays inside the visible runway even at z=0 where the
        wall projection blows out past the screen edges.  A small extra
        margin (half a lane step) keeps the slab reading as "spanning
        everything" rather than ending exactly on the rails.

        NOTE: we bypass `cam.lane_x()` because it applies
        `int(round(lane))` and then uses the lane-bottom LUT whenever
        the rounded index falls inside [0, n-1] — which, due to
        Python's banker's rounding, collapses `-0.5 → 0` (no left
        extrapolation) while `n-0.5 → n` correctly extrapolates to the
        right.  That asymmetry skewed the slab visibly toward the
        right edge.  Here we linearly extrapolate both bounds from
        the lane-bottom step so the slab stays centered.
        """
        n = max(1, cam.n_lanes)
        if n >= 2:
            step = (cam.lane_x_bottom[-1] - cam.lane_x_bottom[0]) / (n - 1)
        else:
            step = 0.0
        bot_l = cam.lane_x_bottom[0]  - 0.5 * step
        bot_r = cam.lane_x_bottom[-1] + 0.5 * step
        converge = (1.0 - z) ** 1.0
        x_l = int(cam.cx + (bot_l - cam.cx) * converge)
        x_r = int(cam.cx + (bot_r - cam.cx) * converge)
        return x_l, x_r

    def _y_band(self, cam: "PerspectiveCamera", z: float):
        """Top-y / bottom-y of the pink band at depth z.

        LOW  : bottom sits on the floor (tracks floor_y); naturally
               slides off the bottom of the viewport when z<0 because
               floor_y(z) grows past cam.H.

        HIGH : centre is anchored ABOVE the horizon by a fixed world-
               offset.  During APPROACH (z>=0) the anchor rises with
               the same (1-z)^1.6 envelope as the floor/ceiling, so the
               bar feels in-sync with the tunnel perspective.  During
               PASS-BY (z<0) we use a STEEPER anchor envelope so the
               bar rises faster than it grows, letting it fully exit
               the top of the viewport by z≈-0.9 instead of lingering
               as a huge slab pinned at the top of the screen.
        """
        if self.kind == 'low':
            fy = cam.floor_y(z)
            cy = cam.ceil_y(z)
            tunnel_h = fy - cy
            h = tunnel_h * self.LOW_HEIGHT_FRAC
            y_b = fy
            y_t = fy - h
        else:
            t_h = max(0.0, (1.0 - z)) ** 1.6
            if z >= 0.0:
                t_a = t_h
            else:
                t_a = (1.0 - z) ** 2.0
            off0 = cam.H * self.HIGH_HORIZON_OFFSET_FRAC
            h_ref = cam.H * self.HIGH_HEIGHT_FRAC
            c = cam.cy_v - t_a * off0
            h = h_ref * t_h
            y_t = c - h * 0.5
            y_b = c + h * 0.5
        return int(y_t), int(y_b)

    # ---------------- rendering ----------------
    def draw(self, canvas: np.ndarray, cam: "PerspectiveCamera",
             cur_frame: int):
        if self.state != 'flying':
            return canvas
        if self.kind == 'middle':
            return self._draw_middle(canvas, cam, cur_frame)
        z = self.depth(cur_frame)

        # Front face
        x_l,  x_r  = self._span_x(cam, z)
        y_t,  y_b  = self._y_band(cam, z)

        # Back face — depth offset.  LOW uses a larger offset so the
        # visible TOP surface of the slab has enough vertical span
        # for the diagonal 3-D ribs to read clearly (user: "cảm giác
        # 3D hơn").  HIGH keeps the thinner slab look.
        z_off_back = 0.10  # same depth offset for both LOW and HIGH
        z_back = min(1.0, z + z_off_back)
        x_lb, x_rb = self._span_x(cam, z_back)
        y_tb, y_bb = self._y_band(cam, z_back)

        if self.kind == 'low':
            return self._draw_low(canvas, cam, z,
                                  x_l, x_r, y_t, y_b,
                                  x_lb, x_rb, y_tb, y_bb)
        return self._draw_high(canvas, cam, z,
                               x_l, x_r, y_t, y_b,
                               x_lb, x_rb, y_tb, y_bb)

    @classmethod
    def _load_texture(cls, path: str | None) -> np.ndarray | None:
        if not path:
            return None
        key = str(path)
        if key not in cls._tex_cache:
            img = cv2.imread(key, cv2.IMREAD_COLOR)
            if img is not None:
                h, w = img.shape[:2]
                if max(h, w) > 512:
                    sc = 512.0 / float(max(h, w))
                    img = cv2.resize(
                        img,
                        (max(1, int(round(w * sc))), max(1, int(round(h * sc)))),
                        interpolation=cv2.INTER_AREA,
                    )
                cls._tex_cache[key] = img
            else:
                cls._tex_cache[key] = None
        return cls._tex_cache.get(key)

    @classmethod
    def _load_hole_mask(cls, path: str | None) -> np.ndarray | None:
        if not path:
            return None
        key = str(path)
        if key not in cls._hole_mask_cache:
            img = cv2.imread(key, cv2.IMREAD_UNCHANGED)
            if img is not None and len(img.shape) == 3 and img.shape[2] == 4:
                cls._hole_mask_cache[key] = img
            else:
                cls._hole_mask_cache[key] = None
        return cls._hole_mask_cache.get(key)

    def _draw_textured_quad(self, canvas: np.ndarray, poly: np.ndarray, tex: np.ndarray) -> bool:
        H, W = canvas.shape[:2]
        x0, y0 = poly.min(axis=0)
        x1, y1 = poly.max(axis=0) + 1
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(W, int(x1)), min(H, int(y1))
        if x1 <= x0 or y1 <= y0:
            return False
        bw, bh = x1 - x0, y1 - y0
        poly_local = poly.astype(np.int32) - np.array([x0, y0], dtype=np.int32)
        th, tw = tex.shape[:2]
        src_pts = np.float32([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]])
        dst_pts = poly_local.astype(np.float32)
        M = cv2.getPerspectiveTransform(src_pts, dst_pts)
        warped = cv2.warpPerspective(tex, M, (bw, bh))
        mask = np.zeros((bh, bw), dtype=np.uint8)
        cv2.fillPoly(mask, [poly_local], 255)
        roi = canvas[y0:y1, x0:x1]
        roi[mask > 0] = warped[mask > 0]
        return True

    def _draw_middle(self, canvas: np.ndarray, cam: "PerspectiveCamera", cur_frame: int):
        z = self.depth(cur_frame)
        if z < -1.0:
            return canvas
        base_canvas = canvas.copy()
        H, _W = canvas.shape[:2]

        # CRITICAL: low/high use 2D-legacy converge ((1-z)^1) while the floor
        # renderer projects WORLD coords with cam.project() (true 1/z).  At
        # z>0 the legacy formula tapers slower → the slab ends up wider than
        # the floor at the same depth.  For `middle` we MUST match the floor
        # exactly, so we project the same world points that _draw_floor_bg
        # projects.
        if cam.n_lanes >= 2:
            outer_left = cam.lane_world_x(0)
            outer_right = cam.lane_world_x(cam.n_lanes - 1)
            half_step = abs(outer_right - outer_left) / max(1, cam.n_lanes - 1) * 0.5
        else:
            outer_left, outer_right, half_step = -0.95, +0.95, 0.5
        xw_left = outer_left - half_step
        xw_right = outer_right + half_step
        wz = cam.z_from_norm(max(0.0, min(1.0, z)))
        p_l = cam.project(xw_left, cam.FLOOR_WORLD_Y, wz)
        p_r = cam.project(xw_right, cam.FLOOR_WORLD_Y, wz)
        if p_l is None or p_r is None:
            return canvas
        x_l = int(round(p_l[0]))
        x_r = int(round(p_r[0]))
        y_bot_f = float(p_l[1])  # exact floor y at this depth
        if x_l > x_r:
            x_l, x_r = x_r, x_l

        # Height in WORLD space, projected with the same 1/z perspective as
        # width and floor.  Calibrated so at z_norm=0 (hit zone) the screen
        # height equals 3× HIGH_HEIGHT_FRAC × cam.H (= 3× a high block at z=0).
        # This gives true perspective scaling: as the block approaches,
        # height grows at the SAME rate as width — feels like the block is
        # moving toward the camera, not growing taller in place.
        world_dh = 3.9 * self.HIGH_HEIGHT_FRAC * cam.H * cam.Z_NEAR / cam.fy
        p_top = cam.project(xw_left, cam.FLOOR_WORLD_Y - world_dh, wz)
        if p_top is None:
            return canvas
        y_bot = int(round(y_bot_f))
        y_top = int(round(float(p_top[1])))
        y_top = max(0, min(H - 1, y_top))
        y_bot = max(0, min(H - 1, y_bot))
        if y_bot <= y_top:
            return canvas
        wall_poly = np.array(
            [(x_l, y_top), (x_r, y_top), (x_r, y_bot), (x_l, y_bot)],
            dtype=np.int32,
        )
        tex = self._load_texture(self.texture_path)
        if tex is not None:
            self._draw_textured_quad(canvas, wall_poly, tex)
        else:
            cv2.fillConvexPoly(canvas, wall_poly, CLR_WALL_PINK)
            stripe_col = (15, 5, 25)
            for i in range(1, 24):
                x = int(x_l + (x_r - x_l) * i / 24.0)
                cv2.line(canvas, (x, y_top), (x, y_bot), stripe_col, 1, cv2.LINE_AA)
        # Middle block now renders as a solid obstacle by default.
        # Only apply cutout when user explicitly provides a mask path.
        if self.hole_mask_path:
            self._punch_hole(canvas, wall_poly, base_canvas)

        # Fade-out across the last 1/10 of travel time.  Alpha goes
        # 1.0 → 0.0 as p_lin moves through [9/10, 1].  We blend the block
        # back toward `base_canvas`; non-block pixels are identical in
        # both buffers so they pass through unchanged.
        travel_f = max(1, self.hit_frame - self.spawn_frame)
        p_lin = (cur_frame - self.spawn_frame) / travel_f
        FADE_START = 0.9
        if p_lin > FADE_START:
            if p_lin >= 1.0:
                canvas[:] = base_canvas
            else:
                alpha = 1.0 - (p_lin - FADE_START) / (1.0 - FADE_START)
                alpha = max(0.0, min(1.0, alpha))
                cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
        return canvas

    def _punch_hole(self, canvas: np.ndarray, wall_poly: np.ndarray,
                    base_canvas: np.ndarray) -> None:
        H, W = canvas.shape[:2]
        cx = W // 2
        cy = int((int(wall_poly[0][1]) + int(wall_poly[2][1])) * 0.5)
        mask = self._load_hole_mask(self.hole_mask_path)
        if mask is not None:
            wall_h = max(1, int(wall_poly[2][1] - wall_poly[0][1]))
            target_h = max(1, int(wall_h * 0.7))
            mh, mw = mask.shape[:2]
            target_w = max(1, int(target_h * mw / max(1, mh)))
            resized = cv2.resize(mask, (target_w, target_h), interpolation=cv2.INTER_AREA)
            x0 = cx - target_w // 2
            y0 = cy - target_h // 2
            x1 = x0 + target_w
            y1 = y0 + target_h
            sx0 = max(0, -x0)
            sy0 = max(0, -y0)
            x0 = max(0, x0)
            y0 = max(0, y0)
            x1 = min(W, x1)
            y1 = min(H, y1)
            if x1 > x0 and y1 > y0:
                crop = resized[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
                alpha = crop[:, :, 3].astype(np.float32) / 255.0
                hole = 1.0 - alpha
                roi = canvas[y0:y1, x0:x1].astype(np.float32)
                base_roi = base_canvas[y0:y1, x0:x1].astype(np.float32)
                out = roi * (1.0 - hole[..., None]) + base_roi * hole[..., None]
                canvas[y0:y1, x0:x1] = out.astype(np.uint8)
                return
        # No fallback rectangular cutout: if mask is absent/invalid,
        # keep the block fully filled.

    # -- per-kind rendering -------------------------------------------------
    def _draw_low(self, canvas, cam, z,
                  x_l, x_r, y_t, y_b,
                  x_lb, x_rb, y_tb, y_bb):
        """Ground slab — a 3-D neon brick with a visible TOP face.

        Camera sits above floor level, so for LOW we SEE the top of
        the slab.  The top is drawn as a darker pink trapezoid
        tapering toward the back-face in perspective; the front face
        carries the bright magenta "energy bar" stripes.  The front
        face uses a ROUNDED rectangle (user: "bo góc để mềm mại
        hơn") and the vertical laser stripes are spaced wider than
        before (user: "giãn các đường kẻ") so the bar reads less
        busy at close range.  No white outline — edges read via the
        tonal step between top and front faces plus a soft rim
        highlight tracing the rounded top edge.
        """
        tex = self._load_texture(self.texture_path)
        if tex is not None:
            front_poly = np.array([(x_l, y_t), (x_r, y_t), (x_r, y_b), (x_l, y_b)], np.int32)
            self._draw_textured_quad(canvas, front_poly, tex)
            return canvas

        front_col    = CLR_WALL_PINK         # bright magenta
        top_col      = (140, 25, 190)        # warm pink for top face
        top_edge_col = (15, 5, 25)           # near-black: used for BOTH
                                             # top-face ribs and front-face
                                             # vertical stripes so the two
                                             # faces read as a single
                                             # grooved brick (user: "số
                                             # vạch các mặt phải đồng nhất")

        scale = cam.scale(z)

        # --- Stripe count — FIXED at 24 lines per face ───────────────────
        # User: "số line đen 2 mặt fix cứng là 24 line".  Same count on
        # both top and front, paired 1-1 so each top rib continues as
        # its matching front stripe and forms a seamless groove
        # wrapping the brick's front-top edge.
        N_STRIPES = 24
        rib_w = max(2, int(2.4 * scale))

        # Pre-compute paired (front-edge X, back-edge X) coordinates.
        strip_xs = []
        for i in range(1, N_STRIPES):       # skip i=0 and i=N_STRIPES
            f = i / N_STRIPES
            sxf = int(x_l  + (x_r  - x_l)  * f)
            sxb = int(x_lb + (x_rb - x_lb) * f)
            strip_xs.append((sxf, sxb))

        # --- TOP face: trapezoid from (front-top line) to (back-top line)
        top_poly = np.array([(x_l, y_t), (x_r, y_t),
                             (x_rb, y_tb), (x_lb, y_tb)], np.int32)
        cv2.fillPoly(canvas, [top_poly], top_col)

        # Dark diagonal ribs that trace from the front edge back to
        # the vanishing point.  Drawn BEFORE the front face so the
        # rounded front carves cleanly over the front ends.
        for sxf, sxb in strip_xs:
            cv2.line(canvas, (sxf, y_t), (sxb, y_tb),
                     top_edge_col, rib_w, lineType=cv2.LINE_AA)

        # --- FRONT face (rounded rectangle) --------------------------------
        # User: "xóa đường kẻ trắng phân cách 2 mặt đi" — the rim
        # highlight that used to trace the front-top arc has been
        # removed, so the transition between top and front faces is
        # read purely from the tonal step + the continuous dark
        # grooves wrapping from one face to the other.
        r = max(3, int(min(x_r - x_l, y_b - y_t) * 0.22))
        front_pts = _rounded_rect_points(x_l, y_t, x_r, y_b, r, n=10)
        cv2.fillPoly(canvas, [front_pts], front_col)

        # Vertical BLACK stripes on the front face at the SAME x-
        # positions as the top ribs (each front stripe is the
        # downward continuation of one top rib).  Clipped to the
        # rounded silhouette via a mask so they don't poke out at
        # the rounded corners.
        mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
        cv2.fillPoly(mask, [front_pts], 255)
        temp = canvas.copy()
        for sxf, _ in strip_xs:
            cv2.line(temp, (sxf, y_t), (sxf, y_b),
                     top_edge_col, rib_w, lineType=cv2.LINE_AA)
        canvas[mask > 0] = temp[mask > 0]
        return canvas

    def _draw_high(self, canvas, cam, z,
                   x_l, x_r, y_t, y_b,
                   x_lb, x_rb, y_tb, y_bb):
        """Overhead bar — neon 3-D brick matching LOW block style.

        Camera is below this block, so the visible secondary face is
        the BOTTOM face (mirrors LOW's top face).  Front face is the
        same rounded-rect magenta with 24 black vertical stripes.
        Bottom face uses 24 matching diagonal ribs so the grooves
        wrap continuously around the bottom-front edge — identical
        design language to the ground slab (user: "thiết kế lại vật
        cản treo", obstacle-visual-style rule).
        """
        tex = self._load_texture(self.texture_path)
        if tex is not None:
            front_poly = np.array([(x_l, y_t), (x_r, y_t), (x_r, y_b), (x_l, y_b)], np.int32)
            self._draw_textured_quad(canvas, front_poly, tex)
            return canvas

        front_col    = CLR_WALL_PINK          # bright magenta
        bot_col      = (140, 25, 190)         # warm purple — bottom face
        top_col      = (70, 25, 95)           # dark plum — hidden top face
        groove_col   = (15, 5, 25)            # near-black grooves (both faces)

        scale = cam.scale(z)

        # ── fixed 24-line groove system (matches LOW) ─────────────────────
        N_STRIPES = 24
        rib_w = max(2, int(2.4 * scale))

        strip_xs = []
        for i in range(1, N_STRIPES):
            f = i / N_STRIPES
            sxf = int(x_l  + (x_r  - x_l)  * f)
            sxb = int(x_lb + (x_rb - x_lb) * f)
            strip_xs.append((sxf, sxb))

        # ── TOP face (hidden / far side) — dark fill only, no ribs ───────
        top_poly = np.array([(x_l, y_t), (x_r, y_t),
                             (x_rb, y_tb), (x_lb, y_tb)], np.int32)
        cv2.fillPoly(canvas, [top_poly], top_col)

        # ── BOTTOM face — trapezoid visible from below ────────────────────
        bot_poly = np.array([(x_l, y_b), (x_r, y_b),
                             (x_rb, y_bb), (x_lb, y_bb)], np.int32)
        cv2.fillPoly(canvas, [bot_poly], bot_col)

        # Diagonal ribs on the bottom face, converging to vanishing point.
        # Each rib runs from the front-bottom edge backward — same x-coords
        # as the matching front stripes.
        for sxf, sxb in strip_xs:
            cv2.line(canvas, (sxf, y_b), (sxb, y_bb),
                     groove_col, rib_w, lineType=cv2.LINE_AA)

        # ── FRONT face (rounded rectangle) ───────────────────────────────
        r = max(3, int(min(x_r - x_l, y_b - y_t) * 0.22))
        front_pts = _rounded_rect_points(x_l, y_t, x_r, y_b, r, n=10)
        cv2.fillPoly(canvas, [front_pts], front_col)

        # Vertical black stripes clipped to rounded silhouette.
        mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
        cv2.fillPoly(mask, [front_pts], 255)
        temp = canvas.copy()
        for sxf, _ in strip_xs:
            cv2.line(temp, (sxf, y_t), (sxf, y_b),
                     groove_col, rib_w, lineType=cv2.LINE_AA)
        canvas[mask > 0] = temp[mask > 0]
        return canvas


class WallTarget(Target):
    """Laser-stripe wall obstacle spanning 2 adjacent lanes."""

    def __init__(self, spawn_frame, hit_frame, lane, is_left, span: int = 2):
        super().__init__(spawn_frame, hit_frame, lane, is_left)
        self.span = span

    def draw(self, canvas, cam, cur_frame):
        if self.state != 'flying':
            return canvas
        z = self.depth(cur_frame)
        # Full tunnel-width wall if span covers all lanes, otherwise lane-based
        if self.span >= cam.n_lanes:
            x_l = int(cam.wall_x(-1, z)) + int(cam.W * 0.02 * cam.scale(z))
            x_r = int(cam.wall_x(+1, z)) - int(cam.W * 0.02 * cam.scale(z))
        else:
            l_start = self.lane - (self.span - 1) / 2.0
            l_end   = self.lane + (self.span - 1) / 2.0
            x_l = int(cam.lane_x(l_start - 0.45, z))
            x_r = int(cam.lane_x(l_end   + 0.45, z))
        y_b = int(cam.floor_y(z))
        y_t = int(cam.ceil_y(z) + (y_b - cam.ceil_y(z)) * 0.10)  # drop a bit
        s = cam.scale(z)

        # back face (slightly deeper)
        z_back = min(1.0, z + 0.06)
        if self.span >= cam.n_lanes:
            x_lb = int(cam.wall_x(-1, z_back)) + int(cam.W * 0.02 * cam.scale(z_back))
            x_rb = int(cam.wall_x(+1, z_back)) - int(cam.W * 0.02 * cam.scale(z_back))
        else:
            x_lb = int(cam.lane_x(l_start - 0.45, z_back))
            x_rb = int(cam.lane_x(l_end   + 0.45, z_back))
        y_bb = int(cam.floor_y(z_back))
        y_tb = int(cam.ceil_y(z_back) + (y_bb - cam.ceil_y(z_back)) * 0.10)

        # side slabs connecting front and back
        for poly_pts in (
            np.array([(x_l, y_b), (x_r, y_b), (x_rb, y_bb), (x_lb, y_bb)], np.int32),
            np.array([(x_l, y_t), (x_r, y_t), (x_rb, y_tb), (x_lb, y_tb)], np.int32),
        ):
            cv2.fillPoly(canvas, [poly_pts], (90, 30, 110))

        # front face with diagonal laser stripes
        front_poly = np.array([(x_l, y_t), (x_r, y_t),
                               (x_r, y_b), (x_l, y_b)], np.int32)
        mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
        cv2.fillPoly(mask, [front_poly], 255)
        sub_region = np.zeros_like(canvas)
        cv2.fillPoly(sub_region, [front_poly], (80, 20, 120))
        # draw diagonal stripes
        stripe_spacing = max(4, int(18 * s))
        xs_min = min(x_l, x_lb)
        xs_max = max(x_r, x_rb) + (y_b - y_t)
        for sx in range(xs_min - (y_b - y_t), xs_max, stripe_spacing):
            pt1 = (sx, y_t)
            pt2 = (sx + (y_b - y_t), y_b)
            cv2.line(sub_region, pt1, pt2, CLR_WALL_PINK,
                     max(2, int(3 * s)), lineType=cv2.LINE_AA)
        # composite stripes only inside front_poly mask
        canvas[mask > 0] = cv2.addWeighted(canvas, 0.2, sub_region, 0.8, 0)[mask > 0]
        cv2.polylines(canvas, [front_poly], True, (120, 50, 200),
                      max(2, int(2 * s)), lineType=cv2.LINE_AA)
        return canvas


# ── Particle ──────────────────────────────────────────────────────────────────
class Particle:
    __slots__ = ('x', 'y', 'vx', 'vy', 'size', 'color', 'life', 'max_life', 'angle', 'spin')

    def __init__(self, x, y, vx, vy, size, color, life):
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.size = size
        self.color = color
        self.life = life
        self.max_life = life
        self.angle = random.uniform(0, 2 * math.pi)
        self.spin = random.uniform(-0.35, 0.35)

    def update(self):
        self.x += self.vx
        self.y += self.vy
        self.vy += 0.45     # gravity
        self.vx *= 0.98
        self.angle += self.spin
        self.life -= 1

    def alive(self) -> bool:
        return self.life > 0

    def draw(self, canvas):
        if self.life <= 0:
            return
        t = self.life / self.max_life
        sz = max(1, int(self.size * (0.4 + 0.6 * t)))
        col = tuple(int(c * (0.4 + 0.6 * t)) for c in self.color)
        # rotated square
        cs = math.cos(self.angle) * sz
        sn = math.sin(self.angle) * sz
        pts = np.array([
            (self.x - cs + sn, self.y - sn - cs),
            (self.x + cs + sn, self.y + sn - cs),
            (self.x + cs - sn, self.y + sn + cs),
            (self.x - cs - sn, self.y - sn + cs),
        ], dtype=np.int32)
        cv2.fillPoly(canvas, [pts], col)


class SideRailRenderer:
    """Draws 3-D floating neon barriers alongside the runway.

    Each barrier segment is a 3-D box that floats ABOVE the floor with a gap
    on both X (away from lane tiles) and Y (lifted off the floor surface), so
    it never overlaps the floor.  Three faces are rendered per segment:
      - inner face  (faces the runway centre — brightest, neon glow)
      - top face    (faces the camera slightly — dimmed ~70%, gives 3-D depth)
      - front face  (nearest Z edge of each chunk — dimmed ~55%, chunky only)

    Parameters
    ----------
    height      : box height in world-Y units (default 0.14)
    offset_x    : X gap from outer tile edge to inner face (default 0.08)
    pulse       : 'none' | 'beat' | 'rms'
    """

    _tex_cache: dict[object, np.ndarray] = {}

    # Box proportions relative to height
    _BOX_LIFT_FRAC  = 0.50   # gap below box = height * this  (Y clearance)
    _BOX_DEPTH_FRAC = 0.55   # box X thickness = height * this

    def __init__(
        self,
        cam: "PerspectiveCamera",
        *,
        color: str = "#FF60FF",
        shape: str = "chunky",
        height: float = 0.14,
        offset_x: float = 0.08,
        image_path: str | None = None,
        texture_non_loop: bool = False,
        pulse: str = "beat",
        pulse_intensity: float = 0.6,
        chevron_depth: float = 1.0,
        chevron_density: int = 6,
        pillar_count: int = 16,
        pillar_radius: float = 1.0,
        chase_mode: str = "time",
        chase_speed_frames: int = 4,
        dot_count: int = 24,
        dot_lines: int = 1,
        dot_size_px: int = 6,
        dot_anim_mode: str = "audio",
        dot_color_near: str = "#FF60FF",
        dot_color_far: str = "#00FFFF",
    ):
        self._cam    = cam
        self._shape  = shape.lower()
        if self._shape in {"pillar", "dot"}:
            image_path = None
        self._texture_non_loop = bool(texture_non_loop)
        self._height = max(0.03, float(height))
        self._pulse  = pulse.lower()
        self._pi     = float(np.clip(pulse_intensity, 0.0, 1.0))
        self._chev_depth   = float(max(0.1, chevron_depth))
        self._chev_density = int(max(2, min(20, chevron_density)))
        self._pillar_count = max(4, min(32, int(pillar_count)))
        self._pillar_radius = float(max(0.2, min(2.0, pillar_radius)))
        self._chase_mode = str(chase_mode).lower()
        if self._chase_mode not in ("time", "beat"):
            self._chase_mode = "time"
        self._chase_speed_frames = max(1, int(chase_speed_frames))
        self._chase_step = 0
        self._chase_frame_counter = 0
        self._dot_count = max(8, min(64, int(dot_count)))
        self._dot_lines = max(1, min(8, int(dot_lines)))
        self._dot_size_px = max(2, min(20, int(dot_size_px)))
        self._dot_anim_mode = str(dot_anim_mode).lower()
        if self._dot_anim_mode not in ("audio", "twinkle", "wave"):
            self._dot_anim_mode = "audio"
        self._dot_near_bgr = np.array(
            _hex_to_bgr(dot_color_near, default=(255, 96, 255)),
            dtype=np.float32,
        )
        self._dot_far_bgr = np.array(
            _hex_to_bgr(dot_color_far, default=(255, 255, 0)),
            dtype=np.float32,
        )

        # Color
        try:
            h = color.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            self._base_bgr = np.array([b, g, r], dtype=np.float32)
        except Exception:
            self._base_bgr = np.array([255, 96, 255], dtype=np.float32)

        # Texture (optional, applied to inner face).
        # Downscale oversized inputs because rail faces are small on-screen;
        # high-res sources only add warp cost with negligible visual gain.
        self._tex: np.ndarray | None = None
        if image_path:
            cache_key = (image_path, "v2")
            if cache_key not in SideRailRenderer._tex_cache:
                img = cv2.imread(image_path, cv2.IMREAD_COLOR)
                if img is not None:
                    MAX_TEX_EDGE = 256
                    h, w = img.shape[:2]
                    longest = max(h, w)
                    if longest > MAX_TEX_EDGE:
                        scale = MAX_TEX_EDGE / float(longest)
                        new_w = max(1, int(round(w * scale)))
                        new_h = max(1, int(round(h * scale)))
                        img = cv2.resize(
                            img, (new_w, new_h),
                            interpolation=cv2.INTER_AREA,
                        )
                    SideRailRenderer._tex_cache[cache_key] = img
            self._tex = SideRailRenderer._tex_cache.get(cache_key)

        # Outer edge of the outermost floor tile
        # (mirrors TunnelRenderer: tile_w = max(0.25, step*0.80))
        if cam.n_lanes >= 2:
            step = abs(cam.lane_world_x(1) - cam.lane_world_x(0))
        else:
            step = cam.LANE_WORLD_X * 2
        tile_half_w  = max(0.25, step * 0.80) / 2.0
        outer_tile_x = cam.LANE_WORLD_X + tile_half_w

        # Box X boundaries (inner face / outer face)
        gap          = max(0.0, float(offset_x))
        box_depth    = self._height * self._BOX_DEPTH_FRAC
        # right side
        self._ri_r   =  outer_tile_x + gap              # inner face X (right)
        self._ro_r   =  outer_tile_x + gap + box_depth  # outer face X (right)
        # left side (mirrored)
        self._ri_l   = -(outer_tile_x + gap)
        self._ro_l   = -(outer_tile_x + gap + box_depth)

        # Box Y boundaries.  When the camera carries an explicit wall-floor
        # gap (driven by the editor's "Gap" handles → wall_floor_gap_frac),
        # the rail bottom snaps to that exact world Y so dragging the
        # handles visibly moves the rail's bottom edge towards/away from
        # the floor sides.  Without an override we use the legacy default
        # lift (height × _BOX_LIFT_FRAC) so existing setups look identical.
        if getattr(cam, "WALL_BOTTOM_WORLD_Y", None) is not None:
            self._bot_y = float(cam.WALL_BOTTOM_WORLD_Y)
            self._top_y = self._bot_y - self._height
        else:
            lift         = self._height * self._BOX_LIFT_FRAC
            self._bot_y  = cam.FLOOR_WORLD_Y - lift
            self._top_y  = cam.FLOOR_WORLD_Y - lift - self._height

        # Z grid
        self._z_slices = np.linspace(cam.Z_NEAR, cam.Z_FAR, 32)
        self._pillar_zs = np.linspace(cam.Z_FAR, cam.Z_NEAR, self._pillar_count)
        spacing = (
            abs(self._pillar_zs[1] - self._pillar_zs[0])
            if self._pillar_count > 1 else 1.0
        )
        self._pillar_half_z = spacing * 0.20
        self._dot_zs = np.linspace(cam.Z_NEAR, cam.Z_FAR, self._dot_count)
        if self._dot_count > 1:
            ts = np.linspace(0.0, 1.0, self._dot_count)[:, None]
        else:
            ts = np.array([[0.0]])
        self._dot_base_colors = (
            self._dot_near_bgr[None, :] * (1 - ts) + self._dot_far_bgr[None, :] * ts
        )

    # ------------------------------------------------------------------
    def _effective_color(self, bass_val: float, hit: bool) -> np.ndarray:
        if self._pulse == "none" or self._pi == 0:
            return self._base_bgr
        flash = (self._pi if hit else 0.0) if self._pulse == "beat" \
                else float(bass_val) * self._pi
        return np.clip(
            self._base_bgr + flash * (255 - self._base_bgr), 0, 255
        ).astype(np.float32)

    def _advance_chase(self, hit: bool) -> None:
        """Advance pillar highlight index according to chase mode."""
        if self._chase_mode == "beat":
            if hit:
                self._chase_step = (self._chase_step + 1) % self._pillar_count
            return
        self._chase_frame_counter += 1
        if self._chase_frame_counter >= self._chase_speed_frames:
            self._chase_frame_counter = 0
            self._chase_step = (self._chase_step + 1) % self._pillar_count

    # ------------------------------------------------------------------
    def draw(
        self,
        canvas: np.ndarray,
        frame_idx: int,
        bass_val: float = 0.0,
        hit: bool = False,
    ) -> np.ndarray:
        color  = self._effective_color(bass_val, hit)
        bgr_t  = (int(color[0]), int(color[1]), int(color[2]))
        # Top face is slightly dimmer (shading cue)
        dim    = np.clip(color * 0.65, 0, 255).astype(np.float32)
        bgr_d  = (int(dim[0]),  int(dim[1]),  int(dim[2]))
        # Front face even dimmer
        dim2   = np.clip(color * 0.45, 0, 255).astype(np.float32)
        bgr_d2 = (int(dim2[0]), int(dim2[1]), int(dim2[2]))

        if self._shape == "pillar":
            self._advance_chase(hit)

        for wx_i, wx_o in (
            (self._ri_r, self._ro_r),   # right rail
            (self._ri_l, self._ro_l),   # left rail
        ):
            if self._shape == "dot":
                self._draw_dots(canvas, wx_i, wx_o, color, frame_idx, bass_val)
            elif self._shape == "pillar":
                self._draw_pillar(canvas, wx_i, wx_o, color)
            elif self._shape == "tube":
                self._draw_tube(canvas, wx_i, wx_o, bgr_t, bgr_d, color)
            elif self._shape == "chevron":
                self._draw_chevrons(canvas, wx_i, wx_o, bgr_t, bgr_d,
                                    color, frame_idx)
            else:
                self._draw_chunky(canvas, wx_i, wx_o, bgr_t, bgr_d,
                                  bgr_d2, color)
        return canvas

    # ── helpers ──────────────────────────────────────────────────────────
    def _project_quad(self, corners4):
        """Project 4 world points → int32 poly, or None if any behind cam."""
        pts = [self._cam.project(x, y, z) for x, y, z in corners4]
        if any(p is None for p in pts):
            return None
        return np.array([[int(p[0]), int(p[1])] for p in pts], dtype=np.int32)

    def _fill_face(self, canvas, corners4, bgr, color, glow=True):
        poly = self._project_quad(corners4)
        if poly is None:
            return
        if self._tex is not None:
            H, W = canvas.shape[:2]
            # Bbox of poly clipped to canvas bounds
            x0, y0 = poly.min(axis=0)
            x1, y1 = poly.max(axis=0) + 1
            x0, y0 = max(0, int(x0)), max(0, int(y0))
            x1, y1 = min(W, int(x1)), min(H, int(y1))
            if x1 <= x0 or y1 <= y0:
                return
            bw, bh = x1 - x0, y1 - y0
            # Translate poly into bbox-local coords
            poly_local = poly - np.array([x0, y0], dtype=np.int32)
            # Warp output to bbox size only (NOT full canvas)
            th, tw = self._tex.shape[:2]
            src = np.float32([[0, 0], [tw, 0], [tw, th], [0, th]])
            M = cv2.getPerspectiveTransform(src, poly_local.astype(np.float32))
            warped = cv2.warpPerspective(self._tex, M, (bw, bh))
            # Mask in bbox-local space
            mask = np.zeros((bh, bw), dtype=np.uint8)
            cv2.fillPoly(mask, [poly_local], 255)
            # Composite into canvas ROI (no full-canvas scan)
            roi = canvas[y0:y1, x0:x1]
            roi[mask > 0] = warped[mask > 0]
        else:
            cv2.fillPoly(canvas, [poly], bgr)
        if glow and self._tex is None:
            col_arr = np.array([bgr[0], bgr[1], bgr[2]], dtype=np.float32)
            _draw_neon_edges(canvas, [poly], col_arr, 1)

    # ── chunky: floating 3-D box segments ────────────────────────────────
    def _draw_chunky(self, canvas, wx_i, wx_o, bgr_t, bgr_d, bgr_d2, color):
        zs   = self._z_slices
        step = zs[1] - zs[0]
        gap  = step * 0.38
        bot, top = self._bot_y, self._top_y
        for i in range(len(zs) - 1):
            z0 = zs[i]   + gap * 0.5
            z1 = zs[i+1] - gap * 0.5
            # Inner face (bright, faces runway)
            self._fill_face(canvas, [
                (wx_i, top, z0), (wx_i, top, z1),
                (wx_i, bot, z1), (wx_i, bot, z0),
            ], bgr_t, color, glow=True)
            # Top face (slightly dim, faces camera)
            self._fill_face(canvas, [
                (wx_i, top, z0), (wx_o, top, z0),
                (wx_o, top, z1), (wx_i, top, z1),
            ], bgr_d, color, glow=False)
            # Front face (nearest edge, most dim)
            self._fill_face(canvas, [
                (wx_i, top, z0), (wx_o, top, z0),
                (wx_o, bot, z0), (wx_i, bot, z0),
            ], bgr_d2, color, glow=False)

    # ── tube: continuous floating strip ──────────────────────────────────
    def _draw_tube(self, canvas, wx_i, wx_o, bgr_t, bgr_d, color):
        zs = self._z_slices
        bot, top = self._bot_y, self._top_y
        # Optional non-loop mode: map the texture ONCE across the full rail
        # length, instead of re-warping per segment (which visually tiles).
        if self._texture_non_loop and self._tex is not None:
            z0 = self._cam.Z_NEAR + 0.05
            z1 = self._cam.Z_FAR
            self._fill_face(canvas, [
                (wx_i, bot, z0), (wx_i, top, z0),
                (wx_i, top, z1), (wx_i, bot, z1),
            ], bgr_t, color, glow=True)
            self._fill_face(canvas, [
                (wx_i, top, z0), (wx_o, top, z0),
                (wx_o, top, z1), (wx_i, top, z1),
            ], bgr_d, color, glow=False)
            return
        for i in range(len(zs) - 1):
            z0, z1 = zs[i], zs[i + 1]
            # Inner face: continuous strip facing runway.
            self._fill_face(canvas, [
                (wx_i, bot, z0), (wx_i, top, z0),
                (wx_i, top, z1), (wx_i, bot, z1),
            ], bgr_t, color, glow=True)
            # Top face: depth cue, slightly dimmer.
            self._fill_face(canvas, [
                (wx_i, top, z0), (wx_o, top, z0),
                (wx_o, top, z1), (wx_i, top, z1),
            ], bgr_d, color, glow=False)

    # ── chevron: glowing open-V neon lines on inner face (>>> style) ─────
    def _draw_chevrons(self, canvas, wx_i, wx_o, bgr_t, bgr_d,
                       color, frame_idx):
        """Draw wall chevrons as glowing open-V neon outlines (no fill).

        Each chevron is a simple 3-point open polyline:
            (top, z_base) → (mid, z_tip) → (bot, z_base)
        rendered with a Gaussian-blurred halo + bright core, identical in
        look to the >>> neon arrows in the reference image.
        """
        cam    = self._cam
        bot, top = self._bot_y, self._top_y
        mid_y  = (bot + top) * 0.5
        half_h = (bot - top) * 0.5          # positive (bot > top in world Y)

        n_slots   = self._chev_density
        spacing   = (cam.Z_FAR - cam.Z_NEAR) / n_slots
        scroll    = (frame_idx * 0.30) % spacing
        _z_safe   = cam.Z_NEAR + 0.05

        # Base arrow_len for 120° opening at the near reference depth,
        # then scaled by the user-facing chevron_depth multiplier.
        _wz_ref   = cam.Z_NEAR + spacing
        _fy_fx    = (cam.fy / cam.fx) if cam.fx > 0 else 1.0
        _wx_safe  = max(0.1, abs(wx_i))
        arrow_len = _fy_fx * half_h * _wz_ref / (_wx_safe * math.sqrt(3))
        arrow_len = max(spacing * 0.06, min(spacing * 0.40, arrow_len))
        arrow_len *= self._chev_depth   # user depth multiplier (>1 = more pointed)

        polys: list[tuple[float, np.ndarray, float]] = []
        for i in range(n_slots + 1):
            wz = cam.Z_NEAR + i * spacing - scroll
            if wz <= _z_safe:
                continue
            z_tip  = max(_z_safe, wz - arrow_len * 0.5)
            z_base = wz + arrow_len * 0.5

            # Open V: 3 world points on the inner face (X = wx_i)
            corners_w = [
                (wx_i, top,   z_base),   # top-far  (open end)
                (wx_i, mid_y, z_tip),    # center tip (near end)
                (wx_i, bot,   z_base),   # bot-far  (open end)
            ]
            proj = [cam.project(x, y, z) for x, y, z in corners_w]
            if any(p is None for p in proj):
                continue
            depth_factor = max(0.15, min(1.0, 5.0 / wz))
            pts = np.array(
                [(int(round(p[0])), int(round(p[1]))) for p in proj],
                dtype=np.int32,
            )
            polys.append((wz, pts, depth_factor))

        polys.sort(key=lambda t: -t[0])   # far first → near last (on top)

        H_cv, W_cv = canvas.shape[:2]
        for _, pts, df in polys:
            # Scale brightness by depth
            scaled = np.clip(color * (0.35 + 0.65 * df), 0, 255).astype(np.float32)
            glow_col = tuple(int(min(255.0, c * 1.2 + 20)) for c in scaled)
            core_col = tuple(int(min(255.0, c * 0.55 + 130)) for c in scaled)
            core_thick = max(2, int(round(1.5 + df * 2.5)))
            wide = max(5, core_thick * 4)

            # Bounding box for the blurred halo (crop to avoid full-frame blur)
            xs, ys = pts[:, 0], pts[:, 1]
            pad = wide + 8
            x0 = max(0, int(xs.min()) - pad)
            y0 = max(0, int(ys.min()) - pad)
            x1 = min(W_cv, int(xs.max()) + pad + 1)
            y1 = min(H_cv, int(ys.max()) + pad + 1)
            if x1 <= x0 or y1 <= y0:
                continue

            crop = canvas[y0:y1, x0:x1]
            shifted = pts - np.array([x0, y0], dtype=pts.dtype)

            # Halo: blurred wide polyline blended via screen-mode (max)
            overlay = np.zeros_like(crop)
            cv2.polylines(overlay, [shifted], False, glow_col,
                          wide, lineType=cv2.LINE_AA)
            k = (wide * 2) | 1
            overlay = cv2.GaussianBlur(overlay, (k, k), 0)
            np.maximum(crop, overlay, out=crop)

            # Bright core: thin crisp line drawn directly on canvas
            cv2.polylines(canvas, [pts], False, core_col,
                          core_thick, lineType=cv2.LINE_AA)

    def _draw_pillar(self, canvas: np.ndarray, wx_i: float, wx_o: float,
                     color: np.ndarray) -> np.ndarray:
        """Draw cylindrical-looking pillar row with one running highlight head."""
        cam = self._cam
        bot, top = self._bot_y, self._top_y
        head_idx = self._chase_step
        dim_color = np.clip(color * 0.25, 0, 255).astype(np.float32)

        # Cylinder approximation: high-sided round prism around the Y axis.
        # Keep radius slightly inside the rail box bounds.
        x_c = (wx_i + wx_o) * 0.5
        radius_x = max(0.01, abs(wx_o - wx_i) * 0.46)
        radius_z = max(0.01, self._pillar_half_z * 0.96)
        radius_x *= self._pillar_radius
        radius_z *= self._pillar_radius
        n_sides = 20
        thetas = np.linspace(0.0, 2.0 * math.pi, n_sides + 1)

        for i, z_c in enumerate(self._pillar_zs):
            is_head = (i == head_idx)
            face_color = color if is_head else dim_color

            # Side facets
            for s in range(n_sides):
                t0 = float(thetas[s])
                t1 = float(thetas[s + 1])
                x0 = x_c + radius_x * math.cos(t0)
                z0 = z_c + radius_z * math.sin(t0)
                x1 = x_c + radius_x * math.cos(t1)
                z1 = z_c + radius_z * math.sin(t1)

                # Simple cylindrical shading: facets facing camera (-Z) are brighter.
                tm = 0.5 * (t0 + t1)
                facing = max(0.0, -math.sin(tm))
                shade = 0.55 + 0.45 * facing
                facet_color = np.clip(face_color * shade, 0, 255).astype(np.float32)
                bgr_facet = (
                    int(facet_color[0]),
                    int(facet_color[1]),
                    int(facet_color[2]),
                )

                self._fill_face(
                    canvas,
                    [(x0, top, z0), (x1, top, z1), (x1, bot, z1), (x0, bot, z0)],
                    bgr_facet,
                    color,
                    glow=is_head and facing > 0.85,
                )

            # Top cap (helps the pillar read as a cylinder, not a flat strip).
            top_pts = []
            for s in range(n_sides):
                tt = float(thetas[s])
                p = cam.project(
                    x_c + radius_x * math.cos(tt),
                    top,
                    z_c + radius_z * math.sin(tt),
                )
                if p is None:
                    top_pts = []
                    break
                top_pts.append((int(p[0]), int(p[1])))
            if len(top_pts) >= 3:
                top_poly = np.array(top_pts, dtype=np.int32)
                top_color = np.clip(face_color * 0.62, 0, 255).astype(np.float32)
                bgr_top = (int(top_color[0]), int(top_color[1]), int(top_color[2]))
                cv2.fillConvexPoly(canvas, top_poly, bgr_top)
                if is_head:
                    _draw_neon_edges(canvas, [top_poly], face_color, 1)

        return canvas

    def _draw_dots(
        self,
        canvas: np.ndarray,
        wx_i: float,
        wx_o: float,
        color: np.ndarray,
        frame_idx: int,
        bass_val: float,
    ) -> np.ndarray:
        """Draw glowing 2D dots projected on the rail inner edge."""
        cam = self._cam
        bot, top = self._bot_y, self._top_y

        n = self._dot_count
        base_size = self._dot_size_px
        anim = self._dot_anim_mode

        if anim == "audio":
            mod_per_dot = np.full(n, 0.4 + 0.6 * float(bass_val), dtype=np.float32)
        elif anim == "twinkle":
            period = 30
            phases = ((frame_idx + np.arange(n) * 7) % period) / float(period)
            mod_per_dot = (
                0.3 + 0.7 * (0.5 + 0.5 * np.sin(phases * 2 * np.pi))
            ).astype(np.float32)
        elif anim == "wave":
            wave_phase = (np.arange(n) / max(1, n - 1) + frame_idx * 0.05) * 2 * np.pi
            mod_per_dot = (
                0.4 + 0.6 * (0.5 + 0.5 * np.sin(wave_phase))
            ).astype(np.float32)
        else:
            mod_per_dot = np.ones(n, dtype=np.float32)

        pulse_norm = max(1e-3, float(np.linalg.norm(self._base_bgr)))
        pulse_factor = float(np.linalg.norm(color) / pulse_norm)

        if self._dot_lines <= 1:
            y_fracs = [0.5]
        else:
            # Split lines vertically on the wall (top -> bottom), not across rail width.
            y_fracs = [j / (self._dot_lines - 1) for j in range(self._dot_lines)]

        for i, wz in enumerate(self._dot_zs):
            bgr_val = self._dot_base_colors[i] * mod_per_dot[i] * pulse_factor
            bgr_val = np.clip(bgr_val, 0, 255)
            col_tuple = (int(bgr_val[0]), int(bgr_val[1]), int(bgr_val[2]))
            depth_factor = max(0.15, min(1.0, 5.0 / max(0.1, float(wz))))
            radius = max(2, int(round(base_size * depth_factor)))
            halo_col = tuple(int(c * 0.5) for c in col_tuple)

            for frac in y_fracs:
                wy = float(top + (bot - top) * frac)
                proj = cam.project(float(wx_i), wy, float(wz))
                if proj is None:
                    continue
                cx, cy = int(proj[0]), int(proj[1])
                cv2.circle(canvas, (cx, cy), radius, col_tuple, -1, lineType=cv2.LINE_AA)
                cv2.circle(
                    canvas,
                    (cx, cy),
                    int(radius * 1.6),
                    halo_col,
                    1,
                    lineType=cv2.LINE_AA,
                )

        return canvas


class ParticleSystem:
    def __init__(self):
        self.ps: list[Particle] = []

    def burst(self, x: int, y: int, color: tuple, count: int = 45):
        """Smaller, faster, more numerous chips (CapCut reference look)."""
        for _ in range(count):
            ang = random.uniform(0, 2 * math.pi)
            spd = random.uniform(4, 14)
            self.ps.append(Particle(
                x, y,
                math.cos(ang) * spd,
                math.sin(ang) * spd - random.uniform(1, 5),
                random.uniform(2.2, 5.5),   # smaller than before
                color,
                random.randint(12, 20),
            ))

    def update(self):
        for p in self.ps:
            p.update()
        self.ps = [p for p in self.ps if p.alive()]

    def draw(self, canvas):
        for p in self.ps:
            p.draw(canvas)


# ── HitFlash: horizontal line + curved light streaks on punch impact ─────────
class HitFlash:
    """Single hit-flash event: a bright horizontal slash through the center
    plus curved ellipse arcs sweeping outward on both sides.

    Lifetime: ~14 frames, fades out. Colour is cyan-white for a neon-motion
    feel (matches the CapCut reference VFX).
    """

    MAX_LIFE = 14

    def __init__(self, cx: int, cy: int, W: int, H: int):
        self.cx, self.cy = cx, cy
        self.W, self.H = W, H
        self.life = self.MAX_LIFE

    def update(self):
        self.life -= 1

    def alive(self) -> bool:
        return self.life > 0

    def draw(self, canvas: np.ndarray):
        if self.life <= 0:
            return
        t      = self.life / self.MAX_LIFE         # 1 -> 0
        alpha  = t ** 0.6
        expand = 1.0 - t

        W = self.W
        col_main = (255, 245, 230)                 # cyan-white (BGR)
        col_soft = (255, 220, 180)

        # Horizontal bright slash across the center (core + glow pass)
        slash_len   = int((W * 0.55) * (0.55 + 0.45 * expand))
        x0 = max(0, self.cx - slash_len)
        x1 = min(W - 1, self.cx + slash_len)
        under = tuple(int(c * alpha * 0.6) for c in col_soft)
        cv2.line(canvas, (x0, self.cy), (x1, self.cy),
                 under, max(3, int(7 * alpha)), lineType=cv2.LINE_AA)
        core = tuple(int(c * alpha) for c in col_main)
        cv2.line(canvas, (x0, self.cy), (x1, self.cy),
                 core, max(2, int(3 * alpha)), lineType=cv2.LINE_AA)


class HitFlashSystem:
    def __init__(self):
        self.fx: list[HitFlash] = []

    def burst(self, cx: int, cy: int, W: int, H: int):
        self.fx.append(HitFlash(cx, cy, W, H))

    def update(self):
        for f in self.fx:
            f.update()
        self.fx = [f for f in self.fx if f.alive()]

    def draw(self, canvas):
        for f in self.fx:
            f.draw(canvas)


# StickmanHUD now lives in its own module so it can be driven as a
# standalone effect (see: python src/stickman.py -i ...).  We just
# re-export the class here for backwards compatibility.
from stickman import StickmanHUD  # noqa: E402, F401


# ── HUD: Viewport shake blocks (4 neon panels in front of the view) ─────────
class ViewportFrame:
    """Four neon-outlined blocks floating at eye-level.

    Static decorative HUD that represents the player's "visor" / cockpit.
    On each punch hit, all four blocks receive a brief random jitter that
    decays over ~0.25s, selling the illusion of a real impact shaking the
    camera/viewport.
    """

    def __init__(self, cam: PerspectiveCamera,
                 neon_color: tuple[int, int, int] | None = None,
                 lane_aligned: bool = False,
                 mode: str = 'punch'):
        """`lane_aligned` is a legacy flag kept for API compat; both modes
        now always use lane-aligned panels so the 4 bottom tiles are the
        forward extension of the 4 lane rails (matches where targets
        actually fly along).  Internally we branch on `mode` to pick the
        right panel depth / width:

        * ``mode='dance'`` — panel back-edge coincides with
          ``DanceTarget`` front-edge at hit_frame, so the stomp tile
          lands flush onto its panel.
        * ``mode='punch'`` — panel back-edge = ``Z_NEAR`` (the hit
          line), panel half-width = ``0.40 × lane_step_world`` so
          adjacent panels leave a small neon seam.  This replaces the
          old hardcoded "cockpit HUD" trapezoids whose pixel positions
          didn't match the 3D lane projection and made flying cubes
          appear to drift off their lane rails near the horizon.
        """
        self.cam = cam
        W, H = cam.W, cam.H
        _ = lane_aligned  # retained for back-compat; ignored

        if mode == 'dance':
            # Flush with DanceTarget front-edge — matches tile landing.
            half_x = DanceTarget.HALF_X
            z_back = max(cam.Z_NEAR - DanceTarget.HALF_Z, 0.1)
        else:
            # Punch: auto-size to 80% of lane step; z_back = Z_NEAR (hit).
            if cam.n_lanes > 1:
                lane_step_world = abs(cam.lane_world_x(1) - cam.lane_world_x(0))
            else:
                lane_step_world = 0.40
            half_x = 0.40 * lane_step_world
            z_back = cam.Z_NEAR
        panels = self._build_lane_aligned_panels(half_x=half_x,
                                                 z_back=z_back)
        self.panels = panels

        # Shake state -------------------------------------------------------
        self.shake_amp   = 0.0   # current jitter magnitude (0..1)
        self.shake_decay = 0.78  # per-frame multiplicative decay
        self.max_offset  = max(4, int(min(W, H) * 0.008))  # peak px jitter
        self._rng        = np.random.default_rng(20260423)

        # Look --------------------------------------------------------------
        # Two states blended by `shake_amp`:
        #   amp == 0 → idle: faint grey outline, no glow.
        #   amp  > 0 → active: neon border + glow + cyan accent tick.
        # `neon_color` (BGR) overrides the default amber; pass None to keep it.
        self._neon_col  = neon_color if neon_color is not None else (255, 170, 90)
        # Warm "core" highlight = neon tinted toward white (brighter at peak).
        self._core_col  = tuple(
            int(min(255, 0.55 * c + 0.45 * 255)) for c in self._neon_col)
        self._accent    = (120, 200, 255)   # cyan accent bar (on hit only)
        self._idle_col  = (95,  95,  95)    # dim grey idle border

    # ------------------------------------------------------------------
    def _build_lane_aligned_panels(self,
                                   half_x: float | None = None,
                                   z_back: float | None = None,
                                   y_target_frac: float = 0.985) -> list:
        """Lane-aligned panels — each panel is lane *i*'s floor strip
        projected forward toward the camera.

        Parameters
        ----------
        half_x : float | None
            Panel half-width in world units.  ``None`` = ``DanceTarget.HALF_X``
            (tile-flush).  Callers (e.g. punch mode) can pass a custom
            value derived from lane step.
        z_back : float | None
            World depth of the panel's back edge (the edge nearer the
            tunnel / hit zone).  ``None`` = ``Z_NEAR - DanceTarget.HALF_Z``
            (coincides with dance tile's front-edge at hit_frame so the
            tile lands flush onto the panel).  Punch mode passes
            ``Z_NEAR`` so the panel back-edge = exactly the hit line —
            flying cubes then visibly enter the panel along its rail.
        y_target_frac : float
            Fraction of screen height the panel's front (closest) edge
            should project to.  Default 0.985 = just above the bottom.

        Net result: panels are the true 3D forward-continuation of each
        lane — both modes now share this geometry so targets flying in
        from the horizon travel exactly down their panel's rail.
        """
        cam = self.cam
        hx  = DanceTarget.HALF_X if half_x is None else float(half_x)
        if z_back is None:
            z_back = max(cam.Z_NEAR - DanceTarget.HALF_Z, 0.1)
        else:
            z_back = max(float(z_back), 0.1)

        # Solve z_front from the target bottom-of-screen y.  Floor plane Y
        # projects as y = cy_pix + fy * FLOOR_WORLD_Y / z, so:
        #   z = fy * FLOOR_WORLD_Y / (y_target - cy_pix)
        y_target = cam.H * y_target_frac
        denom    = y_target - cam.cy_pix
        if denom > 1.0:
            z_front = cam.fy * cam.FLOOR_WORLD_Y / denom
        else:
            z_front = max(z_back - 0.8, 0.1)
        # Safety: keep z_front < z_back with a minimum gap so the
        # trapezoid has real depth.
        z_front = max(min(z_front, z_back - 0.05), 0.1)

        panels = []
        for i in range(cam.n_lanes):
            wx = cam.lane_world_x(i)
            # Corner order = (BL, BR, TR, TL) in screen coords →
            # (front-left, front-right, back-right, back-left) in world.
            corners_w = (
                (wx - hx, cam.FLOOR_WORLD_Y, z_front),  # BL (closer)
                (wx + hx, cam.FLOOR_WORLD_Y, z_front),  # BR
                (wx + hx, cam.FLOOR_WORLD_Y, z_back),   # TR (farther)
                (wx - hx, cam.FLOOR_WORLD_Y, z_back),   # TL
            )
            proj = [cam.project(*p) for p in corners_w]
            if any(p is None for p in proj):
                continue
            panels.append(np.array(
                [(int(round(p[0])), int(round(p[1]))) for p in proj],
                dtype=np.int32))
        return panels

    # ------------------------------------------------------------------
    def trigger(self, intensity: float = 1.0):
        """Call on each punch hit to shake the viewport."""
        self.shake_amp = min(1.0, max(self.shake_amp, float(intensity)))

    def update(self):
        self.shake_amp *= self.shake_decay
        if self.shake_amp < 0.01:
            self.shake_amp = 0.0

    # ------------------------------------------------------------------
    def draw(self, canvas: np.ndarray):
        amp = self.shake_amp
        H, W = canvas.shape[:2]

        # Visual intensity follows the shake envelope: at amp=0 the panels
        # are just faint grey outlines, at amp>0 they light up with the
        # amber neon border + glow (same timing as the shake).
        active = amp > 0.0

        for poly in self.panels:
            # Per-panel independent jitter so tiles tremble slightly out of
            # phase (feels more organic than a single camera shake).
            if active:
                jx = (self._rng.random() - 0.5) * 2.0 * amp * self.max_offset
                jy = (self._rng.random() - 0.5) * 2.0 * amp * self.max_offset * 0.75
            else:
                jx = jy = 0.0
            offset = np.array([int(round(jx)), int(round(jy))], dtype=np.int32)
            pts = poly + offset

            # Dark semi-transparent interior so tunnel floor shows through.
            mask = np.zeros((H, W), dtype=np.uint8)
            cv2.fillConvexPoly(mask, pts, 255)
            idx = mask > 0
            if idx.any():
                canvas[idx] = (canvas[idx].astype(np.float32) * 0.45
                               ).astype(np.uint8)

            if active:
                # Neon glow — alpha modulated by amp so it fades with decay.
                for gw, base_alpha in ((6, 0.18), (3, 0.32)):
                    a = base_alpha * min(1.0, amp * 1.4)
                    if a < 0.02:
                        continue
                    overlay = canvas.copy()
                    cv2.polylines(overlay, [pts], True, self._neon_col,
                                  max(1, gw), lineType=cv2.LINE_AA)
                    cv2.addWeighted(overlay, a, canvas, 1.0 - a, 0,
                                    dst=canvas)

                # Crisp neon border (color lerped from dim grey → amber).
                mix = min(1.0, amp * 1.4)
                border_col = tuple(
                    int(self._idle_col[i] * (1 - mix)
                        + self._neon_col[i] * mix)
                    for i in range(3))
                cv2.polylines(canvas, [pts], True, border_col, 2,
                              lineType=cv2.LINE_AA)

                # Bright core highlight on top/bottom edges — only at high amp.
                if amp > 0.35:
                    bl, br, tr, tl = pts[0], pts[1], pts[2], pts[3]
                    core_gain = min(1.0, (amp - 0.35) / 0.65)
                    core_col = tuple(int(c * core_gain) for c in self._core_col)
                    cv2.line(canvas, tuple(bl), tuple(br), core_col, 1,
                             cv2.LINE_AA)
                    cv2.line(canvas, tuple(tl), tuple(tr), core_col, 1,
                             cv2.LINE_AA)

                # Cyan accent tick — visible only while panel is active.
                accent_gain = min(1.0, amp * 1.6)
                acc_col = tuple(int(c * accent_gain) for c in self._accent)
                bl, br = pts[0], pts[1]
                ccx = int(pts[:, 0].mean())
                ccy = int(pts[:, 1].mean())
                tick_w = max(4, int(abs(br[0] - bl[0]) * 0.22))
                cv2.line(canvas,
                         (ccx - tick_w, ccy), (ccx + tick_w, ccy),
                         acc_col, 2, cv2.LINE_AA)
            else:
                # Idle: just a thin faint grey outline — no glow, no tick.
                cv2.polylines(canvas, [pts], True, self._idle_col, 1,
                              lineType=cv2.LINE_AA)

        return canvas


# ── HUD: Combo + Rating (right panel) ────────────────────────────────────────
class ComboHUD:
    """Displays combo count and latest rating on the right side."""

    RATINGS = ['GOOD', 'GREAT', 'SUPERB', 'PERFECT']
    RATING_COLORS = {
        'GOOD':    (255, 180, 80),
        'GREAT':   (120, 220, 255),
        'SUPERB':  (120, 255, 160),
        'PERFECT': (255, 120, 255),
        'MISS':    (80, 80, 220),
    }

    def __init__(self, cam: PerspectiveCamera):
        self.cam = cam
        self.combo = 0
        self.rating = ''
        self.rating_frame = -999

    def register_hit(self, cur_frame: int):
        self.combo += 1
        # Reference style: always show a simple "GOOD" pill (single rating).
        self.rating = 'GOOD'
        self.rating_frame = cur_frame

    def register_miss(self, cur_frame: int):
        self.combo = 0
        self.rating = 'MISS'
        self.rating_frame = cur_frame

    def draw(self, canvas: np.ndarray, cur_frame: int):
        W, H = self.cam.W, self.cam.H

        # combo number (top right)
        if self.combo > 0:
            txt = f"{self.combo}"
            fs = 2.5 if W >= 1600 else 1.9
            (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_DUPLEX, fs, 4)
            x = W - tw - int(W * 0.03)
            y = int(H * 0.13)
            cv2.putText(canvas, txt, (x, y),
                        cv2.FONT_HERSHEY_DUPLEX, fs, CLR_WHITE, 4,
                        lineType=cv2.LINE_AA)
            lbl = 'COMBO'
            lfs = 0.9 if W >= 1600 else 0.65
            (lw, lh), _ = cv2.getTextSize(lbl, cv2.FONT_HERSHEY_DUPLEX, lfs, 2)
            cv2.putText(canvas, lbl, (W - lw - int(W * 0.03), y + int(lh * 1.4)),
                        cv2.FONT_HERSHEY_DUPLEX, lfs, (200, 200, 210), 2,
                        lineType=cv2.LINE_AA)

        # Rating pop-up as a CYAN/BLUE rounded-rect badge (ref style):
        #     ┌───────────┐
        #     │  GOOD     │   blue gradient, white text, thin border
        #     └───────────┘
        age = cur_frame - self.rating_frame
        if 0 <= age < 14 and self.rating:
            # scale pop-in (0→1.25) over 3 frames then settle to 1.0
            if age < 3:
                scale = 0.6 + age / 3.0 * 0.65
            elif age < 6:
                scale = 1.25 - (age - 3) / 3.0 * 0.25
            else:
                scale = 1.0
            alpha = 1.0 if age < 10 else max(0.0, 1.0 - (age - 10) / 4.0)

            txt = self.rating
            fs  = (1.25 if W >= 1600 else 0.95) * scale
            (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_DUPLEX, fs, 2)
            pad_x = int(th * 0.9)
            pad_y = int(th * 0.55)
            bw, bh = tw + 2 * pad_x, th + 2 * pad_y
            bx1 = W - int(W * 0.03)
            bx0 = bx1 - bw
            by0 = int(H * 0.22)
            by1 = by0 + bh

            # badge fill (cyan-blue gradient approximation: two horizontal bands)
            blue_dk = (200, 130, 60)     # BGR darker cyan-blue
            blue_lt = (255, 200, 120)    # BGR brighter cyan

            overlay = canvas.copy()
            mid = (by0 + by1) // 2
            cv2.rectangle(overlay, (bx0, by0), (bx1, mid),
                          tuple(int(c * alpha) for c in blue_lt), -1)
            cv2.rectangle(overlay, (bx0, mid), (bx1, by1),
                          tuple(int(c * alpha) for c in blue_dk), -1)
            canvas[:] = cv2.addWeighted(overlay, 0.85, canvas, 0.15, 0)

            # thin white border
            cv2.rectangle(canvas, (bx0, by0), (bx1, by1),
                          tuple(int(c * alpha) for c in (255, 255, 255)),
                          max(1, int(2 * alpha)), lineType=cv2.LINE_AA)

            # text (white) centered in badge
            tx = bx0 + pad_x
            ty = by0 + pad_y + th - int(th * 0.05)
            cv2.putText(canvas, txt, (tx, ty),
                        cv2.FONT_HERSHEY_DUPLEX, fs,
                        tuple(int(c * alpha) for c in CLR_WHITE), 2,
                        lineType=cv2.LINE_AA)
        return canvas

    def current_mode(self, cur_frame: int) -> str:
        """Return 'walk' unless recent punch → 'punch_l' / 'punch_r'."""
        return 'walk'


class CountdownHUD:
    """Top-right countdown bound to the currently visible relax obstacle."""

    def __init__(self, cam: PerspectiveCamera, color: str = "#FFFFFF",
                 max_show_sec: float = 5.0,
                 box: tuple[float, float, float, float] | None = None,
                 anim: str = "pop") -> None:
        self.cam = cam
        self._color_bgr = _hex_to_bgr(color, default=(255, 255, 255))
        self._max_show_sec = float(max_show_sec)
        self._font = cv2.FONT_HERSHEY_DUPLEX
        self._anim_mode = self._normalize_anim(anim)
        self._last_text: str | None = None
        self._prev_text: str | None = None
        self._last_change_frame: int = -10_000_000
        self._audio_events: list[tuple[float, bool]] = []
        if box is None:
            box = (0.88, 0.04, 0.10, 0.16)  # top-right default
        self.set_box(*box)

    @staticmethod
    def _normalize_anim(value: object) -> str:
        raw = str(value or "").strip().lower().replace("-", "_")
        if raw in {"flash"}:
            return "flash"
        if raw in {"fade", "fade_cross", "crossfade", "cross_fade"}:
            return "fade_cross"
        if raw in {"shake", "jitter"}:
            return "shake"
        return "pop"

    def set_animation(self, mode: str) -> None:
        self._anim_mode = self._normalize_anim(mode)

    def pop_audio_events(self) -> list[tuple[float, bool]]:
        """Return and clear queued (time_sec, is_last_count) events."""
        out = list(self._audio_events)
        self._audio_events.clear()
        return out

    def set_box(self, x: float, y: float, w: float, h: float) -> None:
        x = max(0.0, min(0.98, float(x)))
        y = max(0.0, min(0.98, float(y)))
        w = max(0.02, min(1.0 - x, float(w)))
        h = max(0.02, min(1.0 - y, float(h)))
        self._box_x = x
        self._box_y = y
        self._box_w = w
        self._box_h = h

    def _draw_glow_text(
        self,
        canvas: np.ndarray,
        *,
        text: str,
        x: int,
        y: int,
        font_scale: float,
        thickness: int,
        alpha: float = 1.0,
        glow_boost: float = 1.0,
    ) -> None:
        if alpha <= 1e-4:
            return
        alpha = float(max(0.0, min(1.0, alpha)))
        glow_boost = float(max(0.2, glow_boost))

        glow = np.zeros_like(canvas)
        for thick, weight in (
            (thickness + 10, 0.10),
            (thickness + 6, 0.18),
            (thickness + 3, 0.28),
        ):
            layer = np.zeros_like(canvas)
            cv2.putText(
                layer, text, (x, y), self._font, font_scale,
                self._color_bgr, thick, cv2.LINE_AA
            )
            glow = cv2.addWeighted(
                glow, 1.0, layer, float(weight * glow_boost), 0.0
            )
        glow = cv2.GaussianBlur(glow, (0, 0), sigmaX=4.0, sigmaY=4.0)
        canvas[:] = cv2.addWeighted(canvas, 1.0, glow, 0.95 * alpha, 0.0)

        crisp = np.zeros_like(canvas)
        cv2.putText(
            crisp, text, (x, y), self._font, font_scale,
            self._color_bgr, max(1, thickness + 2), cv2.LINE_AA
        )
        cv2.putText(
            crisp, text, (x, y), self._font, font_scale,
            (255, 255, 255), max(1, thickness), cv2.LINE_AA
        )
        canvas[:] = cv2.addWeighted(canvas, 1.0, crisp, alpha, 0.0)

    def draw(self, canvas: np.ndarray, targets: list, cur_frame: int, fps: float) -> None:
        active_t = None
        for t in targets:
            if not isinstance(t, RelaxTarget):
                continue
            # Countdown is per-block life cycle: show only while this
            # block is actually in-flight on screen (spawned and not yet
            # removed). Do not show pre-spawn future targets.
            if t.spawn_frame > cur_frame:
                continue
            if t.is_dead(cur_frame):
                continue
            if active_t is None or t.hit_frame < active_t.hit_frame:
                active_t = t
        if active_t is None:
            return

        # Countdown now represents the per-block WAIT window before
        # movement starts.
        move_start = int(active_t.move_start_frame)
        if cur_frame >= move_start:
            return
        time_left = (move_start - cur_frame) / max(1.0, float(fps))
        if time_left < 0.0:
            return
        text = str(max(0, int(np.ceil(time_left))))
        if self._last_text != text:
            self._prev_text = self._last_text
            self._last_text = text
            self._last_change_frame = int(cur_frame)
            self._audio_events.append(
                (float(cur_frame) / max(1.0, float(fps)), text == "1")
            )
        H, W = canvas.shape[:2]
        bx = int(round(self._box_x * W))
        by = int(round(self._box_y * H))
        bw = max(1, int(round(self._box_w * W)))
        bh = max(1, int(round(self._box_h * H)))
        bx = max(0, min(W - 1, bx))
        by = max(0, min(H - 1, by))
        bw = min(bw, W - bx)
        bh = min(bh, H - by)
        if bw <= 1 or bh <= 1:
            return

        # Fit base font size tightly to the user box.
        t_ref = max(1, int(round(2.0)))
        (tw_ref, th_ref), _ = cv2.getTextSize(text, self._font, 1.0, t_ref)
        target_w = max(1, int(bw * 0.995))
        target_h = max(1, int(bh * 0.995))
        scale_w = target_w / max(1.0, float(tw_ref))
        scale_h = target_h / max(1.0, float(th_ref))
        base_scale = max(0.20, min(scale_w, scale_h))
        base_thickness = max(1, int(round(base_scale * 2.0)))

        dt_sec = max(0.0, (float(cur_frame) - float(self._last_change_frame)) / max(1.0, float(fps)))
        anim_progress = min(1.0, dt_sec / 0.16)

        def _fit(scale_mul: float = 1.0) -> tuple[float, int, int, int, int, int]:
            fs = max(0.12, base_scale * float(scale_mul))
            thick = max(1, int(round(fs * 2.0)))
            (tw, th), _ = cv2.getTextSize(text, self._font, fs, thick)
            while (tw > target_w or th > target_h) and fs > 0.12:
                fs *= 0.97
                thick = max(1, int(round(fs * 2.0)))
                (tw, th), _ = cv2.getTextSize(text, self._font, fs, thick)
            tx = bx + (bw - tw) // 2
            ty = by + (bh + th) // 2
            return fs, thick, tw, th, tx, ty

        if self._anim_mode == "flash":
            fs, thick, _tw, _th, x, y = _fit(1.0)
            boost = 1.0 + (1.35 * (1.0 - anim_progress))
            self._draw_glow_text(
                canvas, text=text, x=x, y=y, font_scale=fs, thickness=thick,
                alpha=1.0, glow_boost=boost,
            )
            return

        if self._anim_mode == "fade_cross" and self._prev_text is not None:
            cur_a = max(0.0, min(1.0, anim_progress))
            prev_a = max(0.0, 1.0 - cur_a)
            fs, thick, _tw, _th, x, y = _fit(1.0)
            if prev_a > 1e-4:
                self._draw_glow_text(
                    canvas,
                    text=self._prev_text,
                    x=x,
                    y=y,
                    font_scale=fs,
                    thickness=thick,
                    alpha=prev_a,
                    glow_boost=1.0,
                )
            self._draw_glow_text(
                canvas, text=text, x=x, y=y, font_scale=fs, thickness=thick,
                alpha=cur_a, glow_boost=1.0,
            )
            if anim_progress >= 1.0:
                self._prev_text = None
            return

        if self._anim_mode == "shake":
            fs, thick, _tw, _th, x, y = _fit(1.0)
            amp = 6.0 * (1.0 - anim_progress)
            dx = int(round(math.sin(float(cur_frame) * 1.7) * amp * 0.45))
            dy = int(round(math.sin(float(cur_frame) * 2.8) * amp))
            self._draw_glow_text(
                canvas, text=text, x=x + dx, y=y + dy, font_scale=fs, thickness=thick,
                alpha=1.0, glow_boost=1.0,
            )
            return

        # Default "pop" effect: overshoot scale then settle.
        scale_mul = 1.0 + (0.25 * (1.0 - anim_progress))
        fs, thick, _tw, _th, x, y = _fit(scale_mul)
        self._draw_glow_text(
            canvas, text=text, x=x, y=y, font_scale=fs, thickness=thick,
            alpha=1.0, glow_boost=1.0,
        )


# ── GameManager ──────────────────────────────────────────────────────────────
class GameManager:
    """Schedules targets on audio onsets, auto-hits when they reach hit zone.

    Mới: spawn upfront dựa trên list beat_frames đã peak-pick từ librosa,
         đảm bảo target luôn POP đúng beat.
    """

    def __init__(self, cam: PerspectiveCamera,
                 travel: int = TARGET_TRAVEL_FRAMES,
                 rng_seed: int = 7):
        self.cam = cam
        self.travel = travel
        self.targets: list[Target] = []
        self.last_hit_type = 'step_l'
        self.rng = random.Random(rng_seed)

    def pre_schedule(self, beat_frames: list[int], bass_arr: np.ndarray,
                     min_gap_frames: int = 4,
                     min_lane_gap: int = 0,
                     mode: str | list[str] = 'punch',
                     lane_filter: set[int] | None = None,
                     dance_pair_cycle: int = 4,
                     punch_pair_cycle: int = 4,
                     line_beats: int = 2,
                     beat_density: float = 1.0,
                     wave_columns: list[dict] | None = None,
                     line_zigzag: str = 'vertical',
                     beat_source: str = 'tempo'):
        """Create one target per beat_frame.

        Rules:
        • Strict L ↔ R body-side alternation — no random swaps.  The stickman
          has only two feet, so LEFT-side lanes and RIGHT-side lanes alternate
          exactly the same way they do in 2-lane mode.
        • When there are more than 2 lanes (4-lane layout), each side owns
          half the lanes and cycles through them: left {0,1}, right {2,3}.
          Pattern L0 → R3 → L1 → R2 → L0 → …  (nice diagonal snake while
          still producing a strict L↔R event stream for the stickman).
        • Consecutive beats closer than `min_gap_frames` are merged.
        • Same-lane spawns must be at least `min_lane_gap` frames apart so
          visually we always see gaps between staggered blocks on one lane
          (prevents the "piled-up cubes" effect when travel is long).
        • `mode` switches which block class is spawned on beats.  Accepts
          a single string (``'punch'`` / ``'dance'``) OR a list of strings
          for **combo mode** — beats cycle through the list, e.g.
          ``['punch', 'dance']`` alternates PunchTarget and DanceTarget
          every beat so the player both punches and stomps.  Walls are
          untouched — they always spawn on strong bass regardless of
          mode, and span the FULL tunnel (all lanes).
        • `lane_filter` (optional set of 0-based lane indices) restricts
          spawns to just those lanes.  If a whole side becomes empty after
          filtering (e.g. filter={0,1} on a 4-lane layout = only L lanes),
          every beat goes to the surviving side (still respecting the
          per-side cursor for sub-lane cycling).  Walls are unaffected —
          they always span the full tunnel regardless of filter.
        """
        # Normalize `mode` into a cyclical list so "combo mode" (multiple
        # sub-modes alternating per beat) and the classic single-mode
        # path share one scheduler.  For ['punch','dance'] beat 0 →
        # PunchTarget, beat 1 → DanceTarget, beat 2 → PunchTarget, …
        modes_seq: list[str] = [mode] if isinstance(mode, str) else list(mode)
        if not modes_seq:
            modes_seq = ['punch']

        # Derive a stable "hold length" (in video frames) for line/hold
        # targets from the median inter-beat gap * `line_beats`.
        #
        # Note: hold duration intentionally follows the post-density beat grid
        # so line chains keep a clear, slower sustain when density is low.
        if len(beat_frames) >= 2 and line_beats > 0:
            _gaps = np.diff(np.asarray(beat_frames, dtype=np.int64))
            _median_gap = max(1, int(np.median(_gaps)))
            line_hold_frames = max(4, int(round(line_beats * _median_gap)))
        else:
            line_hold_frames = max(4, self.travel // 3)

        # ── Line-mode: derive chains DIRECTLY from waveform columns ──────
        # Each RMS "column" (rise → peak → descent-midpoint) becomes one
        # block.  `blocks_per_chain` consecutive columns form one chain,
        # and the chain's spawn point is the rise-start of its first
        # column.  This enforces the user's 3 rules:
        #   1. chain start = rise_f of column #1
        #   2. chain total life = end_f of last column − rise_f of first
        #   3. per-block shrink time = (end_f - rise_f) of THAT column
        line_block_hits_by_start:    dict[int, list[int]] = {}
        line_block_shrinks_by_start: dict[int, list[int]] = {}
        line_hold_by_start:          dict[int, int]       = {}

        # ── Line-mode + ARRAY source: derive chains 1:1 from user beats ──
        # When the caller supplies explicit ``--beat_times`` (Studio's
        # "Auto Gen Block" → user-edited tick array), we MUST honour every
        # supplied entry as a real block.  The wave-column derivation
        # below silently picks the strongest RMS columns and re-groups
        # them, which (a) drops user entries and (b) lets chains overlap
        # against the global busy lock — symptom: 61 array beats render
        # only ~44 cubes.
        #
        # Strategy: chunk ``beat_frames`` into groups of
        # ``blocks_per_chain = 2*line_beats`` consecutive entries; each
        # group becomes ONE LineTarget chain with explicit per-block
        # hit frames.  A trailing solo entry (≤ blocks_per_chain−1)
        # is appended to the previous chain so no user beat is dropped
        # (capped at LineTarget's max 8 cubes).  Per-block shrink width
        # = gap to the NEXT block in the chain; the last block uses the
        # median inter-beat gap so the tail block has a sane shrink
        # animation length.
        line_array_chains_used = False
        if (len(modes_seq) == 1
                and modes_seq[0] == 'line'
                and beat_source == 'array'
                and len(beat_frames) >= 2):
            total_frames = max(1, len(bass_arr))
            blocks_per_chain = max(2, 2 * int(line_beats))
            _bf = sorted(int(b) for b in beat_frames
                         if 0 <= int(b) < total_frames)
            if len(_bf) >= 2:
                _gaps = [max(1, _bf[i + 1] - _bf[i])
                         for i in range(len(_bf) - 1)]
                _med_gap = max(1, int(np.median(_gaps))) if _gaps else \
                           max(1, self.travel // 8)

                chain_groups: list[list[int]] = []
                for ci in range(0, len(_bf), blocks_per_chain):
                    grp = list(_bf[ci:ci + blocks_per_chain])
                    if len(grp) >= 2:
                        chain_groups.append(grp)
                    elif len(grp) == 1 and chain_groups \
                            and len(chain_groups[-1]) < 8:
                        # Solo trailing block — append to previous chain
                        # so the user's last beat is not silently dropped.
                        chain_groups[-1].append(grp[0])

                chain_starts: list[int] = []
                for grp in chain_groups:
                    hits = list(grp)
                    for i in range(1, len(hits)):
                        if hits[i] <= hits[i - 1]:
                            hits[i] = hits[i - 1] + 1
                    shrinks = []
                    for i in range(len(hits)):
                        if i < len(hits) - 1:
                            shrinks.append(max(1, hits[i + 1] - hits[i]))
                        else:
                            shrinks.append(_med_gap)
                    start_bf = hits[0]
                    hold_eff = max(1, hits[-1] - hits[0])
                    line_block_hits_by_start[start_bf]    = hits
                    line_block_shrinks_by_start[start_bf] = shrinks
                    line_hold_by_start[start_bf]          = hold_eff
                    chain_starts.append(start_bf)

                if chain_starts:
                    n_blocks = sum(len(g) for g in chain_groups)
                    print(f"[line-array] {len(_bf)} array beats -> "
                          f"{len(chain_starts)} chains "
                          f"({n_blocks} total blocks, "
                          f"<= {blocks_per_chain} per chain, "
                          f"median_gap={_med_gap}f)")
                    beat_frames = chain_starts
                    line_array_chains_used = True

        if (not line_array_chains_used
                and len(modes_seq) == 1
                and modes_seq[0] == 'line'
                and wave_columns is not None
                and len(wave_columns) >= 2):
            total_frames = max(1, len(bass_arr))
            blocks_per_chain = max(2, 2 * int(line_beats))

            # Keep columns in-range and strictly ordered.
            cols_all = [c for c in wave_columns
                        if 0 <= c['rise_f'] < total_frames]
            cols_all.sort(key=lambda c: c['rise_f'])

            # Decide how many chains we want.  Follow the density cadence
            # that produced `beat_frames` so --density still controls the
            # overall chain count (e.g. density=0.5 → ~half as many chains).
            if len(beat_frames) >= 2:
                target_chains = max(1, int(round(
                    len(beat_frames) / blocks_per_chain)))
            else:
                target_chains = max(1, len(cols_all) // blocks_per_chain)

            need_cols = target_chains * blocks_per_chain
            # Pick the strongest `need_cols` columns (by RMS peak
            # height), then re-sort by time and slice into chains.
            cols_strong = sorted(cols_all, key=lambda c: -c['height'])[:need_cols]
            cols_strong.sort(key=lambda c: c['rise_f'])

            chain_groups: list[list[dict]] = []
            for ci in range(0, len(cols_strong), blocks_per_chain):
                grp = cols_strong[ci:ci + blocks_per_chain]
                if len(grp) < blocks_per_chain:
                    break
                chain_groups.append(grp)

            # Build per-chain metadata.  `beat_frames` is replaced with
            # one entry per chain = rise_f of the first column, which
            # is what the rest of the scheduler consumes downstream.
            chain_starts: list[int] = []
            for grp in chain_groups:
                hits    = [int(c['rise_f']) for c in grp]
                shrinks = [max(1, int(c['end_f'] - c['rise_f'])) for c in grp]
                # Enforce strict monotonic ordering on hits.
                for i in range(1, len(hits)):
                    if hits[i] <= hits[i - 1]:
                        hits[i] = min(total_frames - 1, hits[i - 1] + 1)
                start_bf = hits[0]
                hold_eff = max(1, hits[-1] - hits[0])
                line_block_hits_by_start[start_bf]    = hits
                line_block_shrinks_by_start[start_bf] = shrinks
                line_hold_by_start[start_bf]          = hold_eff
                chain_starts.append(start_bf)

            if chain_starts:
                print(f"[line-wave] columns={len(cols_all)}  "
                      f"picked={len(cols_strong)}  "
                      f"chains={len(chain_starts)} x {blocks_per_chain} blocks  "
                      f"base_hold={line_hold_frames}f")
                beat_frames = chain_starts

        relax_wait_f = max(0, int(getattr(self, "RELAX_WAIT_FRAMES", 0)))

        def _spawn_target(m: str, spawn_f: int, bf: int, lane: int,
                          is_left: bool,
                          line_block_hits: list[int] | None = None,
                          line_block_shrinks: list[int] | None = None,
                          line_hold_eff: int | None = None,
                          relax_kind: str | None = None):
            if m == 'dance':
                return DanceTarget(spawn_f, bf, lane, is_left)
            if m == 'line':
                _hold = (line_hold_eff
                         if line_hold_eff is not None
                         else line_hold_frames)
                return LineTarget(spawn_f, bf, lane, is_left,
                                  hold_frames=_hold,
                                  line_beats=line_beats,
                                  block_hit_frames=line_block_hits,
                                  block_shrink_frames=line_block_shrinks,
                                  zigzag=line_zigzag)
            if m == 'relax':
                # Randomise each beat between LOW (ground slab → jump)
                # and HIGH (floating bar → duck).  `relax_kind` lets the
                # caller override to force one or the other (e.g. for
                # testing).
                enabled_kinds = list(getattr(
                    self, "RELAX_ENABLED_KINDS", ("low", "high", "middle")
                ))
                enabled_kinds = [k for k in enabled_kinds if k in ("low", "high", "middle")]
                if not enabled_kinds:
                    enabled_kinds = ["low", "high", "middle"]
                if relax_kind not in enabled_kinds:
                    if len(enabled_kinds) == 1:
                        relax_kind = enabled_kinds[0]
                    elif "middle" in enabled_kinds:
                        ratio_mid = float(getattr(self, "RELAX_KIND_RATIO_MIDDLE", 0.33))
                        ratio_mid = max(0.0, min(1.0, ratio_mid))
                        other = [k for k in enabled_kinds if k != "middle"]
                        if not other:
                            relax_kind = "middle"
                        else:
                            r = self.rng.random()
                            if r < ratio_mid:
                                relax_kind = "middle"
                            else:
                                relax_kind = str(self.rng.choice(other))
                    else:
                        relax_kind = str(self.rng.choice(enabled_kinds))
                target = RelaxTarget(
                    spawn_f, bf, kind=relax_kind, wait_frames=relax_wait_f
                )
                tex_attr = f"RELAX_TEXTURE_{str(relax_kind).upper()}"
                target.texture_path = getattr(self, tex_attr, None)
                if relax_kind == 'middle':
                    target.hole_mask_path = getattr(self, "RELAX_HOLE_MASK_PATH", None)
                return target
            return PunchTarget(spawn_f, bf, lane, is_left)

        def _target_cls_for(m: str):
            # Legacy helper kept for paired-spawn callers that always use
            # 'punch' or 'dance' (line is excluded from pairing for now).
            if m == 'dance':
                return DanceTarget
            return PunchTarget

        n_lanes = self.cam.n_lanes
        # Split lanes into left-side (first half) and right-side (second
        # half).  For 4-lane: L={0,1}, R={2,3}.
        mid = n_lanes // 2
        side_lanes = {
            0: list(range(0, mid)),          # 'L' side
            1: list(range(mid, n_lanes)),    # 'R' side
        }
        # When one side has more than one lane, start its inner cursor from
        # the OUTERMOST lane (visually the outside-edge hit lane) and walk
        # inward — gives the L0→R3→L1→R2 diagonal pattern for 4 lanes.
        side_lanes[1].reverse()
        # Apply user lane filter (keep relative order inside each side).
        if lane_filter is not None:
            mask = set(int(l) for l in lane_filter)
            side_lanes[0] = [l for l in side_lanes[0] if l in mask]
            side_lanes[1] = [l for l in side_lanes[1] if l in mask]
            if not side_lanes[0] and not side_lanes[1]:
                print("[lane_filter] No lanes enabled — "
                      "scheduler will emit zero targets.")

        # ── PAIRED-SPAWN rules (symmetric punch / dance cycles) ────────
        #
        #   Both punch-paired and dance-paired now follow the SAME
        #   generalized cycle model: a rotating cursor walks through
        #   every same-side adjacent lane pair present in the enabled
        #   lane set, and every N-th beat of that sub-mode fires a
        #   paired strike on the next pair.  The N-1 preceding beats of
        #   the sub-mode fall through to the normal single-lane L↔R
        #   cycling.  Cross-side adjacent pairs (e.g. lanes 1,2 on the
        #   4-lane layout = lane-1 L + lane-2 R) are excluded from both
        #   pair lists so combined strikes always lean cleanly toward
        #   ONE side (never straddle the centre).
        #
        #   • PUNCH paired (kind 'L'/'R', stickman 'LL'/'RR'
        #                   double-hand strikes):
        #                    controlled by `punch_pair_cycle` (default
        #                    N=4 → 3 nhịp đấm đơn + 1 nhịp đấm đôi).
        #                    Special values:
        #                      N = 1  → every punch beat is double
        #                               (legacy "strict" behaviour when
        #                               --lanes picks exactly 2 adj lanes)
        #                      N ≤ 0  → punch-pairing disabled entirely.
        #
        #   • DANCE paired (kind 'DL'/'DR', stickman 'JL'/'JR' feet-
        #                   together side-jump):
        #                    controlled by `dance_pair_cycle` (default
        #                    N=4 → 3 nhịp đơn + 1 nhịp chụm, 4/4-aligned).
        #                    Special values:
        #                      N = 1  → every dance beat is "chụm"
        #                      N ≤ 0  → dance-pairing disabled entirely.
        #
        # Both rules can coexist in combo mode, each gated by `cur_mode`
        # so the punch beats follow their cycle and the dance beats
        # follow theirs independently.
        enabled_lanes_sorted = (sorted(lane_filter)
                                if lane_filter is not None
                                else list(range(n_lanes)))
        # Same list is used for both punch and dance — criteria are
        # identical (same-side adjacent, enabled).
        adjacent_pairs: list[tuple[int, int]] = []
        for i in range(len(enabled_lanes_sorted) - 1):
            a, b = enabled_lanes_sorted[i], enabled_lanes_sorted[i + 1]
            if b - a != 1:
                continue
            if (a < mid) != (b < mid):
                continue
            adjacent_pairs.append((a, b))
        punch_adjacent_pairs = list(adjacent_pairs)
        dance_adjacent_pairs = list(adjacent_pairs)
        punch_paired_enabled = (
            bool(punch_adjacent_pairs)
            and 'punch' in modes_seq
            and punch_pair_cycle > 0
        )
        dance_paired_enabled = (
            bool(dance_adjacent_pairs)
            and 'dance' in modes_seq
            and dance_pair_cycle > 0
        )

        def _cycle_desc(n: int, sub: str) -> str:
            if n == 1:
                return f"every {sub} beat"
            return (f"every {n}th {sub} beat "
                    f"({n - 1} đơn + 1 {'chụm' if sub == 'dance' else '2-tay'})")

        def _pairs_desc(pairs: list[tuple[int, int]]) -> str:
            return ", ".join(
                f"({p[0]+1},{p[1]+1})[{'L' if p[1] < mid else 'R'}]"
                for p in pairs
            )

        if punch_paired_enabled:
            rotate_note = ("" if len(punch_adjacent_pairs) == 1
                           else f" rotating across {len(punch_adjacent_pairs)} pairs")
            print(f"[punch-paired] {_cycle_desc(punch_pair_cycle, 'punch')} "
                  f"spawns 2 cubes on same-side adjacent lane pair"
                  f"{rotate_note} → {_pairs_desc(punch_adjacent_pairs)} "
                  f"(stickman double-hand strike).")
        if dance_paired_enabled:
            rotate_note = ("" if len(dance_adjacent_pairs) == 1
                           else f" rotating across {len(dance_adjacent_pairs)} pairs")
            print(f"[dance-paired] {_cycle_desc(dance_pair_cycle, 'dance')} "
                  f"spawns 2 tiles on same-side adjacent lane pair"
                  f"{rotate_note} → {_pairs_desc(dance_adjacent_pairs)} "
                  f"(stickman feet-together side-jump).")

        last_bf = -999
        next_side = 0                         # 0 = L, 1 = R
        # Per-side cursor: which sub-lane to try first on the next hit.
        side_cursor = [0, 0]
        last_spawn_on = [-10 ** 9] * n_lanes
        # Track lanes occupied by an active LINE hold so no other target
        # (punch / dance / line) tries to spawn in the same lane while a
        # long-note bar is still sliding past the camera.  Holds an
        # absolute hit-frame index: any beat with bf < line_busy_until[l]
        # is blocked on that lane.
        line_busy_until = [-10 ** 9] * n_lanes
        # Global line-chain lock: ALL lanes are blocked until the
        # previous chain has fully cleared the screen.  This ensures
        # chains appear strictly one-after-another across the 4 lanes
        # (no two chains side-by-side at the same time).
        line_global_busy_until: int = -10 ** 9
        # ── Relax-mode global lock ─────────────────────────────────────
        # Relax obstacles span the whole tunnel, so only ONE can be in
        # flight at a time — otherwise two full-width slabs would
        # overlap.  We reserve half a travel-window between consecutive
        # relax spawns which also gives the stickman / camera time to
        # finish the dodge animation before the next cue.
        relax_busy_until: int = -10 ** 9
        skipped_early = 0
        skipped_stacked = 0
        # Combo-mode cursor: advances once per SCHEDULED beat-event (incl.
        # walls + paired spawns) so the PunchTarget ↔ DanceTarget
        # alternation follows a stable cadence independent of how many
        # cubes each beat emits.
        emit_idx = 0
        # Counters drive the "(N-1) đơn + 1 đôi" cycles for each
        # paired sub-mode independently.  Punch has its own cursor /
        # counter; dance has its own.  Both only advance after a
        # SUCCESSFUL emit so a lane-stacked skip doesn't shift the
        # cycle across long songs.
        punch_paired_count = 0
        dance_paired_count = 0
        # Rotation cursors — each paired beat picks the next same-side
        # adjacent pair and wraps around.  E.g. 4 lanes all enabled +
        # cycle=4 → beats 4, 8, 12, 16 … land on (0,1), (2,3), (0,1),
        # (2,3) … so the double strikes alternate between L and R side.
        punch_pair_cursor = 0
        dance_pair_cursor = 0

        def _mode_for(idx: int) -> str:
            return modes_seq[idx % len(modes_seq)]

        for bf in beat_frames:
            if bf - last_bf < min_gap_frames:
                continue
            # Resolve target mode first so we can apply mode-specific early-spawn
            # policy (line chains may start "already in flight" at frame 0).
            cur_mode = _mode_for(emit_idx)
            spawn_f = bf - self.travel
            # Allow negative spawn_f: Target.depth() handles that cleanly
            # — the block just appears already partway down the tunnel
            # at frame 0, which is the visually correct behaviour when
            # the song starts mid-measure.  Only drop the beat if its
            # own hit frame is already in the past (bf < 0), which
            # would be impossible to render as a real hit.
            if bf < 0:
                skipped_early += 1
                continue

            target_cls = _target_cls_for(cur_mode)

            # ── RELAX obstacle path (full-tunnel, no lane cycling) ─────
            # Relax obstacles span the entire track, so we spawn ONE
            # per beat regardless of which lane the side-cursor would
            # have picked.  A dedicated global lock (`relax_busy_until`)
            # prevents two slabs overlapping in flight and gives the
            # camera / stickman enough breathing room between dodges.
            #
            # ARRAY-source exception: skip the busy lock entirely so
            # every user-supplied beat materialises as an obstacle.
            # `process_video` already drops `_relax_slow_mult` to 1.0
            # for array source so auto-travel ≈ one user-gap, which
            # naturally prevents two slabs being mid-air at the same
            # time without us needing the lock here.
            if cur_mode == 'relax':
                if beat_source != 'array' and bf < relax_busy_until:
                    skipped_stacked += 1
                    continue
                # Relax semantics: beat-frame is the APPEAR time at horizon.
                # Target then waits `relax_wait_f`, and only after that starts
                # moving for `travel` frames until hit.
                relax_spawn_f = int(bf)
                relax_hit_f = int(bf) + relax_wait_f + self.travel
                t = _spawn_target('relax', relax_spawn_f, relax_hit_f, 0, False)
                self.targets.append(t)
                # Stream obstacles continuously: while one slab is
                # front-and-centre, the NEXT should already be gliding
                # in from the horizon so the viewer sees a pipeline of
                # blocks receding into the vanishing point — never a
                # blank "waiting" gap between them.  Using travel // 4
                # allows ~4 blocks in flight at once; the far blocks
                # are mostly occluded by the near one until it passes,
                # at which point the next one is already large enough
                # to take over seamlessly.  The previous travel // 2
                # value combined with the 4× speed slowdown produced
                # 3-second empty gaps where the player only saw the
                # dying tail of one block before the next appeared.
                # Floor of 8 frames for extreme slow-travel edge cases;
                # also respect `min_gap_frames` so inter-beat spacing
                # set by the caller still wins when larger.
                if beat_source != 'array':
                    relax_busy_until = bf + max(
                        8, self.travel // 4, min_gap_frames)
                last_bf = bf
                emit_idx += 1
                continue

            # ── PAIRED spawn path (punch): (N-1) single + 1 double ──
            # Same cycle model as dance-paired.  On every Nth punch
            # beat (N = punch_pair_cycle), spawn 2 cubes side-by-side
            # on the next same-side adjacent lane pair picked by
            # `punch_pair_cursor` (rotates across all enabled pairs).
            # The 2 cubes share a hit_frame and are tagged 'L'/'R' so
            # the event-emitter in process_video collapses them into
            # ONE 'LL'/'RR' double-hand strike for the stickman.  The
            # N-1 preceding punch beats fall through to the single-
            # lane cycling below (normal 'PL'/'PR' events).
            punch_paired_single = False
            if punch_paired_enabled and cur_mode == 'punch':
                is_double = (
                    (punch_paired_count % punch_pair_cycle)
                    == punch_pair_cycle - 1
                )
                if is_double:
                    pair = punch_adjacent_pairs[
                        punch_pair_cursor % len(punch_adjacent_pairs)
                    ]
                    pair_is_left = pair[1] < mid
                    pair_tag = 'L' if pair_is_left else 'R'
                    # Line-busy guard: if either lane of the pair is
                    # mid-hold, skip the paired punch entirely (let the
                    # cycle retry on the next punch beat).  Cursor must
                    # NOT advance here — otherwise rhythm.py and
                    # stickman.py can desync if their skip patterns
                    # differ across consecutive runs.
                    if any(bf < line_busy_until[l] for l in pair):
                        skipped_stacked += 1
                        continue
                    punch_pair_cursor += 1
                    for lane in pair:
                        tgt = target_cls(spawn_f, bf, lane, pair_is_left)
                        tgt.paired_side = pair_tag
                        self.targets.append(tgt)
                        last_spawn_on[lane] = spawn_f
                    punch_paired_count += 1
                    last_bf = bf
                    emit_idx += 1
                    continue
                punch_paired_single = True

            # ── PAIRED spawn path (dance): (N-1) single + 1 double cycle ──
            # With dance_pair_cycle=N (default 4), the LAST dance beat of
            # each N-length cycle spawns 2 tiles simultaneously on one
            # same-side adjacent lane pair picked from
            # `dance_adjacent_pairs`; the preceding N-1 dance beats fall
            # through to the normal single-lane cycling below.
            #
            # When multiple adjacent pairs are available (e.g. 4 lanes all
            # enabled has both L-pair (0,1) and R-pair (2,3)), the cursor
            # rotates across them so every chụm lands on a different pair —
            # the player sees JL, JR, JL, JR, … alternating between sides.
            # When only one pair is available (e.g. --lanes 1,2), the
            # cursor always points at that single pair.
            #
            # NB: the counter advances ONLY after an event is actually
            # emitted — a lane-stacked skip must NOT shift the cycle, or
            # the pattern drifts after long songs.
            dance_paired_single = False
            if dance_paired_enabled and cur_mode == 'dance':
                is_double = (
                    (dance_paired_count % dance_pair_cycle)
                    == dance_pair_cycle - 1
                )
                if is_double:
                    pair = dance_adjacent_pairs[
                        dance_pair_cursor % len(dance_adjacent_pairs)
                    ]
                    pair_is_left = pair[1] < mid
                    pair_tag = 'DL' if pair_is_left else 'DR'
                    if any(bf < line_busy_until[l] for l in pair):
                        skipped_stacked += 1
                        continue
                    dance_pair_cursor += 1
                    for lane in pair:
                        tgt = target_cls(spawn_f, bf, lane, pair_is_left)
                        # Tagged 'DL'/'DR' so the event-emitter knows to
                        # collapse the 2 tiles into ONE 'JL'/'JR' (feet-
                        # together side-jump), distinct from punch's
                        # 'L'/'R' (double-hand) pairing tag.
                        tgt.paired_side = pair_tag
                        self.targets.append(tgt)
                        last_spawn_on[lane] = spawn_f
                    dance_paired_count += 1
                    last_bf = bf
                    emit_idx += 1
                    continue
                # Single dance beat on the paired side — flag for the
                # end-of-loop counter increment (only after the single
                # lane is successfully chosen + emitted).
                dance_paired_single = True

            # ── pick a lane on the current SIDE (with lane-gap guard) ──
            # Try the preferred sub-lane first; if it's too stacked, walk
            # the other sub-lane(s) on the same side; if the whole side is
            # busy, fall through to the OTHER side; if nothing fits, drop
            # the beat (same failure mode as before).
            chosen_lane = -1
            chosen_side = next_side
            for side_try in (next_side, 1 - next_side):
                lanes_list = side_lanes[side_try]
                if not lanes_list:
                    continue
                # cycle starting from this side's cursor
                start = side_cursor[side_try] % len(lanes_list)
                for off in range(len(lanes_list)):
                    lane_try = lanes_list[(start + off) % len(lanes_list)]
                    # Skip lanes that are still busy holding a line bar.
                    if bf < line_busy_until[lane_try]:
                        continue
                    if spawn_f - last_spawn_on[lane_try] >= min_lane_gap:
                        chosen_lane = lane_try
                        chosen_side = side_try
                        # advance this side's cursor for next time
                        side_cursor[side_try] = (start + off + 1) % len(lanes_list)
                        break
                if chosen_lane != -1:
                    break

            if chosen_lane == -1:
                skipped_stacked += 1
                continue

            is_left = chosen_side == 0

            # For line mode: reject if a previous chain is still on
            # screen (global lock across ALL lanes).
            if cur_mode == 'line' and bf < line_global_busy_until:
                skipped_stacked += 1
                continue

            line_block_hits = None
            line_block_shrinks = None
            line_hold_eff = None
            if cur_mode == 'line':
                line_block_hits    = line_block_hits_by_start.get(int(bf))
                line_block_shrinks = line_block_shrinks_by_start.get(int(bf))
                line_hold_eff      = line_hold_by_start.get(int(bf))
            t = _spawn_target(cur_mode, spawn_f, bf, chosen_lane, is_left,
                              line_block_hits=line_block_hits,
                              line_block_shrinks=line_block_shrinks,
                              line_hold_eff=line_hold_eff)
            self.targets.append(t)
            last_spawn_on[chosen_lane] = spawn_f
            if isinstance(t, LineTarget):
                # Chain life = time from first-block arrival (bf) until
                # the LAST block's back face hits the punch plane (+1
                # buffer frame so a new chain never starts exactly
                # atop the tail of the previous one).
                #
                # ARRAY source exception: when the user supplied explicit
                # ``--beat_times`` we group consecutive entries into
                # chains and the very next chain's first block is
                # itself the next user-supplied beat — by construction
                # spaced ~one gap AFTER this chain's last block.  Adding
                # the legacy `last_width + 2f` tail would push the busy
                # window PAST the next chain's start and silently skip
                # it (symptom: 61 array beats render only ~44 cubes).
                # For array mode we shrink chain_life to the chain's
                # SPAN only (last_hit − first_hit), so the lock releases
                # exactly when the last block of this chain arrives at
                # the punch plane and the next user beat is honoured.
                last_back  = int(t.block_back_frames[-1])
                if beat_source == 'array':
                    chain_life = max(1,
                                     int(t.block_hit_frames[-1])
                                     - int(t.hit_frame))
                else:
                    chain_life = max(1, last_back - int(t.hit_frame)) + 2
                line_busy_until[chosen_lane] = bf + chain_life
                line_global_busy_until       = bf + chain_life
            next_side = 1 - chosen_side
            last_bf = bf
            emit_idx += 1
            if punch_paired_single:
                punch_paired_count += 1
            if dance_paired_single:
                dance_paired_count += 1

        # Count unique hit-frames that actually produced a target.  In
        # paired-spawn mode each beat emits 2 cubes sharing a hit_frame,
        # so `len(self.targets)` double-counts — dedupe via a set so the
        # "merged" (dropped-as-too-close) diagnostic stays accurate.
        used_beats = len({tg.hit_frame for tg in self.targets})
        merged = (len(beat_frames) - used_beats
                  - skipped_early - skipped_stacked)
        print(f"[GameManager] Scheduled {len(self.targets)} targets "
              f"from {len(beat_frames)} beat events  "
              f"(lanes={n_lanes}, skipped {skipped_early} too-early, "
              f"{skipped_stacked} lane-stacked, merged {merged} too-close).",
              flush=True)

    def update(self, cur_frame: int) -> list[Target]:
        """Advance time; return list of newly-hit targets."""
        just_hit = []
        for tg in self.targets:
            # only targets already spawned + not yet hit can be hit now
            if tg.spawn_frame <= cur_frame and tg.check_hit(cur_frame):
                just_hit.append(tg)
        # clean old dead targets (keep pre-scheduled future ones)
        self.targets = [t for t in self.targets if not t.is_dead(cur_frame)]
        return just_hit

    def alive_sorted(self, cur_frame: int) -> list[Target]:
        """Visible+alive targets sorted by depth DESC (far first, painter's algo)."""
        return sorted([t for t in self.targets
                       if t.state == 'flying' and t.spawn_frame <= cur_frame],
                      key=lambda tg: -tg.depth(cur_frame))


# ── RhythmVisualizer ──────────────────────────────────────────────────────────
class RhythmVisualizer:
    def __init__(self):
        self.FPS        = FPS
        self.WIDTH      = 1920
        self.HEIGHT     = 1080
        self.TIME_LIMIT: float | None = None
        self.BLOOM: bool = True
        self.TRAVEL_FRAMES: int = TARGET_TRAVEL_FRAMES
        self.is_mac = IS_MAC
        # -- beat detection tuning (adjustable per song) --
        # 'tempo' = perfectly uniform cadence from BPM (recommended, default)
        # 'beat'  = each beat from librosa beat_track (may jitter)
        # 'onset' = every transient (most blocks, irregular)
        # 'array' = caller supplies the exact hit times in seconds via
        #           BEAT_TIMES — librosa is used only to load the audio
        #           for duration/bass/RMS, never for beat detection.  This
        #           is the deterministic mode used by Studio when the user
        #           has hand-edited the beat ticks before rendering.
        self.BEAT_SOURCE:   str   = 'tempo'
        self.BEAT_SENS:     float = 0.5      # 0..1  higher = more beats kept
        self.BEAT_SUBDIV:   int   = 1        # 1,2,4 – multiply each beat
        self.BEAT_MIN_GAP:  int   = 4        # frames between targets
        self.BEAT_BPM:      float | None = None  # override tempo detection
        self.BEAT_DENSITY:  float = 1.0      # 0.5 = half, 2.0 = double cadence
        # Audio-amplitude floor (0..1) — events whose ``event_heights``
        # entry is below this threshold are dropped right before
        # ``--export_events`` writes the JSON, so the studio's
        # waveform-threshold slider can ask the rhythm core for a
        # pre-filtered set instead of dimming ticks client-side.
        self.BEAT_HEIGHT_THRESHOLD: float = 0.0
        # Hit-time list used in BEAT_SOURCE='array' (seconds, sorted, deduped,
        # already filtered to [0, duration)).  Empty list in any other source.
        self.BEAT_TIMES:    list[float] = []
        self.BLOCK_SPEED:   float = 1.0      # 1.0 = one block/lane visible
                                             # 0.5 = 2× slower (2 visible)
                                             # 0.33 = 3× slower (3 visible), etc.
        self.MAX_PER_LANE:  int   = 3        # hard cap on blocks visible / lane
                                             # (keeps visuals clean regardless
                                             # of density / speed combo)
        # -- cube textures (optional; default = coloured cube + fist icon) --
        self.CUBE_IMAGE:        str | None = None   # same image both sides
        self.CUBE_IMAGE_LEFT:   str | None = None   # left-lane (green) side
        self.CUBE_IMAGE_RIGHT:  str | None = None   # right-lane (red) side
        # -- custom 3-D mesh (.obj/.glb/.stl/.ply); overrides image if set --
        self.CUBE_MODEL:        str | None = None
        self.CUBE_MODEL_LEFT:   str | None = None
        self.CUBE_MODEL_RIGHT:  str | None = None
        self.MESH_WIREFRAME:    bool = False
        # -- scene toggles --
        self.SHOW_FLOOR_PANELS: bool = True   # receding grey tile rows
        self.SHOW_STICKMAN:     bool = True   # left-column punching fighter
        # -- segment background (drawn behind all scene elements) --
        self.BACKGROUND_TYPE:   str = "solid"     # solid | image | video
        self.BACKGROUND_COLOR:  str = "#000000"
        self.BACKGROUND_IMAGE:  str | None = None
        self.BACKGROUND_VIDEO:  str | None = None
        # -- stickman draw-box override (top-left corner + size, in PIXELS).
        #    Any value < 0 means "use the StickmanHUD default for that
        #    component" (left-column HUD: x=W*1%, y=H*9%, w=W*13.5%,
        #    h=H*54%).  All four values are independent so callers can
        #    pin just one (e.g. only re-position) and let the others
        #    auto-derive.  Resolved into a `box=(x, y, w, h)` tuple at
        #    StickmanHUD construction time below.
        self.STICK_X0:          int = -1
        self.STICK_Y0:          int = -1
        self.STICK_W:           int = -1
        self.STICK_H:           int = -1
        # -- color overrides (None = use built-in defaults) --
        self.CUBE_COLOR_LEFT:   tuple | None = None
        self.CUBE_COLOR_RIGHT:  tuple | None = None
        self.PANEL_NEON_COLOR:  tuple | None = None   # viewport 4-tile neon
        # -- event export (for rendering a matched stickman-only video) --
        self.EXPORT_EVENTS:     str | None = None
        # -- detection-only mode: when True, run audio analysis + scheduler
        #    (so EXPORT_EVENTS is populated) then exit BEFORE the heavy
        #    frame rendering loop.  Used by the Studio UI to preview where
        #    blocks will spawn without spending render time.
        self.DETECT_ONLY:       bool = False
        # -- gameplay mode: 'punch' (air cubes + stickman punches) or
        #                   'dance' (floor slabs + stickman stomps)
        self.MODE:              str = 'punch'
        # -- lane filter: None = all 4 lanes active.  Set to a set of
        # 0-based lane indices (e.g. {0, 3}) to restrict spawns to just
        # those lanes (user-facing --lanes uses 1-based numbers).
        self.LANE_FILTER:       set[int] | None = None
        # -- dance paired-spawn cycle length (only active with --lanes
        # picking 2 adjacent same-side lanes in dance/combo mode).  N-1
        # singles + 1 double per cycle; the double lands on the LAST
        # beat of each cycle so the player builds anticipation for the
        # feet-together jump.  Default 4 ≈ one "chụm" per 4/4 bar.
        #   N = 4 (default)  → "đơn, đơn, đơn, CHỤM"  (musical, 4/4-safe)
        #   N = 3            → "đơn, đơn, CHỤM"       (triplet, legacy)
        #   N = 2            → "đơn, CHỤM"            (heavy, drop-style)
        #   N = 1            → every dance beat chụm
        #   N ≤ 0            → disable dance-paired (every beat single)
        self.DANCE_PAIR_CYCLE:  int = 4
        # -- punch paired-spawn cycle.  Exactly the same semantics as
        # DANCE_PAIR_CYCLE but for the punch sub-mode: every N-th punch
        # beat fires 2 cubes on the next same-side adjacent lane pair
        # (rotating cursor) → stickman throws both hands → kind 'LL'/'RR'.
        # Requires at least one same-side adjacent pair in the enabled
        # lane set (automatic on 4-lane all-enabled; also works with
        # --lanes 1,2 / 3,4 / 1,2,3,4 etc.).
        #   N = 4 (default)  → "đấm, đấm, đấm, 2-TAY"  (4/4-safe combo)
        #   N = 1            → every punch beat double (legacy "strict"
        #                      pairing when --lanes picks 2 adj lanes)
        #   N ≤ 0            → disable punch-paired (every beat single)
        self.PUNCH_PAIR_CYCLE:  int = 4
        # -- line (hold-note) length in BEATS.  Line targets are elongated
        # rail-style cubes; the stickman keeps the punch pose extended for
        # `LINE_BEATS` beat intervals after the hit frame while the bar's
        # tail slides past the camera.  Default 2 ≈ "hold through one extra
        # beat".  Only used when 'line' appears in --mode.
        self.LINE_BEATS:        int = 2
        # Draw line-mode block timing debug overlay (timeline markers).
        self.LINE_DEBUG:        bool = False
        # Line-mode zigzag axis: 'vertical' = legacy saw-tooth within a
        # single lane; 'horizontal' = blocks span lane 1 <-> lane 4 and
        # alternate direction (chain is a wide left-right zigzag across
        # the tunnel, with head/tail on the two outer lanes).
        self.LINE_ZIGZAG:       str = 'vertical'
        # ── Relax-mode cadence (solo relax only) ────────────────────────
        # 0.0 (default) = music-driven: obstacles spawn on audio beats
        # (wave-columns or onset events, same as every other mode).
        # >0.0          = fixed-interval: ignore audio and schedule one
        #                 obstacle every N seconds.  Useful for laid-back
        #                 videos where obstacle cadence shouldn't be tied
        #                 to the song's rhythm.  Only applied when
        #                 --mode is SOLO "relax"; combos still follow
        #                 audio beats so inter-mode alternation stays
        #                 coherent.
        self.RELAX_INTERVAL:    float = 0.0
        self.RELAX_TRAVEL_SEC:  float = 0.0
        self.RELAX_WAIT_SEC:    float = 0.0
        self.RELAX_TEXTURE_LOW: str | None = None
        self.RELAX_TEXTURE_HIGH: str | None = None
        self.RELAX_TEXTURE_MIDDLE: str | None = None
        self.RELAX_HOLE_MASK_PATH: str | None = None
        self.RELAX_KIND_RATIO_MIDDLE: float = 0.33
        self.RELAX_SHOW_LOW: bool = True
        self.RELAX_SHOW_HIGH: bool = True
        self.RELAX_SHOW_MIDDLE: bool = True
        self.RELAX_COUNTDOWN_ENABLED: bool = True
        self.RELAX_COUNTDOWN_COLOR: str = "#FFFFFF"
        self.RELAX_COUNTDOWN_MAX_SEC: float = 5.0
        self.RELAX_COUNTDOWN_ANIM: str = "pop"
        self.RELAX_COUNTDOWN_AUDIO_ENABLED: bool = False
        self.RELAX_COUNTDOWN_AUDIO_MODE: str = "default"
        self.RELAX_COUNTDOWN_AUDIO_FILE: str | None = None
        self.RELAX_COUNTDOWN_AUDIO_VOLUME: float = 0.65
        self.RELAX_COUNTDOWN_AUDIO_LAST_MODE: str = "default"
        self.RELAX_COUNTDOWN_AUDIO_LAST_FILE: str | None = None
        self.RELAX_COUNTDOWN_X: float = 0.88
        self.RELAX_COUNTDOWN_Y: float = 0.04
        self.RELAX_COUNTDOWN_W: float = 0.10
        self.RELAX_COUNTDOWN_H: float = 0.16
        self._countdown_audio_events: list[tuple[float, bool]] = []
        # ── Camera perspective overrides ────────────────────────────────
        self.FLOOR_HIT_FRAC:    float | None = None
        self.HORIZON_FRAC:      float | None = None
        self.FLOOR_SPREAD_FRAC: float | None = None
        self.FAR_SPREAD_FRAC:   float | None = None   # independent far-end spread
        # ── Side rails ─────────────────────────────────────────────────
        self.SHOW_SIDE_RAILS:      bool  = False
        self.RAIL_COLOR:           str   = '#FF60FF'
        self.RAIL_SHAPE:           str   = 'chunky'
        self.RAIL_HEIGHT:          float = 0.14
        self.RAIL_OFFSET_X:        float = 0.08
        self.RAIL_IMAGE:           str | None = None
        self.RAIL_PULSE:           str   = 'beat'
        self.RAIL_PULSE_INTENSITY: float = 0.6
        self.RAIL_PILLAR_COUNT:    int = 16
        self.RAIL_PILLAR_RADIUS:   float = 1.0
        self.RAIL_CHASE_MODE:      str = "time"
        self.RAIL_CHASE_SPEED_FRAMES: int = 4
        self.RAIL_DOT_COUNT:       int = 24
        self.RAIL_DOT_SIZE_PX:     int = 6
        self.RAIL_DOT_ANIM_MODE:   str = "audio"
        self.RAIL_DOT_COLOR_NEAR:  str = "#FF60FF"
        self.RAIL_DOT_COLOR_FAR:   str = "#00FFFF"

    # -------------------------------------------------------------------
    def process_video(self, audio_file: str) -> str | None:
        t0 = time.time()
        print("Starting Rhythm processing...")

        # -------- Load cube 3-D meshes (optional) ----------------------
        mesh_left  = mesh_right = None
        mpath_l = self.CUBE_MODEL_LEFT  or self.CUBE_MODEL
        mpath_r = self.CUBE_MODEL_RIGHT or self.CUBE_MODEL
        if mpath_l:
            mesh_left  = _load_mesh(mpath_l)
        if mpath_r:
            mesh_right = _load_mesh(mpath_r)
        PunchTarget.MESH_LEFT      = mesh_left
        PunchTarget.MESH_RIGHT     = mesh_right
        PunchTarget.MESH_WIREFRAME = self.MESH_WIREFRAME
        DanceTarget.MESH_LEFT      = mesh_left
        DanceTarget.MESH_RIGHT     = mesh_right
        DanceTarget.MESH_WIREFRAME = self.MESH_WIREFRAME

        # -------- Load cube textures (optional, skipped if mesh set) ---
        tex_left  = tex_right = None
        if mesh_left is None:
            path_left = self.CUBE_IMAGE_LEFT or self.CUBE_IMAGE
            if path_left:
                tex_left = _load_cube_texture(path_left)
        if mesh_right is None:
            path_right = self.CUBE_IMAGE_RIGHT or self.CUBE_IMAGE
            if path_right:
                tex_right = _load_cube_texture(path_right)
        PunchTarget.TEXTURE_LEFT  = tex_left
        PunchTarget.TEXTURE_RIGHT = tex_right
        DanceTarget.TEXTURE_LEFT  = tex_left
        DanceTarget.TEXTURE_RIGHT = tex_right

        if (mesh_left is None and mesh_right is None
                and tex_left is None and tex_right is None):
            print("[cube_image] Using default coloured cube + fist icon.")

        print(f"Loading audio: {audio_file}")
        y, sr = librosa.load(audio_file, mono=True)
        if self.TIME_LIMIT is not None:
            y = y[:int(self.TIME_LIMIT * sr)]
        total_duration = len(y) / sr

        t_load = time.time()
        print(f"Audio loaded in {t_load - t0:.2f}s")

        print("Extracting audio features...")
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP_LENGTH)
        spec      = librosa.stft(y, hop_length=HOP_LENGTH)
        spec_mag  = librosa.magphase(spec)[0]

        # -------- BEAT DETECTION (configurable) ------------------------
        # sens ∈ [0,1] higher = more beats detected/kept
        sens = float(np.clip(self.BEAT_SENS, 0.0, 1.0))

        if self.BEAT_SOURCE == 'tempo':
            # Uniform cadence: derive BPM + phase from librosa beat_track, then
            # *ignore* individual beat positions — just emit evenly-spaced events.
            # This matches the reference video where blocks flow at a constant
            # rate and always pop on beat because the rate IS the beat.
            if self.BEAT_BPM is not None and self.BEAT_BPM > 0:
                tempo_val = float(self.BEAT_BPM)
                # anchor phase at the first detected beat so first block still
                # lines up with song (fallback: t=0).
                try:
                    _, bh = librosa.beat.beat_track(
                        onset_envelope=onset_env, sr=sr,
                        hop_length=HOP_LENGTH, tightness=120)
                    first_t = float(librosa.frames_to_time(
                        bh[:1], sr=sr, hop_length=HOP_LENGTH)[0]) \
                        if len(bh) else 0.0
                except Exception:
                    first_t = 0.0
            else:
                tempo, bh = librosa.beat.beat_track(
                    onset_envelope=onset_env, sr=sr,
                    hop_length=HOP_LENGTH, tightness=120)
                tempo_val = float(tempo) if np.ndim(tempo) == 0 \
                    else float(tempo[0])
                first_t = float(librosa.frames_to_time(
                    bh[:1], sr=sr, hop_length=HOP_LENGTH)[0]) \
                    if len(bh) else 0.0

            if tempo_val <= 0:
                tempo_val = 120.0        # safety fallback

            period = 60.0 / tempo_val / max(1, self.BEAT_SUBDIV)  # seconds
            # rewind phase anchor to within [0, period) so we start at/near 0
            while first_t - period >= 0:
                first_t -= period

            beat_times = np.arange(first_t, total_duration, period)
            total_detected = len(beat_times)
            kept = total_detected        # no strength filtering in tempo mode

        elif self.BEAT_SOURCE == 'array':
            # Caller-supplied hit times — bypass librosa beat detection
            # entirely.  We trust the list (sorted/deduped already by
            # ``main()`` at parse time) and only clip to the audio's
            # actual duration so out-of-range entries don't crash the
            # frame conversion below.  Density and the wave-column
            # snap-to-peak step are SKIPPED for this source so the
            # rendered blocks land exactly where the caller asked.
            beat_times = np.asarray(self.BEAT_TIMES, dtype=float)
            beat_times = beat_times[(beat_times >= 0.0)
                                    & (beat_times < total_duration)]
            tempo_val = 0.0
            total_detected = len(beat_times)
            kept = total_detected

        elif self.BEAT_SOURCE == 'onset':
            # onset_detect: finds every transient, very sensitive.
            # delta ∈ [0.05 .. 0.60]; higher delta → fewer onsets.
            delta = 0.60 - sens * 0.55
            wait  = max(1, int(6 - sens * 4))        # min hops between onsets
            onset_hops = librosa.onset.onset_detect(
                onset_envelope=onset_env, sr=sr, hop_length=HOP_LENGTH,
                delta=delta, wait=wait, units='frames')
            beat_hops  = onset_hops
            beat_times = librosa.frames_to_time(
                onset_hops, sr=sr, hop_length=HOP_LENGTH)
            tempo_val  = 0.0
            total_detected = len(beat_hops)
            kept = total_detected          # no extra filtering here
        else:
            # beat_track: tempo-aligned musical beats.
            tempo, beat_hops = librosa.beat.beat_track(
                onset_envelope=onset_env, sr=sr, hop_length=HOP_LENGTH,
                tightness=120,
            )
            beat_times = librosa.frames_to_time(
                beat_hops, sr=sr, hop_length=HOP_LENGTH)
            total_detected = len(beat_hops)

            # strength filter: sens=0 keeps only top-half, sens=1 keeps all.
            if len(beat_hops) > 0:
                beat_strengths = onset_env[beat_hops]
                factor = 1.10 - sens * 1.10          # 1.10 .. 0.0
                strength_thresh = float(np.median(beat_strengths)) * factor
                strong_mask = beat_strengths >= strength_thresh
                beat_times = beat_times[strong_mask]
                kept = int(strong_mask.sum())
            else:
                kept = 0
            tempo_val = float(tempo) if np.ndim(tempo) == 0 else float(tempo[0])

        # -------- SUBDIVISION: add intermediate beats -------------------
        # When beat_track catches only every 2nd/4th hit we can inject
        # evenly-spaced sub-beats in between.  (Skipped in 'tempo' mode
        # because subdivision is already baked into the period spacing.)
        if (self.BEAT_SOURCE not in ('tempo', 'array')
                and self.BEAT_SUBDIV > 1 and len(beat_times) >= 2):
            sub_times = []
            for a, b in zip(beat_times, beat_times[1:]):
                sub_times.append(a)
                for k in range(1, self.BEAT_SUBDIV):
                    sub_times.append(a + (b - a) * k / self.BEAT_SUBDIV)
            sub_times.append(beat_times[-1])
            beat_times = np.array(sub_times)

        t_feat = time.time()
        if self.BEAT_SOURCE == 'array':
            print(f"Features extracted in {t_feat - t_load:.2f}s  "
                  f"(source=array, supplied={len(self.BEAT_TIMES)}, "
                  f"in-range={kept}, subdiv/density disabled, "
                  f"min_gap={self.BEAT_MIN_GAP}f still applied)")
        else:
            print(f"Features extracted in {t_feat - t_load:.2f}s  "
                  f"(source={self.BEAT_SOURCE}, sens={sens:.2f}, "
                  f"subdiv={self.BEAT_SUBDIV}, tempo {tempo_val:.1f} BPM, "
                  f"{total_detected} detected -> {kept} kept "
                  f"-> {len(beat_times)} events after subdivision)")

        total_frames = int(total_duration * self.FPS)
        bass_arr = np.zeros(total_frames, dtype=np.float32)
        bass_max = max(np.max(spec_mag[:BASS_RANGE]), 1e-6)
        for f in range(total_frames):
            oi = min(int(f * len(onset_env) / total_frames), len(onset_env) - 1)
            bass_arr[f] = float(np.clip(
                np.mean(spec_mag[:BASS_RANGE, oi]) / bass_max * 3, 0, 1))

        # Debug waveform envelope (RMS) sampled per output frame so it can be
        # drawn directly against frame-based block markers.  Uses the SAME
        # finer hop/frame as detect_wave_columns so the visible wave peaks
        # line up 1:1 with the scheduler's column picks.
        line_dbg_wave: np.ndarray | None = None
        if self.LINE_DEBUG and total_frames > 0:
            _dbg_hop   = 256
            _dbg_frame = 1024
            rms = librosa.feature.rms(y=y, frame_length=_dbg_frame,
                                      hop_length=_dbg_hop)[0]
            line_dbg_wave = np.zeros(total_frames, dtype=np.float32)
            sec_per_hop = _dbg_hop / float(sr)
            for f in range(total_frames):
                t_f = f / float(self.FPS)
                ri = min(int(round(t_f / sec_per_hop)), len(rms) - 1)
                ri = max(0, ri)
                line_dbg_wave[f] = float(rms[ri])
            p95 = float(np.percentile(line_dbg_wave, 95))
            if p95 > 1e-8:
                line_dbg_wave = np.clip(line_dbg_wave / p95, 0.0, 1.0)

        # ── Wave columns: single source of truth for punch-point timing ──
        # Detect rise→peak→descent-midpoint triples on the RMS envelope.
        # Used by:
        #   • line-mode scheduler to derive whole chains (each block = 1
        #     column: arrival=rise_f, shrink=end_f−rise_f).
        #   • ALL other modes (punch / dance / combo / wall) to SNAP each
        #     beat_frame to the closest column.rise_f so every visible
        #     punch lands on a real audio peak instead of drifting on the
        #     onset/tempo grid.  The number and cadence of events still
        #     comes from BEAT_SOURCE + BEAT_DENSITY; only the exact frame
        #     of each event is retimed to match the waveform.
        _mg = max(2, int(self.BEAT_MIN_GAP))
        wave_columns: list[dict] = detect_wave_columns(
            y, sr, HOP_LENGTH, float(self.FPS),
            min_gap_frames=_mg,
            prominence_pct=20.0,
            smooth_win=1,
        )
        if wave_columns:
            print(f"[wave] detected {len(wave_columns)} RMS columns "
                  f"(min_gap={_mg}f, prom=p20)")

        # Beat times -> video frame indices (targets will POP exactly here)
        beat_frames = [int(round(t * self.FPS)) for t in beat_times]
        beat_frames = [bf for bf in beat_frames if 0 <= bf < total_frames]

        # Density control — keep every Nth beat (d<1) or split each interval
        # into sub-beats (d>1).  1.0 keeps the cadence unchanged.
        # SOURCE=='array' is exempt: the caller already chose the exact
        # cadence by hand, so density is force-disabled to keep the
        # rendered blocks at a strict 1:1 with the supplied list.
        if self.BEAT_SOURCE == 'array':
            d = 1.0
        else:
            d = float(self.BEAT_DENSITY)
        if d < 0.999 and len(beat_frames) > 1:
            step = max(1, int(round(1.0 / d)))
            beat_frames = beat_frames[::step]
        elif d > 1.001 and len(beat_frames) >= 2:
            mult = int(round(d))
            dense = []
            for a, b in zip(beat_frames, beat_frames[1:]):
                dense.append(a)
                for k in range(1, mult):
                    dense.append(int(round(a + (b - a) * k / mult)))
            dense.append(beat_frames[-1])
            beat_frames = dense

        # ── setup scene ──────────────────────────────────────────────
        # Resolve mode(s) FIRST — lane count + floor spread + stickman
        # action all depend on the primary mode.  In combo mode
        # (e.g. --mode punch,dance) we pick the visually-richer sub-mode
        # as "primary" for scene dressing (viewport rails, floor spread)
        # — dance wins because its 4 panel rails frame the tunnel;
        # punch borrows those rails happily.
        try:
            modes_seq = _parse_modes(self.MODE)
        except ValueError as exc:
            print(f"[mode] {exc} — falling back to 'punch'.")
            modes_seq = ['punch']
        combo_mode   = len(modes_seq) >= 2
        # onset peaks are often a little late vs. perceived beat attack.
        # For line chains we bias events slightly earlier so visual hit feels
        # tighter to music (empirically ~2 frames @30fps).
        if ('line' in modes_seq
                and self.BEAT_SOURCE == 'onset'
                and len(beat_frames) > 0):
            advance_f = 2
            beat_frames = [max(0, bf - advance_f) for bf in beat_frames]
            beat_frames = sorted(set(beat_frames))
            print(f"[line-timing] onset advance: -{advance_f}f")

        # ── Wave-column beats: one punch per RMS column ───────────────────
        # For every mode EXCEPT solo line (which derives chains directly
        # inside pre_schedule) and --beat_source tempo (which the user
        # explicitly asked for a metronome-steady cadence), REPLACE
        # beat_frames with the rise-start frame of each detected wave
        # column.  The goal is a STRICT 1:1 between the visible
        # waveform columns and the spawned blocks, so --density is
        # intentionally IGNORED here — the user asked "why does my
        # block count not match the column count?", and the answer is
        # that density was previously halving the column list.
        #
        # We still enforce `--beat_min_gap` to collapse genuine
        # double-peaks that are too close to be distinguishable
        # visually (e.g. a hi-hat split-second repeat), but nothing
        # else trims the column list after this point inside the
        # beat-generation stage.  If the envelope is flat (no columns
        # detected) we fall back to the original onset/beat_track
        # beat_frames so nothing breaks on pure-tone inputs.
        solo_line = (len(modes_seq) == 1 and modes_seq[0] == 'line')
        if (not solo_line
                and self.BEAT_SOURCE not in ('tempo', 'array')
                and wave_columns
                and len(wave_columns) >= 1):
            col_rises = sorted(int(c['rise_f']) for c in wave_columns
                               if 0 <= int(c['rise_f']) < total_frames)
            n_cols = len(col_rises)
            # Enforce min_gap so ultra-close double peaks don't collide.
            _mg = max(1, int(self.BEAT_MIN_GAP))
            dedup: list[int] = []
            for bf in col_rises:
                if not dedup or bf - dedup[-1] >= _mg:
                    dedup.append(bf)
            beat_frames = dedup
            print(f"[wave-beats] replaced onset/beat-track events with "
                  f"{n_cols} RMS columns -> {len(beat_frames)} beats "
                  f"(1:1 mapping, density ignored, "
                  f"min_gap={_mg}f merged {n_cols - len(beat_frames)})")
        # Scene dressing: 'dance' wins if present (has lane panel rails);
        # otherwise use the first mode present.  'line' falls back to
        # 'punch' scene dressing since a line bar is still an air target
        # over the same tunnel.
        if 'dance' in modes_seq:
            primary_mode = 'dance'
        elif 'punch' in modes_seq:
            primary_mode = 'punch'
        else:
            # 'line' solo and 'relax' solo both reuse the punch scene
            # dressing (plain tunnel, no lane rails, no air targets).
            primary_mode = 'punch'
        mode         = primary_mode   # used below for scene dressing
        if combo_mode:
            print(f"[mode] combo ✔  beats cycle: "
                  f"{' → '.join(modes_seq)}  (primary={primary_mode})")
        # Both modes use the same 4-lane layout now.  Dance aligns the
        # lanes with its 4 viewport panels; punch keeps a slightly
        # tighter `floor_spread` because it has no panel rails and uses
        # smaller air cubes, so lanes shouldn't fan out as far.
        n_lanes_mode = N_LANES_DANCE if mode == 'dance' else N_LANES
        floor_spread = _FLOOR_SPREAD_BY_MODE.get(mode, 0.50)
        # Apply per-segment camera overrides (from drag-adjust UI)
        cam_kwargs: dict = dict(n_lanes=n_lanes_mode,
                                floor_spread_frac=floor_spread)
        if self.FLOOR_HIT_FRAC is not None:
            cam_kwargs["hit_zone_frac"] = float(self.FLOOR_HIT_FRAC)
        if self.HORIZON_FRAC is not None:
            cam_kwargs["horizon_frac"] = float(self.HORIZON_FRAC)
        if self.FLOOR_SPREAD_FRAC is not None:
            cam_kwargs["floor_spread_frac"] = float(self.FLOOR_SPREAD_FRAC)
        if self.FAR_SPREAD_FRAC is not None:
            cam_kwargs["far_spread_frac"] = float(self.FAR_SPREAD_FRAC)
        if getattr(self, "WALL_FLOOR_GAP_FRAC", None) is not None:
            cam_kwargs["wall_floor_gap_frac"] = float(self.WALL_FLOOR_GAP_FRAC)
        cam       = PerspectiveCamera(self.WIDTH, self.HEIGHT, **cam_kwargs)
        # Apply color overrides to all targets.
        Target.COLOR_LEFT  = self.CUBE_COLOR_LEFT
        Target.COLOR_RIGHT = self.CUBE_COLOR_RIGHT

        # Both modes now use the 4-lane layout, so the scrolling runway
        # tiles should also be drawn 1-per-lane (matches the 4 viewport
        # panels below them and the 4 rails targets fly along).
        tunnel    = TunnelRenderer(cam, show_floor_panels=self.SHOW_FLOOR_PANELS,
                                   lane_tiles=True,
                                   floor_panel_color=getattr(self, "FLOOR_PANEL_COLOR", None),
                                   floor_panel_opacity=float(getattr(self, "FLOOR_PANEL_OPACITY", 1.0)),
                                   floor_panel_blink=getattr(self, "FLOOR_PANEL_BLINK", False),
                                   floor_panel_image=getattr(self, "FLOOR_PANEL_IMAGE", None),
                                   floor_full_static_image=bool(getattr(self, "FLOOR_FULL_STATIC_IMAGE", False)),
                                   floor_layout=getattr(self, "FLOOR_LAYOUT", "auto"),
                                   floor_bg_color=getattr(self, "FLOOR_BG_COLOR", None),
                                   floor_bg_opacity=float(getattr(self, "FLOOR_BG_OPACITY", 1.0)),
                                   chevron_color=getattr(self, "CHEVRON_COLOR", "#FFD700"),
                                   chevron_scroll=bool(getattr(self, "CHEVRON_SCROLL", True)),
                                   chevron_blink=bool(getattr(self, "CHEVRON_BLINK", False)),
                                   chevron_width_frac=float(getattr(self, "CHEVRON_WIDTH_FRAC", 0.45)),
                                   chevron_count=int(getattr(self, "CHEVRON_COUNT", 6)))
        side_rail: SideRailRenderer | None = None
        if self.SHOW_SIDE_RAILS:
            side_rail = SideRailRenderer(
                cam,
                color=self.RAIL_COLOR,
                shape=self.RAIL_SHAPE,
                height=self.RAIL_HEIGHT,
                offset_x=self.RAIL_OFFSET_X,
                image_path=self.RAIL_IMAGE,
                texture_non_loop=bool(getattr(self, "RAIL_TEXTURE_NON_LOOP", False)),
                pulse=self.RAIL_PULSE,
                pulse_intensity=self.RAIL_PULSE_INTENSITY,
                chevron_depth=getattr(self, "RAIL_CHEVRON_DEPTH", 1.0),
                chevron_density=getattr(self, "RAIL_CHEVRON_DENSITY", 6),
                pillar_count=getattr(self, "RAIL_PILLAR_COUNT", 16),
                pillar_radius=getattr(self, "RAIL_PILLAR_RADIUS", 1.0),
                chase_mode=getattr(self, "RAIL_CHASE_MODE", "time"),
                chase_speed_frames=getattr(self, "RAIL_CHASE_SPEED_FRAMES", 4),
                dot_count=getattr(self, "RAIL_DOT_COUNT", 24),
                dot_lines=getattr(self, "RAIL_DOT_LINES", 1),
                dot_size_px=getattr(self, "RAIL_DOT_SIZE_PX", 6),
                dot_anim_mode=getattr(self, "RAIL_DOT_ANIM_MODE", "audio"),
                dot_color_near=getattr(self, "RAIL_DOT_COLOR_NEAR", "#FF60FF"),
                dot_color_far=getattr(self, "RAIL_DOT_COLOR_FAR", "#00FFFF"),
            )
        bg_layer = SegmentBackgroundLayer(
            self.WIDTH,
            self.HEIGHT,
            bg_type=str(getattr(self, "BACKGROUND_TYPE", "solid")),
            color=str(getattr(self, "BACKGROUND_COLOR", "#000000")),
            image_path=getattr(self, "BACKGROUND_IMAGE", None),
            video_path=getattr(self, "BACKGROUND_VIDEO", None),
            fps=float(self.FPS),
        )
        particles = ParticleSystem()
        # Stickman action pick: combo if 2+ modes, else match the single
        # mode's action library.  Solo 'line' uses its own 'line' action
        # so the HOLD_L/HOLD_R poses resolve correctly.
        if combo_mode:
            stick_action = 'combo'
        elif len(modes_seq) == 1 and modes_seq[0] == 'line':
            stick_action = 'line'
        elif len(modes_seq) == 1 and modes_seq[0] == 'relax':
            stick_action = 'relax'
        else:
            stick_action = mode

        # Resolve the stickman's draw-box.  StickmanHUD's default is the
        # left-column HUD strip (x=W*1%, y=H*9%, w=W*13.5%, h=H*54%);
        # any of --stick_x0 / --stick_y0 / --stick_w / --stick_h that
        # the caller passed (>= 0) overrides that component while the
        # rest stay on the legacy default.  Pass `box=None` only when
        # ALL four are negative so StickmanHUD keeps its old behaviour
        # for callers that didn't opt in.
        _stick_box: tuple[int, int, int, int] | None = None
        if any(v >= 0 for v in (self.STICK_X0, self.STICK_Y0,
                                self.STICK_W,  self.STICK_H)):
            _def_x = int(self.WIDTH  * 0.010)
            _def_y = int(self.HEIGHT * 0.09)
            _def_w = int(self.WIDTH  * 0.135)
            _def_h = int(self.HEIGHT * 0.54)
            bx = self.STICK_X0 if self.STICK_X0 >= 0 else _def_x
            by = self.STICK_Y0 if self.STICK_Y0 >= 0 else _def_y
            bw = self.STICK_W  if self.STICK_W  > 0 else _def_w
            bh = self.STICK_H  if self.STICK_H  > 0 else _def_h
            _stick_box = (int(bx), int(by), int(bw), int(bh))
            print(f"[stickman] custom box: x0={bx} y0={by} "
                  f"w={bw} h={bh} (frame {self.WIDTH}x{self.HEIGHT})")

        if self.SHOW_STICKMAN:
            stick = StickmanHUD(cam, action=stick_action,
                                box=_stick_box)
        else:
            stick = None
        combo     = ComboHUD(cam)
        countdown_hud = None
        countdown_audio_events: list[tuple[float, bool]] = []
        if bool(getattr(self, "RELAX_COUNTDOWN_ENABLED", True)):
            countdown_hud = CountdownHUD(
                cam,
                color=str(getattr(self, "RELAX_COUNTDOWN_COLOR", "#FFFFFF")),
                max_show_sec=float(getattr(self, "RELAX_COUNTDOWN_MAX_SEC", 5.0)),
                anim=str(getattr(self, "RELAX_COUNTDOWN_ANIM", "pop")),
                box=(
                    float(getattr(self, "RELAX_COUNTDOWN_X", 0.88)),
                    float(getattr(self, "RELAX_COUNTDOWN_Y", 0.04)),
                    float(getattr(self, "RELAX_COUNTDOWN_W", 0.10)),
                    float(getattr(self, "RELAX_COUNTDOWN_H", 0.16)),
                ),
            )
        viewport  = ViewportFrame(cam, neon_color=self.PANEL_NEON_COLOR,
                                  mode=mode)
        # ── auto-adjust TRAVEL so blocks flow smoothly ──────────────
        # Base auto-travel = one full L↔R cycle = 2 × beat_period, so a new
        # block enters the lane right as the previous one pops.
        # BLOCK_SPEED < 1.0 slows the blocks down (longer travel → more blocks
        # visible at once on each lane, staggered near→far, like the reference
        # tunnel shot); > 1.0 speeds them up.
        # Solo relax obstacles are meant to drift in slowly — the
        # player is just chilling and watching them glide past.  We
        # halve the effective speed (= double the travel window) so
        # each slab is on-screen roughly twice as long as a punch cube
        # would be at the same --speed setting.  Combo modes that
        # include relax keep the shared speed so lane cadence stays
        # coherent with the other sub-modes.
        solo_relax = (len(modes_seq) == 1 and modes_seq[0] == 'relax')
        # 4.0× slowdown for relax mode: originally 2.0, bumped per user
        # feedback ("các khối đi chuyển chậm hơn x2 nữa") — the dodge
        # choreography reads better when blocks drift in slowly enough
        # for the stickman pose + camera bob to settle between beats.
        #
        # ARRAY-source exception: when the caller hand-picks beat times
        # (Studio "Auto Gen Block" → user-edited ticks), the user has
        # already chosen the cadence and expects every entry to spawn
        # an obstacle.  Keeping the 4× multiplier here would auto-blow
        # `travel` to several SECONDS for sparsely-spaced user beats
        # (e.g. 5 s/beat → travel = 40 s) which then silently drops
        # every beat that falls inside the [0, travel) window via the
        # early-spawn filter below — symptom: zero obstacles render.
        # For array+relax we keep the multiplier at 1.0 so travel
        # auto-derives to roughly one user-gap, blocks fit between
        # consecutive user beats, and the early-spawn filter doesn't
        # need to drop anything.
        _relax_slow_mult = (4.0 if solo_relax
                            and self.BEAT_SOURCE != 'array' else 1.0)

        travel = self.TRAVEL_FRAMES
        if self.TRAVEL_FRAMES < 0 and len(beat_frames) >= 2:
            diffs = np.diff(beat_frames)
            base = int(round(np.median(diffs) * 2))     # one L↔R cycle
            speed = max(0.05, float(self.BLOCK_SPEED))
            travel = max(8, int(round(base / speed * _relax_slow_mult)))
            n_in_flight = max(1, int(round(travel / base)))
            slow_note = (f"  relax_slow×{_relax_slow_mult:.1f}"
                         if _relax_slow_mult != 1.0 else "")
            print(f"[travel:auto] period={int(np.median(diffs))}f  "
                  f"base_cycle={base}f  speed={speed:.2f}{slow_note}  "
                  f"travel={travel}f  (~{n_in_flight} blocks/lane visible)")
        elif self.TRAVEL_FRAMES >= 0 and _relax_slow_mult != 1.0:
            # Explicit --travel was given; still halve for relax so the
            # user doesn't have to double it manually.
            travel = max(8, int(round(self.TRAVEL_FRAMES * _relax_slow_mult)))
            print(f"[travel:manual] user travel={self.TRAVEL_FRAMES}f "
                  f"× relax_slow {_relax_slow_mult:.1f} = {travel}f")
        if solo_relax and float(getattr(self, "RELAX_TRAVEL_SEC", 0.0)) > 0.0:
            travel = max(8, int(round(float(self.RELAX_TRAVEL_SEC) * float(self.FPS))))
            print(f"[relax-travel] override travel={self.RELAX_TRAVEL_SEC:.2f}s "
                  f"({travel}f)")

        game = GameManager(cam, travel=travel)

        # Per-lane visual spacing guard: blocks on the same lane must be at
        # least this many SPAWN-frames apart so they sit at distinguishable
        # depths.  This is what prevents "piled-up cubes" when --speed is
        # small (long travel) and onset detection fires rapidly.
        if len(beat_frames) >= 2:
            base_cycle = int(round(np.median(np.diff(beat_frames)) * 2))
        else:
            base_cycle = 16
        # Cap blocks-per-lane to MAX_PER_LANE so they're always spaced out
        # enough to be visually distinct.
        max_per_lane = max(1, int(self.MAX_PER_LANE))
        min_lane_gap = max(1, travel // max_per_lane, base_cycle // 2)
        actual_max = max(1, travel // min_lane_gap)
        print(f"[spacing] min_lane_gap={min_lane_gap}f  "
              f"(max {actual_max} blocks/lane visible)", flush=True)

        if self.LANE_FILTER is not None:
            lane_list = sorted(v + 1 for v in self.LANE_FILTER)
            print(f"[lane_filter] Enabled lanes (1-based): {lane_list}", flush=True)

        # ── Solo-relax fixed-delay cadence override ────────────────────
        # When --mode is solo "relax" AND --relax_interval is set, we
        # REPLACE `beat_frames` with a schedule that guarantees the
        # PREVIOUS block has fully disappeared BEFORE the next one
        # spawns at the horizon.  The CLI value is interpreted as a
        # "time delay" in the strict sense (user: "khối phía trước
        # phải khuất hẵn sau bao nhiêu s thì mới bắt đầu xuất hiện
        # khối tiếp theo"):
        #
        #     next_spawn = prev_die + delay*FPS
        #
        # prev_die is derived analytically from RelaxTarget.is_dead
        # (`hit + travel·1.2/v2`).  With the current semantics, each
        # beat marks SPAWN-at-horizon time, then the block waits
        # `relax_wait_f`, then moves for `travel` frames until hit.
        # So the spawn-to-spawn step is:
        #
        #     step_f = wait + travel + exit_pad + 1 + delay_f
        #            = (wait before movement) + (travel to fly in) +
        #              (travel past camera) +
        #              (requested idle delay)
        solo_relax = (len(modes_seq) == 1 and modes_seq[0] == 'relax')
        relax_wait_f = max(0, int(round(float(self.RELAX_WAIT_SEC) * self.FPS)))
        if solo_relax and self.RELAX_INTERVAL > 0.0:
            # Derive exit_pad from the RelaxTarget motion profile so
            # the schedule stays correct even if the phase parameters
            # change later.
            T_split  = RelaxTarget._phase_split_t()
            z_split  = 1.0 - RelaxTarget.PHASE_SPLIT_D
            v2       = z_split / max(1e-6, 1.0 - T_split)
            exit_pad = int(round(travel * 1.2 / v2))
            delay_f  = max(0, int(round(self.RELAX_INTERVAL * self.FPS)))
            step_f   = max(1, travel + relax_wait_f + exit_pad + 1 + delay_f)
            first_f  = 0
            beat_frames = list(range(first_f, total_frames, step_f))
            print(f"[relax-timer] fixed-delay cadence: "
                  f"delay={self.RELAX_INTERVAL:.2f}s ({delay_f}f) "
                  f"after disappearance  "
                  f"(wait={relax_wait_f}f + travel={travel}f "
                  f"+ exit_pad={exit_pad}f + delay={delay_f}f "
                  f"→ step={step_f}f)  "
                  f"→ {len(beat_frames)} obstacles "
                  f"(first @ frame {first_f} / {first_f/self.FPS:.2f}s)")
        elif solo_relax and self.BEAT_SOURCE == 'array':
            # Array ticks are interpreted as SPAWN times for relax.
            n_before = len(beat_frames)
            beat_frames = [int(bf) for bf in beat_frames
                           if 0 <= int(bf) < total_frames]
            n_clipped = n_before - len(beat_frames)
            print(f"[relax-array] interpreting {n_before} array "
                  f"beat(s) as obstacle SPAWN times "
                  f"(hit_frame = spawn + wait={relax_wait_f}f + travel={travel}f, "
                  f"matching preview).  {len(beat_frames)} block(s) "
                  f"fit within audio ({total_frames}f); dropped "
                  f"{n_clipped} that fell outside the song.")
        elif solo_relax:
            # Music-driven solo-relax: beat ticks are spawn times, so no
            # early-spawn clipping is needed beyond non-negative frames.
            n_before = len(beat_frames)
            beat_frames = [int(bf) for bf in beat_frames if bf >= 0]
            dropped = n_before - len(beat_frames)
            if dropped:
                print(f"[relax-timer] dropped {dropped} early beat(s) "
                      f"(bf<0).")

        game.RELAX_KIND_RATIO_MIDDLE = float(
            max(0.0, min(1.0, getattr(self, "RELAX_KIND_RATIO_MIDDLE", 0.33)))
        )
        game.RELAX_WAIT_FRAMES = relax_wait_f
        game.RELAX_TEXTURE_LOW = getattr(self, "RELAX_TEXTURE_LOW", None)
        game.RELAX_TEXTURE_HIGH = getattr(self, "RELAX_TEXTURE_HIGH", None)
        game.RELAX_TEXTURE_MIDDLE = getattr(self, "RELAX_TEXTURE_MIDDLE", None)
        game.RELAX_HOLE_MASK_PATH = getattr(self, "RELAX_HOLE_MASK_PATH", None)
        enabled_kinds: list[str] = []
        if bool(getattr(self, "RELAX_SHOW_LOW", True)):
            enabled_kinds.append("low")
        if bool(getattr(self, "RELAX_SHOW_HIGH", True)):
            enabled_kinds.append("high")
        if bool(getattr(self, "RELAX_SHOW_MIDDLE", True)):
            enabled_kinds.append("middle")
        game.RELAX_ENABLED_KINDS = tuple(enabled_kinds or ["low", "high", "middle"])
        game.pre_schedule(beat_frames, bass_arr,
                          min_gap_frames=self.BEAT_MIN_GAP,
                          min_lane_gap=min_lane_gap,
                          mode=modes_seq,
                          lane_filter=self.LANE_FILTER,
                          dance_pair_cycle=self.DANCE_PAIR_CYCLE,
                          punch_pair_cycle=self.PUNCH_PAIR_CYCLE,
                          line_beats=self.LINE_BEATS,
                          beat_density=self.BEAT_DENSITY,
                          wave_columns=wave_columns,
                          line_zigzag=self.LINE_ZIGZAG,
                          beat_source=self.BEAT_SOURCE)

        # Give the stickman its beat timeline for pose-sync.
        # We use the ACTUAL scheduled targets (not raw beat_frames) so that:
        #   • lane info drives WHICH arm we throw (left lane → left-arm punch)
        #   • beats that were skipped by the scheduler (lane-stack guard,
        #     too-close merge) don't trigger empty punches.
        # Events are 3-tuples (t_hit, kind, lean_scale).  `lean_scale`
        # multiplies the pose's `lean` (and modestly its `drop`) at the
        # beat's moment so the stickman takes a WIDER step toward outer
        # lanes than toward inner lanes.  For 2-lane punch mode, every
        # target is an "outer" lane, so scale == 1.0 → unchanged legacy
        # animation.  For 4-lane dance mode:
        #   lanes 0, 3  (outer)  → scale = 1.6   (big, wide stomp)
        #   lanes 1, 2  (inner)  → scale = 0.55  (tight, short stomp)
        # The formula is `0.55 + 1.05 * offset_norm` where `offset_norm`
        # is |lane - center| / (half_width), so it extends smoothly to any
        # even lane count.
        stick_events: list[tuple] = []
        nL = max(2, int(cam.n_lanes))
        half = (nL - 1) / 2.0
        # Paired-spawn targets (--lanes 1,2 / 3,4) share the SAME hit_frame;
        # we emit ONE double-hand event per pair, not one per cube.
        seen_paired_hits: set[int] = set()
        for tg in game.targets:
            t_hit = tg.hit_frame / self.FPS
            sustain = 0.0
            if isinstance(tg, WallTarget):
                kind = 'W'
                lean_scale = 1.0
            elif isinstance(tg, RelaxTarget):
                # Relax obstacles → jump (low) or squat (high) — no
                # lane cycling, lean_scale stays neutral.  We fire at
                # `dodge_frame` (Phase 1 → Phase 2 transition) so the
                # stickman starts the pose exactly as visual motion
                # explodes into the "vút" phase.
                #
                # The pose holds until `dodge_end_frame` (dodge + a
                # fraction of travel driven by DODGE_HOLD_FRAC) and
                # then the tween engine recovers to RELAX_STAND.  This
                # gives the avatar a clear "dodge → stand → dodge"
                # rhythm between blocks instead of chaining SQ/JP
                # waypoints end-to-end (which the user observed as
                # "never returning to initial position").
                if tg.kind == 'middle':
                    continue
                kind = 'JP' if tg.kind == 'low' else 'SQ'
                lean_scale = 1.0
                t_hit = tg.dodge_frame / self.FPS
                hold_frames = tg.dodge_end_frame - tg.dodge_frame
                sustain = max(
                    _RELAX_BOB_WINDOW_F, int(hold_frames)
                ) / float(self.FPS)
            elif isinstance(tg, LineTarget):
                # Emit one short event per block in the chain so the
                # stickman arm tracks each cube.  Each event sustains
                # for that block's OWN wave-derived shrink duration so
                # the stickman pose freezes exactly as long as the
                # column is on screen.
                #
                # VERTICAL mode (legacy):
                #   side comes from the chain's is_left (whole chain
                #   lives on one lane), vert alternates D/U per block
                #   to match the up/down zigzag visible on that lane.
                #
                # HORIZONTAL mode:
                #   every block spans OUTER lane 0 ↔ OUTER lane n-1 and
                #   the chain lives high above the horizon.  Instead of
                #   a static up-left / up-right hold, each block emits
                #   a SWEEP event whose start & end poses bracket the
                #   sustain window — the stickman's arm visibly sweeps
                #   across the top of the screen in lock-step with the
                #   block's head → tail direction:
                #     even i (head lane 0, tail lane n-1) → ZSLR  (L→R)
                #     odd  i (head lane n-1, tail lane 0) → ZSRL  (R→L)
                #   lean_scale is forced to the "full outer" value
                #   because every hit spans the outermost lanes
                #   regardless of tg.lane.
                is_horiz = (tg.zigzag == 'horizontal')
                if is_horiz:
                    lean_scale_h = 0.55 + 1.05 * 1.0  # outer-lane value
                else:
                    side_tag_legacy = 'L' if tg.is_left else 'R'
                    if nL <= 2:
                        lean_scale_v = 1.0
                    else:
                        offset_norm = abs(tg.lane - half) / half
                        lean_scale_v = 0.55 + 1.05 * offset_norm
                n = tg.n_cubes
                for i in range(n):
                    t_i  = tg.block_hit_frames[i] / self.FPS
                    dur_i = (tg.block_shrink_dur[i]
                             if i < len(tg.block_shrink_dur) else tg._D)
                    per_sustain_i = max(1, int(dur_i)) / float(self.FPS)
                    if is_horiz:
                        # Sweep left→right for even blocks, right→left
                        # for odd blocks — matches the alternating
                        # head-lane zigzag rendered by LineTarget.draw.
                        kind_i = 'ZSLR' if (i % 2 == 0) else 'ZSRL'
                        stick_events.append((t_i, kind_i,
                                             lean_scale_h, per_sustain_i))
                    else:
                        side_tag = side_tag_legacy
                        vert     = 'D' if (i % 2 == 0) else 'U'
                        stick_events.append((t_i, 'Z' + side_tag + vert,
                                             lean_scale_v, per_sustain_i))
                # Skip the generic single-event append below.
                continue
            elif getattr(tg, 'paired_side', None):
                if tg.hit_frame in seen_paired_hits:
                    continue     # second cube of a pair — already emitted
                seen_paired_hits.add(tg.hit_frame)
                # paired_side tag encodes WHICH paired pattern fired:
                #   'L' / 'R'   → punch paired → 'LL'/'RR' (2 fists)
                #   'DL' / 'DR' → dance paired → 'JL'/'JR' (feet together)
                ptag = tg.paired_side
                if ptag == 'DL':
                    kind = 'JL'
                elif ptag == 'DR':
                    kind = 'JR'
                elif ptag == 'L':
                    kind = 'LL'
                else:
                    kind = 'RR'
                # Paired poses define their own lean; keep scale=1.0 so
                # outer-vs-inner math doesn't distort the symmetric
                # double silhouettes.
                lean_scale = 1.0
            else:
                # Multi-lane safe: left-side half → 'L', right-side half → 'R'.
                side_tag = 'L' if tg.is_left else 'R'
                # In combo mode we need the stickman to know whether this
                # beat is a PUNCH or a STOMP — prefix the kind with the
                # target-class letter ('P' = punch cube, 'D' = dance tile).
                # Single-mode runs keep the legacy 'L'/'R' kinds so older
                # poses/exports stay unchanged.
                if combo_mode:
                    prefix = 'D' if isinstance(tg, DanceTarget) else 'P'
                    kind = prefix + side_tag
                else:
                    kind = side_tag
                if nL <= 2:
                    lean_scale = 1.0
                else:
                    offset_norm = abs(tg.lane - half) / half   # 0..1
                    lean_scale = 0.55 + 1.05 * offset_norm
            if sustain > 0.0:
                stick_events.append((t_hit, kind, lean_scale, sustain))
            else:
                stick_events.append((t_hit, kind, lean_scale))
        if stick is not None:
            stick.set_beat_events(stick_events, self.FPS)
            print(f"[stickman] {len(stick_events)} events  "
                  f"tween={stick._tween_dur:.3f}s  "
                  f"waypoints={len(stick._timeline)}")
        else:
            print(f"[stickman] disabled — {len(stick_events)} events prepared "
                  f"for export only")

        # Optional debug markers: one marker per beat/block event.
        # Works for ALL modes — collects every stickman event regardless
        # of kind prefix so punch/dance/combo/line all show correctly.
        line_dbg_events: list[tuple[int, str]] = []
        if self.LINE_DEBUG:
            for ev in stick_events:
                if len(ev) >= 2 and isinstance(ev[1], str):
                    f_ev = int(round(float(ev[0]) * self.FPS))
                    line_dbg_events.append((f_ev, ev[1]))
            line_dbg_events.sort(key=lambda t: t[0])
            if line_dbg_events:
                ts = ", ".join(f"{f/self.FPS:.3f}" for f, _ in line_dbg_events)
                print(f"[beat-debug] events={len(line_dbg_events)}  t=[{ts}]")

        # Per-event audio amplitude (0..1) — derived from the rise→peak
        # column closest to each event's frame.  Studio uses this to
        # power its waveform threshold slider so the user can mute weak
        # ticks without re-running detect.  Always emitted (even when
        # ``wave_columns`` is empty) so the loader's array-shape
        # contract is stable: same length as ``stick_events`` and all
        # 1.0 when no column data is available (i.e. nothing to
        # filter, every beat is "loud").
        event_heights: list[float] = []
        if wave_columns:
            _max_h = max(float(c.get('height', 0.0))
                         for c in wave_columns)
            _max_h = max(_max_h, 1e-9)
            _col_frames = np.array(
                [int(c['rise_f']) for c in wave_columns], dtype=np.int64
            )
            _col_h = np.array(
                [float(c['height']) / _max_h for c in wave_columns],
                dtype=np.float32,
            )
            for ev in stick_events:
                f_ev = int(round(float(ev[0]) * self.FPS))
                idx = int(np.argmin(np.abs(_col_frames - f_ev)))
                event_heights.append(float(np.clip(_col_h[idx], 0.0, 1.0)))
        else:
            event_heights = [1.0] * len(stick_events)

        # Apply the audio-amplitude threshold (powers the studio's
        # waveform red-line slider).
        #
        # The naive approach — filter ``stick_events`` by their
        # nearest-column height — silently DROPS peaks that the
        # ``GameManager.pre_schedule`` lane-spacing constraints
        # (``MAX_PER_LANE``, ``min_lane_gap``) had already removed
        # from ``self.targets``.  Symptom: the user sees red wave
        # peaks above the threshold line on the timeline that have
        # NO corresponding beat tick, because the scheduler quietly
        # skipped that beat as "lane stacked" before the threshold
        # filter ever ran.
        #
        # The user's expectation is "every red peak above the line
        # is a beat".  To honour that we REGENERATE the event list
        # straight from ``wave_columns`` whose normalised height is
        # ≥ threshold, completely bypassing the lane scheduler for
        # this export.  The rendered video downstream is unaffected:
        # it still runs ``GameManager`` against the supplied beat
        # times via ``--beat_source array`` and applies its own
        # spacing rules — but the studio's timeline preview now
        # matches the visible peaks 1:1.
        #
        # Skipped when:
        #   - ``BEAT_SOURCE == 'array'`` (caller already chose the
        #     exact event set; we must not silently rewrite it).
        #   - No wave columns were detected (pure-tone or silent
        #     audio); the original event list is still useful.
        if (
            self.BEAT_HEIGHT_THRESHOLD > 1e-6
            and self.BEAT_SOURCE != 'array'
            and wave_columns
        ):
            thr = float(self.BEAT_HEIGHT_THRESHOLD)
            _max_h = max(float(c.get('height', 0.0))
                         for c in wave_columns)
            _max_h = max(_max_h, 1e-9)
            new_events: list = []
            new_heights: list[float] = []
            for c in wave_columns:
                h_norm = float(c.get('height', 0.0)) / _max_h
                h_norm = max(0.0, min(1.0, h_norm))
                if h_norm < thr - 1e-6:
                    continue
                t_sec = float(int(c['rise_f'])) / float(self.FPS)
                if t_sec < 0.0 or t_sec >= float(total_duration):
                    continue
                # Generic "punch" kind — the studio timeline ignores
                # ``kind`` for visual styling (it just stores it for
                # round-trip), and the render path overrides it via
                # its own scheduler when the studio re-feeds the
                # times via ``--beat_times``.
                new_events.append((t_sec, 'L'))
                new_heights.append(h_norm)
            print(
                f"[beat_height_threshold {thr:.3f}] derived "
                f"{len(new_events)} events from {len(wave_columns)} "
                f"wave columns (was {len(stick_events)} scheduler "
                f"events; bypassing lane-spacing for export)"
            )
            stick_events  = new_events
            event_heights = new_heights

        # Persist events so stickman.py can render a standalone video that
        # lines up frame-for-frame with THIS rhythm render.
        if getattr(self, 'EXPORT_EVENTS', None):
            from stickman import save_events
            save_events(self.EXPORT_EVENTS, stick_events, meta={
                'event_heights':    event_heights,
                'fps':      self.FPS,
                'duration': float(total_duration),
                'tempo':    float(tempo_val),
                'source':   self.BEAT_SOURCE,
                'subdiv':   self.BEAT_SUBDIV,
                'density':  self.BEAT_DENSITY,
                'travel':   travel,
                'speed':    self.BLOCK_SPEED,
                'max_per_lane': self.MAX_PER_LANE,
                'beat_min_gap': self.BEAT_MIN_GAP,
                'audio':    audio_file,
                'mode':     ','.join(modes_seq),
                'action':   stick_action,
                'dance_pair_cycle': int(self.DANCE_PAIR_CYCLE),
                'punch_pair_cycle': int(self.PUNCH_PAIR_CYCLE),
                'line_beats':       int(self.LINE_BEATS),
                'lanes':    (sorted(v + 1 for v in self.LANE_FILTER)
                             if self.LANE_FILTER is not None else None),
            })
            print(f"[export] Wrote {len(stick_events)} events -> "
                  f"{self.EXPORT_EVENTS}")

        # Detection-only fast-path: bail BEFORE the per-frame rendering
        # loop so the Studio UI gets accurate beat timestamps without
        # paying for the full video render.  Events JSON has already been
        # written above when ``--export_events`` is supplied.
        if getattr(self, 'DETECT_ONLY', False):
            print(f"[detect-only] skipping render loop "
                  f"({len(stick_events)} events ready)")
            return

        # ── render loop ──────────────────────────────────────────────
        # Frames are streamed directly to the encoder as they are built so
        # peak RAM stays at O(1) per frame regardless of video length.
        # Previously all frames were buffered in a list before writing,
        # which caused ArrayMemoryError on long segments (e.g. a 3-minute
        # clip at 1920×1080 requires ~30 GB of RAM for the buffer alone).
        codec_label = "NVENC" if _CUPY else ("avc1" if self.is_mac else "libx264")
        print(f"Rendering frames to encoder ({codec_label})...", flush=True)
        t_render = time.time()
        last_pct = 0

        # Use a temp file in the system temp dir so we never need write
        # access to the cwd (which in a frozen EXE is the dist folder).
        import tempfile as _tmpmod
        _tmp_fd, temp_video = _tmpmod.mkstemp(suffix='.mp4', prefix='rhythm_tmp_')
        import os as _os; _os.close(_tmp_fd)

        try:
            from bundle_paths import find_ffmpeg as _find_ffmpeg
            _ffmpeg_bin = _find_ffmpeg()
        except Exception:
            _ffmpeg_bin = 'ffmpeg'

        if self.is_mac:
            fourcc = cv2.VideoWriter_fourcc(*'avc1')
            _vwriter = cv2.VideoWriter(temp_video, fourcc, self.FPS,
                                       (self.WIDTH, self.HEIGHT))
            _vproc = None
        else:
            vcodec = 'h264_nvenc' if _CUPY else 'libx264'
            preset = 'p4' if vcodec == 'h264_nvenc' else 'fast'
            _vcmd = (f'"{_ffmpeg_bin}" -y -f rawvideo -vcodec rawvideo -pix_fmt bgr24 '
                     f'-s {self.WIDTH}x{self.HEIGHT} -r {self.FPS} -i pipe:0 '
                     f'-vcodec {vcodec} -preset {preset} -b:v 3500k '
                     f'-bf 0 -vsync cfr -pix_fmt yuv420p '
                     f'-r {self.FPS} "{temp_video}"')
            _creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            _vproc = subprocess.Popen(shlex.split(_vcmd), stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL,
                                      creationflags=_creation_flags)
            _vwriter = None

        for fi in range(total_frames):
            pct = int(fi / total_frames * 100)
            if pct // 10 > last_pct:
                elapsed = time.time() - t_render
                fps_r   = fi / elapsed if elapsed > 0 else 0
                eta     = (total_frames - fi) / fps_r if fps_r > 0 else 0
                print(f"Progress: {pct}% | FPS: {fps_r:.1f} | "
                      f"ETA: {eta:.1f}s | live={len(game.targets)} "
                      f"p={len(particles.ps)} combo={combo.combo}")
                last_pct = pct // 10

            # update + collect hits (all targets were pre-scheduled upfront)
            hits = game.update(fi)

            # ── canvas build ────────────────────────────────────────
            canvas = bg_layer.frame(fi)

            # 1. tunnel walls + floor grid
            canvas = tunnel.draw(canvas, fi)

            # 1b. side rails (drawn just after the tunnel grid, before targets)
            if side_rail is not None:
                _bass_val = float(bass_arr[fi]) if fi < len(bass_arr) else 0.0
                _hit_this = len(hits) > 0
                side_rail.draw(canvas, fi, bass_val=_bass_val, hit=_hit_this)

            # 2. targets (back to front)
            for tg in game.alive_sorted(fi):
                canvas = tg.draw(canvas, cam, fi)

            # 4. process hits → VFX (particles only, no flash/slash)
            for tg in hits:
                # RelaxTarget is a DODGE obstacle, not a hit — the player
                # avatar jumps or squats to avoid it.  No particle burst,
                # no combo increment, no viewport shake: those would all
                # read as "you hit / destroyed the block" which is the
                # opposite of the relax mode's intent.  Camera bob and
                # the stickman JP/SQ pose already provide the feedback
                # that the obstacle was successfully avoided.
                if isinstance(tg, RelaxTarget):
                    continue
                # In horizontal-zigzag LineTarget chains each block lands
                # on a different lane (outer-left vs outer-right) and the
                # chain also lives higher in the sky than the normal
                # air-punch zone, so the particle burst must follow both
                # the block's actual X (per-block, not the target's
                # nominal spawn lane) AND its elevated Y.
                _line_horiz = (isinstance(tg, LineTarget)
                               and tg.zigzag == 'horizontal')
                if _line_horiz and tg.last_punched_i >= 0:
                    _lane_frac = (0.0 if (tg.last_punched_i % 2 == 0)
                                  else float(max(0, cam.n_lanes - 1)))
                    x = int(cam.lane_x(_lane_frac, 0.02))
                else:
                    x = int(cam.lane_x(tg.lane, 0.02))
                if isinstance(tg, PunchTarget):
                    if _line_horiz:
                        # Project the block's actual world-Y at the punch
                        # plane so the particles spawn right ON the block,
                        # not down at chest height.
                        _proj = cam.project(0.0,
                                            LineTarget.HORIZONTAL_WY,
                                            cam.Z_NEAR + 0.01)
                        y = int(_proj[1]) if _proj else int(cam.air_y(0.02, 0.55))
                    else:
                        y = int(cam.air_y(0.02, 0.55))
                    count = 50
                    viewport.trigger(1.0)
                elif isinstance(tg, DanceTarget):
                    # Stomp hit – particles burst along the floor plane.
                    y = int(cam.floor_y(0.02)) - 6
                    count = 40
                    viewport.trigger(0.9)
                elif isinstance(tg, StepTarget):
                    y = int(cam.floor_y(0.02)) - 10
                    count = 25
                    viewport.trigger(0.55)
                else:  # wall
                    y = (int(cam.floor_y(0.02)) + int(cam.ceil_y(0.02))) // 2
                    count = 55
                    viewport.trigger(1.2)
                particles.burst(x, y, tg.color, count)
                combo.register_hit(fi)

            # 5. particle update + draw
            particles.update()
            particles.draw(canvas)

            # 6. bloom / glow on everything so far (skip UI to keep text crisp)
            if self.BLOOM:
                canvas = gpu_glow(canvas, sigma=9.0, gain=0.32)

            # 6b. Relax-mode camera bob — vertical post-shift of the scene
            # to sell the jump (canvas drops) / duck (canvas rises).  We
            # apply this AFTER bloom so the bloom glow moves with the
            # scene, and BEFORE the HUDs so stickman / combo / debug
            # overlays stay pinned to their screen positions.
            if 'relax' in modes_seq:
                _bob_dy = _relax_camera_dy(game.targets, fi, self.HEIGHT)
                if _bob_dy != 0:
                    _M = np.float32([[1, 0, 0], [0, 1, _bob_dy]])
                    canvas = cv2.warpAffine(
                        canvas, _M, (self.WIDTH, self.HEIGHT),
                        borderValue=(0, 0, 0))

            # 7. HUDs (drawn last, above bloom)
            viewport.update()
            viewport.draw(canvas)
            if stick is not None:
                stick.draw(canvas, fi)
            combo.draw(canvas, fi)
            if countdown_hud is not None and 'relax' in modes_seq:
                countdown_hud.draw(canvas, game.targets, fi, float(self.FPS))
                countdown_audio_events.extend(countdown_hud.pop_audio_events())

            if self.LINE_DEBUG and line_dbg_events:
                # Top timeline: visualize where each line block event lands.
                x0 = int(self.WIDTH * 0.06)
                x1 = int(self.WIDTH * 0.94)
                y0 = int(self.HEIGHT * 0.055)
                y1 = y0 + 8
                cv2.rectangle(canvas, (x0, y0), (x1, y1), (65, 65, 65), -1)
                cv2.rectangle(canvas, (x0, y0), (x1, y1), (140, 140, 140), 1)

                for idx, (f_ev, kind_ev) in enumerate(line_dbg_events, start=1):
                    px = int(x0 + (x1 - x0) * (f_ev / max(1, total_frames - 1)))
                    if fi < f_ev - 1:
                        col = (120, 120, 120)     # upcoming
                    elif abs(fi - f_ev) <= 1:
                        col = (80, 240, 255)      # active
                    else:
                        col = (80, 220, 120)      # passed
                    cv2.line(canvas, (px, y0 - 6), (px, y1 + 6), col, 1,
                             lineType=cv2.LINE_AA)
                    if idx <= 12:
                        cv2.putText(canvas, str(idx), (px - 5, y0 - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.32, col, 1,
                                    lineType=cv2.LINE_AA)

                # Current frame cursor on timeline.
                px_now = int(x0 + (x1 - x0) * (fi / max(1, total_frames - 1)))
                cv2.line(canvas, (px_now, y0 - 10), (px_now, y1 + 10),
                         (255, 255, 255), 1, lineType=cv2.LINE_AA)
                # Next-event delta readout.
                next_f = next((f for f, _ in line_dbg_events if f >= fi), None)
                dt_txt = "--"
                if next_f is not None:
                    dt_txt = f"{(next_f - fi) / self.FPS:+.3f}s"
                cv2.putText(canvas,
                            f"BEAT DBG events={len(line_dbg_events)}  t={fi/self.FPS:.3f}s  next={dt_txt}",
                            (x0, y1 + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.48,
                            (220, 220, 220), 1, lineType=cv2.LINE_AA)
                # RMS waveform overlay directly under the timeline.
                if line_dbg_wave is not None and len(line_dbg_wave) == total_frames:
                    wy0 = y1 + 30
                    wy1 = wy0 + int(self.HEIGHT * 0.10)
                    cv2.rectangle(canvas, (x0, wy0), (x1, wy1), (40, 40, 40), -1)
                    cv2.rectangle(canvas, (x0, wy0), (x1, wy1), (120, 120, 120), 1)
                    # Baseline
                    cv2.line(canvas, (x0, wy1), (x1, wy1), (90, 90, 90), 1,
                             lineType=cv2.LINE_AA)
                    # Polyline waveform
                    step = max(1, (x1 - x0) // 360)
                    pts = []
                    for x in range(x0, x1 + 1, step):
                        frac = (x - x0) / max(1, (x1 - x0))
                        wf_i = min(total_frames - 1,
                                   max(0, int(round(frac * (total_frames - 1)))))
                        amp = float(line_dbg_wave[wf_i])  # 0..1
                        yv = int(wy1 - amp * (wy1 - wy0 - 2))
                        pts.append((x, yv))
                    if len(pts) >= 2:
                        cv2.polylines(canvas, [np.array(pts, dtype=np.int32)], False,
                                      (130, 170, 255), 1, lineType=cv2.LINE_AA)
                    # Fill under curve (light alpha effect via overlay)
                    if len(pts) >= 2:
                        ov = canvas.copy()
                        fill_poly = [(x0, wy1)] + pts + [(x1, wy1)]
                        cv2.fillPoly(ov, [np.array(fill_poly, dtype=np.int32)],
                                     (70, 110, 170), lineType=cv2.LINE_AA)
                        canvas = cv2.addWeighted(ov, 0.35, canvas, 0.65, 0)

            # Stream the finished frame to the encoder immediately.
            # This keeps memory at O(1) regardless of video duration.
            if _vwriter is not None:
                _vwriter.write(canvas)
            elif _vproc is not None and _vproc.stdin is not None:
                _vproc.stdin.write(canvas.tobytes())

        t_done = time.time()
        print(f"\nFrame rendering done in {t_done - t_render:.2f}s  |  "
              f"avg {total_frames/(t_done-t_render):.1f} FPS")

        # ── finalise encoder ─────────────────────────────────────────
        bg_layer.close()
        if _vwriter is not None:
            _vwriter.release()
        if _vproc is not None:
            if _vproc.stdin:
                try:
                    _vproc.stdin.close()
                except OSError:
                    pass
            _vproc.wait()

        print(f"\nTotal time: {time.time()-t0:.2f}s")
        self._countdown_audio_events = countdown_audio_events
        return temp_video

    # -------------------------------------------------------------------
    def merge_audio(self, temp_video: str, audio_file: str,
                    output_filename: str = 'rhythm_output.mp4') -> bool:
        """Mux audio into already-encoded video without re-encoding video.

        Using -c:v copy avoids NVENC B-frame reordering / encoder latency
        that can introduce sync drift on longer clips.
        """
        print("\nMerging audio...")
        t0 = time.time()
        try:
            try:
                from bundle_paths import find_ffmpeg as _find_ffmpeg
                _ffmpeg_bin = _find_ffmpeg()
            except Exception:
                _ffmpeg_bin = 'ffmpeg'

            def _norm_mode(v: object) -> str:
                raw = str(v or "").strip().lower()
                return "file" if raw == "file" else "default"

            def _norm_last_mode(v: object) -> str:
                raw = str(v or "").strip().lower()
                if raw in {"file", "same"}:
                    return raw
                return "default"

            def _resolve_event_source(is_last: bool) -> tuple[str, str | tuple[float, float]]:
                # Returns ("file", path) or ("tone", (freq_hz, duration_sec)).
                mode = _norm_mode(getattr(self, "RELAX_COUNTDOWN_AUDIO_MODE", "default"))
                last_mode = _norm_last_mode(
                    getattr(self, "RELAX_COUNTDOWN_AUDIO_LAST_MODE", "default")
                )
                regular_file = str(getattr(self, "RELAX_COUNTDOWN_AUDIO_FILE", "") or "").strip()
                last_file = str(getattr(self, "RELAX_COUNTDOWN_AUDIO_LAST_FILE", "") or "").strip()

                if is_last:
                    if last_mode == "same":
                        if mode == "file" and regular_file and Path(regular_file).exists():
                            return ("file", regular_file)
                        return ("tone", (940.0, 0.09))
                    if last_mode == "file" and last_file and Path(last_file).exists():
                        return ("file", last_file)
                    return ("tone", (1260.0, 0.13))

                if mode == "file" and regular_file and Path(regular_file).exists():
                    return ("file", regular_file)
                return ("tone", (940.0, 0.09))

            events = list(getattr(self, "_countdown_audio_events", []) or [])
            enable_cd_audio = bool(getattr(self, "RELAX_COUNTDOWN_AUDIO_ENABLED", False))
            cd_volume = float(max(0.0, min(1.0, getattr(self, "RELAX_COUNTDOWN_AUDIO_VOLUME", 0.65))))

            # Base command: copy pre-rendered video stream, build output audio from
            # original track + optional countdown overlay sounds.
            cmd = [
                _ffmpeg_bin, '-y',
                '-i', temp_video,   # 0: video
                '-i', audio_file,   # 1: source audio
            ]

            filter_parts: list[str] = ["[1:a]aresample=44100,asetpts=PTS-STARTPTS[base]"]
            mix_inputs = ["[base]"]

            next_input_idx = 2
            if enable_cd_audio and events:
                for ev_idx, (ev_t, is_last) in enumerate(events):
                    try:
                        t_sec = max(0.0, float(ev_t))
                    except (TypeError, ValueError):
                        continue
                    src_kind, src_data = _resolve_event_source(bool(is_last))
                    if src_kind == "file":
                        src_path = str(src_data)
                        cmd += ['-i', src_path]
                    else:
                        freq_hz, dur_s = src_data  # type: ignore[misc]
                        cmd += [
                            '-f', 'lavfi',
                            '-t', f'{float(dur_s):.4f}',
                            '-i', f'sine=frequency={float(freq_hz):.2f}:sample_rate=44100',
                        ]
                    delay_ms = int(round(t_sec * 1000.0))
                    label = f"cd{ev_idx}"
                    filter_parts.append(
                        f"[{next_input_idx}:a]aformat=sample_rates=44100,"
                        f"volume={cd_volume:.4f},"
                        f"asetpts=PTS-STARTPTS,adelay={delay_ms}|{delay_ms}[{label}]"
                    )
                    mix_inputs.append(f"[{label}]")
                    next_input_idx += 1

            if len(mix_inputs) > 1:
                filter_parts.append(
                    "".join(mix_inputs)
                    + f"amix=inputs={len(mix_inputs)}:normalize=0[aout]"
                )
                cmd += [
                    '-filter_complex', ';'.join(filter_parts),
                    '-map', '0:v:0',
                    '-map', '[aout]',
                ]
            else:
                cmd += [
                    '-map', '0:v:0',
                    '-map', '1:a:0',
                ]

            cmd += [
                '-c:v', 'copy',     # DON'T re-encode video
                '-c:a', 'aac', '-b:a', '192k',
                '-shortest',
            ]
            if self.TIME_LIMIT:
                cmd += ['-t', str(self.TIME_LIMIT)]
            cmd += [output_filename]

            _creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    creationflags=_creation_flags)
            _, err = proc.communicate()
            if proc.returncode != 0:
                print(f"FFmpeg error: {err.decode(errors='replace')}")
                return False

            import os
            if os.path.exists(temp_video):
                os.remove(temp_video)
            print(f"Audio merged in {time.time()-t0:.2f}s -> {output_filename}")
            return True
        except Exception as e:
            print(f"Error merging audio: {e}")
            return False


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_arguments():
    p = argparse.ArgumentParser(description="Rhythm – tunnel rhythm-game visualization")
    p.add_argument('-W', '--width',    type=int,   default=1920)
    p.add_argument('-H', '--height',   type=int,   default=1080)
    p.add_argument('--fps',            type=int,   default=FPS,
                   help=(f'Output framerate (default {FPS}). Higher = '
                         'smoother motion (recommended 30-60).'))
    p.add_argument('--depth_mode',     type=str, default='linear',
                   choices=['linear', 'inv'],
                   help=('"linear" = block moves at constant WORLD speed '
                         '→ stays tiny at distance for most of travel, '
                         'zooms in only near hit zone (matches reference). '
                         '"inv" = 1/z mapping → linear on-screen growth. '
                         'Default: linear'))
    p.add_argument('--cube_radius',    type=float, default=0.18,
                   help=('Corner rounding of the default neon cubes, '
                         'as a fraction of the shortest projected face '
                         'edge.  0 = perfectly sharp, 0.15-0.25 = soft '
                         'bevel, 0.45 = nearly circular.  Default: 0.18'))
    p.add_argument('-i', '--input',    type=str,   required=True)
    p.add_argument('-o', '--output',   type=str,   required=True)
    p.add_argument('-d', '--duration', type=float, default=None)
    p.add_argument('-a', '--audio',    type=int,   default=0)
    p.add_argument('--bloom',   type=int, default=1, metavar='0|1',
                   help='Enable screen-space bloom glow (default 1)')
    p.add_argument('--floor_panels', type=int, default=1, metavar='0|1',
                   help=('Show the two rows of white/grey neon floor tiles '
                         'running down the tunnel. 0 = hide (minimal tunnel, '
                         'only the viewport panels remain on the ground). '
                         'Default 1.'))
    p.add_argument('--floor_panel_color', type=str, default=None, metavar='#RRGGBB',
                   help='Custom hex color for floor tile neon (e.g. #4af0c8). Default grey.')
    p.add_argument('--floor_panel_opacity', type=float, default=1.0, metavar='0..1',
                   help='Opacity of floor panel tiles (0=transparent, 1=solid). Default 1.0.')
    p.add_argument('--floor_panel_blink', type=int, default=0, metavar='0|1',
                   help='Flash floor tiles on/off each half-second. Default 0.')
    p.add_argument('--floor_panel_image', type=str, default=None, metavar='PATH',
                   help='Image file to perspective-warp onto floor tiles instead of flat fill.')
    p.add_argument('--floor_full_static_image', type=int, default=0, metavar='0|1',
                   help='When 1 AND --floor_panel_image is set, stretch the image '
                        'to the entire floor trapezoid as a single static graphic. '
                        'All other floor effects (chevron, tiles, BG color, blink, '
                        'opacity) are bypassed. Default 0.')
    # ── Floor layout + background ───────────────────────────────────────
    p.add_argument('--floor_layout', type=str, default='auto',
                   choices=['auto', 'chevron_strip'],
                   help='Floor tile layout. auto = mode-dependent (legacy/lane tiles). '
                        'chevron_strip = single >>>-arrow strip down the centre. Default auto.')
    p.add_argument('--floor_bg_color', type=str, default=None, metavar='#RRGGBB',
                   help='Solid background color for the runway trapezoid (drawn under '
                        'tiles/chevron). None = transparent / canvas black (default).')
    p.add_argument('--floor_bg_opacity', type=float, default=1.0, metavar='0..1',
                   help='Opacity of the floor background trapezoid (0=transparent, 1=solid). Default 1.0.')
    p.add_argument('--background_type', type=str, default='solid',
                   choices=['solid', 'image', 'video'],
                   help='Segment background type: solid color, image, or video.')
    p.add_argument('--background_color', type=str, default='#000000', metavar='#RRGGBB',
                   help='Background color when --background_type solid.')
    p.add_argument('--background_image', type=str, default=None, metavar='PATH',
                   help='Background image path when --background_type image.')
    p.add_argument('--background_video', type=str, default=None, metavar='PATH',
                   help='Background video path when --background_type video.')
    # ── Chevron (used only when --floor_layout chevron_strip) ──────────
    p.add_argument('--chevron_color', type=str, default='#FFD700', metavar='#RRGGBB',
                   help='Chevron arrow fill color. Default #FFD700 (gold).')
    p.add_argument('--chevron_scroll', type=int, default=1, metavar='0|1',
                   help='Scroll chevrons toward camera continuously. Default 1.')
    p.add_argument('--chevron_blink', type=int, default=0, metavar='0|1',
                   help='Blink chevrons on/off every 15 frames (~0.5 s). Default 0.')
    p.add_argument('--chevron_width_frac', type=float, default=0.45,
                   help='Chevron strip width as fraction of lane spread (0.1..1.0). Default 0.45.')
    p.add_argument('--chevron_count', type=int, default=6,
                   help='Number of chevrons visible simultaneously (3..12). Default 6.')
    p.add_argument('--stickman', type=int, default=1, metavar='0|1',
                   help=('Show the left-column stickman fighter. 0 = hide '
                         '(useful when compositing a standalone stickman '
                         'video rendered via stickman.py on top). Default 1.'))
    p.add_argument('--stick_x0', type=int, default=-1, metavar='PX',
                   help=('Stickman draw-box left edge in PIXELS. -1 = '
                         'auto (left-column HUD: ~W*1%%). Use together '
                         'with --stick_y0 / --stick_w / --stick_h to '
                         'place the stickman anywhere in the frame.'))
    p.add_argument('--stick_y0', type=int, default=-1, metavar='PX',
                   help=('Stickman draw-box top edge in PIXELS. -1 = '
                         'auto (~H*9%%).'))
    p.add_argument('--stick_w',  type=int, default=-1, metavar='PX',
                   help=('Stickman draw-box width in PIXELS. -1 = '
                         'auto (~W*13.5%%). Pose dimensions auto-scale '
                         'to fit; the body envelope keeps its '
                         '_REF_W:_REF_H ≈ 260:340 aspect ratio so very '
                         'wide boxes leave horizontal padding rather '
                         'than stretching the stickman.'))
    p.add_argument('--stick_h',  type=int, default=-1, metavar='PX',
                   help=('Stickman draw-box height in PIXELS. -1 = '
                         'auto (~H*54%%).'))
    # ── Side rails ─────────────────────────────────────────────────────
    p.add_argument('--far_spread_frac', type=float, default=None,
                   help='Wall spread at far/horizon end (0.05-0.90). '
                        'None = same as near (standard perspective).')
    p.add_argument('--wall_floor_gap_frac', type=float, default=None,
                   help='Vertical gap between near-wall bottom and floor (0.0-0.30). '
                        'None = wall sits exactly on floor.')
    p.add_argument('--floor_hit_frac', type=float, default=None,
                   help='Fraction of frame height where floor meets near-camera edge (0.70-0.95). '
                        'None = use per-mode default (0.86).')
    p.add_argument('--horizon_frac', type=float, default=None,
                   help='Fraction of frame height for the vanishing point / horizon (0.30-0.60). '
                        'None = use default (0.45).')
    p.add_argument('--floor_spread_frac', type=float, default=None,
                   help='Fraction of frame width for the runway half-spread (0.30-0.85). '
                        'None = use per-mode preset.')
    p.add_argument('--side_rails', type=int, default=0, metavar='0|1',
                   help='Draw decorative neon barriers along both sides of the runway. Default 0.')
    p.add_argument('--rail_color', type=str, default='#FF60FF',
                   help='Hex color for side-rail neon (e.g. "#FF60FF"). Default magenta.')
    p.add_argument('--rail_shape', type=str, default='chunky',
                  choices=['chunky', 'tube', 'chevron', 'pillar', 'dot'],
                  help='Rail style: chunky=fence, tube=strip, chevron=arrows, pillar=LED-chase, dot=glowing dots.')
    p.add_argument('--rail_height', type=float, default=0.14,
                   help='Box height (world units). Default 0.14.')
    p.add_argument('--rail_offset_x', type=float, default=0.08,
                   help='Gap from outer lane tile edge to fence face (world units). Default 0.03.')
    p.add_argument('--rail_image', type=str, default=None,
                   help='Optional PNG/JPG to texture rail blocks. None = solid color.')
    p.add_argument('--rail_texture_non_loop', type=int, default=0, metavar='0|1',
                   help='Tube+texture only: 1 = map texture once across full rail length (no tiling). Default 0.')
    p.add_argument('--rail_pulse', type=str, default='beat',
                   choices=['none', 'beat', 'rms'],
                   help='Audio-reactive pulse mode for rails. Default beat.')
    p.add_argument('--rail_pulse_intensity', type=float, default=0.6, metavar='0..1',
                   help='Pulse intensity 0=static, 1=full blink. Default 0.6.')
    p.add_argument('--rail_pillar_count', type=int, default=16, metavar='N',
                   help='Number of pillars in pillar shape (4..32). Default 16.')
    p.add_argument('--rail_pillar_radius', type=float, default=1.0, metavar='MULT',
                   help='Pillar circumference scale (0.2..2.0). <1 smaller pillars, >1 thicker.')
    p.add_argument('--rail_chase_mode', type=str, default='time',
                   choices=['time', 'beat'],
                   help='Chase advance trigger: time=constant interval (frames), beat=on each beat hit. Default time.')
    p.add_argument('--rail_chase_speed_frames', type=int, default=4, metavar='N',
                   help='Frames between chase advances (only for chase_mode=time). Default 4.')
    p.add_argument('--rail_dot_count', type=int, default=24, metavar='N',
                   help='Number of dots per rail (8..64). Default 24.')
    p.add_argument('--rail_dot_lines', type=int, default=1, metavar='N',
                   help='Number of vertical dot lines on each wall (top..bottom, 1..8). Default 1.')
    p.add_argument('--rail_dot_size_px', type=int, default=6, metavar='PX',
                   help='Base dot radius in pixels at Z_NEAR (2..20). Default 6.')
    p.add_argument('--rail_dot_anim_mode', type=str, default='audio',
                   choices=['audio', 'twinkle', 'wave'],
                   help='Dot animation: audio=brightness from bass, twinkle=random fade, wave=sin wave. Default audio.')
    p.add_argument('--rail_dot_color_near', type=str, default='#FF60FF', metavar='#RRGGBB',
                   help='Color of dots closest to camera. Default magenta.')
    p.add_argument('--rail_dot_color_far', type=str, default='#00FFFF', metavar='#RRGGBB',
                   help='Color of dots at vanishing point. Default cyan.')
    p.add_argument('--rail_chevron_depth', type=float, default=1.0, metavar='MULT',
                   help='Chevron pointedness multiplier (shape=chevron only). '
                        '1.0 = 120° opening angle; >1 = more pointed; <1 = flatter. Default 1.0.')
    p.add_argument('--rail_chevron_density', type=int, default=6, metavar='N',
                   help='Number of chevrons visible on each side wall (2-20). Default 6.')
    p.add_argument('--travel',  type=int, default=-1,
                   help=('Frames for target to fly from spawn to hit. '
                         'Default = -1 (auto: matches one L↔R beat cycle so '
                         'blocks never overlap on same lane). '
                         f'Manual example: --travel {TARGET_TRAVEL_FRAMES}'))
    # --- beat detection knobs ---
    p.add_argument('--beat_source', type=str, default='tempo',
                   choices=['tempo', 'beat', 'onset', 'array'],
                   help=('"tempo" = perfectly uniform cadence from BPM '
                         '— blocks flow at constant rate, always pop on beat '
                         '(recommended, matches reference video). '
                         '"beat" = each librosa beat (may jitter). '
                         '"onset" = every transient. '
                         '"array" = caller-supplied hit times (see '
                         '--beat_times / --beats_file); skips audio analysis '
                         'and ignores --density. Default: tempo'))
    p.add_argument('--beat_times', type=str, default=None, metavar='LIST',
                   help=('Comma-separated hit times in seconds, e.g. '
                         '"1.20,1.85,2.40,3.05". Used only when '
                         '--beat_source=array.  Either this or --beats_file '
                         'is REQUIRED in that mode; supplying both is an '
                         'error.  Out-of-range entries (<0 or >=duration) '
                         'are dropped.  Sorted + deduplicated automatically.'))
    p.add_argument('--beats_file', type=str, default=None, metavar='PATH',
                   help=('JSON file containing a flat array of hit times in '
                         'seconds, e.g. [1.20, 1.85, 2.40].  Used only when '
                         '--beat_source=array; alternative to --beat_times '
                         'when the list is too long for the command line.'))
    p.add_argument('--bpm', type=float, default=None,
                   help=('Force BPM in "tempo" mode instead of auto-detection '
                         '(useful when librosa detects half / double the true '
                         'tempo). Example: --bpm 120'))
    p.add_argument('--beat_sens', type=float, default=0.5, metavar='0..1',
                   help=('Beat sensitivity 0..1. Higher = more beats kept. '
                         'Only used in "beat" / "onset" modes. Default 0.5'))
    p.add_argument('--beat_subdiv', type=int, default=1, choices=[1, 2, 4, 8],
                   help=('Blocks per beat. 1 = one per beat, 2 = eighths, '
                         '4 = sixteenths. Default 1.'))
    p.add_argument('--beat_min_gap', type=int, default=4,
                   help=('Minimum frames between consecutive targets '
                         '(to avoid visual overlap). Default 4.'))
    p.add_argument('--beat_height_threshold', type=float, default=0.0,
                   metavar='0..1',
                   help=('Drop events whose audio amplitude (0..1, '
                         'normalised against the loudest column in the '
                         'song) falls below this threshold. Useful for '
                         'silencing weak ticks driven by the rhythm '
                         'studio threshold slider. 0 keeps every event '
                         '(default), 1 keeps only the single loudest.'))
    p.add_argument('--density', type=float, default=None,
                   help=('Overall block density multiplier. '
                         '0.5 = half as many blocks (sparser), '
                         '2.0 = double (denser), '
                         '1.0 = as detected. '
                         'Default depends on --mode: 1.0 for punch, '
                         '0.5 for dance (matches the CapCut reference '
                         'which spawns one tile per 2 beats).'))
    p.add_argument('--max_per_lane', type=int, default=3,
                   help=('Hard cap on blocks visible on each lane. '
                         'Keeps the track readable when --density is high '
                         'and --speed is low. Default 3.'))
    p.add_argument('--speed', type=float, default=1.0,
                   help=('Block movement speed (only applies with auto '
                         'travel, i.e. --travel -1). '
                         '1.0 = default (1 block per lane at a time). '
                         '0.5 = 2x slower — blocks travel for 2 beat cycles '
                         '(2 staggered blocks per lane, tunnel feel). '
                         '0.33 = 3x slower (3 visible), etc. '
                         '2.0 = 2x faster (half cycle). Default 1.0'))
    # --- custom cube textures ---
    p.add_argument('--cube_image', type=str, default=None,
                   help='Optional image (png/jpg) wrapped onto the 3D cube '
                        'faces. Applies to both left and right cubes unless '
                        '--cube_image_left / --cube_image_right are also '
                        'given. When not set, uses default coloured cube + '
                        'fist icon.')
    p.add_argument('--cube_image_left', type=str, default=None,
                   help='Cube texture for LEFT (green-side) targets only.')
    p.add_argument('--cube_image_right', type=str, default=None,
                   help='Cube texture for RIGHT (red-side) targets only.')
    # --- full 3-D mesh (.obj/.glb/.stl/.ply) ---
    p.add_argument('--cube_model', type=str, default=None,
                   help=('Path to a 3-D model file (.obj/.glb/.gltf/.stl/.ply) '
                         'that replaces the cube entirely. Rendered with '
                         'Lambert shading + depth-sort (no GPU needed). '
                         'Model is auto-normalized to fit CUBE_HALF size. '
                         'Overrides --cube_image. Requires: pip install trimesh'))
    p.add_argument('--cube_model_left', type=str, default=None,
                   help='3-D model for LEFT targets only.')
    p.add_argument('--cube_model_right', type=str, default=None,
                   help='3-D model for RIGHT targets only.')
    p.add_argument('--mesh_wireframe', action='store_true',
                   help='Draw white wireframe edges on the mesh faces.')
    # --- color customization ---
    p.add_argument('--cube_color_left', type=str, default=None,
                   metavar='COLOR',
                   help=('Color of LEFT-lane punch cubes. Accepts '
                         '"#RRGGBB", "RRGGBB", or "R,G,B" (0-255). '
                         'Default: green (50,230,80).'))
    p.add_argument('--cube_color_right', type=str, default=None,
                   metavar='COLOR',
                   help=('Color of RIGHT-lane punch cubes. Accepts '
                         '"#RRGGBB", "RRGGBB", or "R,G,B" (0-255). '
                         'Default: red (240,60,40).'))
    p.add_argument('--panel_color', type=str, default=None,
                   metavar='COLOR',
                   help=('Neon color of the 4 viewport panels on the ground '
                         '(only visible while they flash on each punch). '
                         'Accepts "#RRGGBB", "RRGGBB", or "R,G,B". '
                         'Default: amber (90,170,255).'))
    p.add_argument('--mode', type=str, default='punch',
                   help=('Gameplay mode. '
                         '"punch" (default) = air cubes flying at chest '
                         'height, stickman punches them. '
                         '"dance" = flat slabs sliding along the floor, '
                         'stickman stomps on them. '
                         '"line" = elongated rail-style holds with '
                         'shrinking chain segments. '
                         '"relax" = dodge-style: full-tunnel pink '
                         'obstacles arrive from far — a LOW ground '
                         'slab triggers a JUMP (camera + stickman rise); '
                         'a HIGH floating bar triggers a SQUAT '
                         '(camera + stickman dip).  Kind is picked '
                         'randomly per beat. '
                         'COMBO: pass a comma-list like '
                         '"punch,dance" (or "dance,punch,relax") to '
                         'alternate per beat — beat 0 spawns the first '
                         'mode, beat 1 the second, and so on.  Stickman '
                         'switches to a unified "combo" action that '
                         'covers all beat types.'))
    p.add_argument('--dance_pair_cycle', type=int, default=4, metavar='N',
                   help=('Dance paired-spawn cycle length (active when '
                         'the enabled lane set has ≥ 1 same-side '
                         'adjacent pair + dance/combo mode).  N-1 đơn '
                         '+ 1 CHỤM (feet-together jump) per cycle, '
                         'double on the LAST beat of each cycle.  '
                         'N=4 (default) ≈ one chụm per 4/4 bar — most '
                         'musical. N=3 = legacy 2 đơn + 1 chụm (triplet). '
                         'N=2 = heavy alternating. N=1 = every dance '
                         'beat is chụm. N≤0 = disable dance-pairing.'))
    p.add_argument('--punch_pair_cycle', type=int, default=4, metavar='N',
                   help=('Punch paired-spawn cycle length (same model '
                         'as --dance_pair_cycle but for punch beats).  '
                         'Active when enabled lane set has ≥ 1 same-'
                         'side adjacent pair + punch/combo mode.  N-1 '
                         'đấm đơn + 1 đấm 2-tay (stickman throws both '
                         'hands) per cycle, double on the LAST beat.  '
                         'N=4 (default) ≈ one double-punch per 4/4 bar. '
                         'N=1 = every punch beat is double (legacy '
                         '"strict" behaviour when --lanes picks 2 adj '
                         'lanes).  N≤0 = disable punch-pairing.'))
    p.add_argument('--line_beats', type=int, default=2, metavar='N',
                   help=('Hold-note length, in BEATS, for the "line" '
                         'mode.  Each line target is an elongated rail-'
                         'style punch cube; the stickman keeps the '
                         'punch pose extended for ~N beat intervals '
                         'after the hit frame while the bar slides '
                         'past the camera.  Default 2.  Larger = '
                         'longer holds (and longer bar visuals).  '
                         'Only meaningful when "line" is present in '
                         '--mode.'))
    p.add_argument('--line_debug', type=int, default=0, metavar='0|1',
                   help=('Draw line-mode timing debug overlay (block markers '
                         'on a top timeline + current frame indicator). '
                         'Useful for waveform sync checks. Default 0.'))
    p.add_argument('--line_zigzag', type=str, default='vertical',
                   choices=['vertical', 'horizontal'],
                   help=('Line-mode zigzag axis.  "vertical" (default, '
                         'legacy) = each block of a chain tilts up/down '
                         'while staying on a single lane — the chain is '
                         'a saw-tooth rail on one lane.  "horizontal" = '
                         'blocks span the FULL tunnel width (lane 1 '
                         'to lane 4) and alternate direction: block 1 '
                         'goes lane1->lane4, block 2 lane4->lane1, '
                         'block 3 lane1->lane4, etc.  The head (front '
                         'face) and tail (back face) of each block '
                         'always sit on the two outer lanes, producing '
                         'a wide horizontal zig-zag across the tunnel.'))
    p.add_argument('--relax_interval', type=float, default=0.0,
                   metavar='SEC',
                   help=('Solo-relax cadence mode. '
                         '0.0 (default) = "theo nhạc": obstacles spawn '
                         'on the detected audio beats (wave-columns / '
                         'onsets), same as every other mode. '
                         '>0.0 = "theo thời gian": idle DELAY between '
                         'the moment the previous block has fully '
                         'disappeared and the moment the next block '
                         'appears at the horizon.  Guarantees only '
                         'ONE block is visible on screen at a time '
                         '(user: "khối phía trước phải khuất hẵn sau '
                         'bao nhiêu s thì mới bắt đầu xuất hiện khối '
                         'tiếp theo").  Only active when --mode is '
                         'solo "relax"; combo modes that include '
                         'relax still follow audio beats so inter-'
                         'mode alternation stays coherent.  Typical '
                         'values: 0.5–2.0 for calm, breathing '
                         'gameplay.'))
    p.add_argument('--relax_travel_sec', type=float, default=0.0,
                   help=('Relax block travel time in seconds. '
                         '>0 overrides auto travel only for solo relax.'))
    p.add_argument('--relax_wait_sec', type=float, default=0.0,
                  help=('Relax block hold time in seconds before movement. '
                        'Block appears at horizon, waits this long, then '
                        'moves with the normal travel duration.'))
    p.add_argument('--relax_texture_low', type=str, default=None, metavar='PATH',
                   help='Texture image for LOW relax block front face.')
    p.add_argument('--relax_texture_high', type=str, default=None, metavar='PATH',
                   help='Texture image for HIGH relax block front face.')
    p.add_argument('--relax_texture_middle', type=str, default=None, metavar='PATH',
                   help='Texture image for MIDDLE relax block face.')
    p.add_argument('--relax_hole_mask_path', type=str, default=None, metavar='PATH',
                   help='PNG alpha mask for MIDDLE hole (alpha=0 = hole).')
    p.add_argument('--relax_kind_ratio_middle', type=float, default=0.33,
                   help='Spawn ratio for middle relax block kind (0..1).')
    p.add_argument('--relax_show_low', type=int, default=1, metavar='0|1',
                   help='Enable low relax blocks.')
    p.add_argument('--relax_show_high', type=int, default=1, metavar='0|1',
                   help='Enable high relax blocks.')
    p.add_argument('--relax_show_middle', type=int, default=1, metavar='0|1',
                   help='Enable middle relax blocks.')
    p.add_argument('--relax_countdown_enabled', type=int, default=1, metavar='0|1',
                   help='Show relax countdown HUD in top-right.')
    p.add_argument('--relax_countdown_color', type=str, default='#FFFFFF',
                   metavar='#RRGGBB',
                   help='Relax countdown text color.')
    p.add_argument('--relax_countdown_max_sec', type=float, default=5.0,
                   help='Only show countdown if next hit <= this many seconds.')
    p.add_argument('--relax_countdown_anim', type=str, default='pop',
                   choices=('pop', 'flash', 'fade_cross', 'shake'),
                   help='Countdown number transition effect.')
    p.add_argument('--relax_countdown_audio_enabled', type=int, default=0, metavar='0|1',
                   help='Enable per-count countdown tick sounds.')
    p.add_argument('--relax_countdown_audio_mode', type=str, default='default',
                   choices=('default', 'file'),
                   help='Regular count sound source.')
    p.add_argument('--relax_countdown_audio_file', type=str, default=None, metavar='PATH',
                   help='Audio file for regular count ticks (when mode=file).')
    p.add_argument('--relax_countdown_audio_volume', type=float, default=0.65,
                   help='Countdown sound mix volume (0..1).')
    p.add_argument('--relax_countdown_audio_last_mode', type=str, default='default',
                   choices=('default', 'file', 'same'),
                   help='Last-count sound source (1 -> hit).')
    p.add_argument('--relax_countdown_audio_last_file', type=str, default=None, metavar='PATH',
                   help='Audio file for last-count tick (when last_mode=file).')
    p.add_argument('--relax_countdown_x', type=float, default=0.88,
                   help='Countdown box x (0..1, normalized).')
    p.add_argument('--relax_countdown_y', type=float, default=0.04,
                   help='Countdown box y (0..1, normalized).')
    p.add_argument('--relax_countdown_w', type=float, default=0.10,
                   help='Countdown box width (0..1, normalized).')
    p.add_argument('--relax_countdown_h', type=float, default=0.16,
                   help='Countdown box height (0..1, normalized).')
    p.add_argument('--lanes', type=str, default=None, metavar='SPEC',
                   help=('Restrict target spawns to the listed 1-based '
                         'lanes.  Accepts a comma-separated list '
                         '("1,2" = inner-left + inner-right side, '
                         '"1,4" = outer lanes only, '
                         '"1,2,3,4" = all four = default), a range '
                         '("1-3" = lanes 1..3), or "all" / omit for no '
                         'filter.  Invalid values abort with an error.  '
                         'Walls always span the full tunnel regardless '
                         'of this filter.'))
    p.add_argument('--export_events', type=str, default=None, metavar='PATH',
                   help=('Save the scheduled stickman event timeline to a '
                         'JSON file so stickman.py --events_file can render '
                         'a standalone stickman video that syncs perfectly '
                         'with this rhythm video.'))
    p.add_argument('--detect_only', action='store_true',
                   help=('Run audio analysis + beat scheduling (writing '
                         '--export_events JSON) then exit BEFORE the '
                         'frame-render loop.  Used by Studio to preview '
                         'block timing without rendering the video.'))
    p.add_argument('-t', '--token', type=str, default=None)
    p.add_argument('-u', '--url',   type=str, default=None)
    return p.parse_args()


if __name__ == '__main__':
    args = parse_arguments()
    viz = RhythmVisualizer()
    viz.WIDTH         = args.width
    viz.HEIGHT        = args.height
    viz.FPS           = args.fps
    PerspectiveCamera.DEPTH_MODE = args.depth_mode
    PunchTarget.CORNER_RADIUS = max(0.0, min(0.45, args.cube_radius))
    viz.TIME_LIMIT    = args.duration
    viz.BLOOM         = bool(args.bloom)
    viz.TRAVEL_FRAMES = args.travel
    viz.BEAT_SOURCE   = args.beat_source
    viz.BEAT_BPM      = args.bpm
    viz.BEAT_SENS     = args.beat_sens
    viz.BEAT_SUBDIV   = args.beat_subdiv
    viz.BEAT_MIN_GAP  = args.beat_min_gap
    viz.BEAT_HEIGHT_THRESHOLD = max(0.0, min(1.0, float(
        args.beat_height_threshold or 0.0
    )))
    viz.BEAT_TIMES    = _parse_beat_times(args)
    # Per-mode density default: punch = every beat (1.0), dance = every
    # other beat (0.5, matches the CapCut reference's sparser stomp
    # cadence — one tile per 2 beats so only 1–2 tiles visible at a time).
    # An explicit --density overrides this auto-pick.
    if args.density is None:
        try:
            modes_norm = _parse_modes(args.mode)
        except ValueError:
            modes_norm = ['punch']
        if len(modes_norm) >= 2:
            # Combo (e.g. punch+dance): each sub-mode lands on HALF the
            # beats, so per-type cadence already halves naturally → full
            # 1.0 density = every beat populated, alternating type.
            density_default = 1.0
        elif modes_norm[0] == 'dance':
            # Dance solo matches the CapCut reference's sparser stomp
            # cadence — one tile per 2 beats so only 1–2 tiles visible
            # at a time.
            density_default = 0.5
        else:
            density_default = 1.0
        viz.BEAT_DENSITY = density_default
        print(f"[density:auto] mode={','.join(modes_norm)} → "
              f"density={density_default}")
    else:
        viz.BEAT_DENSITY = float(args.density)
    viz.BLOCK_SPEED   = args.speed
    viz.MAX_PER_LANE  = args.max_per_lane
    viz.CUBE_IMAGE        = args.cube_image
    viz.CUBE_IMAGE_LEFT   = args.cube_image_left
    viz.CUBE_IMAGE_RIGHT  = args.cube_image_right
    viz.CUBE_MODEL        = args.cube_model
    viz.CUBE_MODEL_LEFT   = args.cube_model_left
    viz.CUBE_MODEL_RIGHT  = args.cube_model_right
    viz.MESH_WIREFRAME    = args.mesh_wireframe
    viz.SHOW_FLOOR_PANELS  = bool(args.floor_panels)
    viz.FLOOR_PANEL_COLOR  = args.floor_panel_color or None
    viz.FLOOR_PANEL_OPACITY = float(args.floor_panel_opacity)
    viz.FLOOR_PANEL_BLINK  = bool(args.floor_panel_blink)
    viz.FLOOR_PANEL_IMAGE  = args.floor_panel_image or None
    viz.FLOOR_FULL_STATIC_IMAGE = bool(int(args.floor_full_static_image))
    viz.FLOOR_LAYOUT         = args.floor_layout
    viz.FLOOR_BG_COLOR       = args.floor_bg_color or None
    viz.FLOOR_BG_OPACITY     = float(args.floor_bg_opacity)
    viz.BACKGROUND_TYPE      = str(args.background_type or "solid")
    viz.BACKGROUND_COLOR     = str(args.background_color or "#000000")
    viz.BACKGROUND_IMAGE     = args.background_image or None
    viz.BACKGROUND_VIDEO     = args.background_video or None
    viz.CHEVRON_COLOR        = args.chevron_color
    viz.CHEVRON_SCROLL       = bool(int(args.chevron_scroll))
    viz.CHEVRON_BLINK        = bool(int(args.chevron_blink))
    viz.CHEVRON_WIDTH_FRAC   = float(args.chevron_width_frac)
    viz.CHEVRON_COUNT        = int(args.chevron_count)
    viz.FAR_SPREAD_FRAC        = args.far_spread_frac        # None or float
    viz.WALL_FLOOR_GAP_FRAC    = args.wall_floor_gap_frac   # None or float
    viz.FLOOR_HIT_FRAC         = args.floor_hit_frac        # None or float
    viz.HORIZON_FRAC           = args.horizon_frac        # None or float
    viz.FLOOR_SPREAD_FRAC      = args.floor_spread_frac   # None or float
    viz.SHOW_SIDE_RAILS        = bool(args.side_rails)
    viz.RAIL_COLOR             = str(args.rail_color)
    viz.RAIL_SHAPE             = str(args.rail_shape)
    viz.RAIL_HEIGHT            = float(args.rail_height)
    viz.RAIL_OFFSET_X          = float(args.rail_offset_x)
    viz.RAIL_IMAGE             = args.rail_image or None
    viz.RAIL_TEXTURE_NON_LOOP  = bool(int(args.rail_texture_non_loop))
    viz.RAIL_PULSE             = str(args.rail_pulse)
    viz.RAIL_PULSE_INTENSITY   = float(args.rail_pulse_intensity)
    viz.RAIL_PILLAR_COUNT      = int(args.rail_pillar_count)
    viz.RAIL_PILLAR_RADIUS     = float(args.rail_pillar_radius)
    viz.RAIL_CHASE_MODE        = str(args.rail_chase_mode)
    viz.RAIL_CHASE_SPEED_FRAMES = int(args.rail_chase_speed_frames)
    viz.RAIL_DOT_COUNT         = int(args.rail_dot_count)
    viz.RAIL_DOT_LINES         = int(args.rail_dot_lines)
    viz.RAIL_DOT_SIZE_PX       = int(args.rail_dot_size_px)
    viz.RAIL_DOT_ANIM_MODE     = str(args.rail_dot_anim_mode)
    viz.RAIL_DOT_COLOR_NEAR    = str(args.rail_dot_color_near)
    viz.RAIL_DOT_COLOR_FAR     = str(args.rail_dot_color_far)
    viz.RAIL_CHEVRON_DEPTH     = float(args.rail_chevron_depth)
    viz.RAIL_CHEVRON_DENSITY   = int(args.rail_chevron_density)
    viz.SHOW_STICKMAN      = bool(args.stickman)
    viz.STICK_X0          = int(args.stick_x0)
    viz.STICK_Y0          = int(args.stick_y0)
    viz.STICK_W           = int(args.stick_w)
    viz.STICK_H           = int(args.stick_h)
    try:
        viz.CUBE_COLOR_LEFT   = _parse_color(args.cube_color_left)
        viz.CUBE_COLOR_RIGHT  = _parse_color(args.cube_color_right)
        viz.PANEL_NEON_COLOR  = _parse_color(args.panel_color)
    except ValueError as e:
        print(f"[color] {e}")
        sys.exit(1)
    viz.EXPORT_EVENTS = args.export_events
    viz.DETECT_ONLY   = bool(args.detect_only)
    # Validate --mode early so bad spellings (e.g. "pouch,dance") surface
    # at CLI parse time instead of halfway through rendering.
    try:
        _parse_modes(args.mode)
    except ValueError as e:
        print(f"[--mode] {e}")
        sys.exit(1)
    viz.MODE          = args.mode
    viz.DANCE_PAIR_CYCLE = int(args.dance_pair_cycle)
    viz.PUNCH_PAIR_CYCLE = int(args.punch_pair_cycle)
    viz.LINE_BEATS       = max(1, int(args.line_beats))
    viz.LINE_DEBUG       = bool(args.line_debug)
    viz.LINE_ZIGZAG      = str(args.line_zigzag).lower().strip() or 'vertical'
    viz.RELAX_INTERVAL   = max(0.0, float(args.relax_interval))
    viz.RELAX_TRAVEL_SEC = max(0.0, float(args.relax_travel_sec))
    viz.RELAX_WAIT_SEC   = max(0.0, float(args.relax_wait_sec))
    viz.RELAX_TEXTURE_LOW = args.relax_texture_low or None
    viz.RELAX_TEXTURE_HIGH = args.relax_texture_high or None
    viz.RELAX_TEXTURE_MIDDLE = args.relax_texture_middle or None
    viz.RELAX_HOLE_MASK_PATH = args.relax_hole_mask_path or None
    viz.RELAX_KIND_RATIO_MIDDLE = max(
        0.0, min(1.0, float(args.relax_kind_ratio_middle))
    )
    viz.RELAX_SHOW_LOW = bool(int(args.relax_show_low))
    viz.RELAX_SHOW_HIGH = bool(int(args.relax_show_high))
    viz.RELAX_SHOW_MIDDLE = bool(int(args.relax_show_middle))
    viz.RELAX_COUNTDOWN_ENABLED = bool(int(args.relax_countdown_enabled))
    viz.RELAX_COUNTDOWN_COLOR = str(args.relax_countdown_color)
    viz.RELAX_COUNTDOWN_MAX_SEC = max(0.0, float(args.relax_countdown_max_sec))
    viz.RELAX_COUNTDOWN_ANIM = CountdownHUD._normalize_anim(args.relax_countdown_anim)
    viz.RELAX_COUNTDOWN_AUDIO_ENABLED = bool(int(args.relax_countdown_audio_enabled))
    viz.RELAX_COUNTDOWN_AUDIO_MODE = str(args.relax_countdown_audio_mode or "default")
    viz.RELAX_COUNTDOWN_AUDIO_FILE = args.relax_countdown_audio_file or None
    viz.RELAX_COUNTDOWN_AUDIO_VOLUME = max(
        0.0, min(1.0, float(args.relax_countdown_audio_volume))
    )
    viz.RELAX_COUNTDOWN_AUDIO_LAST_MODE = str(
        args.relax_countdown_audio_last_mode or "default"
    )
    viz.RELAX_COUNTDOWN_AUDIO_LAST_FILE = args.relax_countdown_audio_last_file or None
    viz.RELAX_COUNTDOWN_X = max(0.0, min(1.0, float(args.relax_countdown_x)))
    viz.RELAX_COUNTDOWN_Y = max(0.0, min(1.0, float(args.relax_countdown_y)))
    viz.RELAX_COUNTDOWN_W = max(0.02, min(1.0, float(args.relax_countdown_w)))
    viz.RELAX_COUNTDOWN_H = max(0.02, min(1.0, float(args.relax_countdown_h)))
    # Lane filter is always evaluated against the 4-lane layout both modes
    # now use (see N_LANES / N_LANES_DANCE).  --lanes uses 1-based indices.
    try:
        viz.LANE_FILTER = _parse_lanes(args.lanes, n_lanes=N_LANES_DANCE)
    except ValueError as e:
        print(f"[--lanes] {e}")
        sys.exit(1)

    import os
    if not args.detect_only:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    temp = viz.process_video(args.input)
    if args.detect_only:
        # Detection-only mode: events JSON has been written by --export_events
        # (if supplied) and no video was rendered.  Nothing more to do.
        pass
    elif temp:
        out_path = args.output if args.output.endswith('.mp4') else args.output + '.mp4'
        if args.audio:
            viz.merge_audio(temp, args.input, out_path)
        else:
            import shutil
            shutil.move(temp, out_path)
            print(f"Video saved to: {out_path}")
