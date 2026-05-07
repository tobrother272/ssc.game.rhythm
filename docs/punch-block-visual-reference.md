# PunchTarget Block — Visual Reference Standard

## Mục tiêu

File này định nghĩa **visual standard cuối cùng** cho PunchTarget block 3D, dựa trên reference image user cung cấp. Mọi spec / code change liên quan tới PunchTarget render phải dùng file này làm **acceptance reference**.

## Reference image source

Block VÀNG (lane outer-right, near hit) trong reference image — đây là visual chuẩn để match.

---

## 1. MÀU SẮC & ĐỘ SÁNG TỐI 3 FACE

### Block VÀNG (base BGR ≈ `(50, 180, 255)` saturated amber)

| Face | Color BGR (estimate) | Brightness factor vs base | Mô tả |
|---|---|---|---|
| **Top** | `(122, 215, 244)` cream-yellow | **~1.40×** + ngả trắng | Sáng nhất, có highlight |
| **Front** | `(44, 172, 240)` saturated amber | **~1.00×** canonical | Đậm sắc, full saturation, mặt nhìn chính |
| **Side (left)** | `(26, 133, 194)` dark amber | **~0.60×** | Shadow, tối hơn front rõ rệt |

### Block XANH DƯƠNG (base BGR ≈ `(230, 80, 30)` blue)

| Face | Color BGR (estimate) | Brightness factor |
|---|---|---|
| **Top** | `(255, 180, 130)` light blue ngả trắng | **~1.40×** + ngả trắng |
| **Front** | `(230, 80, 30)` saturated blue | **~1.00×** canonical |
| **Side (right)** | `(140, 50, 18)` dark navy | **~0.60×** |

### Brightness ratio CHỐT

```
Top : Front : Side  =  1.40 : 1.00 : 0.60
```

**Quan sát quan trọng**:
- **Top SÁNG hơn front rõ rệt** + có ngả trắng (highlight)
- **Front giữ CANONICAL color** — đây là mặt user expect thấy đúng màu
- **Side TỐI hơn front rõ rệt** — không phải chỉ tối nhẹ

→ Brightness model formula đề xuất:

```python
top_col   = base * 1.20 + 255 * 0.20   # +20% bright + 20% white tint
front_col = base                         # canonical (1.0×)
side_col  = base * 0.55                  # 55% (slightly stronger than 0.45 in current spec)
```

So với current cv2 path:
| Field | Current | Reference standard |
|---|---|---|
| Top brightness factor | `c*1.15 + 255*0.15` | `c*1.20 + 255*0.20` |
| Front | `side*1.30*depth_gain` (BUG) | `c` canonical |
| Side | `c*0.45` | `c*0.55` (slightly brighter) |

---

## 2. DẠNG KHỐI 3D

### Shape
- Cube vuông thuần (cả 3 chiều bằng nhau, CUBE_HALF=0.154 world units)
- KHÔNG phải pill, KHÔNG phải sphere
- Có corner radius **NHẸ** (~5-8% size) — bo tròn nhẹ ở 4 góc top
- KHÔNG có texture detail trên surface (uniform color per face)

### Aspect ratio (3 face visible)
- **Front face**: chiếm ~70% silhouette area (mặt chính)
- **Top face**: chiếm ~20% (do camera nhìn xéo từ trên)
- **Side face**: chiếm ~10% (chỉ thấy 1 bên do perspective)

### Camera angle
- First-person low angle
- Camera ở origin, looking +Z forward
- Eye level slightly above block center → top face visible
- Block ở outer lane → side face (đối diện camera direction) visible

### Material feel
- **Glossy/satin** (không flat matte)
- Subtle highlight gradient từ top edge xuống → cảm giác light from above
- Có **gradient nhẹ** trong từng face (không flat fill)

### Corner rounding implementation
- Current cv2 path: `CORNER_RADIUS=0.08`, áp dụng cho front + top, side skip (mỏng)
- Reference: nên giữ `CORNER_RADIUS=0.06-0.08` cho subtle look
- ModernGL path: SDF rounded corners trong fragment shader

---

## 3. CÁC CẠNH (EDGES)

### Rim lighting trên 3 edges chính

| Edge | Highlight color | Thickness | Strength |
|---|---|---|---|
| **Top-front** (FLT→FRT) | `(144, 232, 255)` vàng-trắng | ~2-3px | **MẠNH** |
| **Top-side** (FRT→BRT cho block right) | Same color | ~2-3px | **MEDIUM** |
| **Front-side** (vertical FRT→FRb) | Same color | ~2px | **WEAK** (subtle) |

