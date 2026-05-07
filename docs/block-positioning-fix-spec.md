# Block Positioning Fix — Trajectory + Yaw Billboard

## Mục tiêu

Fix 2 vấn đề positioning của **PunchTarget block 3D** quan sát từ render preview:

1. **Trajectory mismatch**: Block không bay đúng quỹ đạo song song với floor lanes — block "co cụm" gần tâm ở mid-Z trong khi floor lanes spread rộng.
2. **No yaw rotation**: Block là AABB (axis-aligned cube) — front face luôn perpendicular Z axis. Block ở outer lane → camera nhìn xéo → thấy side face nhiều hơn front face → trông kỳ.

> **Ràng buộc**: KHÔNG đụng game logic (timing, hit detection, scoring). Chỉ visual positioning. Áp dụng cho cả **cv2 path hiện tại** và **ModernGL path tương lai**.

---

## Tóm tắt 2 fix

| Fix | Vấn đề | Solution | Effort |
|---|---|---|---|
| **#1 Trajectory** | Block X lệch khỏi floor lane ở mid-Z (lệch tới 227px ở 1280p) | Anchor block screen X theo `cam.lane_x()`, back-compute world X | 5 phút |
| **#2 Yaw Billboard** | Block không xoay → front face không quay về camera | Apply yaw rotation `-atan2(wx, wz) * 0.75` quanh Y axis | 10 phút |

**Combined effort cv2 path**: ~15 dòng code change.
**Combined effort ModernGL path**: ~25 dòng code change (vertex shader + instance buffer + render loop).

---

# Fix #1 — Trajectory match floor lane

## Root cause

Project có **2 hệ tọa độ KHÁC NHAU** cho floor lanes vs punch blocks:

| Element | Method | Math | Quỹ đạo |
|---|---|---|---|
| **Floor lanes** | `cam.lane_x(lane, z)` | `cx + (bot_x - cx) * (1 - z)` LINEAR interp | Đường thẳng straight đều |
| **Punch blocks** | `cam.lane_world_x(lane)` + `cam.project()` | `cx + fx * wx / wz` 1/z hyperbolic | Đường cong nhanh về tâm |

→ Chỉ MATCH ở 2 điểm: `z_norm=0` (near) và `z_norm=1` (far vanishing).
→ Ở mid-Z, block X **gần tâm hơn** floor lane X đáng kể.

### Bằng chứng tính toán (W=1280, fov=55°, lane 0)

| z_norm | Floor lane X | Block X (hiện tại) | **Lệch** |
|---|---|---|---|
| 0.0 (near) | 192 | 193 | 1px ✅ |
| 0.3 | 326 | 553 | **227px** ❌ |
| 0.5 | 416 | 580 | **164px** ❌ |
| 0.7 | 506 | 605 | **99px** ❌ |
| 0.9 | 595 | 631 | 36px |
| 1.0 (vanish) | 640 | 640 | 0 ✅ |

## Solution — Anchor screen X to floor lane interp

```python
# Compute target screen X based on floor lane formula
sx_center = cam.lane_x(self.lane, z_norm)

# Back-compute world X so cam.project() reproduces sx_center
# Pinhole: sx = cx + fx * wx / wz  →  wx = (sx - cx) * wz / fx
wx = (sx_center - cam.cx_pix) * wz / cam.fx
```

### Tại sao approach này work

1. **Screen X match floor lane EXACTLY** — cùng formula
2. **Cube size + perspective** vẫn đúng — `wz` không đổi, 8 corners vẫn project qua pinhole standard
3. **Top/front/side face shading** không bị ảnh hưởng
4. **Glow ROI bbox** vẫn đúng — hull computed sau projection

### Tradeoff

- ⚠️ Block KHÔNG bay theo đường thẳng trong **world space** (wx thay đổi theo z_norm)
- ✅ Game logic không đụng (collision dùng lane index, không dùng wx)
- ✅ Visually perfect (match floor lane visualization)

---

# Fix #2 — Yaw Billboard Rotation

## Root cause

Cube là **axis-aligned (AABB)** — 8 corners tính ở local space `(±HX, ±HY, ±HZ)`, world position chỉ là translation `(wx, wy, wz)`. KHÔNG có rotation.

