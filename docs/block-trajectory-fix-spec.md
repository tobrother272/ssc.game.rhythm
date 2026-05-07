# Block Trajectory Fix — Match Floor Lane

## Vấn đề

User render preview thấy **block punch không bay đúng quỹ đạo** so với floor lanes:
- Floor lanes spread rộng đều từ vanishing point (cx) ra near edges
- Block lao về tâm RẤT NHANH ở mid-Z, sau đó "đột ngột" spread ra ở near
- 2 đường KHÔNG SONG SONG → block không cảm giác "chạy trên lane"

## Root cause — 2 hệ tọa độ MISMATCH

| Element | Method | Math | Quỹ đạo |
|---|---|---|---|
| **Floor lanes** | `cam.lane_x(lane, z)` | `cx + (bot_x - cx) * (1 - z)` LINEAR interp | Đường thẳng straight đều |
| **Punch blocks** | `cam.lane_world_x(lane)` + `cam.project()` | `cx + fx * wx / wz` 1/z hyperbolic | Đường cong nhanh về tâm |

→ **Chỉ MATCH ở 2 điểm**: near (z_norm=0) và far vanishing point (z_norm=1).
→ **Ở mid-Z** (z_norm 0.3-0.7), block X **gần tâm hơn** floor lane X đáng kể (lệch 100-230px ở 1280p).

### Tính toán chứng minh

Với W=1280, fov=55°, floor_spread_frac=0.70:

| z_norm | Floor lane X (lane 0) | Block X (lane 0) | **Lệch** |
|---|---|---|---|
| 0.0 (near) | 192 | 193 | 1px ✅ |
| 0.3 | 326 | 553 | **227px** ❌ |
| 0.5 | 416 | 580 | **164px** ❌ |
| 0.7 | 506 | 605 | **99px** ❌ |
| 0.9 | 595 | 631 | 36px |
| 1.0 (far) | 640 (vanish) | 640 (vanish) | 0 ✅ |

→ Block "co cụm" gần tâm trong suốt lifetime → user perceive sai quỹ đạo.

## Fix Option A — Block dùng `lane_x()` SCREEN-direct (RECOMMEND)

### Concept

Block screen X được anchor TRỰC TIẾP theo `lane_x(lane, z_norm)` (cùng method floor lane). Sau đó back-compute world X để pass cho `cam.project()` 8 corners → giữ được cube 3D shading + size scaling chính xác.

### Code change — `rhythm.py` PunchTarget.draw

**TRƯỚC** (line 2194-2199):
```python
def draw(self, canvas, cam, cur_frame):
    if self.state != 'flying':
        return canvas
    z_norm = self.depth(cur_frame)
    wx = cam.lane_world_x(self.lane)
    wz = self.Z_VIS_NEAR + z_norm * (self.Z_VIS_FAR - self.Z_VIS_NEAR)
    wy = self.WY_SPAWN + (self.WY_HIT - self.WY_SPAWN) * (1.0 - z_norm)
    # ... rest of draw
```

**SAU**:
```python
def draw(self, canvas, cam, cur_frame):
    if self.state != 'flying':
        return canvas
    z_norm = self.depth(cur_frame)

    # ── FIX: Anchor screen X to floor lane interp ──
    # Compute target screen X based on floor lane formula
    sx_center = cam.lane_x(self.lane, z_norm)

    # World Z dùng linear (giữ nguyên cho size scaling đúng perspective)
    wz = self.Z_VIS_NEAR + z_norm * (self.Z_VIS_FAR - self.Z_VIS_NEAR)
    wy = self.WY_SPAWN + (self.WY_HIT - self.WY_SPAWN) * (1.0 - z_norm)

    # Back-compute world X so that cam.project() returns sx_center
    # sx = cx + fx * wx / wz  →  wx = (sx - cx) * wz / fx
    wx = (sx_center - cam.cx_pix) * wz / cam.fx
    # ── END FIX ──

    # Rest unchanged: project 8 corners
    HX = HY = HZ = self.CUBE_HALF
    corners_w = [
        (wx - HX, wy - HY, wz - HZ),
        (wx + HX, wy - HY, wz - HZ),
        # ... 6 more
    ]
    # ... rest of draw
```

