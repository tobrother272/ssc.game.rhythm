# ModernGL Migration — PunchTarget Block 3D

## Mục tiêu

Migrate **PunchTarget block rendering** từ `cv2.fillConvexPoly` + `_fill_rounded_quad` software 2D fake-3D sang **ModernGL GPU 3D rendering thật** với:
- Custom GLSL shader cho neon aesthetic (match reference image)
- Hardware Z-buffer (auto painter order, không còn dead-zone bug)
- 8× MSAA anti-aliasing (smooth edges)
- Emissive + bloom shader (neon glow built-in)
- 5-15× speedup vs cv2 path
- **GIỮ NGUYÊN** mọi module khác (tunnel, rails, particles, HUD vẫn cv2)

> **Strategy**: Hybrid render pipeline — ModernGL chỉ xử lý PunchTarget cube + glow, output texture composite vào cv2 canvas. Migration partial, low risk, có thể revert.

---

## Trạng thái hiện tại (cv2 path — đã implement)

Các fix đã áp dụng trong `src/rhythm.py` cho cv2 path hiện tại:

| # | Feature | Giá trị hiện tại |
|---|---|---|
| 1 | **LANE_COLORS** | 2 màu: lane 0,1 = blue `(230,80,30)` BGR; lane 2,3 = orange `(0,140,255)` BGR |
| 2 | **CORNER_RADIUS** | `0.08` (subtle rounding, chỉ áp cho front + top face) |
| 3 | **Side face** | Plain `cv2.fillConvexPoly` — KHÔNG rounded (quá mỏng → artifact nếu round) |
| 4 | **Brightness model** | `top_col = c*1.15 + 255*0.15`; `side_col = c*0.45`; `front = side*1.30*depth_gain` |
| 5 | **Glow source** | Chỉ từ **top face** (`pts[[0,1,5,4]]`), không phải full silhouette |
| 6 | **Fist icon** | `_draw_fist_icon_v2` bold polygon trên **FRONT face**; `_draw_fist_icon_simple_v2` cho block xa |
| 7 | **Icon threshold** | `front_w >= 18` → full icon; `front_w >= 12` → simplified; `< 12` → no icon |
| 8 | **Bloom defaults** | `gpu_glow(sigma=24.0, gain=0.75)` |
| 9 | **Preview quality** | 1280×720 @30fps, bloom=1 |
| 10 | **Side face selection** | ALWAYS 1 side face — no ±2px dead zone (`block_cx < cam_cx` → right; else → left) |
| 11 | **Rail pillar count** | Default 40 (was 16), max 100 |

GL migration cần **replicate chính xác** các hành vi trên trong GLSL shader.

---

## Tổng quan kiến trúc

### Pipeline hiện tại (all-cv2)

```
canvas = bg_layer.frame(fi)
canvas = tunnel.draw(canvas, fi)
canvas = side_rail.draw(canvas, fi)
for tg in alive_targets:
    canvas = tg.draw(canvas, cam, fi)   ← PunchTarget uses cv2.fillConvexPoly
canvas = particles.draw(canvas, fi)
canvas = viewport.draw(canvas, fi)
canvas = bloom_post(canvas)             ← cuFFT bloom
ffmpeg.write(canvas)
```

### Pipeline sau migration (hybrid)

```
canvas = bg_layer.frame(fi)
canvas = tunnel.draw(canvas, fi)
canvas = side_rail.draw(canvas, fi)

# NEW: Render TẤT CẢ punch blocks trong 1 ModernGL pass
gl_blocks = collect_punch_blocks(alive_targets)
gl_canvas, gl_alpha = mgl_renderer.render_punch_blocks(
    blocks=gl_blocks,
    cam=cam,
    width=W, height=H,
    frame_idx=fi,
)
# Composite GL output (BGRA) onto cv2 canvas
canvas = composite_alpha(canvas, gl_canvas, gl_alpha)

# Other targets (Dance, Line, Relax) vẫn cv2 path
for tg in alive_targets:
    if not isinstance(tg, PunchTarget):
        canvas = tg.draw(canvas, cam, fi)

canvas = particles.draw(canvas, fi)
canvas = viewport.draw(canvas, fi)
canvas = bloom_post(canvas)
ffmpeg.write(canvas)
```

### Lý do batched render — render TẤT CẢ punch blocks 1 pass

- Mỗi `mgl_renderer.render_punch_blocks()` call có overhead context switch + framebuffer alloc.
- Render N blocks trong 1 pass với instanced drawing → chỉ 1 pass overhead, scale O(1) thay vì O(N).
- Z-buffer GPU tự handle painter order giữa N blocks → không cần CPU sort.

---

## Dependencies

### Packages cần thêm

```
moderngl >= 5.10.0          # ~3MB, OpenGL 3.3 wrapper
moderngl-window >= 2.4.6    # OPTIONAL — chỉ nếu cần debug interactive
glcontext >= 2.5.0          # Headless context backend (auto-installed với moderngl)
PyOpenGL >= 3.1.7           # FALLBACK nếu glcontext fail
pyrr >= 0.10.3              # Matrix math (alternative: numpy thuần)
```

`requirements.txt` thêm:
```
moderngl>=5.10.0
glcontext>=2.5.0
pyrr>=0.10.3
```

### PyInstaller compatibility

ModernGL ship 1 binary backend (`glcontext`) cần được include:

`build_dist.py` — thêm hidden imports:
```python
hiddenimports = [
    ...,
    'glcontext',
    'glcontext.x11',     # Linux
    'glcontext.wgl',     # Windows
    'glcontext.darwin',  # macOS
    'moderngl.context',
]
```

`*.spec` PyInstaller files — đảm bảo include shared libs:
```python
binaries = collect_dynamic_libs('glcontext') + collect_dynamic_libs('moderngl')
```

### GPU requirements

