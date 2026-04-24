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
import ffmpeg
from authorization import authourize_user

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
    valid = {'punch', 'dance', 'line'}
    bad = [p for p in parts if p not in valid]
    if bad:
        raise ValueError(
            f"Unknown mode(s) {bad}; allowed: 'punch', 'dance', 'line', "
            f"or comma-combined e.g. 'punch,dance,line'.")
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
TARGET_TRAVEL_FRAMES = 40    # how many frames a target takes to cross from z=1 → z=0
SPAWN_COOLDOWN       = 3
ONSET_SPAWN_THRESH   = 0.35
WALL_SPAWN_PROB      = 0.12
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

    # ---------- 3D projection ----------
    def project(self, wx: float, wy: float, wz: float):
        """Project world point → (sx, sy, depth_scale). None if behind cam."""
        if wz <= 0.05:
            return None
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


# ── TunnelRenderer ────────────────────────────────────────────────────────────
class TunnelRenderer:
    """Draws the receding 3D tunnel: floor grid + side walls with neon strips."""

    def __init__(self, cam: PerspectiveCamera, show_floor_panels: bool = True,
                 lane_tiles: bool = False):
        """
        `lane_tiles`: when True, floor panels are drawn directly UNDER each
        lane (one column of tiles per lane, derived from `cam.lane_world_x`).
        Used by dance mode so the 4 lanes have visible "runway" tiles.
        When False (default), uses the original 2 columns flanking the
        center — matches the original punch-mode look.
        """
        self.cam = cam
        self.show_floor_panels = show_floor_panels
        self.lane_tiles = lane_tiles

    def draw(self, canvas: np.ndarray, frame: int) -> np.ndarray:
        """Dark 3D tunnel with floor panels receding toward the horizon.

        Uses the camera's 3D projection so floor tiles are true trapezoid
        perspective (not hand-faked), matching the CapCut reference.
        """
        cam = self.cam

        if self.show_floor_panels:
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

            # Pass 1: dark grey fill for each tile (ground-panel feel).
            for _, poly, df, _ in floor_polys:
                base = int(45 * df)
                cv2.fillPoly(canvas, [poly], (base, base + 2, base + 2),
                             lineType=cv2.LINE_AA)

            # Pass 2: rim outline.  In `lane_tiles` mode we want the
            # runway tiles to read as a QUIET grid so the bright DanceTarget
            # stomp-pads pop on top; in legacy (punch) mode we keep the
            # original neon glow that gives the tunnel its energetic feel.
            if self.lane_tiles:
                for _, poly, df, _ in floor_polys:
                    c = int(60 + 60 * df)             # dim grey, 60..120
                    cv2.polylines(canvas, [poly], True, (c, c, c), 1,
                                  lineType=cv2.LINE_AA)
            else:
                for _, poly, df, _ in floor_polys:
                    glow = floor_glow_color * (0.40 + 0.60 * df)
                    thickness = max(1, int(round(1 + df * 1.2)))
                    _draw_neon_edges(canvas, [poly], glow, thickness)

        # -- Faint horizon / runway glow line (ambient neon) --
        y_hz = int(cam.cy_pix + 2)
        cv2.line(canvas, (0, y_hz), (cam.W, y_hz), (70, 60, 80), 1,
                 lineType=cv2.LINE_AA)

        # -- Dark wall edge hints (very subtle perspective lines) --
        for side in (-1, +1):
            pts = []
            for z_n in [0, 0.35, 0.65, 0.85, 0.95]:
                pts.append((int(cam.wall_x(side, z_n)),
                            int(cam.floor_y(z_n))))
            for a, b in zip(pts, pts[1:]):
                cv2.line(canvas, a, b, (55, 45, 35), 1, lineType=cv2.LINE_AA)

        return canvas

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
    core_col = tuple(int(min(255.0, c + 170)) for c in base_color)
    wide = max(3, core_thick * 4)

    overlay = np.zeros_like(crop)
    for poly in polys:
        shifted = poly - np.array([x0, y0], dtype=poly.dtype)
        cv2.polylines(overlay, [shifted], True, glow_col,
                      wide, lineType=cv2.LINE_AA)
    k = (wide * 2) | 1
    overlay = cv2.GaussianBlur(overlay, (k, k), 0)
    np.maximum(crop, overlay, out=crop)

    for poly in polys:
        cv2.polylines(canvas, [poly], True, core_col,
                      core_thick, lineType=cv2.LINE_AA)


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

    def draw(self, canvas, cam, cur_frame):
        if self.state != 'flying':
            return canvas
        z_norm = self.depth(cur_frame)
        wz = cam.z_from_norm(z_norm)
        wx = cam.lane_world_x(self.lane)
        wy = cam.AIR_WORLD_Y

        mesh = self.MESH_LEFT if self.is_left else self.MESH_RIGHT
        tex  = self.TEXTURE_LEFT if self.is_left else self.TEXTURE_RIGHT

        if mesh is not None:
            # Full 3-D mesh (.obj / .glb / .stl / .ply) with Lambert shading
            draw_mesh_3d(canvas, cam, mesh,
                         (wx, wy, wz),
                         self.CUBE_HALF,
                         base_color=self.color,
                         rim=CLR_WHITE if self.MESH_WIREFRAME else None)
        elif tex is not None:
            draw_cube_3d_textured(canvas, cam,
                                  (wx, wy, wz),
                                  self.CUBE_HALF,
                                  tex[0], tex[1],
                                  rim=CLR_WHITE)
        else:
            # Yaw cube so its front face rotates TOWARD the center axis:
            # left-lane cubes tilt clockwise (yaw > 0) and right-lane
            # cubes tilt counter-clockwise (yaw < 0).  The two cubes
            # mirror each other and both expose their inner side face +
            # top face → looks like solid geometry instead of a flat
            # square staring into the camera.
            yaw = 0.35 if self.is_left else -0.35
            cube_info = draw_cube_3d(canvas, cam,
                                     (wx, wy, wz),
                                     self.CUBE_HALF,
                                     color=self.color,
                                     yaw=yaw,
                                     corner_radius=self.CORNER_RADIUS)
            if cube_info is not None and cube_info['size'] >= 22:
                _draw_fist_icon(canvas, cube_info['cx'], cube_info['cy'],
                                int(cube_info['size'] * 0.58),
                                CLR_WHITE)
        return canvas


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

    # Frames between consecutive cube CENTRES.  Must be ≥ 1.
    # 6 = 8th-note spacing at ~152 BPM / 30 fps (beat≈12f / 2 subdivisions).
    # With hz = D_z/2 this gives depth = D_z = 2× the original touching depth.
    CHAIN_D: int = 6
    CORNER_RADIUS: float = 0.12
    # World-Z where the head cube centre freezes.
    # Set so front_z = HEAD_Z_CLAMP − hz ≈ Z_NEAR → fills ~20 % of frame.
    HEAD_Z_CLAMP: float = 4.1
    # Legacy stubs — kept so external callers that inspect these
    # attributes do not crash (values are no longer used by draw()).
    LENGTH_MULT: float = 2.8
    THICKNESS_MULT: float = 0.92

    def __init__(self, spawn_frame: int, hit_frame: int, lane: int,
                 is_left: bool, hold_frames: int,
                 line_beats: int = 2):
        super().__init__(spawn_frame, hit_frame, lane, is_left)
        self.hold_frames = max(1, int(hold_frames))
        self.line_beats  = max(1, int(line_beats))
        D = max(1, int(self.CHAIN_D))
        self._D = D
        # N cubes fill the hold window end-to-end.  +1 so the last
        # cube's back face sits exactly at hit_frame + hold_frames.
        self.n_cubes = min(4, max(2, self.hold_frames // D + 1))
        # hit_frame      = frame when the HEAD cube reaches the hit zone
        #                  and FREEZES (stops moving).
        # final_hit_frame= frame when the HEAD cube is punched LAST,
        #                  after all tail cubes have passed through.
        # New hit order: head (i=0) first, then tails sequentially.
        # final_hit_frame = frame when the LAST tail cube clears.
        self.freeze_frame     = self.hit_frame          # kept for compat; unused
        self.final_hit_frame  = self.hit_frame + (self.n_cubes - 1) * D

    # ── lifecycle --------------------------------------------------
    def check_hit(self, cur_frame: int) -> bool:
        """Fire once when the HEAD cube gets its final punch (last in chain)."""
        if self.state != 'flying':
            return False
        if self.hit_exec_f < 0 and cur_frame >= self.final_hit_frame:
            self.hit_exec_f = cur_frame
            return True
        return False

    def is_dead(self, cur_frame: int) -> bool:
        if self.hit_exec_f < 0:
            return False
        # Wait for the last block's shrink animation to finish
        # (lasts D more frames after the final hit).
        if cur_frame > self.final_hit_frame + self._D + 1:
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
        D = self._D

        # Auto-size hz so adjacent cubes appear end-to-end touching.
        #   velocity  = (Z_FAR - Z_NEAR) / travel  [wu/frame]
        #   D_z       = D * velocity                [wu between centres]
        #   hz        = D_z / 2                     [half-extent in Z]
        # This ratio is perspective-invariant: cubes look touching at
        # any depth.
        hx = PunchTarget.CUBE_HALF
        # Depth = 10× half-width (5× the previous 2×).
        hz = hx * 10.0
        # Small yaw (~10°) gives 3D depth cue without making the cube
        # appear disproportionately wide due to the visible side face.
        yaw = 0.18 if self.is_left else -0.18
        wx = cam.lane_world_x(self.lane)
        wy = cam.AIR_WORLD_Y

        freeze_frame    = self.freeze_frame
        final_hit_frame = self.final_hit_frame

        # ── Zigzag chain rendering ────────────────────────────────────────
        # Hit order: head (i=0) first, then tails i=1..n-1 sequentially.
        # Segments alternate Y so adjacent blocks touch at Y = wy:
        #   even i (0,2) → centre at wy + hx  (below horizon, "down")
        #   odd  i (1,3) → centre at wy - hx  (above horizon, "up")
        #
        # Each block has two phases:
        #   • Approaching: normal depth, moving toward camera.
        #   • Shrinking  : front face frozen at hit zone; back face retreats
        #                  toward front face over D frames, then block vanishes.

        # ── Derive hz for exact touching ──────────────────────────────────
        velocity = (cam.Z_FAR - cam.Z_NEAR) / travel   # wu / frame
        D_z      = D * velocity                          # wu between centres
        hz       = D_z * 0.42                            # slight gap between blocks

        def _seg_wy(idx: int) -> float:
            """Y centre for segment idx: even=below-horizon, odd=above."""
            return (wy + hx) if (idx % 2 == 0) else (wy - hx)

        # ── Build segment list ─────────────────────────────────────────────
        # Each block is a TILTED prism:
        #   wy_f = front-face Y centre = _seg_wy(i)
        #   wy_b = back-face  Y centre = _seg_wy(i+1)
        # So the tail of block i (wy_b) == the head of block i+1 (wy_f) →
        # perfect junction, no gap, no overlap.
        #
        # Format: (fz, bz, wy_f, wy_b, is_neon, cube_i, shrink_t)
        segs = []
        neon_i: int | None = None

        for i in range(self.n_cubes):
            wy_f = _seg_wy(i)
            wy_b = _seg_wy(i + 1)          # back Y always = next block's front Y

            eff_hit_i  = self.hit_frame + i * D
            frames_ago = cur_frame - eff_hit_i

            if frames_ago < 0:
                # ── Approaching ──────────────────────────────────────────
                z_norm_i = (-frames_ago) / travel
                if z_norm_i > 1.0:
                    continue
                if neon_i is None:
                    neon_i = i
                wz_i = cam.z_from_norm(max(0.0, min(1.0, z_norm_i)))
                segs.append((
                    max(wz_i - hz, cam.Z_NEAR + 0.01),
                    min(wz_i + hz, cam.Z_FAR  - 0.01),
                    wy_f, wy_b, i == neon_i, i, 0.0
                ))

            elif frames_ago < D:
                # ── Shrinking ─────────────────────────────────────────────
                # Front face frozen at hit zone.
                # Back face retreats in Z AND Y toward the front face.
                shrink_t = frames_ago / D
                fz_fixed = cam.Z_NEAR + 0.01
                bz_now   = fz_fixed + hz * (1.0 - shrink_t)
                if bz_now <= fz_fixed + 0.005:
                    continue
                # Back Y interpolates toward front Y as block collapses.
                wy_b_now = wy_b + (wy_f - wy_b) * shrink_t
                segs.append((fz_fixed, bz_now, wy_f, wy_b_now,
                             False, i, shrink_t))
            # else: fully collapsed, skip

        if not segs:
            return canvas

        segs.sort(key=lambda s: s[0], reverse=True)   # back-to-front

        col     = self.color
        rim_col = tuple(min(255, int(c * 1.8)) for c in col)

        def _pt(x, y, z):
            p = cam.project(x, y, z)
            return (int(p[0]), int(p[1])) if p else None

        for fz, bz, wy_f, wy_b, is_neon, cube_i, shrink_t in segs:
            # Front face corners (centred at wy_f)
            fTL = _pt(wx - hx, wy_f + hx, fz)
            fTR = _pt(wx + hx, wy_f + hx, fz)
            fBR = _pt(wx + hx, wy_f - hx, fz)
            fBL = _pt(wx - hx, wy_f - hx, fz)
            if None in (fTL, fTR, fBR, fBL):
                continue

            # Back face corners (centred at wy_b — different Y for tilt)
            bTL = _pt(wx - hx, wy_b + hx, bz)
            bTR = _pt(wx + hx, wy_b + hx, bz)
            bBR = _pt(wx + hx, wy_b - hx, bz)
            bBL = _pt(wx - hx, wy_b - hx, bz)

            fade = 1.0 - shrink_t * 0.6

            if is_neon:
                s_bright = 0.82 * fade
                f_bright = 1.00 * fade
                edge_col = tuple(int(c * fade) for c in rim_col)
                side_lw  = 2
                front_lw = 3
            else:
                s_bright = 0.18 * fade
                f_bright = 0.22 * fade
                edge_col = tuple(int(c * 0.55 * fade) for c in rim_col)
                side_lw  = 1
                front_lw = 1

            # ── SIDE FACE (tilted — back corners at wy_b) ─────────────────
            if self.is_left:
                sf = (fTR, fBR, bBR, bTR)
            else:
                sf = (fTL, fBL, bBL, bTL)

            if all(p is not None for p in sf):
                side_col = tuple(int(c * s_bright) for c in col)
                cv2.fillPoly(canvas, [np.array(sf, dtype=np.int32)],
                             side_col, lineType=cv2.LINE_AA)

            # ── TOP / BOTTOM connecting face ───────────────────────────────
            # For a tilted block (wy_f ≠ wy_b) the horizontal face that
            # "caps" the slant is visible:
            #   wy_b < wy_f  → block rises toward back  → TOP face visible
            #   wy_b > wy_f  → block falls toward back  → BOTTOM face visible
            horiz_bright = (s_bright + f_bright) * 0.5
            horiz_col    = tuple(int(c * horiz_bright) for c in col)
            if wy_b < wy_f:
                # TOP face: upper edges of front and back faces
                h0 = _pt(wx - hx, wy_f - hx, fz)
                h1 = _pt(wx + hx, wy_f - hx, fz)
                h2 = _pt(wx + hx, wy_b - hx, bz)
                h3 = _pt(wx - hx, wy_b - hx, bz)
            else:
                # BOTTOM face: lower edges of front and back faces
                h0 = _pt(wx - hx, wy_f + hx, fz)
                h1 = _pt(wx + hx, wy_f + hx, fz)
                h2 = _pt(wx + hx, wy_b + hx, bz)
                h3 = _pt(wx - hx, wy_b + hx, bz)
            if all(p is not None for p in (h0, h1, h2, h3)):
                horiz_poly = np.array([h0, h1, h2, h3], dtype=np.int32)
                cv2.fillPoly(canvas, [horiz_poly], horiz_col, cv2.LINE_AA)

            # ── FRONT FACE ────────────────────────────────────────────────
            fill_col   = tuple(int(c * f_bright) for c in col)
            front_poly = np.array([fTL, fTR, fBR, fBL], dtype=np.int32)
            cv2.fillPoly(canvas, [front_poly], fill_col, lineType=cv2.LINE_AA)

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
                     line_beats: int = 2):
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
        # targets from the median inter-beat gap * `line_beats`.  This
        # anchors the bar's sustain to musical tempo rather than absolute
        # time, so it "feels" like a real long-note across any song BPM.
        if len(beat_frames) >= 2 and line_beats > 0:
            _gaps = np.diff(np.asarray(beat_frames, dtype=np.int64))
            _median_gap = max(1, int(np.median(_gaps)))
            line_hold_frames = max(4, int(line_beats * _median_gap))
        else:
            line_hold_frames = max(4, self.travel // 3)

        def _spawn_target(m: str, spawn_f: int, bf: int, lane: int,
                          is_left: bool):
            if m == 'dance':
                return DanceTarget(spawn_f, bf, lane, is_left)
            if m == 'line':
                return LineTarget(spawn_f, bf, lane, is_left,
                                  hold_frames=line_hold_frames,
                                  line_beats=line_beats)
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
            if bf - self.travel < 0:
                skipped_early += 1
                continue
            spawn_f = bf - self.travel

            # target type
            b = float(bass_arr[min(bf, len(bass_arr) - 1)])
            r = self.rng.random()
            if b > 0.60 and r < WALL_SPAWN_PROB:
                # Full-tunnel wall spanning every lane.
                center_lane = (n_lanes - 1) / 2.0
                t = WallTarget(spawn_f, bf, lane=center_lane,
                               is_left=False, span=n_lanes)
                self.targets.append(t)
                last_bf = bf
                emit_idx += 1
                continue

            cur_mode   = _mode_for(emit_idx)
            target_cls = _target_cls_for(cur_mode)

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

            t = _spawn_target(cur_mode, spawn_f, bf, chosen_lane, is_left)
            self.targets.append(t)
            last_spawn_on[chosen_lane] = spawn_f
            if isinstance(t, LineTarget):
                # Block this lane until the bar has fully slid past
                # the camera (hit + hold, plus a 2-frame buffer).
                line_busy_until[chosen_lane] = bf + line_hold_frames + 2
                # Also set the global chain lock so the NEXT line
                # beat (on any lane) only fires after this chain ends.
                line_global_busy_until = bf + line_hold_frames + 2
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
              f"{skipped_stacked} lane-stacked, merged {merged} too-close).")

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
        self.BEAT_SOURCE:   str   = 'tempo'
        self.BEAT_SENS:     float = 0.5      # 0..1  higher = more beats kept
        self.BEAT_SUBDIV:   int   = 1        # 1,2,4 – multiply each beat
        self.BEAT_MIN_GAP:  int   = 4        # frames between targets
        self.BEAT_BPM:      float | None = None  # override tempo detection
        self.BEAT_DENSITY:  float = 1.0      # 0.5 = half, 2.0 = double cadence
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
        # -- color overrides (None = use built-in defaults) --
        self.CUBE_COLOR_LEFT:   tuple | None = None
        self.CUBE_COLOR_RIGHT:  tuple | None = None
        self.PANEL_NEON_COLOR:  tuple | None = None   # viewport 4-tile neon
        # -- event export (for rendering a matched stickman-only video) --
        self.EXPORT_EVENTS:     str | None = None
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
        if (self.BEAT_SOURCE != 'tempo'
                and self.BEAT_SUBDIV > 1 and len(beat_times) >= 2):
            sub_times = []
            for a, b in zip(beat_times, beat_times[1:]):
                sub_times.append(a)
                for k in range(1, self.BEAT_SUBDIV):
                    sub_times.append(a + (b - a) * k / self.BEAT_SUBDIV)
            sub_times.append(beat_times[-1])
            beat_times = np.array(sub_times)

        t_feat = time.time()
        print(f"Features extracted in {t_feat - t_load:.2f}s  "
              f"(source={self.BEAT_SOURCE}, sens={sens:.2f}, subdiv={self.BEAT_SUBDIV}, "
              f"tempo {tempo_val:.1f} BPM, "
              f"{total_detected} detected -> {kept} kept "
              f"-> {len(beat_times)} events after subdivision)")

        total_frames = int(total_duration * self.FPS)
        bass_arr = np.zeros(total_frames, dtype=np.float32)
        bass_max = max(np.max(spec_mag[:BASS_RANGE]), 1e-6)
        for f in range(total_frames):
            oi = min(int(f * len(onset_env) / total_frames), len(onset_env) - 1)
            bass_arr[f] = float(np.clip(
                np.mean(spec_mag[:BASS_RANGE, oi]) / bass_max * 3, 0, 1))

        # Beat times -> video frame indices (targets will POP exactly here)
        beat_frames = [int(round(t * self.FPS)) for t in beat_times]
        beat_frames = [bf for bf in beat_frames if 0 <= bf < total_frames]

        # Density control — keep every Nth beat (d<1) or split each interval
        # into sub-beats (d>1).  1.0 keeps the cadence unchanged.
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
        # Scene dressing: 'dance' wins if present (has lane panel rails);
        # otherwise use the first mode present.  'line' falls back to
        # 'punch' scene dressing since a line bar is still an air target
        # over the same tunnel.
        if 'dance' in modes_seq:
            primary_mode = 'dance'
        elif 'punch' in modes_seq:
            primary_mode = 'punch'
        else:
            primary_mode = 'punch'  # 'line' solo uses punch scene dressing
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
        cam       = PerspectiveCamera(self.WIDTH, self.HEIGHT,
                                      n_lanes=n_lanes_mode,
                                      floor_spread_frac=floor_spread)
        # Apply color overrides to all targets.
        Target.COLOR_LEFT  = self.CUBE_COLOR_LEFT
        Target.COLOR_RIGHT = self.CUBE_COLOR_RIGHT

        # Both modes now use the 4-lane layout, so the scrolling runway
        # tiles should also be drawn 1-per-lane (matches the 4 viewport
        # panels below them and the 4 rails targets fly along).
        tunnel    = TunnelRenderer(cam, show_floor_panels=self.SHOW_FLOOR_PANELS,
                                   lane_tiles=True)
        particles = ParticleSystem()
        # Stickman action pick: combo if 2+ modes, else match the single
        # mode's action library.  Solo 'line' uses its own 'line' action
        # so the HOLD_L/HOLD_R poses resolve correctly.
        if combo_mode:
            stick_action = 'combo'
        elif len(modes_seq) == 1 and modes_seq[0] == 'line':
            stick_action = 'line'
        else:
            stick_action = mode
        stick     = StickmanHUD(cam, action=stick_action) if self.SHOW_STICKMAN else None
        combo     = ComboHUD(cam)
        viewport  = ViewportFrame(cam, neon_color=self.PANEL_NEON_COLOR,
                                  mode=mode)
        # ── auto-adjust TRAVEL so blocks flow smoothly ──────────────
        # Base auto-travel = one full L↔R cycle = 2 × beat_period, so a new
        # block enters the lane right as the previous one pops.
        # BLOCK_SPEED < 1.0 slows the blocks down (longer travel → more blocks
        # visible at once on each lane, staggered near→far, like the reference
        # tunnel shot); > 1.0 speeds them up.
        travel = self.TRAVEL_FRAMES
        if self.TRAVEL_FRAMES < 0 and len(beat_frames) >= 2:
            diffs = np.diff(beat_frames)
            base = int(round(np.median(diffs) * 2))     # one L↔R cycle
            speed = max(0.05, float(self.BLOCK_SPEED))
            travel = max(8, int(round(base / speed)))
            n_in_flight = max(1, int(round(travel / base)))
            print(f"[travel:auto] period={int(np.median(diffs))}f  "
                  f"base_cycle={base}f  speed={speed:.2f}  "
                  f"travel={travel}f  (~{n_in_flight} blocks/lane visible)")

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
              f"(max {actual_max} blocks/lane visible)")

        if self.LANE_FILTER is not None:
            lane_list = sorted(v + 1 for v in self.LANE_FILTER)
            print(f"[lane_filter] Enabled lanes (1-based): {lane_list}")
        game.pre_schedule(beat_frames, bass_arr,
                          min_gap_frames=self.BEAT_MIN_GAP,
                          min_lane_gap=min_lane_gap,
                          mode=modes_seq,
                          lane_filter=self.LANE_FILTER,
                          dance_pair_cycle=self.DANCE_PAIR_CYCLE,
                          punch_pair_cycle=self.PUNCH_PAIR_CYCLE,
                          line_beats=self.LINE_BEATS)

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
            elif isinstance(tg, LineTarget):
                # Emit one short event per block in the chain so the
                # stickman arm tracks each cube: UP for odd-indexed
                # blocks (above horizon) and DOWN for even-indexed ones
                # (below horizon).  Each event sustains for CHAIN_D
                # frames so the pose freezes exactly until the next
                # block arrives.
                side_tag = 'L' if tg.is_left else 'R'
                if nL <= 2:
                    lean_scale = 1.0
                else:
                    offset_norm = abs(tg.lane - half) / half
                    lean_scale = 0.55 + 1.05 * offset_norm
                D   = tg._D
                n   = tg.n_cubes
                per_sustain = D / float(self.FPS)
                # Head (i=0) hits first, then tails i=1..n-1 sequentially.
                # even i (0,2) = DOWN, odd i (1,3) = UP.
                for i in range(n):
                    t_i  = (tg.hit_frame + i * D) / self.FPS
                    vert = 'D' if (i % 2 == 0) else 'U'
                    stick_events.append((t_i, 'Z' + side_tag + vert,
                                         lean_scale, per_sustain))
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

        # Persist events so stickman.py can render a standalone video that
        # lines up frame-for-frame with THIS rhythm render.
        if getattr(self, 'EXPORT_EVENTS', None):
            from stickman import save_events
            save_events(self.EXPORT_EVENTS, stick_events, meta={
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
            print(f"[export] Wrote {len(stick_events)} events → "
                  f"{self.EXPORT_EVENTS}")

        # ── render loop ──────────────────────────────────────────────
        all_frames: list[np.ndarray] = []
        print("Rendering frames...")
        t_render = time.time()
        last_pct = 0

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
            canvas = np.full((self.HEIGHT, self.WIDTH, 3), CLR_BG, dtype=np.uint8)

            # 1. tunnel walls + floor grid
            canvas = tunnel.draw(canvas, fi)

            # 2. targets (back to front)
            for tg in game.alive_sorted(fi):
                canvas = tg.draw(canvas, cam, fi)

            # 4. process hits → VFX (particles only, no flash/slash)
            for tg in hits:
                x = int(cam.lane_x(tg.lane, 0.02))
                if isinstance(tg, PunchTarget):
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

            # 7. HUDs (drawn last, above bloom)
            viewport.update()
            viewport.draw(canvas)
            if stick is not None:
                stick.draw(canvas, fi)
            combo.draw(canvas, fi)

            all_frames.append(canvas)

        t_done = time.time()
        print(f"\nFrame rendering done in {t_done - t_render:.2f}s  |  "
              f"avg {total_frames/(t_done-t_render):.1f} FPS")

        # ── write video ──────────────────────────────────────────────
        temp_video = 'temp_rhythm.mp4'
        print("Writing video (NVENC)..." if (not self.is_mac and _CUPY) else "Writing video...")
        t_write = time.time()

        if self.is_mac:
            fourcc = cv2.VideoWriter_fourcc(*'avc1')
            out = cv2.VideoWriter(temp_video, fourcc, self.FPS, (self.WIDTH, self.HEIGHT))
            for frm in all_frames:
                out.write(frm)
            out.release()
        else:
            vcodec = 'h264_nvenc' if _CUPY else 'libx264'
            preset = 'p4'         if vcodec == 'h264_nvenc' else 'fast'
            # -bf 0   : no B-frames (avoid reorder delays)
            # -vsync cfr: constant frame rate (1:1 pipe frame -> 1/FPS timestamp)
            # -pix_fmt yuv420p: broad compatibility
            cmd = (f'ffmpeg -y -f rawvideo -vcodec rawvideo -pix_fmt bgr24 '
                   f'-s {self.WIDTH}x{self.HEIGHT} -r {self.FPS} -i pipe:0 '
                   f'-vcodec {vcodec} -preset {preset} -b:v 3500k '
                   f'-bf 0 -vsync cfr -pix_fmt yuv420p '
                   f'-r {self.FPS} "{temp_video}"')
            proc = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for frm in all_frames:
                proc.stdin.write(frm.tobytes())
            proc.stdin.close()
            proc.wait()

        print(f"Video written in {time.time()-t_write:.2f}s")
        print(f"\nTotal time: {time.time()-t0:.2f}s")
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
            # Use plain ffmpeg CLI with -c:v copy + -shortest to keep A/V aligned.
            cmd = ['ffmpeg', '-y',
                   '-i', temp_video,       # already-encoded video (keep as-is)
                   '-i', audio_file,       # source audio
                   '-map', '0:v:0',        # take video from input 0
                   '-map', '1:a:0',        # take audio from input 1
                   '-c:v', 'copy',         # DON'T re-encode video
                   '-c:a', 'aac', '-b:a', '192k',
                   '-shortest']
            if self.TIME_LIMIT:
                cmd += ['-t', str(self.TIME_LIMIT)]
            cmd += [output_filename]

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
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
    p.add_argument('--stickman', type=int, default=1, metavar='0|1',
                   help=('Show the left-column stickman fighter. 0 = hide '
                         '(useful when compositing a standalone stickman '
                         'video rendered via stickman.py on top). Default 1.'))
    p.add_argument('--travel',  type=int, default=-1,
                   help=('Frames for target to fly from spawn to hit. '
                         'Default = -1 (auto: matches one L↔R beat cycle so '
                         'blocks never overlap on same lane). '
                         f'Manual example: --travel {TARGET_TRAVEL_FRAMES}'))
    # --- beat detection knobs ---
    p.add_argument('--beat_source', type=str, default='tempo',
                   choices=['tempo', 'beat', 'onset'],
                   help=('"tempo" = perfectly uniform cadence from BPM '
                         '— blocks flow at constant rate, always pop on beat '
                         '(recommended, matches reference video). '
                         '"beat" = each librosa beat (may jitter). '
                         '"onset" = every transient. Default: tempo'))
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
                         'COMBO: pass a comma-list like '
                         '"punch,dance" (or "dance,punch") to alternate '
                         'per beat — beat 0 spawns the first mode, '
                         'beat 1 the second, and so on.  Stickman '
                         'switches to a unified "combo" action that '
                         'punches for P-beats and stomps for D-beats.'))
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
    p.add_argument('-t', '--token', type=str, default=None)
    p.add_argument('-u', '--url',   type=str, default=None)
    return p.parse_args()