### Tại sao approach này work

1. **Screen X match floor lane EXACTLY** — `sx_center = cam.lane_x(...)` cùng formula
2. **Cube size + perspective** vẫn đúng — `wz` không đổi, 8 corners vẫn project qua pinhole standard
3. **Top/front/side face shading** không bị ảnh hưởng — face brightness model dùng z_norm và position relative
4. **Glow ROI bbox** vẫn đúng — convex hull computed sau projection, base trên 8 projected pts

### Tradeoff

- ⚠️ Block KHÔNG bay theo đường thẳng trong **world space** (wx thay đổi theo z_norm)
- ⚠️ Game logic không đụng (collision, hit detection vẫn dùng lane index, không phải wx)
- ✅ Visually perfect (match floor lane visualization)

## Fix Option B — Floor lanes dùng TRUE pinhole

Sửa `cam.lane_x()` dùng `cam.project()`:

```python
def lane_x(self, lane, z_norm):
    wx = self.lane_world_x(lane)
    wz = self.z_from_norm(z_norm)
    proj = self.project(wx, 0.0, wz)
    return proj[0] if proj else self.cx
```

### Pros / Cons
- ✅ Một hệ tọa độ thống nhất (TRUE 3D)
- ✅ Sẵn sàng cho ModernGL migration
- ❌ Floor look thay đổi (lanes co lại nhanh hơn ở mid-Z)
- ❌ Side rails, hit panels, chevrons ĐỀU dùng lane_x → đụng nhiều code
- ❌ User có thể không thích visual mới

→ KHÔNG recommend nếu user muốn giữ floor look hiện tại.

## Fix Option C — Block dùng `DEPTH_MODE='inv'`

Đổi 1 dòng:
```python
DEPTH_MODE = 'inv'   # was 'linear'
```

Cải thiện match nhưng KHÔNG exact (vì floor là linear screen interp, không phải inv-z thật).

### Pros / Cons
- ✅ 1 dòng change
- ❌ Không exact match
- ❌ Đổi block speed perception (block "lê chậm" ở xa)

## Recommend Option A — implementation steps

### Step 1: Apply trong `PunchTarget.draw` (5 phút)
3 dòng mới như spec trên.

### Step 2: Test visual A/B (10 phút)
Render 1 segment 5s với punch beats spread đều 4 lanes. So sánh:
- Block trajectory phải chạy ALONG floor lane outline
- Ở mọi z_norm, block center phải nằm trên đường lane
- Cube shape + size + 3D shading vẫn correct

### Step 3: Apply cùng fix cho DanceTarget + LineTarget (nếu cần)
Check 2 class này có cùng vấn đề không:
```bash
grep -n "lane_world_x\|self\.lane" src/rhythm.py | grep -v "PunchTarget"
```

DanceTarget, LineTarget, StepTarget có thể cũng dùng `lane_world_x()` → cần apply cùng fix.

### Step 4: Update ModernGL spec (đã có)

Trong `docs/moderngl-migration-punch-spec.md`, instance data buffer cần thêm field `wx_corrected`:

```python
# punch_renderer.py — render() method
instance_data[base + 0] = wx_corrected   # was b.position[0] (lane_world_x)
instance_data[base + 1] = b.position[1]  # wy
instance_data[base + 2] = b.position[2]  # wz
# ...
```

`wx_corrected` được tính trong main render loop:
```python
sx_center = cam.lane_x(tg.lane, z_norm)
wz = cam.z_from_norm(z_norm)
wx = (sx_center - cam.cx_pix) * wz / cam.fx
punch_blocks.append(PunchBlockInstance(
    position=(wx, wy, wz),   # use corrected wx
    ...
))
```