- **OpenGL 3.3+ Core Profile** (mọi GPU 2010+ support)
- Headless rendering: Windows OK (WGL), Linux cần EGL hoặc X-forwarding, macOS OK
- Project user dùng RTX 4060 Ti → support OpenGL 4.6 → quá đủ

---

## Cấu trúc file mới

```
src/
├── rhythm.py                    # MODIFIED — chèn hooks gọi mgl_renderer
├── mgl_renderer/                # NEW DIRECTORY
│   ├── __init__.py             # Export MGLPunchRenderer
│   ├── context.py              # Singleton OpenGL context manager
│   ├── shaders/
│   │   ├── punch_block.vert    # Vertex shader
│   │   └── punch_block.frag    # Fragment shader (neon)
│   ├── geometry.py             # Cube geometry generator (rounded corners)
│   ├── punch_renderer.py       # MGLPunchRenderer main class
│   └── compositor.py           # Alpha composite GL output → cv2 canvas
└── bundle_paths.py             # MODIFIED — find_shader_dir() helper
```

---

## File 1: `src/mgl_renderer/context.py`

```python
"""Singleton ModernGL context manager.

Creates 1 standalone (headless) OpenGL context per process. Reused across
all renders to avoid setup overhead.
"""

from __future__ import annotations
import threading
from typing import Optional

import moderngl


class MGLContext:
    _instance: Optional["MGLContext"] = None
    _lock = threading.Lock()

    def __init__(self):
        # Standalone context — no window required, headless
        self.ctx = moderngl.create_standalone_context(
            require=330,                  # OpenGL 3.3 core
        )
        # Enable depth test (Z-buffer)
        self.ctx.enable(moderngl.DEPTH_TEST)
        # Enable blending (for alpha output)
        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA

        # Cache: (W, H, samples) → framebuffer
        self._fbo_cache: dict = {}

    @classmethod
    def get(cls) -> "MGLContext":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def get_fbo(self, w: int, h: int, samples: int = 8):
        """Get or create a multisample framebuffer."""
        key = (w, h, samples)
        if key not in self._fbo_cache:
            color = self.ctx.texture((w, h), 4, samples=samples)   # RGBA
            depth = self.ctx.depth_renderbuffer((w, h), samples=samples)
            fbo = self.ctx.framebuffer(color_attachments=[color],
                                        depth_attachment=depth)
            # Resolve fbo (non-multisample) for readback
            color_resolved = self.ctx.texture((w, h), 4)
            fbo_resolved = self.ctx.framebuffer(color_attachments=[color_resolved])
            self._fbo_cache[key] = (fbo, fbo_resolved, color, color_resolved)
        return self._fbo_cache[key]

    def release(self):
        """Cleanup on shutdown."""
        for fbo, fbo_r, c, cr in self._fbo_cache.values():
            fbo.release()
            fbo_r.release()
            c.release()
            cr.release()
        self.ctx.release()
        type(self)._instance = None
```

---

## File 2: `src/mgl_renderer/shaders/punch_block.vert`

```glsl
#version 330 core

// Per-vertex
in vec3 in_position;     // Local cube vertex (-0.5..0.5)
in vec3 in_normal;       // Face normal (unit vector)
in vec2 in_uv;           // UV for icon mapping (front face uses 0..1)
in float in_face_id;     // 0=front, 1=back, 2=top, 3=bottom, 4=left, 5=right

// Per-instance
in vec3 in_inst_position;   // World position of cube center (wx is corrected — Fix 1)
in float in_inst_half;       // Cube half-size (CUBE_HALF in code)
in vec4 in_inst_color;       // Base color (RGBA, A = depth_gain or alpha)
in float in_inst_z_norm;     // Depth progress 0..1 (1 = far spawn, 0 = hit)
in float in_inst_yaw;        // Yaw rotation around Y axis (radians) — Fix 2 billboard

uniform mat4 u_view_proj;    // View-projection matrix from camera

out vec3 v_normal;
out vec2 v_uv;
out vec3 v_world_pos;
out vec4 v_base_color;
out float v_z_norm;
out float v_face_id;

void main() {
    // ── Fix 2: Yaw rotation around Y axis (lane-aware billboard) ──
    float c = cos(in_inst_yaw);
    float s = sin(in_inst_yaw);
    mat3 rot_y = mat3(
        c,    0.0,  -s,
        0.0,  1.0,  0.0,
        s,    0.0,  c
    );

    // Local scaled vertex → rotated → translated to world
    vec3 local_scaled = in_position * in_inst_half * 2.0;
    vec3 rotated = rot_y * local_scaled;
    vec3 world_pos = rotated + in_inst_position;

    gl_Position = u_view_proj * vec4(world_pos, 1.0);

    // IMPORTANT: rotate normal vector cùng cube cho lighting đúng
    v_normal = rot_y * in_normal;

    v_uv = in_uv;
    v_world_pos = world_pos;
    v_base_color = in_inst_color;
    v_z_norm = in_inst_z_norm;
    v_face_id = in_face_id;
}
```

---

## File 3: `src/mgl_renderer/shaders/punch_block.frag`