### Anti-aliasing
- **Tất cả edges anti-aliased smooth** (LINE_AA at minimum)
- KHÔNG jagged step pixels
- Khi resolution thấp (preview 540p) — vẫn phải có MSAA hoặc subpixel smooth

### Edge bottom
- **KHÔNG có rim light** (shadow zone)
- Edge giao với front bottom + side bottom = transition tối, không highlight

### Edge orientation
- 3 edges visible (top-front, top-side, front-side)
- Hidden edges (top-back, side-back, etc) không cần render

### Implementation note
```python
# Rim light formula
RIM_COLOR = base_color * 1.45 + (30, 30, 30)   # bright + slight white shift
RIM_THICKNESS = max(1, int(top_w * 0.025))      # ~2.5% face width

# Apply on top-front edge (always)
cv2.line(canvas, pts[0], pts[1], RIM_COLOR, RIM_THICKNESS, lineType=cv2.LINE_AA)

# Top-side edge (depends on which side visible)
if block_screen_cx < cam_cx:
    cv2.line(canvas, pts[1], pts[5], RIM_COLOR, RIM_THICKNESS, ...)
else:
    cv2.line(canvas, pts[0], pts[4], RIM_COLOR, RIM_THICKNESS, ...)

# Front-side edge (subtle, weaker)
RIM_DIM = tuple(int(c * 0.7) for c in RIM_COLOR)
RIM_DIM_THICKNESS = max(1, RIM_THICKNESS - 1)
```

---

## 4. GLOW HALO

### Spec
- **Neon halo** xung quanh block
- Color = base color (không phải white)
- Lan ra ~30-50px ở 1080p
- Multi-layer feel:
  - Inner glow: tight (~5-10px), strong intensity
  - Outer glow: spread (~30-50px), low intensity
- Mạnh nhất ở các **edge highlight** zones

### Implementation
- cv2 path: `_draw_block_glow_roi` với `kernel=35-45`, `weight=0.95`
- Glow source: convex hull của 8 projected corners (NOT chỉ top face như current)
- ModernGL path: built-in trong fragment shader (Fresnel rim + outer halo via SDF)

### Bloom global
- Sau khi block render xong, full canvas bloom với `gpu_glow(sigma=24, gain=0.75)`
- Bloom global LÀM ĐẸP THÊM nhưng KHÔNG thay glow per-block
- Cả 2 đều cần thiết

---

## 5. FIST ICON TRÊN FRONT FACE

### Position
- **FRONT face** (mặt camera nhìn thẳng) — KHÔNG phải top face
- Centered trong front face
- Size ~75-80% front face dimension

### Design (closed fist, NOT open palm)
- **Đen solid** (silhouette)
- **Outline đậm** ~3-4% icon size, màu đen `(20, 20, 20)`
- **Fill** ~95% area, màu trắng `(245, 245, 245)`
- **4 knuckle bumps** trên top edge của fist
- **3 finger grooves** vertical bên dưới knuckles (đường rãnh chia ngón)
- **Thumb wrapped horizontal** ở giữa fist (taper sang phải)

### Polygon vertex layout
- ~14-20 vertices cho silhouette outline
- Đối xứng trái-phải

### Reference design ID
- Match `_draw_fist_icon_v3` spec trong `docs/layer-combo-spec.md` (closed fist v3)

---

## 6. BACKGROUND CONTEXT

### Floor
- 2 track pink/magenta `#E91E63` chevron
- Tách rời ở giữa (gap ~10% lane width)
- Stripes scrolling perspective

### Side rails
- Slim ceiling beams (không phải walls)
- Floor accent dot lights pink `#FF1493`

### Lighting environment
- Background đen sâu `#000000` to `#0A0814`
- Có sparkle / star points distant (subtle)
- Bloom global tỏa từ blocks + tracks

---

## 7. BRIGHTNESS MODEL — RECOMMENDED FORMULAS

### cv2 path (rhythm.py)