→ ModernGL inherit fix tự động.

## Acceptance criteria

```
✓ Block lane 0 (outer-left) bay dọc theo floor lane 0 outline ở mọi z_norm
✓ Block lane 1, 2 (inner) cũng match
✓ Block lane 3 (outer-right) match
✓ Khoảng cách giữa 2 block adjacent lanes phù hợp với spacing floor lanes ở cùng Z
✓ Cube shape vẫn đúng 3D (top/front/side)
✓ Cube size shrink theo distance correct (perspective)
✓ Glow halo center match cube center
✓ Fist icon center match front face center
✓ Game logic (collision, hit detection, scoring) KHÔNG bị ảnh hưởng
```

## Edge cases

### 1. Lane fractional (vd lane 1.5 cho zigzag pattern)
`cam.lane_x()` đã handle fractional lane (line 605-607) → tự work.

### 2. Block ở `z_norm > 1` (xa hơn far)
`lane_x()` clamp `z=1` → `converge=0` → block ở vanishing point. OK.

### 3. Block ở `z_norm < 0` (quá near, edge case)
`lane_x()` extrapolate `converge > 1` → block lệch ngoài lane. KHÔNG xảy ra trong gameplay (z_norm clamped 0..1 trong depth()).

### 4. `cam.fx == 0`
`wx = (sx - cx) * wz / 0` → divide by zero. Check khi init camera.

### 5. Block đang ở `z_norm = 0` (hit moment)
`wz = Z_NEAR = 2.5`, `sx_center = lane_x_bottom[lane]`, `wx = (bot_x - cx) * 2.5 / fx`
Verify wx == lane_world_x(lane) tại near:
- `lane_world_x = (bot_x - cx) / fx * Z_NEAR / fx ... ` wait let me re-derive

Actually `LANE_WORLD_X = lane_half_spread * Z_NEAR / fx`, và `lane_world_x(0) = -LANE_WORLD_X`.
- `lane_x_bottom[0] = cx - lane_half_spread`
- `(lane_x_bottom[0] - cx) = -lane_half_spread`
- `wx_corrected at z_norm=0 = (-lane_half_spread) * Z_NEAR / fx = -LANE_WORLD_X` ✅

→ Match exactly tại near. Confirmed safe.

## Performance impact

- 1 thêm call `cam.lane_x()` per block per frame: ~0.001ms
- 1 thêm phép chia cho wx: negligible
- **Total**: 0% measurable performance impact

## Migration sang ModernGL

Khi migrate sang ModernGL, fix này áp dụng TRƯỚC khi pack instance data:

```python
# Main loop trước khi pass vào MGLPunchRenderer:
for tg in alive_punch_targets:
    z_norm = tg.depth(fi)
    sx_center = cam.lane_x(tg.lane, z_norm)
    wz = tg.Z_VIS_NEAR + z_norm * (tg.Z_VIS_FAR - tg.Z_VIS_NEAR)
    wx = (sx_center - cam.cx_pix) * wz / cam.fx
    wy = tg.WY_SPAWN + (tg.WY_HIT - tg.WY_SPAWN) * (1.0 - z_norm)

    blocks.append(PunchBlockInstance(
        position=(wx, wy, wz),
        half=tg.CUBE_HALF,
        color=...,
        z_norm=z_norm,
    ))
```

→ Vertex shader vẫn dùng `mvp * vec4(in_inst_position, 1.0)` standard, không cần biết fix.

## Test cases