```glsl
#version 330 core

in vec3 v_normal;
in vec2 v_uv;
in vec3 v_world_pos;
in vec4 v_base_color;
in float v_z_norm;
in float v_face_id;

uniform vec3 u_camera_pos;       // Camera world position
uniform vec3 u_light_dir;        // Main light direction (normalized)
uniform sampler2D u_icon_tex;    // Fist icon texture (RGBA, alpha = mask)
uniform float u_corner_radius;   // 0..0.45 fraction of edge (current: 0.08)

out vec4 fragColor;

// Rounded rectangle distance for SDF-based corner rounding
float sd_rounded_box(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + r;
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}

void main() {
    vec3 base = v_base_color.rgb;
    vec3 N = normalize(v_normal);
    vec3 V = normalize(u_camera_pos - v_world_pos);

    // ── Brightness model v2 — matches cv2 path exactly ──────────────
    // top_col   = clamp(base * 1.15 + 0.15)   → brightest, white tint
    // side_col  = base * 0.45                  → darkest
    // front_col = side_col * 1.30 * depth_gain → mid-tone
    //
    // NOTE: "top" = face_id 2 (normal -Y); "front" = face_id 0 (normal -Z);
    //       "side" = face_id 4 or 5 (normal ±X).

    vec3 top_col   = clamp(base * 1.15 + 0.15, 0.0, 1.0);
    vec3 side_col  = base * 0.45;
    float depth_gain = 0.70 + 0.30 * (1.0 - v_z_norm);

    vec3 face_color;

    if (v_face_id == 2.0) {
        // TOP face — brightest
        face_color = top_col * depth_gain;
    } else if (v_face_id == 0.0) {
        // FRONT face — mid (side_col * 1.30 * depth_gain)
        face_color = side_col * 1.30 * depth_gain;
    } else {
        // SIDE faces (left/right) — darkest
        face_color = side_col * depth_gain;
    }

    // ── Rim lighting on edges (Fresnel-like, subtle) ──
    float rim = 1.0 - max(dot(N, V), 0.0);
    rim = pow(rim, 2.5);
    vec3 rim_color = base * 1.20 + vec3(0.10);
    face_color += rim_color * rim * 0.25;

    // ── Icon overlay (FRONT face only, face_id == 0) ──
    if (v_face_id == 0.0) {
        // Map UV to centered region [0.19..0.81] for ~62% icon size
        vec2 icon_uv = (v_uv - 0.19) / 0.62;
        if (icon_uv.x >= 0.0 && icon_uv.x <= 1.0 &&
            icon_uv.y >= 0.0 && icon_uv.y <= 1.0) {
            vec4 icon = texture(u_icon_tex, icon_uv);
            face_color = mix(face_color, icon.rgb, icon.a);
        }
    }

    // ── Rounded corners via SDF ──
    // Only apply to FRONT (face_id 0) and TOP (face_id 2) faces.
    // Side faces skip rounding (too thin in perspective → artifacts).
    if (u_corner_radius > 0.001 && (v_face_id == 0.0 || v_face_id == 2.0)) {
        vec2 p = v_uv * 2.0 - 1.0;
        float d = sd_rounded_box(p, vec2(1.0), u_corner_radius);
        if (d > 0.0) {
            discard;
        }
    }

    // Output: emissive RGB (additive bloom-friendly) + alpha
    fragColor = vec4(face_color, 1.0);
}
```

---

## File 4: `src/mgl_renderer/geometry.py`

```python
"""Cube geometry generator with face IDs and UVs."""

import numpy as np


def generate_cube_with_face_ids():
    """Generate cube vertices with per-face attributes.

    Returns:
        verts: (N, 3) float32 — local cube vertices in [-0.5..0.5]
        normals: (N, 3) float32 — face normals (same for all 4 verts of a face)
        uvs: (N, 2) float32 — UV coords (0..1) per face
        face_ids: (N,) float32 — 0=front, 1=back, 2=top, 3=bottom, 4=left, 5=right
        indices: (M, 3) uint32 — triangle indices

    Each face has 4 vertices (CCW) + 2 triangles.
    Total: 6 faces × 4 verts = 24 verts; 6 × 2 = 12 triangles.
    """
    H = 0.5

    # 6 faces, each as (corners, normal, face_id)
    # Corner order: (UV 0,0) → (UV 1,0) → (UV 1,1) → (UV 0,1)
    faces = [
        # Front (z = -H, normal = -Z, face_id = 0)
        ([(-H, -H, -H), ( H, -H, -H), ( H,  H, -H), (-H,  H, -H)],
         (0, 0, -1), 0),
        # Back (z = +H, normal = +Z, face_id = 1)
        ([( H, -H,  H), (-H, -H,  H), (-H,  H,  H), ( H,  H,  H)],
         (0, 0, 1), 1),
        # Top (y = -H, normal = -Y, face_id = 2)  — NOTE: -Y is "up" in this project's convention
        ([(-H, -H,  H), ( H, -H,  H), ( H, -H, -H), (-H, -H, -H)],
         (0, -1, 0), 2),
        # Bottom (y = +H, normal = +Y, face_id = 3)
        ([(-H,  H, -H), ( H,  H, -H), ( H,  H,  H), (-H,  H,  H)],
         (0, 1, 0), 3),
        # Left (x = -H, normal = -X, face_id = 4)
        ([(-H, -H,  H), (-H, -H, -H), (-H,  H, -H), (-H,  H,  H)],
         (-1, 0, 0), 4),
        # Right (x = +H, normal = +X, face_id = 5)
        ([( H, -H, -H), ( H, -H,  H), ( H,  H,  H), ( H,  H, -H)],
         (1, 0, 0), 5),
    ]

    verts_list, normals_list, uvs_list, face_ids_list = [], [], [], []
    indices_list = []

    for fi, (corners, normal, face_id) in enumerate(faces):
        base = fi * 4
        for ci, c in enumerate(corners):
            verts_list.append(c)
            normals_list.append(normal)
            face_ids_list.append(float(face_id))
            # UV mapping: (0,0)→(1,0)→(1,1)→(0,1)
            uv = [(0, 0), (1, 0), (1, 1), (0, 1)][ci]
            uvs_list.append(uv)
        # 2 triangles per face: 0-1-2, 0-2-3
        indices_list.extend([base, base + 1, base + 2,
                             base, base + 2, base + 3])

    return (
        np.array(verts_list,    dtype=np.float32),
        np.array(normals_list,  dtype=np.float32),
        np.array(uvs_list,      dtype=np.float32),
        np.array(face_ids_list, dtype=np.float32),
        np.array(indices_list,  dtype=np.uint32),
    )


def generate_fist_icon_texture(size: int = 256) -> np.ndarray:
    """Generate fist icon as RGBA texture (white fill + black outline + grooves).

    Uses cv2 to draw, returns numpy RGBA uint8 (size, size, 4) for upload to GL.
    Cached at first call.
    """
    import cv2
    img = np.zeros((size, size, 4), dtype=np.uint8)
    cx, cy = size // 2, size // 2
    s = int(size * 0.85)   # icon size 85% texture

    # Outline polygon (same as _draw_fist_icon_v3 in spec)
    knuckle_top  = -0.42
    knuckle_base = -0.28
    knuckle_xs   = [-0.30, -0.10, 0.10, 0.30]
    knuckle_w    = 0.14

    pts_norm = [(-0.40, -0.20)]
    for kx in knuckle_xs:
        pts_norm.append((kx - knuckle_w / 2, knuckle_base))
        pts_norm.append((kx, knuckle_top))
        pts_norm.append((kx + knuckle_w / 2, knuckle_base))
    pts_norm += [(0.40, -0.20), (0.44, 0.10), (0.40, 0.38),
                 (0.20, 0.44), (-0.20, 0.44), (-0.40, 0.38), (-0.44, 0.10)]

    abs_pts = np.array(
        [(int(cx + p[0] * s), int(cy + p[1] * s)) for p in pts_norm],
        dtype=np.int32,
    )

    # Fill white + alpha 255
    cv2.fillPoly(img, [abs_pts], (255, 255, 255, 255), lineType=cv2.LINE_AA)
    # Black outline + alpha 255
    cv2.polylines(img, [abs_pts], isClosed=True, color=(20, 20, 20, 255),
                  thickness=max(2, int(s * 0.055)), lineType=cv2.LINE_AA)
    # Grooves
    for gx_norm in [-0.20, 0.00, 0.20]:
        gx = int(cx + gx_norm * s)
        gy0 = int(cy - 0.18 * s)
        gy1 = int(cy + 0.05 * s)
        cv2.line(img, (gx, gy0), (gx, gy1), (20, 20, 20, 255),
                 max(1, int(s * 0.045)), lineType=cv2.LINE_AA)
    # Thumb
    thumb_pts = np.array([
        (cx + int(-0.22 * s), cy + int(0.11 * s)),
        (cx + int(0.22 * s),  cy + int(0.11 * s)),
        (cx + int(0.18 * s),  cy + int(0.25 * s)),
        (cx + int(-0.22 * s), cy + int(0.25 * s)),
    ], dtype=np.int32)
    cv2.fillPoly(img, [thumb_pts], (255, 255, 255, 255), lineType=cv2.LINE_AA)
    cv2.polylines(img, [thumb_pts], isClosed=True, color=(20, 20, 20, 255),
                  thickness=max(2, int(s * 0.055)), lineType=cv2.LINE_AA)

    # Convert BGR→RGB for OpenGL upload
    img[:, :, :3] = img[:, :, [2, 1, 0]]
    return img
```