→ Mặt front của cube **luôn perpendicular với Z axis**. Camera ở origin, block ở outer lane (wx≠0) → camera nhìn block xéo → thấy side face nhiều hơn → trông kỳ.

## Solution — Yaw rotation around Y axis

Tính góc yaw theo công thức:

```python
import math
yaw_angle = -math.atan2(wx, wz) * YAW_FACTOR
```

### Tính cụ thể (W=1280, lane 0 outer-left)

| z_norm | wz | yaw (full, factor=1.0) | yaw (recommend, factor=0.75) |
|---|---|---|---|
| 0.0 (near) | 2.5 | +20° | **+15°** |
| 0.3 | 10.15 | +5.2° | +3.9° |
| 0.5 | 15.25 | +3.5° | +2.6° |
| 0.7 | 20.35 | +2.6° | +2.0° |
| 1.0 (vanish) | 28 | +1.9° | +1.4° |

→ Near nghiêng nhiều, far nghiêng ít — đúng intuition (block xa nhỏ, ít cần xoay).

## YAW_FACTOR control

| YAW_FACTOR | Effect | Trade-off |
|---|---|---|
| 0.0 | Không xoay (AABB current) | Sai như hiện tại |
| 0.5 | Nghiêng 50% | Compromise — vẫn thấy side rõ |
| **0.75** | **RECOMMEND** | Front thẳng + chút side cho 3D pop |
| 1.0 | Full billboard | Mất hoàn toàn side face |

→ Default `YAW_FACTOR = 0.75`. Có thể expose qua render_settings:
```python
punch_yaw_factor: float = Field(default=0.75, ge=0.0, le=1.0)
```

## Apply rotation matrix lên 8 corners

```python
cos_y = math.cos(yaw)
sin_y = math.sin(yaw)

# Rotation matrix around Y axis:
#   [  cos_y   0   sin_y ]
#   [    0     1     0   ]
#   [ -sin_y   0   cos_y ]

for lx, ly, lz in local_corners:
    rx = lx * cos_y + lz * sin_y
    ry = ly
    rz = -lx * sin_y + lz * cos_y
    corners_w.append((wx + rx, wy + ry, wz + rz))
```

---

# Combined Patch — cv2 path

## File: `src/rhythm.py` PunchTarget.draw

**Replace block tính corners (line ~2194-2214)** với:

```python
import math

def draw(self, canvas, cam, cur_frame):
    if self.state != 'flying':
        return canvas
    z_norm = self.depth(cur_frame)

    # ── Fix 1: Trajectory match floor lane ──
    sx_center = cam.lane_x(self.lane, z_norm)
    wz = self.Z_VIS_NEAR + z_norm * (self.Z_VIS_FAR - self.Z_VIS_NEAR)
    wy = self.WY_SPAWN + (self.WY_HIT - self.WY_SPAWN) * (1.0 - z_norm)
    wx = (sx_center - cam.cx_pix) * wz / cam.fx

    # ── Fix 2: Yaw billboard rotation ──
    YAW_FACTOR = 0.75
    yaw = -math.atan2(wx, wz) * YAW_FACTOR
    cos_y = math.cos(yaw)
    sin_y = math.sin(yaw)

    # ── 8 cube corners with rotation applied ──
    HX = HY = HZ = self.CUBE_HALF
    local_corners = [
        (-HX, -HY, -HZ),  # 0 FLT
        ( HX, -HY, -HZ),  # 1 FRT
        ( HX,  HY, -HZ),  # 2 FRb
        (-HX,  HY, -HZ),  # 3 FLb
        (-HX, -HY,  HZ),  # 4 BLT
        ( HX, -HY,  HZ),  # 5 BRT
        ( HX,  HY,  HZ),  # 6 BRb
        (-HX,  HY,  HZ),  # 7 BLb
    ]
    corners_w = []
    for lx, ly, lz in local_corners:
        rx = lx * cos_y + lz * sin_y
        ry = ly
        rz = -lx * sin_y + lz * cos_y
        corners_w.append((wx + rx, wy + ry, wz + rz))

    # Sau đó project 8 corners như cũ
    proj = [cam.project(*p) for p in corners_w]
    if any(p is None for p in proj):
        return canvas
    pts = np.array(
        [(int(round(p[0])), int(round(p[1]))) for p in proj],
        dtype=np.int32,
    )

    # ... rest unchanged (face shading, glow, icon)
```