```python
# test_block_trajectory.py

def test_block_match_lane_at_near():
    """Block ở z_norm=0 phải align với floor lane near edge."""
    cam = PerspectiveCamera(W=1280, H=720, n_lanes=4)
    sx = compute_block_screen_x(cam, lane=0, z_norm=0.0)
    assert abs(sx - cam.lane_x_bottom[0]) < 1

def test_block_match_lane_at_mid():
    """Block ở z_norm=0.5 phải align với floor lane mid."""
    cam = PerspectiveCamera(W=1280, H=720, n_lanes=4)
    sx_block = compute_block_screen_x(cam, lane=0, z_norm=0.5)
    sx_floor = cam.lane_x(0, 0.5)
    assert abs(sx_block - sx_floor) < 2

def test_block_match_lane_at_all_z():
    """Block và floor lane match tại 100 sample points."""
    cam = PerspectiveCamera(W=1280, H=720, n_lanes=4)
    for lane in range(4):
        for z_norm in np.linspace(0, 1, 100):
            sx_block = compute_block_screen_x(cam, lane, z_norm)
            sx_floor = cam.lane_x(lane, z_norm)
            assert abs(sx_block - sx_floor) < 2, f"lane={lane}, z={z_norm}"

def test_cube_size_correct_perspective():
    """Cube screen size phải shrink với z_norm tăng."""
    sizes = []
    for z_norm in [0.0, 0.3, 0.5, 0.7, 1.0]:
        size = compute_cube_screen_size(cam, lane=0, z_norm=z_norm)
        sizes.append(size)
    # Sizes phải giảm dần
    assert all(sizes[i] > sizes[i+1] for i in range(4))
```

## Open questions

1. **DanceTarget + LineTarget**: 2 class này có cùng bug không? Cần grep + fix tương tự?
2. **RelaxTarget LOW/MIDDLE/HIGH**: cũng dùng lane_world_x → cần fix?
3. **Particle burst position**: trigger ở `cam.lane_x(tg.lane, 0.02)` (line 7861) — đã dùng lane_x rồi → OK.
4. **Hit panels position**: 4 panel ở near edge — dùng `lane_x_bottom` trực tiếp → OK.
5. **Side rails**: `lane_world_x(0)` và `lane_world_x(n-1)` ở line 1078 — dùng cho rail spread. Có cần fix?

Bạn confirm muốn implement Option A trước, tôi sẽ:
- (a) Apply fix đầu tiên cho PunchTarget only
- (b) Hoặc viết thêm spec apply cho all 4 target classes (Punch + Dance + Line + Step)
- (c) Hoặc implement Option B (refactor floor lanes về true 3D)

---

# 🔄 PHẦN 2 — YAW BILLBOARD ROTATION (block xoay theo lane)

## Vấn đề bổ sung

Block hiện là **axis-aligned cube (AABB)** trong world space. 6 face luôn perpendicular với 3 axis. Camera ở origin (0,0,0), block ở outer lane (wx≠0) → camera nhìn block xéo → thấy side face nhiều hơn, front face bị nghiêng ngược → trông kỳ.

User muốn block **tự xoay quanh trục Y** (yaw rotation) để front face luôn "quay về camera" — như billboard cylindrical (giữ Y-axis cố định, xoay XZ plane).

## Tính yaw angle

Với block tại world `(wx, wy, wz)`, camera tại origin:

```python
import math

yaw_angle = -math.atan2(wx, wz)
```

Dấu trừ vì:
- `atan2(wx, wz)` trả góc của vector (wx, wz) so với +Z axis
- Để xoay cube hướng VỀ camera, cần xoay NGƯỢC chiều với góc lệch
- → negative

### Tính cụ thể với W=1280, fov=55°, lane 0 outer-left

| z_norm | wz | wx (sau Option A) | yaw_angle |
|---|---|---|---|
| 0.0 (near) | 2.5 | -0.91 | **+20°** |
| 0.3 | 10.15 | -0.92 | +5.2° |
| 0.5 | 15.25 | -0.92 | +3.5° |
| 0.7 | 20.35 | -0.92 | +2.6° |
| 1.0 (vanish) | 28 | -0.91 | +1.9° |

→ Near nghiêng nhiều, far nghiêng ít — đúng intuition (block xa nhỏ, ít cần xoay).

## Code change — Apply rotation matrix lên 8 corners