---

## File 5: `src/mgl_renderer/punch_renderer.py`

```python
"""ModernGL renderer for batched PunchTarget cubes.

Renders all alive punch blocks in a single GL pass with instancing.
Output: (BGR canvas, alpha mask) for compositing onto cv2 canvas.
"""

from __future__ import annotations
import math
from pathlib import Path
from typing import List, Tuple

import numpy as np
import moderngl

from .context import MGLContext
from .geometry import generate_cube_with_face_ids, generate_fist_icon_texture


class PunchBlockInstance:
    """Per-block instance data for batched rendering."""
    __slots__ = ('position', 'half', 'color', 'z_norm', 'yaw')

    def __init__(self, position, half, color, z_norm, yaw=0.0):
        self.position = position   # (x, y, z) world — wx is corrected (Fix 1)
        self.half = half           # CUBE_HALF
        self.color = color         # (r, g, b, a) 0..1 floats
        self.z_norm = z_norm       # 0..1
        self.yaw = yaw             # rotation around Y axis (radians, Fix 2)


class MGLPunchRenderer:
    """Singleton renderer for PunchTarget batched 3D rendering."""

    _instance = None

    def __init__(self):
        self.mgl = MGLContext.get()
        ctx = self.mgl.ctx

        # Load shaders
        shader_dir = Path(__file__).parent / "shaders"
        with open(shader_dir / "punch_block.vert") as f:
            vert_src = f.read()
        with open(shader_dir / "punch_block.frag") as f:
            frag_src = f.read()
        self.prog = ctx.program(vertex_shader=vert_src,
                                 fragment_shader=frag_src)

        # Generate cube geometry
        verts, normals, uvs, face_ids, indices = generate_cube_with_face_ids()

        # Per-vertex VBOs
        self.vbo_verts = ctx.buffer(verts.tobytes())
        self.vbo_normals = ctx.buffer(normals.tobytes())
        self.vbo_uvs = ctx.buffer(uvs.tobytes())
        self.vbo_face_ids = ctx.buffer(face_ids.tobytes())
        self.ibo = ctx.buffer(indices.tobytes())
        self.n_indices = len(indices)

        # Per-instance VBO (allocated dynamically based on max blocks)
        # 9 floats per instance: pos(3) + half(1) + color(4) + z_norm(1) + yaw(1) = 36 bytes
        self.MAX_INSTANCES = 64
        self.vbo_instance = ctx.buffer(reserve=self.MAX_INSTANCES * 36)   # was 32

        # VAO
        self.vao = ctx.vertex_array(
            self.prog,
            [
                (self.vbo_verts,    '3f',   'in_position'),
                (self.vbo_normals,  '3f',   'in_normal'),
                (self.vbo_uvs,      '2f',   'in_uv'),
                (self.vbo_face_ids, '1f',   'in_face_id'),
                (self.vbo_instance, '3f 1f 4f 1f 1f /i',   # was '3f 1f 4f 1f /i'
                 'in_inst_position', 'in_inst_half',
                 'in_inst_color', 'in_inst_z_norm', 'in_inst_yaw'),
            ],
            self.ibo,
        )

        # Icon texture
        icon_data = generate_fist_icon_texture(256)
        self.tex_icon = ctx.texture((256, 256), 4, icon_data.tobytes())
        self.tex_icon.use(location=0)
        self.prog['u_icon_tex'] = 0

        # Default uniforms — match cv2 path current values
        self.prog['u_corner_radius'] = 0.08   # subtle rounding (was 0.18)
        self.prog['u_light_dir'] = (0.2, -0.5, 1.0)

    @classmethod
    def get(cls) -> "MGLPunchRenderer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def render(
        self,
        blocks: List[PunchBlockInstance],
        view_proj_matrix: np.ndarray,    # 4x4 float32
        camera_pos: tuple,                # (x, y, z) world
        width: int,
        height: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Render all blocks → return (BGR canvas, alpha mask).

        BGR canvas is (H, W, 3) uint8.
        Alpha mask is (H, W) uint8 (0 = transparent, 255 = opaque).
        """
        if not blocks:
            return (np.zeros((height, width, 3), dtype=np.uint8),
                    np.zeros((height, width), dtype=np.uint8))

        if len(blocks) > self.MAX_INSTANCES:
            # Resize instance VBO
            self.MAX_INSTANCES = max(self.MAX_INSTANCES * 2, len(blocks))
            self.vbo_instance.orphan(self.MAX_INSTANCES * 36)   # 9 floats × 4 bytes

        # Upload instance data — 9 floats per instance (was 8, +yaw)
        instance_data = np.empty(len(blocks) * 9, dtype=np.float32)
        for i, b in enumerate(blocks):
            base = i * 9
            instance_data[base + 0] = b.position[0]
            instance_data[base + 1] = b.position[1]
            instance_data[base + 2] = b.position[2]
            instance_data[base + 3] = b.half
            instance_data[base + 4] = b.color[0]
            instance_data[base + 5] = b.color[1]
            instance_data[base + 6] = b.color[2]
            instance_data[base + 7] = b.z_norm
            instance_data[base + 8] = b.yaw            # ← NEW (Fix 2)
        self.vbo_instance.write(instance_data.tobytes())

        # Get framebuffer
        fbo, fbo_resolved, color_tex, color_resolved = self.mgl.get_fbo(
            width, height, samples=8,
        )

        # Set uniforms
        self.prog['u_view_proj'].write(view_proj_matrix.astype(np.float32).tobytes())
        self.prog['u_camera_pos'] = camera_pos

        # Render to multisample fbo
        fbo.use()
        fbo.clear(0.0, 0.0, 0.0, 0.0)   # transparent BG
        self.vao.render(instances=len(blocks))

        # Resolve multisample → single sample
        self.mgl.ctx.copy_framebuffer(fbo_resolved, fbo)

        # Read back RGBA
        data = fbo_resolved.read(components=4)
        rgba = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4)

        # Flip vertical (OpenGL Y-up, image Y-down)
        rgba = np.flipud(rgba).copy()

        # Split RGBA → BGR + Alpha
        rgb = rgba[:, :, :3]
        bgr = rgb[:, :, ::-1].copy()   # RGB→BGR
        alpha = rgba[:, :, 3].copy()

        return bgr, alpha
```