## Áp dụng cùng pattern cho các target classes khác

Các class sau cũng dùng `cam.lane_world_x(self.lane)` → cùng vấn đề. Apply pattern tương tự:

| Class | File location (approx) | Cần fix |
|---|---|---|
| **PunchTarget** | `rhythm.py:2091` | ✅ Spec này |
| **DanceTarget** | `rhythm.py:2710` | ⚠️ Optional (check visual) |
| **LineTarget** | `rhythm.py:3060` | ⚠️ Optional |
| **StepTarget** | tùy | ⚠️ Check |
| **RelaxTarget LOW/MID/HIGH** | `rhythm.py:2953` | ⚠️ Check |

Recommend làm PunchTarget trước, render thử, nếu OK thì apply tiếp các class khác.

---

# ModernGL Integration

Khi migrate sang ModernGL, **cả 2 fix integrate sẵn** — không phải làm thêm trong shader CV2 path.

## Vertex shader `punch_block.vert`

```glsl
#version 330 core

// Per-vertex
in vec3 in_position;
in vec3 in_normal;
in vec2 in_uv;
in float in_face_id;

// Per-instance
in vec3 in_inst_position;   // World position (wx is corrected — Fix 1)
in float in_inst_half;
in vec4 in_inst_color;
in float in_inst_z_norm;
in float in_inst_yaw;        // ← NEW (Fix 2)

uniform mat4 u_view_proj;

out vec3 v_normal;
out vec2 v_uv;
out vec3 v_world_pos;
out vec4 v_base_color;
out float v_z_norm;
out float v_face_id;

void main() {
    // ── Fix 2: Yaw rotation around Y axis ──
    float c = cos(in_inst_yaw);
    float s = sin(in_inst_yaw);
    mat3 rot_y = mat3(
        c,    0.0,  -s,
        0.0,  1.0,  0.0,
        s,    0.0,  c
    );

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

## `PunchBlockInstance` dataclass

```python
class PunchBlockInstance:
    """Per-block instance data for batched rendering."""
    __slots__ = ('position', 'half', 'color', 'z_norm', 'yaw')

    def __init__(self, position, half, color, z_norm, yaw=0.0):
        self.position = position   # (x, y, z) world — wx is corrected (Fix 1)
        self.half = half
        self.color = color
        self.z_norm = z_norm
        self.yaw = yaw             # rotation around Y axis (radians, Fix 2)
```

## Per-instance VBO + VAO

```python
# Tăng instance stride 32 → 36 bytes (8→9 floats)
self.MAX_INSTANCES = 64
self.vbo_instance = ctx.buffer(reserve=self.MAX_INSTANCES * 36)

self.vao = ctx.vertex_array(
    self.prog,
    [
        (self.vbo_verts,    '3f', 'in_position'),
        (self.vbo_normals,  '3f', 'in_normal'),
        (self.vbo_uvs,      '2f', 'in_uv'),
        (self.vbo_face_ids, '1f', 'in_face_id'),
        (self.vbo_instance, '3f 1f 4f 1f 1f /i',   # was '3f 1f 4f 1f /i'
         'in_inst_position', 'in_inst_half',
         'in_inst_color', 'in_inst_z_norm', 'in_inst_yaw'),
    ],
    self.ibo,
)
```

## Render method — pack 9 floats per instance

```python
def render(self, blocks, view_proj_matrix, camera_pos, width, height):
    if not blocks:
        return (...)

    if len(blocks) > self.MAX_INSTANCES:
        self.MAX_INSTANCES = max(self.MAX_INSTANCES * 2, len(blocks))
        self.vbo_instance.orphan(self.MAX_INSTANCES * 36)

    # Pack 9 floats per instance (was 8, +yaw)
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

    # ... rest of render
```

## Main render loop integration trong `rhythm.py`

```python
import math