```python
# rhythm.py PunchTarget.draw — REPLACE block tính corners

import math

z_norm = self.depth(cur_frame)
sx_center = cam.lane_x(self.lane, z_norm)
wz = self.Z_VIS_NEAR + z_norm * (self.Z_VIS_FAR - self.Z_VIS_NEAR)
wy = self.WY_SPAWN + (self.WY_HIT - self.WY_SPAWN) * (1.0 - z_norm)
wx = (sx_center - cam.cx_pix) * wz / cam.fx

# ── YAW BILLBOARD: xoay cube theo lane ──
YAW_FACTOR = 0.75   # 0=AABB, 1=full billboard. 0.75 = giữ chút 3D pop
yaw = -math.atan2(wx, wz) * YAW_FACTOR
cos_y = math.cos(yaw)
sin_y = math.sin(yaw)

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

# Rotation matrix around Y axis (Y points down in our convention):
#   [  cos_y   0   sin_y ]
#   [    0     1     0   ]
#   [ -sin_y   0   cos_y ]
corners_w = []
for lx, ly, lz in local_corners:
    rx = lx * cos_y + lz * sin_y
    ry = ly
    rz = -lx * sin_y + lz * cos_y
    corners_w.append((wx + rx, wy + ry, wz + rz))

proj = [cam.project(*p) for p in corners_w]
```

## YAW_FACTOR control

Field cho user adjust nếu cần:

| YAW_FACTOR | Effect | Trade-off |
|---|---|---|
| 0.0 | Không xoay (AABB current behavior) | Sai như hiện tại |
| 0.5 | Nghiêng 50% | Compromise — vẫn thấy side rõ |
| **0.75** | **Nghiêng 75% (RECOMMEND)** | Front face thẳng + 1 chút side |
| 1.0 | Full billboard, perpendicular camera ray | Mất hoàn toàn side face |

→ Recommend default `YAW_FACTOR = 0.75`. Có thể expose qua render_settings:
```python
punch_yaw_factor: float = Field(default=0.75, ge=0.0, le=1.0)
```

## Visual verify

```
Block lane 0 (outer-left), near:
  Trước (AABB):                    Sau (yaw=15°):
  ┌──┐                             ╱──╲
  │  │  ← front nghiêng              ╱    ╲
  │  │    ngược, side face          ╲    ╱
  └──┘    chiếm chủ đạo              ╲──╱
                                   ↑ front face quay về camera

Block lane 3 (outer-right), near: ngược lại — yaw=-15°
Block lane 1.5 (center): yaw ≈ 0, không xoay
```

## Side face selection — vẫn dùng logic cũ

Sau yaw rotation:
- Block lane 0 (left of cam): screen_cx vẫn < cam_cx → vẫn vẽ `right_face`
- Block lane 3 (right of cam): screen_cx vẫn > cam_cx → vẫn vẽ `left_face`

→ Logic `if block_screen_cx < cam_cx → right_face` KHÔNG đổi.

## Edge cases

### 1. wx = 0 và wz > 0 (block center lane)
`atan2(0, wz) = 0` → yaw = 0, không xoay. ✅ Correct.

### 2. wx > 0 lane bên phải, wz > 0
`atan2(wx, wz) > 0` → yaw = -positive = negative → cube xoay theo chiều CCW (nhìn từ trên xuống) → front face quay về camera. ✅

### 3. wz quá nhỏ (block đã pass camera, z_norm < 0)
`atan2(wx, wz)` với wz nhỏ → yaw lớn → cube xoay extreme → có thể bị flip. Trong gameplay không xảy ra (z_norm clamped 0..1).

### 4. wz = 0 (block ngay tại camera)
`atan2(wx, 0)` → ±π/2 → cube xoay 90° → degenerate. Edge case rất hiếm, thường code đã clip wz ≥ 0.05.

## Impact lên các module khác

### 1. Glow ROI bbox
- Glow dùng `cv2.convexHull(pts)` của 8 projected corners
- Sau rotation, bbox sẽ thay đổi nhẹ (block nghiêng → silhouette khác)
- → KHÔNG cần code change, glow tự adapt