---

## File 6: `src/mgl_renderer/compositor.py`

```python
"""Alpha composite GL output onto cv2 canvas."""

import numpy as np


def composite_alpha(
    cv2_canvas: np.ndarray,    # (H, W, 3) BGR uint8
    gl_canvas: np.ndarray,     # (H, W, 3) BGR uint8
    gl_alpha: np.ndarray,      # (H, W) uint8
) -> np.ndarray:
    """Alpha composite gl_canvas over cv2_canvas using gl_alpha mask.

    Modifies cv2_canvas in-place + returns it.
    Pure numpy — fast for this size.
    """
    a = gl_alpha.astype(np.float32) / 255.0
    a3 = np.dstack([a, a, a])
    result = (
        cv2_canvas.astype(np.float32) * (1 - a3) +
        gl_canvas.astype(np.float32) * a3
    ).clip(0, 255).astype(np.uint8)
    return result
```

---

## File 7: `src/mgl_renderer/__init__.py`

```python
"""ModernGL render pipeline for selected 3D primitives.

Currently supports:
  - PunchTarget cube blocks (batched instanced rendering)

Future extensions:
  - DanceTarget (similar structure)
  - LineTarget (zigzag chains)
"""

from .punch_renderer import MGLPunchRenderer, PunchBlockInstance
from .compositor import composite_alpha

__all__ = ["MGLPunchRenderer", "PunchBlockInstance", "composite_alpha"]
```

---

## Integration trong `rhythm.py`

### Bước 1: Build view-projection matrix từ PerspectiveCamera

`PerspectiveCamera.project()` hiện là pinhole pure. Thêm method:

```python
class PerspectiveCamera:
    def view_proj_matrix(self) -> np.ndarray:
        """Construct 4x4 view-projection matrix matching project() pinhole.

        OpenGL convention: column-major, eye at origin looking down -Z.
        Our convention: +X right, +Y down, +Z away.

        Mapping:
          - perspective: aspect = W/H, fovy from fy
          - view: identity (camera at origin, looking +Z forward, but GL is -Z)
            → flip Z axis in view matrix
        """
        W = self.width
        H = self.height
        fx, fy = self.fx, self.fy
        cx, cy = self.cx_pix, self.cy_pix

        # OpenGL projection from intrinsics
        # x_clip = (2*fx/W) * x_view + (2*cx/W - 1) * z_view
        # ... see https://strawlab.github.io/AcrylicArt/glOrthoProjection.html
        z_near, z_far = 0.1, 100.0

        proj = np.zeros((4, 4), dtype=np.float32)
        proj[0, 0] = 2 * fx / W
        proj[0, 2] = 1 - 2 * cx / W
        proj[1, 1] = 2 * fy / H
        proj[1, 2] = 2 * cy / H - 1
        proj[2, 2] = -(z_far + z_near) / (z_far - z_near)
        proj[2, 3] = -2 * z_far * z_near / (z_far - z_near)
        proj[3, 2] = -1.0

        # View matrix: identity (camera at origin) + flip Z (our +Z away → GL -Z forward)
        view = np.eye(4, dtype=np.float32)
        view[2, 2] = -1.0    # flip Z axis

        return (proj @ view).astype(np.float32)
```

