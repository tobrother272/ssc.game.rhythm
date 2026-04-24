"""
lightle.py — Ánh sáng chớp nháy theo nhạc (Light pulse visualization).

Một hình tròn duy nhất ở trung tâm:
  - Thở theo nhịp (bass → phình to, treble → rung)
  - Glow nhiều lớp phát sáng
  - Shockwave ring bùng ra khi bass mạnh
  - Flash trắng trên beat
  - Màu sắc neon chuyển dần

Dựa trên kiến trúc bubble.py, đơn giản hóa: không có particle, trail, dot vật lý.
"""

import numpy as np
import librosa
import cv2
import math
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
except Exception as _e:
    _CUPY = False
    print(f"[CPU] CuPy/cuFFT not available – falling back to CPU")


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
    else:
        ch = arr_gpu.astype(cp.complex64)
        return cp.real(cp.fft.ifft2(cp.fft.fft2(ch) * k_fft)).astype(cp.float32)


def gpu_blur(img: np.ndarray, ksize: tuple, sigmaX: float = 0) -> np.ndarray:
    if not _CUPY:
        return cv2.GaussianBlur(img, ksize, sigmaX)
    kw, kh = ksize
    sx = sigmaX if sigmaX > 0 else _ks2s(kw)
    sy = sigmaX if sigmaX > 0 else _ks2s(kh)
    blurred = _fft_blur_gpu(cp.asarray(img, dtype=cp.float32), sy, sx)
    return cp.asnumpy(cp.clip(blurred, 0, 255)).astype(np.uint8)


def gpu_add_weighted(src1, a1, src2, a2, gamma=0):
    if not _CUPY:
        return cv2.addWeighted(src1, a1, src2, a2, gamma)
    g1 = cp.asarray(src1, dtype=cp.float32)
    g2 = cp.asarray(src2, dtype=cp.float32)
    return cp.asnumpy(cp.clip(g1 * a1 + g2 * a2 + gamma, 0, 255)).astype(np.uint8)

# ── Constants ─────────────────────────────────────────────────────────────────
FPS = 24
HOP_LENGTH = 512
BASS_RANGE = 20
BASS_THRESHOLD = 0.30
SHOCKWAVE_COOLDOWN = 8   # frames between shockwaves
MAX_SHOCKWAVES = 6
IS_MAC = platform.system() == 'Darwin'

COLOR_SEQUENCE = [
    (0, 255, 255),    # vàng neon
    (0, 255, 140),    # vàng xanh neon
    (50, 255, 0),     # xanh lá neon
    (255, 100, 0),    # xanh dương neon
    (255, 0, 140),    # xanh tím neon
    (255, 0, 255),    # hồng tím neon
    (150, 0, 255),    # tím neon
    (0, 140, 255),    # cam neon
]

# ── ColorSystem ────────────────────────────────────────────────────────────────
class ColorSystem:
    def __init__(self):
        self.color_sequence = COLOR_SEQUENCE
        self.current_index = 0
        self.transition_progress = 0.0
        self.transition_speed = 0.006
        self.current_color = list(self.color_sequence[0])

    def update(self):
        cur = self.color_sequence[self.current_index]
        nxt = self.color_sequence[(self.current_index + 1) % len(self.color_sequence)]
        t = math.sin(self.transition_progress * math.pi / 2)
        for i in range(3):
            self.current_color[i] = int(cur[i] * (1 - t) + nxt[i] * t)
        self.transition_progress += self.transition_speed
        if self.transition_progress >= 1:
            self.transition_progress = 0.0
            self.current_index = (self.current_index + 1) % len(self.color_sequence)
            self.current_color = list(self.color_sequence[self.current_index])
        glow = 1.3 + math.sin(self.transition_progress * math.pi) * 0.1
        return tuple(min(255, int(c * glow)) for c in self.current_color)

    def get_color(self):
        return tuple(int(c) for c in self.current_color)