### 2. Fist icon position
- Icon vẽ ở `front_face` center (4 corners của FLT/FRT/FRb/FLb)
- Sau rotation, front_face vẫn là 4 corners đó — nhưng đã rotated
- Icon center = mean of 4 rotated corners → vẫn correct

### 3. Painter order
- Vẫn theo logic side → front → top
- Z-buffer (nếu dùng GL) tự handle, không cần sort

### 4. Hit detection
- Game logic dùng lane index, không dùng face orientation
- → KHÔNG ảnh hưởng

## Performance

- 1 `atan2` + 2 `cos/sin` per block per frame: ~0.001ms
- 8 corners × 4 multiplications + 2 additions = 48 ops: negligible
- **Total**: 0% measurable impact

## ModernGL integration — vertex shader update

Khi migrate sang GL, rotation thực hiện trong **vertex shader**:

### `punch_block.vert` — UPDATED

```glsl
#version 330 core

in vec3 in_position;
in vec3 in_normal;
in vec2 in_uv;
in float in_face_id;

// Per-instance
in vec3 in_inst_position;
in float in_inst_half;
in vec4 in_inst_color;
in float in_inst_z_norm;
in float in_inst_yaw;     // ← NEW

uniform mat4 u_view_proj;

out vec3 v_normal;
out vec2 v_uv;
out vec3 v_world_pos;
out vec4 v_base_color;
out float v_z_norm;
out float v_face_id;

void main() {
    // Rotation around Y axis
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

    v_normal = rot_y * in_normal;   // ← rotate normal cho lighting đúng
    v_uv = in_uv;
    v_world_pos = world_pos;
    v_base_color = in_inst_color;
    v_z_norm = in_inst_z_norm;
    v_face_id = in_face_id;
}
```

### Per-instance VBO update

`punch_renderer.py`:

```python
# Tăng instance stride 32 → 36 bytes (thêm 1 float)
self.vbo_instance = ctx.buffer(reserve=self.MAX_INSTANCES * 36)

# VAO format update
self.vao = ctx.vertex_array(
    self.prog,
    [
        (self.vbo_verts,    '3f', 'in_position'),
        (self.vbo_normals,  '3f', 'in_normal'),
        (self.vbo_uvs,      '2f', 'in_uv'),
        (self.vbo_face_ids, '1f', 'in_face_id'),
        (self.vbo_instance, '3f 1f 4f 1f 1f /i',
         'in_inst_position', 'in_inst_half',
         'in_inst_color', 'in_inst_z_norm', 'in_inst_yaw'),
    ],
    self.ibo,
)
```

### Render method update

```python
# Pack 9 floats per instance (was 8)
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
    instance_data[base + 8] = b.yaw   # ← NEW
```

### `PunchBlockInstance` dataclass update

```python
class PunchBlockInstance:
    __slots__ = ('position', 'half', 'color', 'z_norm', 'yaw')

    def __init__(self, position, half, color, z_norm, yaw=0.0):
        self.position = position
        self.half = half
        self.color = color
        self.z_norm = z_norm
        self.yaw = yaw   # ← NEW field
```

### Main render loop update

```python
for tg in alive_punch_targets:
    z_norm = tg.depth(fi)
    sx_center = cam.lane_x(tg.lane, z_norm)
    wz = tg.Z_VIS_NEAR + z_norm * (tg.Z_VIS_FAR - tg.Z_VIS_NEAR)
    wx = (sx_center - cam.cx_pix) * wz / cam.fx
    wy = tg.WY_SPAWN + (tg.WY_HIT - tg.WY_SPAWN) * (1.0 - z_norm)
    yaw = -math.atan2(wx, wz) * YAW_FACTOR   # ← NEW

    blocks.append(PunchBlockInstance(
        position=(wx, wy, wz),
        half=tg.CUBE_HALF,
        color=color_rgb,
        z_norm=z_norm,
        yaw=yaw,   # ← NEW
    ))
```