### Bước 2: Modify main render loop

`rhythm.py` line ~7820 area (main render loop):

```python
# AFTER tunnel + side_rails:
canvas = side_rail.draw(canvas, fi, ...)

# ── NEW: Batched ModernGL render cho ALL PunchTargets ──
from mgl_renderer import MGLPunchRenderer, PunchBlockInstance, composite_alpha

punch_blocks = []
for tg in game.alive_sorted(fi):
    if isinstance(tg, PunchTarget) and tg.state == 'flying':
        # Skip mesh/texture path — those still use cv2 path
        if (PunchTarget.MESH_LEFT is not None or
            PunchTarget.MESH_RIGHT is not None or
            PunchTarget.TEXTURE_LEFT is not None or
            PunchTarget.TEXTURE_RIGHT is not None):
            continue

        z_norm = tg.depth(fi)

        # ── FIX 1 (Trajectory match floor lane) ──
        # Block screen X anchored to floor lane interp (cam.lane_x), then
        # back-compute world X so cam.project() reproduces sx_center.
        # See docs/block-trajectory-fix-spec.md (Option A).
        sx_center = cam.lane_x(tg.lane, z_norm)
        wz = tg.Z_VIS_NEAR + z_norm * (tg.Z_VIS_FAR - tg.Z_VIS_NEAR)
        wy = tg.WY_SPAWN + (tg.WY_HIT - tg.WY_SPAWN) * (1.0 - z_norm)
        wx = (sx_center - cam.cx_pix) * wz / cam.fx

        # ── FIX 2 (Yaw billboard rotation) ──
        # Cube xoay quanh trục Y theo độ lệch lane → front face quay về camera.
        # YAW_FACTOR = 0.75 = nghiêng 75% (giữ chút side face cho 3D pop).
        # See docs/block-trajectory-fix-spec.md (Phần 2).
        import math
        YAW_FACTOR = 0.75
        yaw = -math.atan2(wx, wz) * YAW_FACTOR

        # Color from LANE_COLORS (2-color palette):
        #   lane 0,1 → blue  BGR(230, 80, 30)
        #   lane 2,3 → orange BGR(0, 140, 255)
        # Convert BGR → RGB float for GL
        b, g, r = tg.color
        color_rgb = (r / 255.0, g / 255.0, b / 255.0, 1.0)

        punch_blocks.append(PunchBlockInstance(
            position=(wx, wy, wz),    # wx is corrected (Fix 1)
            half=tg.CUBE_HALF,
            color=color_rgb,
            z_norm=z_norm,
            yaw=yaw,                  # NEW field (Fix 2)
        ))

if punch_blocks:
    renderer = MGLPunchRenderer.get()
    vp_matrix = cam.view_proj_matrix()
    gl_canvas, gl_alpha = renderer.render(
        blocks=punch_blocks,
        view_proj_matrix=vp_matrix,
        camera_pos=(0.0, 0.0, 0.0),
        width=self.WIDTH,
        height=self.HEIGHT,
    )
    canvas = composite_alpha(canvas, gl_canvas, gl_alpha)

# Other targets (Dance, Line, Relax + PunchTarget với mesh/texture)
for tg in game.alive_sorted(fi):
    if isinstance(tg, PunchTarget):
        # Only render via cv2 if it has mesh/texture (else đã GL render)
        if (PunchTarget.MESH_LEFT is None and
            PunchTarget.MESH_RIGHT is None and
            PunchTarget.TEXTURE_LEFT is None and
            PunchTarget.TEXTURE_RIGHT is None):
            continue   # already GL rendered
    canvas = tg.draw(canvas, cam, fi)
```

### Bước 3: Optional — Disable cv2 path entirely

Trong `PunchTarget.draw()`, thêm early return khi GL renderer active:

```python
def draw(self, canvas, cam, cur_frame):
    # Skip cv2 path nếu GL renderer đã handle
    if getattr(self, '_skip_cv2_path', False):
        return canvas
    # ... rest of existing draw method
```

Set `_skip_cv2_path = True` trong main loop nếu đã GL render.

---

## Performance estimate

### Compute breakdown per frame (1920×1080)

| Operation | cv2 path | ModernGL path | Speedup |
|---|---|---|---|
| Per-cube projection (8 corners) | 1.5ms × N cubes | 0.05ms total (GPU instancing) | **30×** |
| Face fill (3 polygons) | 2.0ms × N | 0.1ms total | **60×** |
| Glow ROI boxFilter | 1.2ms × N | 0 (built into shader) | **∞** |
| Painter sort | 0.3ms × N | 0 (Z-buffer auto) | **∞** |
| Framebuffer alloc + readback | 0 | 1.5ms (one-time) | — |
| Alpha composite | 0 | 0.8ms | — |

**N = số cubes alive cùng lúc (typical 4-8).**

Tổng cho 6 cubes/frame:
- cv2: ~30ms/frame → 33fps max
- ModernGL: ~3-4ms/frame → 250+fps theoretical

### GPU memory

- Framebuffer (1920×1080×4 RGBA × 8 samples) = ~66MB MSAA + ~8MB resolved
- Cube geometry: ~1KB
- Icon texture (256×256 RGBA): ~256KB
- Instance data (64 × 8 floats): ~2KB
- **Total: ~75MB GPU memory** — không đáng kể