punch_blocks = []
for tg in game.alive_sorted(fi):
    if isinstance(tg, PunchTarget) and tg.state == 'flying':
        # Skip mesh/texture path
        if (PunchTarget.MESH_LEFT is not None or
            PunchTarget.MESH_RIGHT is not None or
            PunchTarget.TEXTURE_LEFT is not None or
            PunchTarget.TEXTURE_RIGHT is not None):
            continue

        z_norm = tg.depth(fi)

        # ── Fix 1: Trajectory match floor lane ──
        sx_center = cam.lane_x(tg.lane, z_norm)
        wz = tg.Z_VIS_NEAR + z_norm * (tg.Z_VIS_FAR - tg.Z_VIS_NEAR)
        wy = tg.WY_SPAWN + (tg.WY_HIT - tg.WY_SPAWN) * (1.0 - z_norm)
        wx = (sx_center - cam.cx_pix) * wz / cam.fx

        # ── Fix 2: Yaw billboard rotation ──
        YAW_FACTOR = 0.75
        yaw = -math.atan2(wx, wz) * YAW_FACTOR

        # Color BGR → RGB float
        b, g, r = tg.color
        color_rgb = (r / 255.0, g / 255.0, b / 255.0, 1.0)

        punch_blocks.append(PunchBlockInstance(
            position=(wx, wy, wz),    # corrected wx (Fix 1)
            half=tg.CUBE_HALF,
            color=color_rgb,
            z_norm=z_norm,
            yaw=yaw,                   # NEW (Fix 2)
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
```

---

# Migration Plan

| Phase | Step | Effort | Risk |
|---|---|---|---|
| **1** | Apply Fix #1 + #2 cho PunchTarget cv2 path | 15 phút | Thấp |
| **2** | Render preview test, verify visual | 10 phút | Thấp |
| **3** | Apply pattern cho DanceTarget (nếu cùng vấn đề) | 15 phút | Thấp |
| **4** | Apply pattern cho LineTarget (nếu cùng vấn đề) | 15 phút | Thấp |
| **5** | Apply pattern cho RelaxTarget (nếu cùng vấn đề) | 15 phút | Thấp |
| **6** | (Khi migrate ModernGL) — fix tự inherit qua spec | 0 phút | — |

**Total cv2 path**: 1-1.5 giờ for 4 target classes.

---

# Edge Cases

## 1. Lane fractional (vd lane 1.5 cho zigzag)
`cam.lane_x()` đã handle fractional lane (line 605-607) → tự work.

## 2. Block ở center lane (lane 1.5 với n_lanes=4)
- `wx ≈ 0`, `atan2(0, wz) = 0` → `yaw = 0` → không xoay ✅
- Trajectory: `sx_center ≈ cx_pix`, `wx ≈ 0` → match center ✅

## 3. Block ở `z_norm > 1` (xa hơn far)
`lane_x()` clamp `z=1` → `converge=0` → block ở vanishing point. Yaw tính bình thường.

## 4. Block ở `z_norm < 0` (quá near, edge case)
Không xảy ra trong gameplay (z_norm clamped 0..1 trong `depth()`).

## 5. `wz = 0` (block ngay tại camera)
`atan2(wx, 0) = ±π/2` → cube xoay 90° → degenerate. Edge case rất hiếm, code đã clip wz ≥ 0.05 trong `cam.project()`.

## 6. `cam.fx == 0`
`wx = (sx - cx) * wz / 0` → divide by zero. Check khi init camera, không xảy ra với fov_deg > 0.

---

# Verification — Test Cases

## Trajectory test

```python
def test_block_match_lane_at_near():
    cam = PerspectiveCamera(W=1280, H=720, n_lanes=4)
    z_norm = 0.0
    sx_center = cam.lane_x(0, z_norm)
    wz = 2.5
    wx = (sx_center - cam.cx_pix) * wz / cam.fx
    proj = cam.project(wx, 0, wz)
    assert abs(proj[0] - sx_center) < 1   # ≤ 1px error

def test_block_match_lane_at_mid():
    cam = PerspectiveCamera(W=1280, H=720, n_lanes=4)
    for lane in range(4):
        for z_norm in [0.1, 0.3, 0.5, 0.7, 0.9]:
            sx_center = cam.lane_x(lane, z_norm)
            wz = 2.5 + z_norm * 25.5
            wx = (sx_center - cam.cx_pix) * wz / cam.fx
            proj = cam.project(wx, 0, wz)
            assert abs(proj[0] - sx_center) < 2, f"lane={lane}, z={z_norm}"
```

## Yaw test

```python
def test_yaw_zero_at_center_lane():
    yaw = -math.atan2(0, 10) * 0.75
    assert abs(yaw) < 0.01

def test_yaw_positive_at_left_lane():
    wx, wz = -0.91, 2.5
    yaw = -math.atan2(wx, wz) * 0.75
    assert yaw > 0
    assert math.degrees(yaw) > 10

def test_yaw_negative_at_right_lane():
    wx, wz = 0.91, 2.5
    yaw = -math.atan2(wx, wz) * 0.75
    assert yaw < 0

def test_yaw_decreases_with_distance():
    yaw_near = abs(-math.atan2(-0.91, 2.5) * 0.75)
    yaw_far  = abs(-math.atan2(-0.91, 25.0) * 0.75)
    assert yaw_near > yaw_far

def test_yaw_factor_zero_disables_rotation():
    yaw = -math.atan2(-0.91, 2.5) * 0.0
    assert yaw == 0
```

## Visual A/B regression

```
1. Capture frame screenshot trước khi apply fix → save as before.png
2. Apply 2 fix
3. Capture cùng frame screenshot sau → save as after.png
4. Verify:
   ✓ Block trajectory align với floor lane outline
   ✓ Block ở outer lane: front face nghiêng về camera (yaw ~15°)
   ✓ Block ở center lane: yaw ≈ 0
   ✓ Block size + 3D shape vẫn correct
   ✓ Fist icon vẫn centered trên front face
   ✓ Glow halo follow cube silhouette
```

---

# Acceptance Criteria

```
✓ Block lane 0 (outer-left) bay align với floor lane 0 outline ở mọi z_norm (≤ 2px lệch)
✓ Block lane 1, 2 (inner) cũng align
✓ Block lane 3 (outer-right) align
✓ Khoảng cách 2 block adjacent lanes match spacing floor lanes ở cùng Z
✓ Block ở near outer lane: front face nghiêng ~15° quay về camera (yaw_factor=0.75)
✓ Block ở center lane: yaw = 0, không xoay
✓ Block ở far (z_norm > 0.8): yaw < 2°, gần như không xoay
✓ Side face vẫn visible nhẹ (vì yaw_factor < 1.0) cho 3D pop
✓ Cube shape + size shrinking theo perspective vẫn correct
✓ Top/front/side face shading không bị regress
✓ Fist icon centered trên front face (rotated cùng cube)
✓ Glow halo center match cube center
✓ Game logic (collision, hit detection, scoring) KHÔNG bị ảnh hưởng
✓ Performance impact: 0% measurable (per-block <0.005ms thêm)
```

---

# Performance Impact

Per-block per-frame overhead:
- 1 `cam.lane_x()` call: ~0.001ms
- 1 phép chia tính `wx`: negligible
- 1 `math.atan2` + 2 `cos/sin`: ~0.001ms
- 8 corners × 4 multiplications + 2 additions = 48 ops: negligible
- **Total**: ~0.003ms per block per frame

Với 6 blocks alive cùng lúc, 30fps render: **+0.5ms/sec total** = 0% measurable impact.

---

# Open Questions

1. **YAW_FACTOR default**:
   - Recommend `0.75` (giữ chút 3D pop)
   - Hay aggressive `1.0` (full billboard, mất hết side)?
   - Hay conservative `0.5` (vẫn thấy side rõ)?

2. **Apply scope**:
   - Chỉ PunchTarget (5 phút)?
   - Hay đồng thời PunchTarget + DanceTarget + LineTarget + RelaxTarget (1-1.5 giờ)?

3. **Expose qua render_settings**:
   - Cho user adjust `punch_yaw_factor` trong Inspector?
   - Hay hardcode 0.75?

4. **Trajectory fix cần cho cả mesh/texture path không**?
   - Mesh/texture path có dùng `lane_world_x()` không?
   - Nếu có → cần apply cùng fix.

5. **DanceTarget step icon position**:
   - DanceTarget vẽ floor squares với foot icon — không phải cube 3D như punch
   - Yaw fix có applicable không? (có thể yaw=0 là correct cho dance)

---

# References

- File spec ModernGL: `docs/moderngl-migration-punch-spec.md` (đã integrate cả 2 fix)
- File analysis trước: `docs/block-trajectory-fix-spec.md` (deprecated — replaced bởi spec này)
- Code chính: `src/rhythm.py` PunchTarget class (~line 2091-2290)
- Camera math: `src/rhythm.py` PerspectiveCamera class (~line 440-610)