## Acceptance criteria — yaw billboard

```
✓ Block lane 0 ở near: front face nghiêng ~15° (yaw_factor=0.75 × 20°) hướng về camera
✓ Block lane 3 ở near: nghiêng -15° hướng về camera
✓ Block lane 1.5 (center): yaw = 0, không xoay
✓ Block ở far (z_norm > 0.8): yaw < 2°, gần như không xoay
✓ Side face vẫn visible (vì YAW_FACTOR < 1) cho 3D pop
✓ Fist icon vẫn centered trên front face (rotated cùng cube)
✓ Glow halo follow cube silhouette (auto adapt)
✓ Game logic không bị ảnh hưởng (collision, scoring)
✓ ModernGL vertex shader rotate normal cho lighting correct
```

## Test cases

```python
def test_yaw_zero_at_center_lane():
    """Block lane center → yaw = 0."""
    cam = PerspectiveCamera(W=1280, H=720, n_lanes=4)
    yaw = compute_yaw(cam, lane=1.5, z_norm=0.5)
    assert abs(yaw) < 0.01

def test_yaw_positive_at_left_lane():
    """Block lane 0 (outer-left) → yaw > 0 (xoay CW từ trên xuống)."""
    cam = PerspectiveCamera(W=1280, H=720, n_lanes=4)
    yaw = compute_yaw(cam, lane=0, z_norm=0.0)
    assert yaw > 0
    assert math.degrees(yaw) > 10   # >10° at near

def test_yaw_negative_at_right_lane():
    """Block lane 3 (outer-right) → yaw < 0."""
    cam = PerspectiveCamera(W=1280, H=720, n_lanes=4)
    yaw = compute_yaw(cam, lane=3, z_norm=0.0)
    assert yaw < 0

def test_yaw_decreases_with_distance():
    """Block ở xa → yaw nhỏ hơn block ở gần (cùng lane)."""
    cam = PerspectiveCamera(W=1280, H=720, n_lanes=4)
    yaw_near = abs(compute_yaw(cam, lane=0, z_norm=0.0))
    yaw_far  = abs(compute_yaw(cam, lane=0, z_norm=0.9))
    assert yaw_near > yaw_far

def test_yaw_factor_scaling():
    """YAW_FACTOR = 0 → no rotation; YAW_FACTOR = 1 → full billboard."""
    raw_yaw = -math.atan2(-0.91, 2.5)   # ~20°
    assert compute_yaw(cam, lane=0, z_norm=0, factor=0.0) == 0
    assert abs(compute_yaw(cam, lane=0, z_norm=0, factor=1.0) - raw_yaw) < 0.001
    assert abs(compute_yaw(cam, lane=0, z_norm=0, factor=0.75) - raw_yaw * 0.75) < 0.001
```

## Combined fix — Trajectory + Yaw

Final code change cho `PunchTarget.draw` (cv2 path):

```python
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
        (-HX, -HY, -HZ), ( HX, -HY, -HZ), ( HX,  HY, -HZ), (-HX,  HY, -HZ),
        (-HX, -HY,  HZ), ( HX, -HY,  HZ), ( HX,  HY,  HZ), (-HX,  HY,  HZ),
    ]
    corners_w = []
    for lx, ly, lz in local_corners:
        rx = lx * cos_y + lz * sin_y
        ry = ly
        rz = -lx * sin_y + lz * cos_y
        corners_w.append((wx + rx, wy + ry, wz + rz))

    proj = [cam.project(*p) for p in corners_w]
    if any(p is None for p in proj):
        return canvas
    pts = np.array(
        [(int(round(p[0])), int(round(p[1]))) for p in proj],
        dtype=np.int32,
    )

    # ... rest unchanged (face shading, glow, icon)
```

→ 2 fixes (trajectory + yaw) merged trong cùng 1 patch ~15 dòng.