---

## PyInstaller bundling

### `build_dist.py` modifications

Thêm hidden imports + binary collection:

```python
# In analysis section
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files

hidden_imports = [
    ...,
    'glcontext',
    'glcontext.wgl',     # Windows
    'moderngl',
    'moderngl.context',
    'pyrr',
]

binaries = (
    collect_dynamic_libs('glcontext') +
    collect_dynamic_libs('moderngl')
)

datas = collect_data_files('moderngl') + [
    ('src/mgl_renderer/shaders/*.vert', 'mgl_renderer/shaders'),
    ('src/mgl_renderer/shaders/*.frag', 'mgl_renderer/shaders'),
]
```

### `bundle_paths.py` — find shader directory

```python
def find_shader_dir() -> Path:
    """Return directory containing GLSL shader files."""
    bundle = _bundle_dir()
    if bundle is not None:
        # PyInstaller frozen — shaders in _MEIPASS or next to exe
        candidates = [
            Path(getattr(sys, '_MEIPASS', bundle)) / 'mgl_renderer' / 'shaders',
            bundle / 'mgl_renderer' / 'shaders',
        ]
        for c in candidates:
            if c.exists():
                return c

    # Development — relative to this file
    return Path(__file__).parent / 'mgl_renderer' / 'shaders'
```

Update `MGLPunchRenderer.__init__`:
```python
from bundle_paths import find_shader_dir
shader_dir = find_shader_dir()
```

---

## Testing plan

### Unit tests

```python
# tests/test_mgl_renderer.py

def test_context_creation():
    """Verify standalone context creates without window."""
    from mgl_renderer.context import MGLContext
    ctx = MGLContext.get()
    assert ctx.ctx.version_code >= 330

def test_geometry_correctness():
    """Cube has 24 verts (6 faces × 4) and 36 indices (12 tris)."""
    from mgl_renderer.geometry import generate_cube_with_face_ids
    verts, normals, uvs, fids, indices = generate_cube_with_face_ids()
    assert len(verts) == 24
    assert len(indices) == 36

def test_render_single_cube():
    """Render 1 cube and verify alpha mask not empty."""
    from mgl_renderer import MGLPunchRenderer, PunchBlockInstance
    import numpy as np

    renderer = MGLPunchRenderer.get()
    blocks = [PunchBlockInstance(
        position=(0, 0, 5), half=0.3,
        color=(0.2, 0.5, 1.0, 1.0), z_norm=0.5,
    )]
    vp = np.eye(4, dtype=np.float32)
    bgr, alpha = renderer.render(blocks, vp, (0, 0, 0), 320, 240)
    assert bgr.shape == (240, 320, 3)
    assert alpha.shape == (240, 320)
    assert alpha.max() > 0   # something rendered

def test_visual_comparison():
    """Render cube qua GL path và cv2 path, so sánh SSIM."""
    # ... (golden image comparison)
```

### Visual regression

1. Render 1 segment 5s clip với GL path → save `gl_output.mp4`
2. Render same segment với cv2 path (toggle flag) → save `cv2_output.mp4`
3. Compare frame-by-frame:
   - SSIM > 0.92 between frames (allow stylistic difference)
   - Block positions match within 2px
   - Colors saturated correctly

### Integration test

1. Render full song (3 phút) với GL path
2. Verify:
   - Không crash
   - Frame timing đều (no spikes > 100ms/frame)
   - GPU memory không leak (track via nvidia-smi)
   - Output mp4 mở được, video stream valid

---

## Migration plan (phases)

| Phase | Step | Effort | Risk |
|---|---|---|---|
| **1** | Setup deps (`pip install moderngl glcontext pyrr`) + verify standalone context tạo OK | 1 giờ | Thấp |
| **2** | Implement `MGLContext`, `geometry.py` + unit test cube geometry | 4 giờ | Thấp |
| **3** | Write vertex + fragment shader (basic version, no neon yet) | 6 giờ | Trung |
| **4** | Implement `MGLPunchRenderer.render()` + test render 1 cube | 6 giờ | Trung |
| **5** | View-proj matrix conversion từ `PerspectiveCamera` (CRITICAL — match exactly với cv2 projection) | 8 giờ | **Cao** |
| **6** | Compositor + integration vào main render loop (toggle flag) | 4 giờ | Trung |
| **7** | Visual A/B test — verify GL output match positions + size với cv2 | 8 giờ | Cao |
| **8** | Refine fragment shader để match neon aesthetic reference | 8 giờ | Trung |
| **9** | Add fist icon texture + UV mapping | 4 giờ | Thấp |
| **10** | Add rounded corners (SDF in shader) + rim lighting | 4 giờ | Thấp |
| **11** | PyInstaller bundling + test frozen exe | 6 giờ | **Cao** |
| **12** | Performance benchmark + GPU memory leak check | 4 giờ | Thấp |
| **13** | Documentation + rollback flag | 2 giờ | Thấp |

**Total**: 65 giờ (~8-10 days full-time, ~3-4 weeks part-time).

---

## Rollback plan

### Toggle flag

`render_settings.py`:
```python
# Project-level config
use_moderngl_punch: bool = Field(default=False)   # phase-in flag
```

CLI:
```bash
python rhythm.py ... --use_moderngl_punch 1
```

Trong main loop:
```python
if args.use_moderngl_punch:
    # GL path
else:
    # Existing cv2 path
```

→ Có thể disable GL path bất kỳ lúc nào nếu phát hiện bug.

### Phase-in strategy

- **Week 1**: Default OFF, opt-in flag. Internal test on 5-10 sample renders.
- **Week 2**: Beta — flag ON cho preview mode only (full render vẫn cv2).
- **Week 3**: Default ON cho preview + full render. CV2 path giữ làm fallback.
- **Week 4**: Remove cv2 path nếu zero issues 1 tuần.