# ── Shockwave ─────────────────────────────────────────────────────────────────
class Shockwave:
    """Vòng sóng xung kích mở rộng ra ngoài khi bass mạnh."""

    def __init__(self, cx, cy, color, max_radius, speed=12):
        self.cx = cx
        self.cy = cy
        self.radius = 0.0
        self.max_radius = max_radius
        self.color = color
        self.alpha = 1.0
        self.speed = speed
        self.alive = True

    def update(self):
        self.radius += self.speed
        self.alpha = max(0.0, 1.0 - (self.radius / self.max_radius) ** 0.7)
        if self.radius >= self.max_radius:
            self.alive = False

    def draw(self, canvas: np.ndarray) -> np.ndarray:
        if not self.alive or self.alpha < 0.01:
            return canvas
        r = int(self.radius)
        thickness = max(1, int(5 * self.alpha))
        color = tuple(int(c * self.alpha) for c in self.color)
        cv2.circle(canvas, (self.cx, self.cy), r, color, thickness, lineType=cv2.LINE_AA)
        # inner echo ring
        if r > 20:
            echo_r = max(0, r - 15)
            echo_color = tuple(int(c * self.alpha * 0.4) for c in self.color)
            cv2.circle(canvas, (self.cx, self.cy), echo_r, echo_color, max(1, thickness - 2),
                       lineType=cv2.LINE_AA)
        return canvas


