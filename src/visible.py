"""
visible.py — Flying objects zooming in from the vanishing point, popping on beat.

Concept (Beat Saber / rhythm game style):
  - Các khối hình (circle hoặc custom image) xuất hiện từ TÂM màn hình (vanishing point)
  - Zoom in dần + bay RA NGOÀI theo một hướng ngẫu nhiên → cảm giác "chạy từ phía sau tới"
  - Khi có beat/onset mạnh → object gần camera nhất sẽ POP (phình to + fade ra)
  - Objects tự chết khi bay ra khỏi màn hình

Dựa trên kiến trúc của lightle.py, tái sử dụng GPU blur và audio pipeline.
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


# ── Constants ─────────────────────────────────────────────────────────────────
FPS = 24
HOP_LENGTH = 512
BASS_RANGE = 20
IS_MAC = platform.system() == 'Darwin'

# Spawn/pop tuning
SPAWN_COOLDOWN        = 2      # minimum frames between consecutive spawns
POP_COOLDOWN          = 3      # minimum frames between consecutive pops
BASE_SPAWN_RATE       = 0.18   # prob of spawn per frame at low energy
HIGH_SPAWN_RATE       = 0.65   # prob of spawn per frame at high energy
POP_ENERGY_THRESHOLD  = 0.55   # energy level that triggers a pop
DEFAULT_LIFESPAN      = 42     # frames (≈ 1.75s @ 24fps) for object to fly from center to edge

COLOR_SEQUENCE = [
    (0, 80, 255),     # red
    (0, 180, 255),    # orange
    (0, 255, 255),    # yellow
    (50, 255, 50),    # green
    (255, 180, 50),   # cyan-blue
    (255, 80, 120),   # indigo
    (255, 50, 255),   # magenta
    (180, 100, 255),  # pink
]


# ── FlyingObject ──────────────────────────────────────────────────────────────
class FlyingObject:
    """Một khối bay từ tâm → ra ngoài, zoom-in theo thời gian, có thể pop.

    Life cycle:
        alive  : đang bay ra, scale tăng theo progress^1.7
        popping: beat kích hoạt → phình to + fade trong `pop_duration` frames
        dead   : xóa khỏi render list
    """

    POP_DURATION = 5   # frames for pop animation

    def __init__(self, spawn_frame: int,
                 direction: tuple[float, float],
                 max_travel: float,
                 lifespan: int,
                 color: tuple[int, int, int],
                 start_scale: float = 0.05,
                 end_scale: float = 1.10):
        self.spawn_frame = spawn_frame
        self.dx, self.dy = direction
        self.max_travel  = max_travel
        self.lifespan    = lifespan
        self.color       = color
        self.start_scale = start_scale
        self.end_scale   = end_scale
        self.state       = 'alive'
        self.pop_frame   = -1

    def progress(self, cur_frame: int) -> float:
        return max(0.0, min(1.0, (cur_frame - self.spawn_frame) / self.lifespan))

    def pop(self, cur_frame: int):
        if self.state == 'alive':
            self.state = 'popping'
            self.pop_frame = cur_frame

    def is_dead(self) -> bool:
        return self.state == 'dead'

    def render_params(self, cur_frame: int, cx: int, cy: int, base_size: int):
        """Return (x, y, scale_px, alpha, popping) or None if dead."""
        p = self.progress(cur_frame)

        # easing: slow at start (far away), fast at end (close to camera)
        p_pos   = p ** 1.65
        p_scale = p ** 1.80

        x = cx + self.dx * p_pos * self.max_travel
        y = cy + self.dy * p_pos * self.max_travel

        scale = self.start_scale + (self.end_scale - self.start_scale) * p_scale
        scale_px = max(1, int(base_size * scale))
        alpha = 1.0

        if self.state == 'popping':
            pf = (cur_frame - self.pop_frame) / self.POP_DURATION
            if pf >= 1.0:
                self.state = 'dead'
                return None
            # pop: expand + fade
            scale_px = int(scale_px * (1.0 + pf * 0.9))
            alpha    = 1.0 - pf
        elif p >= 1.0:
            self.state = 'dead'
            return None

        return (int(x), int(y), scale_px, alpha, self.state == 'popping', p)


# ── ObjectRenderer ────────────────────────────────────────────────────────────
class ObjectRenderer:
    """Render helper cho FlyingObject — dùng circle hoặc custom image.

    - Circle mode: vẽ filled circle + glow blur GPU
    - Image mode : composite RGBA image với glow mask pre-computed theo viền
    """

    def __init__(self, base_size: int, image_path: str | None = None,
                 glow_enabled: bool = True):
        self.base_size = base_size
        self.image_path = image_path
        self.glow_enabled = glow_enabled

        if image_path:
            self._init_image(image_path)

    def _init_image(self, path: str):
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise FileNotFoundError(f"Cannot load image: {path}")

        target = self.base_size
        h, w   = img.shape[:2]
        s      = target / max(h, w)
        new_h, new_w = max(1, int(h * s)), max(1, int(w * s))
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

        if img.ndim == 3 and img.shape[2] == 4:
            self._img_bgr   = img[:, :, :3].copy()
            self._img_alpha = img[:, :, 3].astype(np.float32) / 255.0
        else:
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            self._img_bgr   = img.copy()
            gray            = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            _, mu8          = cv2.threshold(gray, 15, 255, cv2.THRESH_BINARY)
            self._img_alpha = mu8.astype(np.float32) / 255.0

        self._img_wh = (new_w, new_h)

        # pre-compute single wide glow mask (simpler than lightle's 4 layers)
        dil_px = max(1, int(self.base_size * 0.12))
        sigma  = max(0.5, self.base_size * 0.10)
        pad    = dil_px + 6
        self._glow_pad = pad

        mask_u8     = (self._img_alpha * 255).clip(0, 255).astype(np.uint8)
        mask_padded = np.pad(mask_u8, pad, mode='constant', constant_values=0)
        kernel      = cv2.getStructuringElement(
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
        self._glow_mask = blurred.astype(np.float32)

        print(f"[Visible] Image loaded: {path}  size={new_w}x{new_h}  pad={pad}px")

    # ── circle rendering ──────────────────────────────────────────────
    def draw_circle(self, canvas: np.ndarray, x: int, y: int, size: int,
                    color: tuple[int, int, int], alpha: float,
                    popping: bool) -> np.ndarray:
        H, W = canvas.shape[:2]
        r = max(1, size // 2)
        col_arr = np.array(color, dtype=np.float32)

        # glow (bigger when popping)
        glow_mul = 2.4 if popping else 1.9
        glow_r   = int(r * glow_mul)
        glow_layer = np.zeros_like(canvas)
        cv2.circle(glow_layer, (x, y), glow_r,
                   tuple(int(c * 0.8) for c in color), -1, lineType=cv2.LINE_AA)

        if _CUPY and self.glow_enabled:
            sigma = max(2.0, r * 0.45)
            g = cp.asarray(glow_layer, dtype=cp.float32)
            blurred = _fft_blur_gpu(g, sigma, sigma)
            c_gpu = cp.asarray(canvas, dtype=cp.float32)
            c_gpu = cp.clip(c_gpu + blurred * (0.55 * alpha), 0, 255)
            canvas = cp.asnumpy(c_gpu).astype(np.uint8)
        elif self.glow_enabled:
            ks = max(3, int(r * 2 + 1) | 1)
            blurred = cv2.GaussianBlur(glow_layer, (ks, ks), r * 0.45)
            canvas = cv2.addWeighted(canvas, 1.0, blurred, 0.55 * alpha, 0)

        # solid core
        core_color = tuple(min(255, int(c * (1.0 + (0.4 if popping else 0)))) for c in color)
        if alpha >= 0.99:
            cv2.circle(canvas, (x, y), r, core_color, -1, lineType=cv2.LINE_AA)
        else:
            overlay = canvas.copy()
            cv2.circle(overlay, (x, y), r, core_color, -1, lineType=cv2.LINE_AA)
            canvas = cv2.addWeighted(overlay, alpha, canvas, 1 - alpha, 0)

        # highlight spot
        spot_r = max(1, r // 4)
        spot_color = tuple(min(255, int(c + 80)) for c in color)
        cv2.circle(canvas, (x - r // 4, y - r // 4), spot_r,
                   spot_color, -1, lineType=cv2.LINE_AA)
        return canvas

    # ── image rendering ───────────────────────────────────────────────
    def draw_image(self, canvas: np.ndarray, x: int, y: int, size: int,
                   color: tuple[int, int, int], alpha: float,
                   popping: bool) -> np.ndarray:
        H, W = canvas.shape[:2]
        bw, bh = self._img_wh
        sf = size / max(bw, bh)                 # scale factor wrt loaded size
        new_w = max(1, int(bw * sf))
        new_h = max(1, int(bh * sf))
        pad   = max(1, int(self._glow_pad * sf))
        img_x0 = x - new_w // 2
        img_y0 = y - new_h // 2

        # ── glow ──────────────────────────────────────────────────────
        if self.glow_enabled:
            gm_w = max(1, int(self._glow_mask.shape[1] * sf))
            gm_h = max(1, int(self._glow_mask.shape[0] * sf))
            gm = cv2.resize(self._glow_mask, (gm_w, gm_h),
                            interpolation=cv2.INTER_LINEAR)
            glow_intensity = (1.8 if popping else 1.1) * alpha

            px0 = img_x0 - pad; py0 = img_y0 - pad
            sx0 = max(0, -px0); sy0 = max(0, -py0)
            sx1 = min(gm_w,  W - px0); sy1 = min(gm_h, H - py0)
            dx0 = max(0, px0);  dy0 = max(0, py0)
            if sx0 < sx1 and sy0 < sy1:
                region = gm[sy0:sy1, sx0:sx1]
                col    = np.array(color, dtype=np.float32)
                colored = region[:, :, np.newaxis] * col * glow_intensity
                dh = sy1 - sy0; dw = sx1 - sx0
                canvas[dy0:dy0+dh, dx0:dx0+dw] = np.clip(
                    canvas[dy0:dy0+dh, dx0:dx0+dw].astype(np.float32)
                    + colored, 0, 255).astype(np.uint8)

        # ── core image ────────────────────────────────────────────────
        core = cv2.resize(self._img_bgr,  (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        ca   = cv2.resize(self._img_alpha,(new_w, new_h), interpolation=cv2.INTER_LINEAR)
        ca   = ca * alpha

        # flash-white when popping
        if popping:
            white = np.full_like(core, 255, dtype=np.float32)
            core  = np.clip(core.astype(np.float32) * 0.55 + white * 0.45,
                            0, 255).astype(np.uint8)

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

    def draw(self, canvas, x, y, size, color, alpha, popping):
        if self.image_path:
            return self.draw_image(canvas, x, y, size, color, alpha, popping)
        return self.draw_circle(canvas, x, y, size, color, alpha, popping)


# ── ObjectManager ─────────────────────────────────────────────────────────────
class ObjectManager:
    """Spawn + update + pop logic cho tập hợp FlyingObjects.

    Spawn rate driven by audio energy. On strong onset → pop closest-to-camera.
    """

    # Preset trục bay (radian). Object chỉ bay theo các hướng trong preset.
    AXIS_PRESETS: dict[str, list[float]] = {
        'horizontal': [math.pi, 0.0],                                   # ← →
        'vertical':   [-math.pi / 2, math.pi / 2],                      # ↑ ↓
        'diagonal':   [3 * math.pi / 4, math.pi / 4,                    # ↖ ↗
                       -3 * math.pi / 4, -math.pi / 4],                 # ↙ ↘
        'cross':      [math.pi, 0.0, -math.pi / 2, math.pi / 2],        # ← → ↑ ↓
        'star':       [                                                 # 8 directions
            0.0, math.pi / 4, math.pi / 2, 3 * math.pi / 4,
            math.pi, -3 * math.pi / 4, -math.pi / 2, -math.pi / 4,
        ],
    }

    def __init__(self, cx: int, cy: int, W: int, H: int,
                 base_size: int, lifespan: int = DEFAULT_LIFESPAN,
                 axes: str = 'horizontal',
                 angle_jitter: float = 0.0,
                 rng_seed: int = 42):
        self.cx, self.cy = cx, cy
        self.W, self.H   = W, H
        self.base_size   = base_size
        self.lifespan    = lifespan
        self._angles     = self.AXIS_PRESETS.get(axes, self.AXIS_PRESETS['horizontal'])
        self._angle_jitter = max(0.0, angle_jitter)

        # travel distance: đủ để object bay ra khỏi canvas rìa xa nhất
        self.max_travel  = math.hypot(W, H) * 0.60

        self.objects: list[FlyingObject] = []
        self.last_spawn_frame = -999
        self.last_pop_frame   = -999
        self.color_idx = 0
        self.rng = random.Random(rng_seed)

    def _next_color(self) -> tuple[int, int, int]:
        c = COLOR_SEQUENCE[self.color_idx]
        self.color_idx = (self.color_idx + 1) % len(COLOR_SEQUENCE)
        return c

    def spawn(self, cur_frame: int):
        ang = self.rng.choice(self._angles)
        if self._angle_jitter > 0:
            ang += self.rng.uniform(-self._angle_jitter, self._angle_jitter)
        dx, dy = math.cos(ang), math.sin(ang)
        color = self._next_color()
        # life variation
        life = int(self.lifespan * self.rng.uniform(0.85, 1.15))
        obj = FlyingObject(cur_frame, (dx, dy), self.max_travel, life, color)
        self.objects.append(obj)
        self.last_spawn_frame = cur_frame

    def _pop_most_advanced(self, cur_frame: int):
        """Pop object gần camera nhất (progress cao nhất, chưa pop)."""
        alive = [o for o in self.objects if o.state == 'alive']
        if not alive:
            return
        # chỉ pop nếu có ít nhất 1 object progress >= 0.35 (đã hiện rõ)
        candidates = [o for o in alive if o.progress(cur_frame) >= 0.30]
        if not candidates:
            return
        target = max(candidates, key=lambda o: o.progress(cur_frame))
        target.pop(cur_frame)
        self.last_pop_frame = cur_frame

    def update(self, cur_frame: int, energy: float, bass: float):
        # spawn logic: probability increases with energy
        spawn_rate = BASE_SPAWN_RATE + (HIGH_SPAWN_RATE - BASE_SPAWN_RATE) * energy
        if cur_frame - self.last_spawn_frame >= SPAWN_COOLDOWN:
            if self.rng.random() < spawn_rate:
                self.spawn(cur_frame)

        # pop logic: strong onset triggers pop
        if (energy >= POP_ENERGY_THRESHOLD
                and cur_frame - self.last_pop_frame >= POP_COOLDOWN):
            self._pop_most_advanced(cur_frame)

        # auto-clean dead
        self.objects = [o for o in self.objects if not o.is_dead()]

    def render(self, canvas: np.ndarray, renderer: ObjectRenderer,
               cur_frame: int) -> np.ndarray:
        # prepare (obj, params) list, sorted by progress ASC (back → front)
        to_draw = []
        for o in self.objects:
            p = o.render_params(cur_frame, self.cx, self.cy, self.base_size)
            if p is None:
                continue
            to_draw.append((p[5], o, p))   # (progress, obj, params)

        to_draw.sort(key=lambda t: t[0])   # back to front

        for _prog, obj, (x, y, size, alpha, popping, _p) in to_draw:
            canvas = renderer.draw(canvas, x, y, size, obj.color, alpha, popping)
        return canvas


# ── VisibleVisualizer ─────────────────────────────────────────────────────────
class VisibleVisualizer:
    def __init__(self):
        self.FPS        = FPS
        self.WIDTH      = 1920
        self.HEIGHT     = 1080
        self.TIME_LIMIT: float | None = None
        self.IMAGE_PATH: str | None   = None
        self.GLOW_ENABLED: bool       = True
        self.LIFESPAN: int            = DEFAULT_LIFESPAN
        self.AXES: str                = 'horizontal'
        self.ANGLE_JITTER: float      = 0.0
        self.is_mac = IS_MAC

    def process_video(self, audio_file: str) -> str | None:
        t0 = time.time()
        print("Starting Visible processing...")

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

        # setup scene
        cx = self.WIDTH  // 2
        cy = self.HEIGHT // 2
        base_size = int(min(self.WIDTH, self.HEIGHT) * 0.22)   # full-size object at scale 1.0

        renderer = ObjectRenderer(base_size,
                                  image_path=self.IMAGE_PATH,
                                  glow_enabled=self.GLOW_ENABLED)
        manager = ObjectManager(cx, cy, self.WIDTH, self.HEIGHT,
                                base_size, lifespan=self.LIFESPAN,
                                axes=self.AXES,
                                angle_jitter=self.ANGLE_JITTER)
        print(f"[Visible] axes={self.AXES}  angles={manager._angles}  jitter=±{self.ANGLE_JITTER:.2f}rad")

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
                print(f"Progress: {pct}% | FPS: {fps_r:.1f} | ETA: {eta:.1f}s | live={len(manager.objects)}")
                last_pct = pct // 10

            bass   = float(bass_arr[fi])
            energy = float(energy_arr[fi])

            canvas = np.zeros((self.HEIGHT, self.WIDTH, 3), dtype=np.uint8)
            manager.update(fi, energy, bass)
            canvas = manager.render(canvas, renderer, fi)
            all_frames.append(canvas)

        t_done = time.time()
        print(f"\nFrame rendering done in {t_done - t_render:.2f}s  |  avg {total_frames/(t_done-t_render):.1f} FPS")

        # write video
        temp_video = 'temp_visible.mp4'
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

    def merge_audio(self, temp_video: str, audio_file: str,
                    output_filename: str = 'visible_output.mp4') -> bool:
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


# ── CLI ────────────────────────────────────────────────────────────────────────
def parse_arguments():
    p = argparse.ArgumentParser(description="Visible – Flying objects appearing on beat")
    p.add_argument('-W', '--width',    type=int,   default=1920)
    p.add_argument('-H', '--height',   type=int,   default=1080)
    p.add_argument('-i', '--input',    type=str,   required=True,  help='Input audio file')
    p.add_argument('-o', '--output',   type=str,   required=True,  help='Output video file (.mp4)')
    p.add_argument('-d', '--duration', type=float, default=None,   help='Duration seconds (full if omitted)')
    p.add_argument('-a', '--audio',    type=int,   default=0,      help='Include audio in output (1/0)')

    p.add_argument('--image_path', type=str, default=None,
                   help='Custom image (PNG/JPG). Alpha channel used if RGBA.')
    p.add_argument('--glow',       type=int, default=1, metavar='0|1',
                   help='Enable glow around objects (default: 1)')
    p.add_argument('--lifespan',   type=int, default=DEFAULT_LIFESPAN,
                   help=f'Frames for an object to fly from center to edge (default: {DEFAULT_LIFESPAN} ≈ {DEFAULT_LIFESPAN/FPS:.2f}s)')
    p.add_argument('--axes', type=str, default='horizontal',
                   choices=['horizontal', 'vertical', 'diagonal', 'cross', 'star'],
                   help='Fly direction preset (default: horizontal = ← + →).  '
                        'horizontal: 2 dirs, vertical: 2 dirs, diagonal: 4 corners, '
                        'cross: 4 perpendicular, star: 8 dirs.')
    p.add_argument('--angle_jitter', type=float, default=0.0,
                   help='Random angle jitter in radians (default: 0 = strict axis). '
                        'Try 0.08 for subtle variation.')

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

    viz = VisibleVisualizer()
    viz.WIDTH        = args.width
    viz.HEIGHT       = args.height
    viz.TIME_LIMIT   = args.duration
    viz.IMAGE_PATH   = args.image_path
    viz.GLOW_ENABLED = bool(args.glow)
    viz.LIFESPAN     = args.lifespan
    viz.AXES         = args.axes
    viz.ANGLE_JITTER = args.angle_jitter

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