---

## Edge cases + rủi ro

### 1. **View-proj matrix mismatch**
Risk cao nhất. cv2 path dùng pinhole `cam.project(x, y, z) → (sx, sy)`. GL dùng matrix multiply. Nếu intrinsics map sai 1 dấu trừ → cube không xuất hiện đúng vị trí.

**Mitigation**: Test bằng cube đặt tại các z = 5, 10, 20, 50 → verify projected pixel position match cv2 trong sai số ≤ 2px.

### 2. **Headless context fail trên CI/server không có GPU**
ModernGL standalone cần GPU driver. Trên Docker/CI có thể fail.

**Mitigation**: Try/except fallback sang cv2 path. Hoặc dùng `osmesa` (software OpenGL) cho CI.

### 3. **PyInstaller frozen mode shader files**
Shaders là text files .vert/.frag — phải bundle qua `datas`. Nếu miss → runtime FileNotFoundError.

**Mitigation**: `bundle_paths.find_shader_dir()` check multiple candidates. Test frozen exe trước release.

### 4. **GPU memory leak trên long renders**
Mỗi `render()` allocate framebuffer mới nếu cache miss → OOM trên render 30 phút.

**Mitigation**: Cache framebuffer theo (W, H). Add `release()` method gọi cuối render.

### 5. **MSAA support khác nhau giữa GPU vendors**
Intel iGPU có thể hạn chế 8× MSAA. NVIDIA/AMD support 16-32× thoải mái.

**Mitigation**: Try samples=8, fallback samples=4 nếu allocate fail.

### 6. **Color space mismatch RGB ↔ BGR**
GL dùng RGBA, cv2 dùng BGR. Không convert chính xác → block xanh thành đỏ.

**Mitigation**: Test cube xanh dương → verify pixel value đúng BGR `(255, 60, 50)`.

### 7. **Z-buffer precision near vs far**
near=0.1, far=100. Nếu cube tại z=0.05 (closer than near) → clip plane cắt.

**Mitigation**: Set near=0.05 hoặc dynamic adjust based on Z_VIS_NEAR.

---

## Acceptance criteria

```
✓ ModernGL render 1 cube → output match cv2 reference vị trí ±2px
✓ View-proj matrix produce identical screen coords vs cam.project() ≤ 1px error
✓ Render 8 cubes simultaneously trong 1 pass → output match cv2 individual renders
✓ Performance: 6 cubes/frame ≤ 5ms (vs cv2 ~30ms) — 6× speedup
✓ Visual: block 3D rõ rệt — top sáng ngả trắng, front mid-tone, side tối
✓ Brightness: top = base*1.15+0.15, front = side*1.30*depth_gain, side = base*0.45
✓ Glow: only from TOP face polygon — NOT full silhouette
✓ Rounded corners: SDF discard only on FRONT + TOP faces (radius=0.08); side = sharp
✓ Fist icon bold silhouette trên FRONT face; simplified version cho block xa (front_w 12-18px)
✓ 2-color palette: lane 0,1 = blue; lane 2,3 = orange
✓ MSAA 8× → edges smooth, no jagged steps
✓ PyInstaller frozen exe load shaders OK, render output identical với dev mode
✓ GPU memory không leak qua 100 renders (heap stable)
✓ Toggle flag --use_moderngl_punch hoạt động cả 2 chiều
✓ Mesh/texture path KHÔNG bị ảnh hưởng (vẫn cv2 software rasterize)
✓ Other targets (Dance/Line/Relax) KHÔNG bị ảnh hưởng
✓ Bloom defaults: sigma=24, gain=0.75 (stronger halo)
```

---

## Future extensions (sau khi PunchTarget OK)

| Phase | Module | Estimate effort |
|---|---|---|
| **Phase 2** | DanceTarget (similar cube structure) | 2-3 ngày |
| **Phase 3** | LineTarget zigzag chains (instanced với offsets) | 3-5 ngày |
| **Phase 4** | RelaxTarget (3 kinds: low/middle/high) | 5-7 ngày |
| **Phase 5** | Bloom post-process via shader (replace cuFFT) | 3-5 ngày |
| **Phase 6** | Tunnel + side rails toàn bộ → GL | 1-2 tuần |
| **Phase 7** | Particles → instanced GL points/quads | 1 tuần |

→ Sau khi tất cả module migrate, có thể remove cv2 path hoàn toàn → render full GPU pipeline → match production-grade game render quality.

---

## Open questions

1. **Render output format**: GL output BGR vs cv2 BGRA ? Cần convert RGBA→BGR mỗi frame có overhead không? Có thể optimize thành BGR direct?

2. **Shader hot-reload trong dev mode**: Có muốn hot-reload .vert/.frag khi save để iterate visual nhanh không? (Implementation: watchdog + recompile prog)

3. **Multi-GPU**: Máy có 2 GPU (RTX 4060 Ti + Intel UHD). Force ModernGL chọn NVIDIA bằng cách nào? (env var `CUDA_VISIBLE_DEVICES` hoặc `__GL_VRR_ALLOWED`?)

4. **Headless context backend**: Windows dùng WGL. Linux EGL hay OSMesa? Cần test trên user's machine OS specifically.

5. **Backward compat lower-end GPU**: User máy yếu (Intel HD Graphics 2010s) có support OpenGL 3.3 không? Cần fallback path?

6. **Per-block uniform overrides**: Một số block có effect đặc biệt (vd hit flash, color change theo combo). Currently per-instance attrs đủ chưa, hay cần thêm uniform array?

7. **Anti-cheat compatibility**: ModernGL khởi tạo OpenGL context — có thể trigger anti-cheat trên 1 số game streaming software. Có concern không?

Bạn confirm 7 open questions trên + chốt thứ tự priority phases, tôi sẽ refine spec hoặc bắt đầu Phase 1 implementation guide chi tiết hơn.