# ── LightShape ────────────────────────────────────────────────────────────────
class LightShape:
    """
    Hình dạng ánh sáng chớp nháy theo nhạc.

    Hai chế độ:
      circle (mặc định) — hình tròn với glow GPU blur.
      image  (--image_path) — ảnh bất kỳ; glow bám sát đường viền hình.

    Cả hai chế độ đều có:
      - Thở / phình to theo bass
      - Flash trắng theo beat/onset
      - Shockwave ring bùng ra khi bass mạnh
      - Màu neon chuyển dần
    """

    # Glow layer configs: (dil_factor, sigma_factor, base_alpha)
    _GLOW_CFGS = [
        (0.90, 0.30, 0.18),   # ambient bloom  (rất rộng, rất mờ)
        (0.28, 0.14, 0.35),   # mid glow
        (0.07, 0.055, 0.60),  # close glow (sát viền)
        (0.015, 0.022, 0.78), # core halo
    ]

    # layer keys — order must match _GLOW_CFGS indices
    LAYER_KEYS = ['ambient', 'mid_glow', 'close_glow', 'core_halo']

    # Perspective slots: (x_frac, y_frac, scale_frac, phase_shift_rad, flip_h)
    #   x_frac: offset from center as fraction of half-canvas-width
    #           (smaller |x| = closer to vanishing point at center)
    #   y_frac: offset from center as fraction of half-canvas-height
    #           (negative = up, near vanishing point; positive = down, near camera)
    #   scale_frac: size relative to base (far < near)
    #   phase_shift_rad: breath animation offset → "flowing ripple" across slots
    #   flip_h: mirror horizontally for right-side instances
    #
    # Layout forms two diagonals converging toward the center:
    #     left-back ↘ . ↙ right-back      (higher, smaller, closer to center)
    #                 ·
    #   left-front ↗     ↖ right-front    (lower, larger, further from center)
    #
    # Ordered back→front (painter's algorithm)
    # Layout theo ảnh tham chiếu "HOLE IN THE WALL":
    #   - Mũi nhọn (tip) luôn hướng RA NGOÀI cạnh canvas
    #   - Đuôi (phần mở) hướng VÀO TÂM
    #   - Inner chevron: nhỏ hơn, cao hơn một chút, gần tâm
    #   - Outer chevron: lớn hơn, thấp hơn một chút, sát cạnh
    # → flow từ trong-trên-nhỏ xuống ngoài-dưới-lớn = cảm giác chiều sâu 3D
    #
    # Giả định ảnh nguồn có mũi hướng sang TRÁI:
    #   - Bên TRÁI canvas: flip_h=False → giữ nguyên, mũi ra ngoài (trái)
    #   - Bên PHẢI canvas: flip_h=True  → lật, mũi ra ngoài (phải)
    _PERSPECTIVE_SLOTS = [
        (-0.35, -0.08, 0.48, math.pi * 0.50, False),  # left-inner  (upper, small)
        (+0.35, -0.08, 0.48, math.pi * 0.50, True),   # right-inner (upper, small, mirrored)
        (-0.78, +0.14, 0.95, 0.0,            False),  # left-outer  (lower, large)
        (+0.78, +0.14, 0.95, 0.0,            True),   # right-outer (lower, large, mirrored)
    ]

    def __init__(self, cx: int, cy: int, base_radius: int,
                 image_path: str | None = None,
                 layers_on: dict | None = None,
                 use_perspective: bool = False):
        self.cx = cx
        self.cy = cy
        self.base_radius = base_radius
        self.color_system = ColorSystem()
        self.image_path = image_path

        # which layers are active (all ON by default)
        default = {k: True for k in self.LAYER_KEYS}
        default['solid_core'] = True
        self.layers_on: dict[str, bool] = {**default, **(layers_on or {})}
        self.use_perspective = use_perspective

        # physics (shared by both modes)
        self.breath_phase   = 0.0
        self.smooth_bass    = 0.0
        self.smooth_energy  = 0.0
        self.beat_flash     = 0.0

        # shockwaves (shared)
        self.shockwaves: list[Shockwave] = []
        self.last_shock_frame = -999
        self.max_shock_radius = max(cx, cy) * 1.6

        if image_path:
            self._init_image(image_path)

    # ── image mode init ────────────────────────────────────────────────
    def _init_image(self, path: str):
        """Load image, extract mask, pre-compute normalized glow masks."""
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise FileNotFoundError(f"Cannot load image: {path}")

        # resize to fit inside base_radius*2 square
        target = self.base_radius * 2
        h, w   = img.shape[:2]
        s      = target / max(h, w)
        new_h, new_w = max(1, int(h * s)), max(1, int(w * s))
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

        # split BGR and alpha mask
        if img.ndim == 3 and img.shape[2] == 4:
            self._img_bgr  = img[:, :, :3].copy()
            self._img_alpha = img[:, :, 3].astype(np.float32) / 255.0
        else:
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            self._img_bgr   = img.copy()
            gray            = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            _, mu8          = cv2.threshold(gray, 15, 255, cv2.THRESH_BINARY)
            self._img_alpha = mu8.astype(np.float32) / 255.0

        self._img_wh = (new_w, new_h)   # base (width, height) before any scaling

        # Pre-compute glow masks in a padded workspace so outer glow isn't clipped.
        max_dil = max(1, int(self.base_radius * self._GLOW_CFGS[0][0]))
        pad     = max_dil + 8
        self._glow_pad = pad

        mask_u8 = (self._img_alpha * 255).clip(0, 255).astype(np.uint8)
        # pad mask (add 'pad' pixels of zero on all sides)
        mask_padded = np.pad(mask_u8, pad, mode='constant', constant_values=0)

        self._glow_masks: list[tuple[np.ndarray, float]] = []
        for dil_f, sig_f, base_alpha in self._GLOW_CFGS:
            dil_px = max(1, int(self.base_radius * dil_f))
            sigma  = max(0.5, self.base_radius * sig_f)
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (dil_px * 2 + 1, dil_px * 2 + 1))
            dilated = cv2.dilate(mask_padded, kernel).astype(np.float32) / 255.0
            ksize   = int(6 * sigma + 1) | 1
            ksize   = min(ksize, min(dilated.shape[:2]))
            if ksize % 2 == 0:
                ksize -= 1
            blurred = cv2.GaussianBlur(dilated, (ksize, ksize), sigma)
            mx = blurred.max()
            if mx > 0:
                blurred /= mx
            self._glow_masks.append((blurred.astype(np.float32), base_alpha))

        print(f"[LightShape] Image loaded: {path}  size={new_w}x{new_h}  "
              f"pad={pad}px  {len(self._glow_masks)} glow layers pre-computed")

    # ── instance layout helper ─────────────────────────────────────────
    def _instances(self, H: int, W: int) -> list:
        """Return [(cx, cy, scale_factor, phase_offset, flip_h), ...] for all instances.
        Single-instance mode: one centered entry.
        Perspective mode: 4 entries ordered back→front."""
        bass_push = self.smooth_bass * 0.45
        if not self.use_perspective:
            breath = math.sin(self.breath_phase) * 0.03
            return [(self.cx, self.cy, 1.0 + bass_push + breath, 0.0, False)]

        result = []
        for xf, yf, sf, po, fh in self._PERSPECTIVE_SLOTS:
            breath = math.sin(self.breath_phase + po) * 0.03
            result.append((
                self.cx + int(xf * W * 0.5),
                self.cy + int(yf * H * 0.5),
                sf * (1.0 + bass_push + breath),
                po,
                fh,
            ))
        return result

    # ── shared update ──────────────────────────────────────────────────
    def update(self, bass: float, energy: float, frame: int):
        self.color_system.update()
        self.breath_phase  += 0.04
        self.smooth_bass    = 0.45 * self.smooth_bass   + 0.55 * bass
        self.smooth_energy  = 0.70 * self.smooth_energy + 0.30 * energy

        if energy > 0.65:
            self.beat_flash = min(1.0, self.beat_flash + energy * 0.6)
        self.beat_flash *= 0.75

        if (bass > BASS_THRESHOLD
                and frame - self.last_shock_frame > SHOCKWAVE_COOLDOWN
                and len(self.shockwaves) < MAX_SHOCKWAVES):
            color = self.color_system.get_color()
            speed = int(10 + bass * 18)
            self.shockwaves.append(
                Shockwave(self.cx, self.cy, color, self.max_shock_radius, speed))
            self.last_shock_frame = frame

        for s in self.shockwaves:
            s.update()
        self.shockwaves = [s for s in self.shockwaves if s.alive]

    # ── draw ───────────────────────────────────────────────────────────
    def draw(self, canvas: np.ndarray) -> np.ndarray:
        if self.image_path:
            return self._draw_image(canvas)
        return self._draw_circle(canvas)

    # ── circle mode draw ───────────────────────────────────────────────
    def _draw_circle(self, canvas: np.ndarray) -> np.ndarray:
        H, W      = canvas.shape[:2]
        on        = self.layers_on
        color     = self.color_system.get_color()
        base_col  = np.array(color, dtype=np.float32)
        light_col = np.minimum(base_col * 1.6, 255)
        f         = self.beat_flash
        flash_col = tuple(int(c + f * (255 - c)) for c in color)

        instances = self._instances(H, W)

        # ── CPU: draw ALL glow circles for ALL instances ───────────────
        # batched into one list → single GPU session below
        all_layers: list[tuple[np.ndarray, float, float, float]] = []
        core_draws: list[tuple[int, int, int]] = []   # (cx, cy, radius)

        for cx_i, cy_i, sf_i, _po, _fh in instances:
            radius = max(4, int(self.base_radius * sf_i))
            core_draws.append((cx_i, cy_i, radius))

            if on['ambient']:
                bloom_r = int(radius * (3.5 + self.smooth_bass * 2))
                bloom = np.zeros_like(canvas)
                cv2.circle(bloom, (cx_i, cy_i), bloom_r,
                           tuple(map(int, base_col * 0.6)), -1)
                all_layers.append((bloom,
                                   0.18 + self.smooth_energy * 0.12,
                                   bloom_r * 0.3, bloom_r * 0.3))

            if on['mid_glow']:
                mid_r = int(radius * (1.8 + self.smooth_bass))
                mid = np.zeros_like(canvas)
                cv2.circle(mid, (cx_i, cy_i), mid_r,
                           tuple(map(int, base_col)), -1)
                all_layers.append((mid,
                                   0.30 + self.smooth_bass * 0.25,
                                   mid_r * 0.18, mid_r * 0.18))

            if on['close_glow']:
                close_r = int(radius * (1.3 + self.smooth_bass * 0.5))
                close = np.zeros_like(canvas)
                cv2.circle(close, (cx_i, cy_i), close_r,
                           tuple(map(int, light_col)), -1)
                all_layers.append((close,
                                   0.55 + self.smooth_bass * 0.25,
                                   close_r * 0.12, close_r * 0.12))

            if on['core_halo']:
                halo = np.zeros_like(canvas)
                cv2.circle(halo, (cx_i, cy_i), int(radius * 1.1), flash_col, -1)
                all_layers.append((halo, 0.70, radius * 0.06, radius * 0.06))

        # ── GPU/CPU: blur + blend all layers in one session ────────────
        if all_layers:
            if _CUPY:
                result_gpu = cp.asarray(canvas, dtype=cp.float32)
                for limg, la, sy, sx in all_layers:
                    limg_gpu = cp.asarray(limg, dtype=cp.float32)
                    blurred  = _fft_blur_gpu(limg_gpu, max(sy, 0.5), max(sx, 0.5))
                    result_gpu = cp.clip(result_gpu + blurred * la, 0, 255)
                canvas = cp.asnumpy(result_gpu).astype(np.uint8)
            else:
                for limg, la, sy, sx in all_layers:
                    kw = int(6 * max(sx, 0.5) + 1) | 1
                    kh = int(6 * max(sy, 0.5) + 1) | 1
                    blurred = cv2.GaussianBlur(limg, (kw, kh), 0)
                    canvas  = cv2.addWeighted(canvas, 1, blurred, la, 0)

        # ── shockwaves ─────────────────────────────────────────────────
        for s in self.shockwaves:
            canvas = s.draw(canvas)

        # ── solid core for all instances (back→front = correct overlap) ─
        if on['solid_core']:
            for cx_i, cy_i, radius in core_draws:
                cv2.circle(canvas, (cx_i, cy_i), radius,
                           flash_col, -1, lineType=cv2.LINE_AA)
                spot_r = max(2, int(radius * (0.35 + f * 0.25)))
                cv2.circle(canvas, (cx_i, cy_i), spot_r,
                           tuple(min(255, int(c * 1.8 + f * 60)) for c in color),
                           -1, lineType=cv2.LINE_AA)
                spec_r = max(1, radius // 8)
                sa = int(180 + f * 75)
                cv2.circle(canvas,
                           (cx_i - radius // 5, cy_i - radius // 5),
                           spec_r, (sa, sa, sa), -1, lineType=cv2.LINE_AA)
        return canvas

    # ── image mode draw ────────────────────────────────────────────────
    def _draw_image(self, canvas: np.ndarray) -> np.ndarray:
        H, W      = canvas.shape[:2]
        color     = self.color_system.get_color()
        color_arr = np.array(color, dtype=np.float32)
        f         = self.beat_flash
        flash_col = tuple(int(c + f * (255 - c)) for c in color)
        flash_arr = np.array(flash_col, dtype=np.float32)
        tint_s    = self.smooth_bass * 0.25

        instances = self._instances(H, W)

        # ── Glow layers for all instances (pure numpy, baked blur) ─────
        canvas_f = canvas.astype(np.float32)

        for cx_i, cy_i, sf_i, _po, flip_h in instances:
            bw, bh = self._img_wh
            new_w  = max(1, int(bw * sf_i))
            new_h  = max(1, int(bh * sf_i))
            pad    = max(1, int(self._glow_pad * sf_i))
            img_x0 = cx_i - new_w // 2
            img_y0 = cy_i - new_h // 2

            for (norm_mask, base_alpha), key in zip(self._glow_masks, self.LAYER_KEYS):
                if not self.layers_on.get(key, True):
                    continue

                total_w = max(1, int(norm_mask.shape[1] * sf_i))
                total_h = max(1, int(norm_mask.shape[0] * sf_i))
                scaled_mask = cv2.resize(norm_mask, (total_w, total_h),
                                         interpolation=cv2.INTER_LINEAR)
                if flip_h:
                    scaled_mask = np.fliplr(scaled_mask)

                px0 = img_x0 - pad;  py0 = img_y0 - pad
                src_x0 = max(0, -px0); src_y0 = max(0, -py0)
                dst_x0 = max(0,  px0); dst_y0 = max(0,  py0)
                src_x1 = min(total_w, W - px0)
                src_y1 = min(total_h, H - py0)
                if src_x0 >= src_x1 or src_y0 >= src_y1:
                    continue

                region  = scaled_mask[src_y0:src_y1, src_x0:src_x1]
                colored = region[:, :, np.newaxis] * flash_arr
                intensity = base_alpha * (1.0 + self.smooth_bass * 0.6 + f * 0.35)
                dst_h = src_y1 - src_y0;  dst_w = src_x1 - src_x0
                canvas_f[dst_y0:dst_y0+dst_h, dst_x0:dst_x0+dst_w] = np.clip(
                    canvas_f[dst_y0:dst_y0+dst_h, dst_x0:dst_x0+dst_w]
                    + colored * intensity, 0, 255)

        canvas = canvas_f.astype(np.uint8)

        # ── Shockwaves ─────────────────────────────────────────────────
        for s in self.shockwaves:
            canvas = s.draw(canvas)

        # ── Core images back→front (painter's algorithm) ───────────────
        if not self.layers_on.get('solid_core', True):
            return canvas

        for cx_i, cy_i, sf_i, _po, flip_h in instances:
            bw, bh = self._img_wh
            new_w  = max(1, int(bw * sf_i))
            new_h  = max(1, int(bh * sf_i))
            img_x0 = cx_i - new_w // 2
            img_y0 = cy_i - new_h // 2

            core = cv2.resize(self._img_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            ca   = cv2.resize(self._img_alpha, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            if flip_h:
                core = np.fliplr(core)
                ca   = np.fliplr(ca)

            # flash brightness
            if f > 0.02:
                white = np.full_like(core, 255, dtype=np.float32)
                core  = np.clip(core.astype(np.float32) * (1 - f * 0.35)
                                + white * f * 0.35, 0, 255).astype(np.uint8)
            # color tint on bass
            if tint_s > 0:
                tint = np.broadcast_to(color_arr, core.shape).copy()
                core = np.clip(core.astype(np.float32) * (1 - tint_s)
                               + tint * tint_s, 0, 255).astype(np.uint8)

            cx0 = max(0, -img_x0);        cy0 = max(0, -img_y0)
            cx1 = min(new_w, W - img_x0); cy1 = min(new_h, H - img_y0)
            dx0 = max(0, img_x0);         dy0 = max(0, img_y0)
            if cx0 < cx1 and cy0 < cy1:
                a  = ca[cy0:cy1, cx0:cx1, np.newaxis]
                bg = canvas[dy0:dy0+(cy1-cy0), dx0:dx0+(cx1-cx0)].astype(np.float32)
                fg = core[cy0:cy1, cx0:cx1].astype(np.float32)
                canvas[dy0:dy0+(cy1-cy0), dx0:dx0+(cx1-cx0)] = np.clip(
                    bg * (1 - a) + fg * a, 0, 255).astype(np.uint8)

        return canvas


# ── LightVisualizer ────────────────────────────────────────────────────────────
class LightVisualizer:
    def __init__(self):
        self.FPS        = FPS
        self.WIDTH      = 1920
        self.HEIGHT     = 1080
        self.TIME_LIMIT: float | None = None
        self.IMAGE_PATH: str | None   = None
        self.LAYERS_ON: dict[str, bool] = {k: True for k in LightShape.LAYER_KEYS}
        self.LAYERS_ON['solid_core'] = True
        self.USE_PERSPECTIVE: bool = False
        self.is_mac = IS_MAC

    def process_video(self, audio_file: str) -> str | None:
        t0 = time.time()
        print("Starting Lightle processing...")

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
        t_feat = time.time()
        print(f"Features extracted in {t_feat - t_load:.2f}s")

        total_frames = int(total_duration * self.FPS)
        energy_arr = np.zeros(total_frames, dtype=np.float32)
        bass_arr   = np.zeros(total_frames, dtype=np.float32)
        onset_max  = max(np.max(onset_env), 1e-6)
        bass_max   = max(np.max(spec_mag[:BASS_RANGE]), 1e-6)

        for f in range(total_frames):
            oi = min(int(f * len(onset_env) / total_frames), len(onset_env) - 1)
            energy_arr[f] = float(np.clip(onset_env[oi] / onset_max * 2, 0, 1))
            bass_arr[f]   = float(np.clip(
                np.mean(spec_mag[:BASS_RANGE, oi]) / bass_max * 3, 0, 1))

        # LightShape centered in frame
        cx     = self.WIDTH  // 2
        cy     = self.HEIGHT // 2
        base_r = min(self.WIDTH, self.HEIGHT) // 5
        light  = LightShape(cx, cy, base_r,
                            image_path=self.IMAGE_PATH,
                            layers_on=self.LAYERS_ON,
                            use_perspective=self.USE_PERSPECTIVE)

        # Render
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
                print(f"Progress: {pct}% | FPS: {fps_r:.1f} | ETA: {eta:.1f}s")
                last_pct = pct // 10

            bass   = bass_arr[fi]
            energy = energy_arr[fi]

            canvas = np.zeros((self.HEIGHT, self.WIDTH, 3), dtype=np.uint8)
            light.update(bass, energy, fi)
            canvas = light.draw(canvas)
            all_frames.append(canvas)

        t_done = time.time()
        print(f"\nFrame rendering done in {t_done - t_render:.2f}s  |  avg {total_frames/(t_done-t_render):.1f} FPS")

        # Write video
        temp_video = 'temp_lightle.mp4'
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
            preset = 'p4'        if vcodec == 'h264_nvenc' else 'fast'
            cmd = (f'ffmpeg -y -f rawvideo -vcodec rawvideo -pix_fmt bgr24 '
                   f'-s {self.WIDTH}x{self.HEIGHT} -r {self.FPS} -i pipe:0 '
                   f'-vcodec {vcodec} -preset {preset} -b:v 2500k "{temp_video}"')
            proc = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for frm in all_frames:
                proc.stdin.write(frm.tobytes())
            proc.stdin.close()
            proc.wait()

        print(f"Video written in {time.time()-t_write:.2f}s")
        print(f"\nTotal time: {time.time()-t0:.2f}s")
        return temp_video

    # ------------------------------------------------------------------
    def merge_audio(self, temp_video: str, audio_file: str,
                    output_filename: str = 'lightle_output.mp4') -> bool:
        print("\nMerging audio...")
        t0 = time.time()
        try:
            if platform.system() == 'Linux':
                cmd = ['ffmpeg', '-i', temp_video, '-i', audio_file,
                       '-c:v', 'libx264', '-c:a', 'aac', '-preset', 'fast',
                       '-b:v', '2500k', '-y', output_filename]
                if self.TIME_LIMIT:
                    cmd = cmd[:-1] + ['-t', str(self.TIME_LIMIT), output_filename]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                _, err = proc.communicate()
                if proc.returncode != 0:
                    print(f"FFmpeg error: {err.decode()}")
                    return False
            else:
                iv = ffmpeg.input(temp_video)
                ia = ffmpeg.input(audio_file, ss=0)
                kwargs = dict(acodec='aac', video_bitrate='2500k')
                if self.TIME_LIMIT:
                    kwargs['t'] = self.TIME_LIMIT
                if self.is_mac:
                    kwargs['vcodec'] = 'h264_videotoolbox'
                else:
                    kwargs['vcodec'] = 'h264_nvenc' if _CUPY else 'libx264'
                    kwargs['preset'] = 'p4' if _CUPY else 'fast'
                stream = ffmpeg.output(iv, ia, output_filename, **kwargs).overwrite_output()
                ffmpeg.run(stream, capture_stdout=True, capture_stderr=True)

            import os
            if os.path.exists(temp_video):
                os.remove(temp_video)
            print(f"Audio merged in {time.time()-t0:.2f}s → {output_filename}")
            return True
        except Exception as e:
            print(f"Error merging audio: {e}")
            return False

    # ------------------------------------------------------------------
    @staticmethod
    def run(audio_file: str, output: str, with_audio: bool):
        viz = LightVisualizer()
        temp = viz.process_video(audio_file)
        if temp:
            if with_audio:
                viz.merge_audio(temp, audio_file, output)
            else:
                import os, shutil
                shutil.move(temp, output if output.endswith('.mp4') else output + '.mp4')


# ── CLI ────────────────────────────────────────────────────────────────────────
def parse_arguments():
    p = argparse.ArgumentParser(description="Lightle – Light pulse visualization")
    p.add_argument('-W', '--width',       type=int,   default=1920)
    p.add_argument('-H', '--height',      type=int,   default=1080)
    p.add_argument('-i', '--input',       type=str,   required=True,  help='Input audio file')
    p.add_argument('-o', '--output',      type=str,   required=True,  help='Output video file (.mp4)')
    p.add_argument('-d', '--duration',    type=float, default=None,   help='Duration in seconds (default: full)')
    p.add_argument('-a', '--audio',       type=int,   default=0,      help='Include audio in output (1/0)')
    p.add_argument('--image_path',   type=str, default=None,
                   help='Custom shape image (PNG/JPG). Alpha channel used as mask if RGBA.')
    p.add_argument('--perspective',  type=int, default=0, metavar='0|1',
                   help='Perspective 3D layout: 4 copies (2 left + 2 right) with depth scaling. '
                        'Right side is horizontally mirrored.')

    # ── layer toggles (1 = ON, 0 = OFF, default all ON) ──────────────
    g = p.add_argument_group('layer toggles (1=on, 0=off, default: all 1)')
    g.add_argument('--ambient',    type=int, default=1, metavar='0|1',
                   help='Ambient bloom  — wide, very soft background glow')
    g.add_argument('--mid_glow',   type=int, default=1, metavar='0|1',
                   help='Mid glow       — medium halo around shape')
    g.add_argument('--close_glow', type=int, default=1, metavar='0|1',
                   help='Close glow     — tight glow hugging the shape edge')
    g.add_argument('--core_halo',  type=int, default=1, metavar='0|1',
                   help='Core halo      — thin innermost glow ring')
    g.add_argument('--solid_core', type=int, default=1, metavar='0|1',
                   help='Solid core     — the filled shape + bright spot + specular')

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

    viz = LightVisualizer()
    viz.WIDTH      = args.width
    viz.HEIGHT     = args.height
    viz.TIME_LIMIT = args.duration
    viz.IMAGE_PATH       = args.image_path
    viz.USE_PERSPECTIVE  = bool(args.perspective)
    viz.LAYERS_ON  = {
        'ambient':    bool(args.ambient),
        'mid_glow':   bool(args.mid_glow),
        'close_glow': bool(args.close_glow),
        'core_halo':  bool(args.core_halo),
        'solid_core': bool(args.solid_core),
    }

    import os
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    temp = viz.process_video(args.input)
    if temp:
        if args.audio:
            out_path = args.output if args.output.endswith('.mp4') else args.output + '.mp4'
            viz.merge_audio(temp, args.input, out_path)
        else:
            import shutil
            out_path = args.output if args.output.endswith('.mp4') else args.output + '.mp4'
            shutil.move(temp, out_path)
            print(f"Video saved to: {out_path}")