if __name__ == '__main__':
    args = parse_arguments()
    if args.token:
        if not authourize_user(args.token, args.url):
            print("Authentication failed.")
            sys.exit(1)
    else:
        print("No token provided – authentication skipped.")
        sys.exit(1)

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
    viz.SHOW_FLOOR_PANELS = bool(args.floor_panels)
    viz.SHOW_STICKMAN     = bool(args.stickman)
    try:
        viz.CUBE_COLOR_LEFT   = _parse_color(args.cube_color_left)
        viz.CUBE_COLOR_RIGHT  = _parse_color(args.cube_color_right)
        viz.PANEL_NEON_COLOR  = _parse_color(args.panel_color)
    except ValueError as e:
        print(f"[color] {e}")
        sys.exit(1)
    viz.EXPORT_EVENTS = args.export_events
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
    # Lane filter is always evaluated against the 4-lane layout both modes
    # now use (see N_LANES / N_LANES_DANCE).  --lanes uses 1-based indices.
    try:
        viz.LANE_FILTER = _parse_lanes(args.lanes, n_lanes=N_LANES_DANCE)
    except ValueError as e:
        print(f"[--lanes] {e}")
        sys.exit(1)

    import os
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    temp = viz.process_video(args.input)
    if temp:
        out_path = args.output if args.output.endswith('.mp4') else args.output + '.mp4'
        if args.audio:
            viz.merge_audio(temp, args.input, out_path)
        else:
            import shutil
            shutil.move(temp, out_path)
            print(f"Video saved to: {out_path}")
