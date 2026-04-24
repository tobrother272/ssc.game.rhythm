import numpy as np
import librosa
import cv2
import random
import ffmpeg
import platform
import time  # Add at the top with other imports
import math
import argparse
from authorization import *
import sys

# ── GPU acceleration (CuPy + cuFFT) ─────────────────────────────────────────
try:
    import cupy as cp
    cp.array([1])           # trigger CUDA init / device check
    # quick cuFFT smoke-test (no nvrtc required)
    _t = cp.zeros((4, 4), dtype=cp.complex64)
    cp.fft.fft2(_t)
    _CUPY = True
    print("[GPU] CuPy + cuFFT acceleration enabled")
except Exception as _e:
    _CUPY = False
    print(f"[CPU] CuPy/cuFFT not available ({_e}) – falling back to CPU")

def _ks2s(k: int) -> float:
    """Convert OpenCV kernel size → sigma (OpenCV's own formula)."""
    return max(0.3 * ((k - 1) * 0.5 - 1) + 0.8, 0.01)

# Cache: (sigma_y, sigma_x, H, W) → pre-computed FFT kernel on GPU
_KERNEL_CACHE: dict = {}

def _get_kernel_fft(sigma_y: float, sigma_x: float, H: int, W: int):
    """Return cached cuFFT of a 2-D Gaussian kernel (separable, zero-padded)."""
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

def _fft_blur_gpu(arr_gpu: "cp.ndarray", sigma_y: float, sigma_x: float) -> "cp.ndarray":
    """Blur a float32 HxWxC GPU array using cuFFT convolution (per channel)."""
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
    """GaussianBlur on GPU (cuFFT) with automatic CPU fallback."""
    if not _CUPY:
        return cv2.GaussianBlur(img, ksize, sigmaX)
    kw, kh = ksize
    sx = sigmaX if sigmaX > 0 else _ks2s(kw)
    sy = sigmaX if sigmaX > 0 else _ks2s(kh)
    arr = cp.asarray(img, dtype=cp.float32)
    blurred = _fft_blur_gpu(arr, sy, sx)
    return cp.asnumpy(cp.clip(blurred, 0, 255)).astype(np.uint8)

def gpu_add_weighted(src1: np.ndarray, a1: float,
                     src2: np.ndarray, a2: float,
                     gamma: float = 0) -> np.ndarray:
    """addWeighted on GPU (CuPy elementwise) with automatic CPU fallback."""
    if not _CUPY:
        return cv2.addWeighted(src1, a1, src2, a2, gamma)
    g1 = cp.asarray(src1, dtype=cp.float32)
    g2 = cp.asarray(src2, dtype=cp.float32)
    return cp.asnumpy(cp.clip(g1 * a1 + g2 * a2 + gamma, 0, 255)).astype(np.uint8)
# ────────────────────────────────────────────────────────────────────────────
# Video Constants
FPS = 24
WIDTH = 1920
HEIGHT = 1080
TIME_LIMIT = 12  # seconds
VIDEO_BITRATE = '2500k'
BUFFER_SIZE = 32

# Audio Processing Constants
HOP_LENGTH = 512
BASS_RANGE = 20
ENERGY_MULTIPLIER = 2
BASS_MULTIPLIER = 3
BASS_THRESHOLD = 0.4
BASS_COOLDOWN = 10

# Dot Movement Constants
BASE_VELOCITY = -2
BOOST_VELOCITY = -45    # Tăng tốc độ boost dọc để tạo hiệu ứng bắn lên
BOOST_DISTANCE = random.randint(350, 450)  # Random độ cao boost

# Dot Boost Angle Constants
CENTER_BOOST_ANGLE_MIN = 10  # Góc nhỏ hơn ở gần tâm
CENTER_BOOST_ANGLE_MAX = 20
MAX_BOOST_ANGLE_MIN = 25    # Góc vừa phải khi xa tâm
MAX_BOOST_ANGLE_MAX = 35

# Dot Rotation Constants
ROTATION_BASE = 0.3  # Tốc độ xoay cơ bản
ROTATION_AMPLITUDE = 0.2  # Biên độ dao động của xoay
ROTATION_SPEED = 2  # Tốc độ xoay của dot

# Trail Constants
TRAIL_FADE_DURATION = 3.0  # seconds
TRAIL_MAX_POINTS = 1

# Dot Management Constants
MIN_DOTS = 6  # Giảm số lượng dots tối thiểu (từ 8)
MAX_DOTS = 8  # Giảm số lượng dots tối đa (từ 12)
SPAWN_COOLDOWN = 10  # Tăng thời gian chờ giữa các lần spawn (từ 30)
MIN_X_DISTANCE = 200  # Tăng khoảng cách tối thiểu giữa các dots (từ 150)
MIN_HEIGHT_DIFF = 120  # Tăng độ chênh lệch độ cao tối thiểu (từ 80)
EDGE_THRESHOLD = WIDTH // 4
EDGE_MARGIN = WIDTH // 6

# Color System Constants
COLOR_SEQUENCE = [
    (0, 255, 255),     # Vàng neon rực rỡ
    (0, 255, 140),     # Vàng xanh neon
    (50, 255, 0),      # Xanh lá neon sáng
    (130, 255, 0),     # Xanh lá ngọc
    (255, 100, 0),     # Xanh dương neon
    (255, 0, 140),     # Xanh tím neon
    (255, 0, 255),     # Hồng tím neon
    (150, 0, 255),     # Tím neon
    (0, 0, 255),       # Đỏ neon
    (0, 140, 255),     # Cam neon
]