```python
# Replace block tính brightness
base = tuple(int(c) for c in self.color)

# Top: 1.20× base + 20% white tint
top_col = tuple(
    int(min(255, c * 1.20 + 255 * 0.20))
    for c in base
)

# Front: CANONICAL (1.0× base)
front_col = tuple(int(c) for c in base)

# Side: 0.55× base
side_col = tuple(int(c * 0.55) for c in base)

# Depth gain (slight darkening for far blocks)
depth_gain = 0.70 + 0.30 * (1.0 - z_norm)

# Apply scaling
top_scaled   = tuple(int(min(255, c * depth_gain)) for c in top_col)
front_scaled = tuple(int(min(255, c * depth_gain)) for c in front_col)
side_scaled  = tuple(int(min(255, c * depth_gain)) for c in side_col)
```

### ModernGL fragment shader

```glsl
// Per-face brightness factors
float top_factor   = 1.20;
float front_factor = 1.00;
float side_factor  = 0.55;

vec3 white_tint = vec3(0.0);
float face_factor;

if (v_face_id == 2.0) {           // top
    face_factor = top_factor;
    white_tint = vec3(0.20);
} else if (v_face_id == 0.0) {    // front
    face_factor = front_factor;
} else {                          // side
    face_factor = side_factor;
}

vec3 face_color = base * face_factor + white_tint;

// Subtle gradient mỗi face
float gradient_factor = 1.0;
if (v_face_id == 0.0) {           // front: top→bottom
    gradient_factor = mix(1.05, 0.85, v_uv.y);
} else if (v_face_id == 2.0) {    // top: front→back
    gradient_factor = mix(1.10, 0.85, v_uv.y);
} else {                          // side: front→back
    gradient_factor = mix(1.05, 0.80, v_uv.y);
}
face_color *= gradient_factor;

// Fresnel rim
float rim = 1.0 - max(dot(N, V), 0.0);
rim = pow(rim, 1.8);
vec3 rim_color = base * 1.45 + vec3(0.18);
face_color += rim_color * rim * 0.40;
```

---

## 8. ACCEPTANCE CHECKLIST

Khi render output match reference image:

```
✓ Top face SÁNG hơn front rõ rệt (~1.40× vs 1.00×) + ngả trắng
✓ Front face giữ CANONICAL base color (saturated)
✓ Side face TỐI hơn front rõ rệt (~0.60× vs 1.00×)
✓ Brightness ratio top:front:side ≈ 1.4:1.0:0.6
✓ 3D structure rõ rệt — front ~70%, top ~20%, side ~10% silhouette
✓ Cube vuông với corner radius NHẸ (5-8%)
✓ Material glossy/satin (có gradient subtle, không flat)
✓ Edge top-front có RIM LIGHT mạnh (vàng-trắng, ~2-3px)
✓ Edge top-side có rim light medium
✓ Edge front-side có rim subtle
✓ Tất cả edges anti-aliased smooth (LINE_AA hoặc MSAA)
✓ KHÔNG jagged pixel steps ở mọi resolution
✓ Glow halo neon xung quanh block (~30-50px)
✓ Glow color = base color (không white)
✓ Fist icon trên FRONT face (không phải top)
✓ Fist closed (knuckles + grooves + thumb wrap), KHÔNG phải open palm
✓ Icon đen solid với outline đậm + fill trắng
✓ Icon size ~75-80% front face, centered
✓ Block ở outer lane CÓ XOAY (yaw billboard) → front face quay về camera
```

---

## 9. SỰ KHÁC BIỆT VỚI CURRENT cv2 PATH

| Aspect | Current cv2 (rhythm.py) | Reference standard |
|---|---|---|
| Top brightness | `c*1.15 + 255*0.15` | `c*1.20 + 255*0.20` (sáng hơn) |
| Front brightness | `side*1.30*depth_gain` (BUG) | `c` canonical |
| Side brightness | `c*0.45` | `c*0.55` (slightly brighter) |
| Front/top gradient | Flat fill | Subtle gradient |
| Rim lighting | KHÔNG có | Có trên 3 edges |
| Edge AA | LINE_AA only | LINE_AA + MSAA (GL path) |
| Yaw rotation | KHÔNG | YAW_FACTOR=0.75 |
| Trajectory match floor | Lệch ~227px ở mid-Z | Match ≤ 2px |

---

## 10. RELATED SPECS

- `docs/punch-block-render-fix-spec.md` — Brightness fix (Phase 2 v3)
- `docs/block-positioning-fix-spec.md` — Trajectory + Yaw billboard
- `docs/moderngl-migration-punch-spec.md` — GL pipeline implementation
- `docs/layer-combo-spec.md` — Fist icon design v3 (NEON GLOW STYLE v3)

Mọi spec trên phải tham chiếu đến file này như **single source of truth** cho visual standard.