# System Detection
IS_MAC = platform.system() == 'Darwin'

# Thêm constants mới cho góc chéo
EDGE_BOOST_MULTIPLIER_MIN = 3.5  # Góc chéo lớn cho dots ở rìa
EDGE_BOOST_MULTIPLIER_MAX = 4.0
CENTER_BOOST_MULTIPLIER_MIN = 2.0  # Góc chéo nhỏ cho dots ở giữa
CENTER_BOOST_MULTIPLIER_MAX = 3.5

class ColorSystem:
    def __init__(self):
        self.color_sequence = COLOR_SEQUENCE
        self.current_index = 0
        self.transition_progress = 0
        self.transition_speed = 0.008
        self.current_color = list(self.color_sequence[0])
        
    def update(self):
        current_color = self.color_sequence[self.current_index]
        next_index = (self.current_index + 1) % len(self.color_sequence)
        next_color = self.color_sequence[next_index]
        
        # Chuyển màu mượt mà với hàm sin
        t = math.sin(self.transition_progress * math.pi / 2)  # Tạo chuyển động mượt
        
        # Cập nhật từng kênh màu
        for i in range(3):
            self.current_color[i] = int(current_color[i] * (1 - t) + next_color[i] * t)
            
        self.transition_progress += self.transition_speed
        
        # Chuyển sang màu tiếp theo
        if self.transition_progress >= 1:
            self.transition_progress = 0
            self.current_index = next_index
            self.current_color = list(self.color_sequence[self.current_index])
        
        # Tăng cường độ sáng cho hiệu ứng neon
        glow_factor = 1.3 + math.sin(self.transition_progress * math.pi) * 0.1  # Thêm hiệu ứng nhấp nháy nhẹ
        return tuple(min(255, int(c * glow_factor)) for c in self.current_color)
        
    def get_color(self):
        return tuple(int(c) for c in self.current_color)

    @staticmethod
    def draw_glow_effect(canvas, shape_func, params, color, alpha, num_glows=8, energy=0, pulse_phase=0):
        canvas = np.array(canvas, dtype=np.uint8)
        if 'thickness' in params:
            del params['thickness']

        # ── CPU: draw base overlay & all glow overlays ───────────────────────
        base_overlay = np.zeros_like(canvas)
        if shape_func == cv2.circle:
            cv2.circle(base_overlay, params['center'], params['radius'], color, -1)
        else:
            shape_func(base_overlay, **params)

        # Pre-draw all glow layers on CPU (cheap primitive ops)
        glow_layers: list[tuple[np.ndarray, float, tuple]] = []  # (array, blend_alpha, ksize)
        blur_ksize_circle = (5, 5)
        blur_ksize_shape  = (3, 3)
        for glow_size in range(num_glows - 1, -1, -1):
            g_alpha = alpha * (0.2 - glow_size * 0.02)
            ov = np.zeros_like(canvas)
            gp = params.copy()
            if 'radius' in gp:
                gp['radius'] = int(gp['radius'] * 1.05 + glow_size * 2)
                cv2.circle(ov, gp['center'], gp['radius'], color, -1)
                ksize = blur_ksize_circle
            elif 'size' in gp:
                gp['size'] = int(gp['size'] + glow_size * 2)
                if not params.get('is_filled', True):
                    gp['is_filled'] = False
                shape_func(ov, **gp)
                ksize = blur_ksize_shape
            else:
                ksize = blur_ksize_shape
            glow_layers.append((ov, g_alpha, ksize))

        # Circle-only: pre-draw the 4 large diamond glow shapes on CPU
        circle_layers: list[tuple[np.ndarray, float, tuple]] = []
        if shape_func == cv2.circle:
            rect_width  = params['radius'] * 3
            rect_height = params['radius'] * 0.25
            y1 = params['center'][1] - rect_height // 2
            y2 = y1 + rect_height
            base_extend  = params['radius'] * (1 + energy * 2)
            base_color   = np.array(color, dtype=np.float32)
            light_color  = np.minimum(base_color * 1.5, 255)
            cx = params['center'][0]

            left_extend  = base_extend * (2 + math.sin(pulse_phase) * 0.8)
            right_extend = base_extend * (2 + math.sin(pulse_phase + math.pi) * 0.8)
            outer_glow = np.zeros_like(canvas)
            pts = np.array([[cx - rect_width - left_extend,  (y1+y2)//2],
                            [cx,                              y1 - rect_height*4],
                            [cx + rect_width + right_extend, (y1+y2)//2],
                            [cx,                              y2 + rect_height*4]], np.int32)
            cv2.fillPoly(outer_glow, [pts], tuple(map(int, base_color)))
            circle_layers.append((outer_glow, alpha * 0.3, (351, 351)))

            ml = base_extend * (1.5 + math.sin(pulse_phase + math.pi/4) * 0.6)
            mr = base_extend * (1.5 + math.sin(pulse_phase + math.pi + math.pi/4) * 0.6)
            middle_glow = np.zeros_like(canvas)
            pts_m = np.array([[cx - rect_width - ml,  (y1+y2)//2],
                              [cx,                     y1 - rect_height*3],
                              [cx + rect_width + mr,   (y1+y2)//2],
                              [cx,                     y2 + rect_height*3]], np.int32)
            cv2.fillPoly(middle_glow, [pts_m], tuple(map(int, light_color)))
            circle_layers.append((middle_glow, alpha * 0.5, (251, 251)))

            il = base_extend * (1.2 + math.sin(pulse_phase + math.pi/2) * 0.4)
            ir = base_extend * (1.2 + math.sin(pulse_phase + math.pi + math.pi/2) * 0.4)
            inner_glow = np.zeros_like(canvas)
            pts_i = np.array([[cx - rect_width - il,  (y1+y2)//2],
                              [cx,                     y1 - rect_height*2],
                              [cx + rect_width + ir,   (y1+y2)//2],
                              [cx,                     y2 + rect_height*2]], np.int32)
            cv2.fillPoly(inner_glow, [pts_i], tuple(map(int, light_color)))
            circle_layers.append((inner_glow, alpha * 0.6, (201, 201)))

            voff      = math.sin(pulse_phase) * rect_height * 3
            highlight = np.zeros_like(canvas)
            cv2.rectangle(highlight,
                          (int(cx - rect_width - base_extend), int(y1 + voff - rect_height*0.3)),
                          (int(cx + rect_width + base_extend), int(y1 + voff + rect_height*0.3)),
                          tuple(map(int, light_color)), -1)
            circle_layers.append((highlight, alpha * 0.8 * (1 + energy * 0.5), (151, 11)))

        # ── GPU: apply all blurs + blends in one GPU session (cuFFT) ─────────
        if _CUPY:
            result_gpu = cp.asarray(canvas, dtype=cp.float32)

            for ov, g_alpha, (kw, kh) in glow_layers:
                ov_gpu  = cp.asarray(ov, dtype=cp.float32)
                blurred = _fft_blur_gpu(ov_gpu, _ks2s(kh), _ks2s(kw))
                result_gpu = cp.clip(result_gpu + blurred * g_alpha, 0, 255)

            for ov, g_alpha, (kw, kh) in circle_layers:
                ov_gpu  = cp.asarray(ov, dtype=cp.float32)
                blurred = _fft_blur_gpu(ov_gpu, _ks2s(kh), _ks2s(kw))
                result_gpu = cp.clip(result_gpu + blurred * g_alpha, 0, 255)

            base_gpu   = cp.asarray(base_overlay, dtype=cp.float32)
            result_gpu = cp.clip(result_gpu + base_gpu * (alpha * 0.1), 0, 255)
            return cp.asnumpy(result_gpu).astype(np.uint8)

        # ── CPU fallback ─────────────────────────────────────────────────────
        result = canvas.copy()
        for ov, g_alpha, ksize in glow_layers:
            ov = cv2.GaussianBlur(ov, ksize, 0)
            result = cv2.addWeighted(result, 1, ov, g_alpha, 0)
        for ov, g_alpha, ksize in circle_layers:
            ov = cv2.GaussianBlur(ov, ksize, 0)
            result = cv2.addWeighted(result, 1, ov, g_alpha, 0)
        result = cv2.addWeighted(result, 1, base_overlay, alpha * 0.1, 0)
        return result

class Particle:
    def __init__(self, pos, color, velocity, life=1.0, size=3):
        self.pos = list(pos)
        self.original_color = color
        self.color = list(color)
        self.velocity = velocity
        self.life = life
        self.original_life = life
        self.size = size
        self.original_size = size  # Lưu kích thước ban đầu để scale tốt hơn

    def update(self):
        # Cập nhật vị trí mượt hơn
        self.pos[0] += self.velocity[0]
        self.pos[1] += self.velocity[1]
        
        # Độ mờ dần mượt hơn
        self.life -= 0.02  # Tăng tốc độ mờ dần
        alpha = (self.life / self.original_life)  # Độ trong suốt tuyến tính
        self.color = [min(255, c * alpha) for c in self.original_color]
        
        # Kích thước giảm dần tuyến tính
        self.size = self.original_size * (self.life / self.original_life)
        
        return self.life > 0

class ParticleSystem:
    def __init__(self):
        self.particles = []
        self.last_pos = None

    def emit(self, pos, color, strength=1.0):
        if self.last_pos is None:
            self.last_pos = pos
            return

        # Tính khoảng cách di chuyển
        distance = ((pos[0] - self.last_pos[0])**2 + (pos[1] - self.last_pos[1])**2)**0.5
        num_particles = min(50, max(20, int(distance / 3)))  # Tăng số lượng particles

        # Tạo các particles dọc theo đường di chuyển
        for i in range(num_particles):
            # Interpolate vị trí giữa last_pos và pos
            t = i / num_particles
            x = int(self.last_pos[0] + (pos[0] - self.last_pos[0]) * t)
            y = int(self.last_pos[1] + (pos[1] - self.last_pos[1]) * t)
            
            # Thêm một chút random để tạo hiệu ứng tự nhiên hơn
            x += random.randint(-10, 10)
            y += random.randint(-10, 10)

            # Độ trong suốt và kích thước giảm dần theo khoảng cách
            alpha = 1.0 - (i / num_particles) * 0.7
            size = max(5, 40 - (i * 0.8))  # Kích thước lớn hơn và giảm chậm hơn

            # Tạo màu sáng hơn cho particles
            bright_color = tuple(min(255, int(c * 1.5)) for c in color)
            
            # Tạo particle với velocity ngẫu nhiên
            velocity = [random.uniform(-1, 1), random.uniform(-1, 1)]
            
            self.particles.append(Particle(
                (x, y),
                bright_color,
                velocity,
                life=alpha * 1.5,  # Tăng thời gian sống
                size=size
            ))

        self.last_pos = pos

    def update_and_draw(self, canvas):
        # Tạo canvas riêng cho particles
        particle_canvas = np.zeros_like(canvas)
        
        # Cập nhật và vẽ từng particle
        active_particles = []
        for particle in self.particles:
            if particle.update():  # Cập nhật vị trí và độ sống
                pos = tuple(map(int, particle.pos))
                color = tuple(map(int, particle.color))
                size = int(particle.size)

                # Vẽ glow effect
                glow_size = size * 2
                glow_color = tuple(min(255, int(c * 0.8)) for c in color)
                cv2.circle(particle_canvas, pos, glow_size, glow_color, -1)
                cv2.circle(particle_canvas, pos, size, color, -1)
                
                active_particles.append(particle)

        self.particles = active_particles

        # Áp dụng blur mạnh hơn cho hiệu ứng mềm mại
        particle_canvas = gpu_blur(particle_canvas, (31, 31))

        # Kết hợp với canvas chính
        result = gpu_add_weighted(canvas, 1, particle_canvas, 1)
        return result
    
class Trail:
    def __init__(self, max_points=TRAIL_MAX_POINTS):
        self.points = []
        self.max_points = max_points
        self.should_fade = False
        self.fade_start_time = None
        self.fade_duration = TRAIL_FADE_DURATION
        self.is_fading = False
        self.alpha = 255
        self.current_pos = None
        self.boost_start_y = None
        self.last_pos = None
        self.color = None
        self.trail_segments = []

    def is_fully_faded(self):
        if not self.should_fade:
            return False
        if self.alpha <= 0:
            return True
        return False

    def start_trail(self, pos):
        self.current_pos = pos
        self.boost_start_y = pos[1]
        self.is_moving = True
        self.should_fade = False
        self.fade_counter = 0
        self.alpha = 1.0
        self.last_pos = pos
        self.trail_segments = [(pos, 1.0)]  # Khởi tạo segment đầu tiên

    def add_point(self, pos, color, should_fade=False):
        self.current_pos = pos
        self.color = color
        
        if self.last_pos:
            # Thêm segment mới vào trail
            self.trail_segments.append((pos, 1.0))
            # Giới hạn số lượng segment để tránh trail quá dài
            if len(self.trail_segments) > 30:
                self.trail_segments = self.trail_segments[-30:]
        
        self.last_pos = pos

    def draw(self, canvas):
        if self.current_pos is None or self.boost_start_y is None:
            return canvas

        if self.should_fade:
            if self.fade_counter < self.fade_duration:
                self.fade_counter += 1
                self.alpha = 1.0 - (self.fade_counter / self.fade_duration)
            else:
                self.alpha = 0
                self.boost_start_y = None
                self.is_moving = False
                return canvas

        glow_canvas = np.zeros_like(canvas)
        
        # Tính toán độ dài trail theo chiều dọc
        vertical_length = abs(self.boost_start_y - self.current_pos[1])
        
        # Tính toán độ lệch ngang của trail
        start_x = self.trail_segments[0][0][0] if self.trail_segments else self.current_pos[0]
        horizontal_offset = self.current_pos[0] - start_x
        
        num_segments = 30
        for i in range(num_segments):
            t = i / num_segments
            
            # Tính toán vị trí theo đường chéo
            y = int(self.current_pos[1] + t * vertical_length)
            x = int(self.current_pos[0] - t * horizontal_offset)  # Đi ngược lại để vẽ trail
            
            # Điều chỉnh size và alpha theo khoảng cách
            size = int(15 * (1 - t * 0.6))
            current_alpha = self.alpha * (1 - t * 0.4)
            
            # Vẽ điểm chính
            cv2.circle(glow_canvas,
                      (x, y),
                      size,
                      tuple(int(c * current_alpha) for c in self.color),
                      -1)
            
            # Vẽ glow
            glow_size = int(size * 1.5)
            glow_color = tuple(int(min(255, c * 1.5 * current_alpha)) for c in self.color)
            cv2.circle(glow_canvas,
                      (x, y),
                      glow_size,
                      glow_color,
                      -1)

        glow_canvas = gpu_blur(glow_canvas, (21, 21))
        canvas = gpu_add_weighted(canvas, 1, glow_canvas, 0.6)

        return canvas

class AudioDot:
    def __init__(self, pos, velocity_x=0):
        self.center = pos
        self.radius = 100
        self.rotation_angle = 0
        self.rotation_speed = 2
        self.pulse_phase = 0
        self.frame_count = 0
        self.is_filled = True
        self.particle_system = ParticleSystem()
        
        # Physics parameters
        self.y_pos = pos[1]
        self.x_pos = pos[0]
        self.base_y = pos[1]
        self.velocity_y = BASE_VELOCITY
        self.velocity_x = velocity_x
        self.constant_speed = BASE_VELOCITY
        self.boost_velocity = BOOST_VELOCITY
        self.boost_end_velocity = BASE_VELOCITY
        
        # Bass detection
        self.bass_energy = 0
        self.smooth_bass = 0
        self.bass_threshold = BASS_THRESHOLD
        self.last_boost_frame = 0
        self.bass_cooldown = BASS_COOLDOWN
        self.trail = Trail(max_points=1)
        self.is_boosting = False
        self.start_boost_y = None
        self.boost_distance = BOOST_DISTANCE
        self.boost_target_y = None
        self.boost_direction = 0  # Lưu hướng boost
        
        self.trail = Trail(max_points=1)
        self.glow_alpha = 0.0
        self.color_system = ColorSystem()  # Create instance here

    @staticmethod
    def draw_triangle(img, center, size, rotation, color, is_filled=True):
        """
        Vẽ tam giác với các tham số đã cho
        """
        angle = np.radians(rotation)
        points = []
        for i in range(3):
            point_angle = angle + (i * 2 * np.pi / 3)
            x = int(center[0] + size * np.cos(point_angle))
            y = int(center[1] + size * np.sin(point_angle))
            points.append([x, y])
        points = np.array([points], dtype=np.int32)
        
        if is_filled:
            cv2.fillPoly(img, points, color, lineType=cv2.LINE_AA)
        else:
            cv2.polylines(img, points, True, color, 2, lineType=cv2.LINE_AA)
        return img, points

    def update(self, energy, frame):
        frames_since_last_boost = frame - self.last_boost_frame
        
        self.smooth_bass = 0.7 * self.smooth_bass + 0.3 * self.bass_energy
        
        # Xử lý bass boost với hướng chéo
        if frames_since_last_boost > self.bass_cooldown:
            if self.bass_energy > self.bass_threshold and not self.is_boosting:
                center_x = WIDTH // 2
                distance_from_center = abs(self.x_pos - center_x)
                
                # Điều chỉnh công thức tính góc để tạo hiệu ứng toả ra
                normalized_distance = (distance_from_center / (WIDTH / 2.5)) ** 0.8
                
                # Tính range góc boost dựa trên khoảng cách
                min_angle = CENTER_BOOST_ANGLE_MIN + (MAX_BOOST_ANGLE_MIN - CENTER_BOOST_ANGLE_MIN) * normalized_distance
                max_angle = CENTER_BOOST_ANGLE_MAX + (MAX_BOOST_ANGLE_MAX - CENTER_BOOST_ANGLE_MAX) * normalized_distance
                
                # Random góc trong range đã tính
                boost_angle = random.uniform(min_angle, max_angle)
                
                # Xác định hướng boost dựa vào vị trí
                horizontal_boost = -boost_angle if self.x_pos < center_x else boost_angle
                
                self.velocity_x = horizontal_boost
                self.velocity_y = self.boost_velocity
                self.boost_target_y = self.y_pos - self.boost_distance
                self.boost_direction = -1 if self.x_pos < center_x else 1
                
                self.is_boosting = True
                self.last_boost_frame = frame
                self.trail = Trail(max_points=1)
                self.trail.start_trail((self.x_pos, self.y_pos))

        # Cập nhật vị trí
        if self.is_boosting:
            # Trong quá trình boost, duy trì chuyển động chéo
            self.x_pos += self.velocity_x
            self.y_pos += self.velocity_y
        elif not self.trail or (self.trail and self.trail.is_fully_faded()):
            time_factor = frame * 0.02
            outward_rotation = self.boost_direction * (ROTATION_BASE + math.sin(time_factor) * ROTATION_AMPLITUDE)
            self.x_pos += outward_rotation
            self.y_pos += self.velocity_y
        else:
            self.y_pos += self.velocity_y
        
        # Cập nhật trail
        if self.is_boosting:
            dot_color = self.color_system.get_color()
            self.trail.add_point((self.x_pos, self.y_pos), dot_color)
        
        # Kiểm tra kết thúc boost
        if self.is_boosting and self.boost_target_y is not None:
            if self.y_pos <= self.boost_target_y:
                self.y_pos = self.boost_target_y
                self.velocity_y = self.constant_speed
                self.velocity_x = 0
                self.is_boosting = False
                self.boost_target_y = None
                if self.trail:
                    self.trail.should_fade = True
        
        # Bỏ giới hạn EDGE_MARGIN, để dot tự do di chuyển
        
        self.center = (int(self.x_pos), int(self.y_pos))
        self.rotation_angle -= self.rotation_speed
        self.rotation_angle %= 360
        
        self.frame_count += 1
        return True

    def draw(self, canvas, energy, frame):
        # Vẽ trail trước
        canvas = self.trail.draw(canvas)

        # Vẽ particle system trước
        canvas = self.particle_system.update_and_draw(canvas)
        
        # Lấy màu từ GLOBAL_COLOR thay vì dùng màu cố định
        current_color = self.color_system.get_color()  # Use instance method
        
        # Giữ nguyên phần còn lại của draw
        triangle_size = int(25 * (1 + 0.3 * energy))
        base_circle_size = triangle_size * 0.65
        pulse_amount = base_circle_size * 0.03
        circle_radius = int(base_circle_size + pulse_amount * math.sin(self.pulse_phase))
        
        # Calculate alpha for circle
        transition_frame = self.frame_count % 60
        pre_fill_frames = 15
        fade_frames = 30
        
        if not self.is_filled and transition_frame < fade_frames:
            circle_alpha = 1 - (transition_frame / fade_frames)
        elif not self.is_filled and transition_frame >= (60 - pre_fill_frames):
            circle_alpha = (transition_frame - (60 - pre_fill_frames)) / pre_fill_frames
        else:
            circle_alpha = 1 if self.is_filled else 0
        
        # Draw circle with glow using current_color thay vì color cố định
        if circle_alpha > 0:
            circle_params = {
                'center': self.center,
                'radius': circle_radius
            }
            canvas = ColorSystem.draw_glow_effect(
                canvas=canvas,
                shape_func=cv2.circle,
                params=circle_params,
                color=current_color,  # Sử dụng màu từ GLOBAL_COLOR
                alpha=circle_alpha,
                energy=energy,
                pulse_phase=self.pulse_phase
            )
        
        # Draw triangle with glow using current_color
        triangle_params = {
            'center': self.center,
            'size': triangle_size,
            'rotation': self.rotation_angle,
            'color': current_color,  # Sử dụng màu từ GLOBAL_COLOR
            'is_filled': self.is_filled
        }
        
        # Calculate transition parameters
        transition_start, transition_end = 0.7, 0.9
        if circle_alpha <= transition_start:
            current_alpha, current_num_glows = 1.0, 8
        elif circle_alpha >= transition_end:
            current_alpha, current_num_glows = 0.25, 4
        else:
            t = (circle_alpha - transition_start) / (transition_end - transition_start)
            current_alpha = 1.0 - t * (1.0 - 0.25)
            current_num_glows = int(8 - t * (8 - 4))
        
        # Apply glow effect with current_color
        canvas = ColorSystem.draw_glow_effect(
            canvas,
            lambda img, **params: AudioDot.draw_triangle(img, **params)[0],
            triangle_params,
            current_color,  # Sử dụng màu từ GLOBAL_COLOR
            current_alpha,
            num_glows=current_num_glows
        )
        
        # Kiểm tra points có hợp lệ không
        if len(AudioDot.draw_triangle(canvas, **triangle_params)[1]) < 3:  # Tam giác cần ít nhất 3 điểm
            return canvas
            
        pts = np.array([AudioDot.draw_triangle(canvas, **triangle_params)[1]], np.int32)
        if pts.size == 0:  # Kiểm tra mảng không rỗng
            return canvas
            
        if circle_radius > 0:
            # Vẽ hình tròn và tam giác bình thường
            cv2.circle(canvas, self.center, int(circle_radius), current_color, -1)
            cv2.polylines(canvas, [pts], True, current_color, 2)
            self.glow_alpha = 0.0
        else:
            self.glow_alpha = min(0.8, self.glow_alpha + 0.2)
            
            cv2.polylines(canvas, [pts], True, current_color, 3)
            
            blur_frame = canvas.copy()
            cv2.polylines(blur_frame, [pts], True, current_color, 7)
            blur_frame = gpu_blur(blur_frame, (21, 21))
            canvas = gpu_add_weighted(canvas, 1, blur_frame, self.glow_alpha)
        
        return canvas

class DotManager:
    def __init__(self):
        self.dots = []
        self.min_dots = MIN_DOTS
        self.max_dots = MAX_DOTS
        self.bass_threshold = BASS_THRESHOLD
        self.spawn_cooldown = SPAWN_COOLDOWN
        self.last_spawn_frame = 0
        self.left_count = 0
        self.right_count = 0
        self.last_spawn_height = HEIGHT + 50
        self.last_spawn_x = None
        self.color_system = ColorSystem()  # Create instance if needed

    def spawn_new_dot(self):
        # Decide to spawn on the left or right to balance
        spawn_left = self.left_count <= self.right_count
        
        if random.random() < 0.1:
            spawn_left = not spawn_left
            
        if spawn_left:
            x = random.randint(WIDTH // 4, WIDTH // 2 - 100)
            self.left_count += 1
        else:
            x = random.randint(WIDTH // 2 + 100, 3 * WIDTH // 4)
            self.right_count += 1
            
        # Create a random height for the new dot
        min_height = HEIGHT + 50
        max_height = HEIGHT + 200
        
        # Ensure the new height is at least 50 pixels different from the last spawn height
        while True:
            y = random.randint(min_height, max_height)
            if abs(y - self.last_spawn_height) >= 50:
                break
        
        self.last_spawn_height = y  # Save this height for the next spawn
        
        # Calculate initial horizontal movement direction
        center_x = WIDTH // 2
        distance_from_center = abs(x - center_x)
        edge_threshold = WIDTH // 4
        
        if distance_from_center > edge_threshold:
            direction = -1 if x < center_x else 1
            horizontal_speed = random.uniform(0.8, 1.2)
        else:
            direction = -1 if x < center_x else 1
            horizontal_speed = random.uniform(0.3, 0.6)
            
        return AudioDot((x, y), horizontal_speed * direction)

    def update(self, current_bass, current_energy, frame_count):
        # Remove dots that have moved off the screen (above or sides)
        self.dots = [dot for dot in self.dots if 0 <= dot.x_pos <= WIDTH and dot.y_pos < HEIGHT + 50]

        # Ensure minimum number of dots when bass is above threshold
        if current_bass > self.bass_threshold or len(self.dots) < self.min_dots:
            while len(self.dots) < self.min_dots:
                self.dots.append(self.spawn_new_dot())
                self.last_spawn_frame = frame_count

            # Spawn additional dot if below max dots
            if len(self.dots) < self.max_dots:
                self.dots.append(self.spawn_new_dot())
                self.last_spawn_frame = frame_count

        # Update all dots
        for dot in self.dots:
            dot.bass_energy = current_bass
            dot.update(current_energy, frame_count)
    
    def draw(self, canvas, current_energy, frame_count):
        for dot in self.dots:
            canvas = dot.draw(canvas, current_energy, frame_count)
        return canvas

class AudioVisualizer:
    def __init__(self):
        self.FPS = FPS
        self.WIDTH = WIDTH
        self.HEIGHT = HEIGHT
        self.TIME_LIMIT = TIME_LIMIT
        self.GLOBAL_COLOR = ColorSystem()
        self.is_mac = IS_MAC
        self.buffer_size = BUFFER_SIZE

    def process_video(self, audio_file: str):
        start_time = time.time()  # Start timing
        print("Starting video processing...")
        
        print("Loading audio file:", audio_file)
        y, sr = librosa.load(audio_file)
        
        # Determine total duration based on TIME_LIMIT
        if self.TIME_LIMIT is None:
            total_duration = len(y) / sr  # Use the full audio duration
        else:
            total_duration = self.TIME_LIMIT

        # Calculate the number of samples to process
        num_samples = int(total_duration * sr)
        y = y[:num_samples]
        
        audio_load_time = time.time()
        print(f"Audio loaded in {audio_load_time - start_time:.2f} seconds")
        
        print("Calculating audio features...")
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP_LENGTH)
        tempo, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=HOP_LENGTH)
        
        spec = librosa.stft(y, hop_length=HOP_LENGTH)
        spec_mag = librosa.magphase(spec)[0]
        
        feature_time = time.time()
        print(f"Audio features calculated in {feature_time - audio_load_time:.2f} seconds")
        
        print("Pre-calculating energy values...")
        total_frames = int(total_duration * self.FPS)
        energy_array = np.zeros(total_frames)
        bass_array = np.zeros(total_frames)
        
        for frame in range(total_frames):
            onset_index = min(int(frame * len(onset_env) / total_frames), len(onset_env) - 1)
            energy_array[frame] = float(onset_env[onset_index])
            energy_array[frame] = np.clip(energy_array[frame] / np.max(onset_env) * 2, 0, 1)
            
            bass_range = spec_mag[:20, onset_index]
            bass_array[frame] = float(np.mean(bass_range))
            bass_array[frame] = np.clip(bass_array[frame] / np.max(spec_mag[:20]) * 3, 0, 1)

        energy_time = time.time()
        print(f"Energy values calculated in {energy_time - feature_time:.2f} seconds")
        
        dot_manager = DotManager()
        for _ in range(dot_manager.min_dots):
            dot_manager.dots.append(dot_manager.spawn_new_dot())
            
        try:
            # Tạo mảng để lưu tất cả frames
            all_frames = []
            print("Rendering frames...")
            render_start = time.time()
            last_progress = 0
            
            for frame_count in range(total_frames):
                # Print progress every 10%
                progress = (frame_count / total_frames) * 100
                if int(progress) // 10 > last_progress:
                    current_time = time.time()
                    elapsed = current_time - render_start
                    fps = frame_count / elapsed if elapsed > 0 else 0
                    eta = (total_frames - frame_count) / fps if fps > 0 else 0
                    print(f"Progress: {progress:.1f}% | FPS: {fps:.1f} | ETA: {eta:.1f}s")
                    last_progress = int(progress) // 10
                
                self.GLOBAL_COLOR.update()
                canvas = np.zeros((self.HEIGHT, self.WIDTH, 3), dtype=np.uint8)
                
                current_energy = energy_array[frame_count]
                current_bass = bass_array[frame_count]
                
                dot_manager.update(current_bass, current_energy, frame_count)
                canvas = dot_manager.draw(canvas, current_energy, frame_count)
                
                all_frames.append(canvas)
            
            render_time = time.time()
            print(f"\nFrame rendering completed in {render_time - render_start:.2f} seconds")
            print(f"Average FPS: {total_frames / (render_time - render_start):.1f}")
            
            temp_video = 'temp_output.mp4'
            print("Writing frames to video file (NVENC)..." if (not self.is_mac and _CUPY)
                  else "Writing frames to video file...")
            write_start = time.time()

            if self.is_mac:
                # Mac: VideoToolbox via cv2
                fourcc = cv2.VideoWriter_fourcc(*'avc1')
                out = cv2.VideoWriter(temp_video, fourcc, self.FPS, (self.WIDTH, self.HEIGHT))
                if not out.isOpened():
                    print("Error: VideoWriter không mở được")
                    return
                for frame in all_frames:
                    out.write(frame)
                out.release()
            else:
                # Windows: pipe raw BGR frames → ffmpeg → h264_nvenc
                import subprocess, shlex
                vcodec = 'h264_nvenc' if _CUPY else 'libx264'
                cmd = (f'ffmpeg -y -f rawvideo -vcodec rawvideo -pix_fmt bgr24 '
                       f'-s {self.WIDTH}x{self.HEIGHT} -r {self.FPS} -i pipe:0 '
                       f'-vcodec {vcodec} -preset {"p4" if vcodec=="h264_nvenc" else "fast"} '
                       f'-b:v 2500k "{temp_video}"')
                proc = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for frame in all_frames:
                    proc.stdin.write(frame.tobytes())
                proc.stdin.close()
                proc.wait()

            write_time = time.time()
            print(f"Frames written in {write_time - write_start:.2f} seconds")
            
            total_time = time.time() - start_time
            print(f"\nTotal processing time: {total_time:.2f} seconds")
            print(f"Breakdown:")
            print(f"- Audio loading: {audio_load_time - start_time:.2f}s")
            print(f"- Feature extraction: {feature_time - audio_load_time:.2f}s")
            print(f"- Energy calculation: {energy_time - feature_time:.2f}s")
            print(f"- Frame rendering: {render_time - render_start:.2f}s")
            print(f"- Video writing: {write_time - write_start:.2f}s")
            print(f"- Finalization: {time.time() - write_time:.2f}s")
            
            return temp_video
            
        except Exception as e:
            print(f"Error occurred after {time.time() - start_time:.2f} seconds: {str(e)}")
            if 'out' in locals():
                out.release()
            return None

    def merge_audio(self, temp_video: str, audio_file: str, output_filename: str = 'final_output.mp4'):
        print("\nMerging audio...")
        merge_start = time.time()
        try:
            # Kiểm tra hệ điều hành
            if platform.system() == 'Linux':
                import subprocess
                
                # Tạo command cho ffmpeg trên Linux
                command = [
                    'ffmpeg',
                    '-i', temp_video,
                    '-i', audio_file,
                    '-c:v', 'libx264',
                    '-c:a', 'aac',
                    '-preset', 'fast',
                    '-b:v', '2500k',
                    '-t', str(self.TIME_LIMIT),
                    '-y',  # Overwrite output file if exists
                    output_filename
                ]
                
                # Chạy command
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = process.communicate()
                
                if process.returncode != 0:
                    print(f"FFmpeg error: {stderr.decode()}")
                    return False
            else:
                # Sử dụng ffmpeg-python cho Mac và Windows
                input_video = ffmpeg.input(temp_video)
                input_audio = ffmpeg.input(audio_file, ss=0)
                
                # Sử dụng VideoToolbox trên Mac
                if self.is_mac:
                    stream = ffmpeg.output(input_video, input_audio, 
                                        output_filename,
                                        acodec='aac',
                                        vcodec='h264_videotoolbox',  # Sử dụng VideoToolbox encoder
                                        preset='fast',
                                        video_bitrate='2500k',  # Thêm bitrate để cải thiện chất lượng
                                        t=self.TIME_LIMIT)
                else:
                    stream = ffmpeg.output(input_video, input_audio,
                                        output_filename,
                                        acodec='aac',
                                        vcodec='h264_nvenc',
                                        preset='p4',
                                        video_bitrate='2500k',
                                        t=self.TIME_LIMIT)
                
                stream = stream.overwrite_output()
                ffmpeg.run(stream, capture_stdout=True, capture_stderr=True)
            
            # Xóa file tạm sau khi merge xong
            import os
            if os.path.exists(temp_video):
                os.remove(temp_video)
            
            print(f"Audio merged in {time.time() - merge_start:.2f} seconds")
            print(f"Video exported to: {output_filename}")
            return True
            
        except Exception as e:
            print(f'Error during audio merging after {time.time() - merge_start:.2f} seconds:')
            print(str(e))
            return False

    @staticmethod
    def animate_single_dot(audio_file: str, audio: int):
        try:
            visualizer = AudioVisualizer()
            temp_video = visualizer.process_video(audio_file)
            if audio and temp_video:
                visualizer.merge_audio(temp_video, audio_file)
        except Exception as e:
            print(f"Main error: {str(e)}")

def parse_arguments():
    parser = argparse.ArgumentParser(description="Audio Visualizer")
    parser.add_argument('-W', '--width', type=int, default=1920, help='Width (in pixels) of the video')
    parser.add_argument('-H', '--height', type=int, default=1080, help='Height (in pixels) of the video')
    parser.add_argument('-i', '--input', type=str, required=True, help='Path to the input audio file')
    parser.add_argument('-o', '--output', type=str, required=True, help='Path to the output images folder')
    parser.add_argument('-d', '--duration', type=int, help='Duration (in seconds) of the video. If not specified, export the entire video.')
    parser.add_argument('--mindot', type=int, default=MIN_DOTS, help='Minimum number of dots')
    parser.add_argument('--maxdot', type=int, default=MAX_DOTS, help='Maximum number of dots')
    parser.add_argument('-t', '--token', type=str, help='Token')
    parser.add_argument('-u', '--url', type=str, help='URL')
    parser.add_argument('-a', '--audio', type=int, help='Audio')
    return parser.parse_args()

if __name__ == "__main__":
    try:
        args = parse_arguments()

        if args.token:
            isAuth = authourize_user(args.token, args.url)
            if not isAuth:
                sys.exit(1)
        else:
            print("Failed to authenticate the user. Please provide a valid token.")
            sys.exit(1)

        audio_file = args.input
        output_folder = args.output
        WIDTH = args.width
        HEIGHT = args.height
        TIME_LIMIT = args.duration if args.duration else None  # Use provided duration or None for full length
        MIN_DOTS = args.mindot
        MAX_DOTS = args.maxdot
        AUDIO = args.audio
        AudioVisualizer.animate_single_dot(audio_file, AUDIO)
    except Exception as e:
        print(f"Main error: {str(e)}")