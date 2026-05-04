# Hit-Block Overlay — Pinhole Projection Fix Spec (Approach A)

## Mục tiêu

Sửa logic tính 4 hit-block trapezoids trong `FloorWallOverlay._hit_block_polys` (file `studio/editor/preview_panel.py`) để **match chính xác** với renderer's actual tile rendering từ `TunnelRenderer._draw_floor_tiles_legacy` (file `src/rhythm.py`).

Hiện tại overlay dùng heuristic `(1-z)^1.6` exponent → vẽ hit blocks SAI vị trí, kích thước. Khi user bật Edit Layout thấy 4 panels nhỏ ở vị trí A, khi tắt overlay thấy 4 tiles thực ở vị trí B. Hai vị trí khác nhau ~50px theo y, ~100px theo width.

Spec này thay heuristic bằng **pinhole projection** đồng bộ với `PerspectiveCamera.project()`.

Spec này độc lập với:
- [`preview-overlay-fixes-spec.md`](./preview-overlay-fixes-spec.md) — fix data-loss + add floor overlay handles + merge stickman vào Edit Layout button. Spec này là **hậu quả** sau khi `_hit_block_polys` đã được thêm vào (Feature 1 của preview-overlay spec). Nếu chưa implement preview-overlay spec, fix này áp dụng vào file gốc cũng OK.

---

## Code hiện tại (sai)

`FloorWallOverlay._hit_block_polys` (preview_panel.py dòng ~445-484):

```python
def _hit_block_polys(self, n: int = 4) -> list:
    """Trapezoid polygons matching the nearest floor-tile row drawn by
    _draw_floor_tiles_legacy(lane_tiles=True) in rhythm.py.
    ...
    """
    hy = self._hit_y()
    cy = self._hz_y()
    if hy <= cy or n < 2:
        return []
    cx = self.width() / 2.0
    lx = float(self._near_lx_raw())
    rx = float(self._near_rx_raw())
    step = (rx - lx) / (n - 1)
    half_w = step * 0.40

    # Heuristic exponents (SAI — không match renderer)
    c_front_y = (1.0 - 0.02) ** 1.6   # ≈ 0.968
    c_back_y  = (1.0 - 0.12) ** 1.6   # ≈ 0.814
    c_front_x = (1.0 - 0.02)
    c_back_x  = (1.0 - 0.12)

    y_front = int(cy + c_front_y * (hy - cy))
    y_back  = int(cy + c_back_y  * (hy - cy))
    polys = []
    for i in range(n):
        xc = lx + i * step
        xl = xc - half_w
        xr = xc + half_w
        polys.append([
            QPoint(int(cx + (xl - cx) * c_front_x), y_front),
            QPoint(int(cx + (xr - cx) * c_front_x), y_front),
            QPoint(int(cx + (xr - cx) * c_back_x),  y_back),
            QPoint(int(cx + (xl - cx) * c_back_x),  y_back),
        ])
    return polys
```

Vấn đề: dùng `(1-z)^1.6` cho y và `(1-z)^1.0` cho x. Đây là **heuristic curve**, không phải pinhole projection thực mà renderer dùng.

---

## Math chuẩn (renderer)

`TunnelRenderer._draw_floor_tiles_legacy` (rhythm.py dòng ~1152-1267):

```python
# World constants
z_slots = [3.0, 5.5, 8.5, 12.5, 17.5]  # tile z positions (world meters)
tile_len = 1.6                           # tile depth along z
scroll = (frame * 0.30) % (z_slots[1] - z_slots[0])  # = (frame*0.3) % 2.5

# Lane positions (world)
x_centers = [cam.lane_world_x(i) for i in range(cam.n_lanes)]
step = abs(cam.lane_world_x(1) - cam.lane_world_x(0))
tile_w = max(0.25, step * 0.80)

# Per tile per lane:
for lane_i, xc in enumerate(x_centers):
    for z_c in z_slots:
        wz = z_c - scroll
        if wz < cam.Z_NEAR + 0.2:
            continue   # tile clipped (too close to camera)
        corners_world = [
            (xc - tile_w/2, FLOOR_WORLD_Y, wz - tile_len/2),  # near-left
            (xc + tile_w/2, FLOOR_WORLD_Y, wz - tile_len/2),  # near-right
            (xc + tile_w/2, FLOOR_WORLD_Y, wz + tile_len/2),  # far-right
            (xc - tile_w/2, FLOOR_WORLD_Y, wz + tile_len/2),  # far-left
        ]
        proj = [cam.project(*c) for c in corners_world]
```

`cam.project()` là pinhole projection:

```python
def project(self, wx, wy, wz):
    if wz <= 0:
        return None
    sx = self.cx_pix + self.fx * wx / wz
    sy = self.cy_pix + self.fy * wy / wz
    return (sx, sy)  # Plus depth_scale, omitted for brevity
```

Với:
- `cx_pix = W / 2` (pixel center horizontal)
- `cy_pix = cy_v = horizon_frac * H` (pixel center vertical = horizon line)
- `fx = W / 2 / tan(fov_deg / 2 * π/180)` (focal length, in pixels)
- `fy = fx` (square pixels)

Constants ở `PerspectiveCamera.__init__`:
- `Z_NEAR = 2.5`
- `Z_FAR = 28.0`
- `fov_deg = 55.0` (default)
- `FLOOR_WORLD_Y = (y_hit - cy_pix) * Z_NEAR / fy`
- `LANE_WORLD_X = lane_half_spread * Z_NEAR / fx`
  - `lane_half_spread = W * floor_spread_frac * 0.5`

`cam.lane_world_x(i)`:
- Lane spacing trong world = `2 * LANE_WORLD_X / (n_lanes - 1)`
- `lane_world_x(i) = -LANE_WORLD_X + i * spacing`
- Với `n_lanes = 4`: `[-L, -L/3, +L/3, +L]` (L = LANE_WORLD_X)

---

## Code mới (đúng)

Replace `_hit_block_polys` bằng:

```python
import math   # add at top of file if not already

def _hit_block_polys(self, n: int = 4, frame_idx: int = 0) -> list:
    """Trapezoid polygons matching the nearest floor-tile row drawn by
    _draw_floor_tiles_legacy(lane_tiles=True) in rhythm.py.

    Uses **pinhole projection** to exactly match PerspectiveCamera.project()
    in rhythm.py. Optional ``frame_idx`` enables tile scroll animation;
    pass 0 for a static representation (z_slots[0] without scroll).
    """
    H_w = self.height()
    W_w = self.width()
    if H_w <= 0 or W_w <= 0 or n < 2:
        return []

    hy = self._hit_y()         # y_hit in widget pixels
    cy = self._hz_y()          # cy_v (horizon) in widget pixels
    if hy <= cy:
        return []              # invalid: floor above horizon

    # ── Mirror PerspectiveCamera constants ────────────────────────────
    fov_deg = 55.0
    Z_NEAR = 2.5
    cx_pix = W_w / 2.0
    cy_pix = float(cy)         # horizon in widget pixels
    fx = W_w / 2.0 / math.tan(math.radians(fov_deg) / 2.0)
    fy = fx

    # FLOOR_WORLD_Y derived from y_hit (same formula as renderer)
    FLOOR_WORLD_Y = (float(hy) - cy_pix) * Z_NEAR / fy

    # Lane positions (world) — match cam.lane_world_x
    # lane_half_spread = W * floor_spread_frac * 0.5  (floor_spread = self._near_spread)
    lane_half_spread_widget = W_w * self._near_spread * 0.5
    LANE_WORLD_X = lane_half_spread_widget * Z_NEAR / fx
    if n == 1:
        x_centers = [0.0]
    else:
        spacing = 2.0 * LANE_WORLD_X / (n - 1)
        x_centers = [-LANE_WORLD_X + i * spacing for i in range(n)]

    # Tile geometry — match renderer
    z_slots = [3.0, 5.5, 8.5, 12.5, 17.5]
    tile_len = 1.6
    step_world = abs(x_centers[1] - x_centers[0]) if n >= 2 else 0.0
    tile_w = max(0.25, step_world * 0.80)

    # Tile scroll — match renderer's `scroll = (frame * 0.30) % 2.5`
    scroll = (float(frame_idx) * 0.30) % (z_slots[1] - z_slots[0])

    # Pick FIRST visible tile per lane (smallest wz that passes clip)
    polys = []
    z_clip = Z_NEAR + 0.2
    for lane_i, xc in enumerate(x_centers):
        chosen_wz = None
        for z_c in z_slots:
            wz = z_c - scroll
            if wz < z_clip:
                continue
            chosen_wz = wz
            break
        if chosen_wz is None:
            continue
        # Project 4 world corners to widget pixels
        corners_world = [
            (xc - tile_w / 2.0, FLOOR_WORLD_Y, chosen_wz - tile_len / 2.0),
            (xc + tile_w / 2.0, FLOOR_WORLD_Y, chosen_wz - tile_len / 2.0),
            (xc + tile_w / 2.0, FLOOR_WORLD_Y, chosen_wz + tile_len / 2.0),
            (xc - tile_w / 2.0, FLOOR_WORLD_Y, chosen_wz + tile_len / 2.0),
        ]
        proj = []
        valid = True
        for wx, wy, wz_c in corners_world:
            if wz_c <= 1e-6:
                valid = False
                break
            sx = cx_pix + fx * wx / wz_c
            sy = cy_pix + fy * wy / wz_c
            proj.append(QPoint(int(round(sx)), int(round(sy))))
        if valid and len(proj) == 4:
            polys.append(proj)
    return polys
```

### Điểm khác biệt code cũ → mới

| Aspect | Cũ (heuristic) | Mới (pinhole) |
|---|---|---|
| `c_front_y` | `(1-0.02)^1.6 ≈ 0.968` | `1 + Z_NEAR/wz_front` (qua project) |
| `c_back_y`  | `(1-0.12)^1.6 ≈ 0.814` | `1 + Z_NEAR/wz_back` (qua project) |
| `c_front_x` | `(1-0.02) = 0.98` | `Z_NEAR/wz_front` |
| `c_back_x`  | `(1-0.12) = 0.88` | `Z_NEAR/wz_back` |
| z range | hardcoded `0.02..0.12` (z_norm) | dynamic `z_slots[0..4] - scroll` (world) |
| Tile width | `step * 0.80` (screen px) | `step_world * 0.80` (world meters) |
| Scroll | không có | có (nếu pass `frame_idx`) |

---

## Touch points

### 1. Sửa `_hit_block_polys` (preview_panel.py)

Replace toàn bộ method theo code mới ở trên. Thêm `import math` ở đầu file nếu chưa có.

### 2. Optional: Pass `frame_idx` để có scroll động

Hiện tại overlay không biết current frame. Gọi từ `paintEvent`:

```python
# CŨ:
for _poly in self._hit_block_polys():
    p.drawPolygon(_poly)

# MỚI:
fi = self._cached_frame_idx   # see below
for _poly in self._hit_block_polys(n=4, frame_idx=fi):
    p.drawPolygon(_poly)
```

Cần thêm `self._cached_frame_idx: int = 0` vào `__init__`. Update mỗi tick từ ngoài:

```python
def set_frame_idx(self, frame_idx: int) -> None:
    """Update cached frame index for accurate tile scroll visualization."""
    self._cached_frame_idx = int(frame_idx)
    if self._floor_wall_edit_active or True:  # always update if needed
        self.update()
```

(Method này đặt vào `FloorWallOverlay`.)

### 3. PreviewPanel call `set_frame_idx` mỗi tick

Trong `_render_live_frame` (preview_panel.py dòng ~2920), sau khi render frame xong:

```python
# Sau dòng cuối self.live_label.setPixmap(pix):
if self._floor_wall_edit_active and hasattr(self, "floor_wall_overlay"):
    # Compute frame index from t_sec * fps
    rdr = self._live_renderer
    if rdr is not None:
        fi = int(round(float(t_sec) * float(rdr.fps)))
        self.floor_wall_overlay.set_frame_idx(fi)
```

### 4. Verify n_lanes constant

Renderer dùng `cam.n_lanes` (= `N_LANES = 4` từ rhythm.py constant). Overlay hiện hardcode `n=4` trong default param. Match ✓.

Nếu sau này renderer support n_lanes != 4, overlay cần sync. Hiện tại không có concern.

---

## Optional simpler version (no scroll)

Nếu không muốn deal với scroll animation, có thể giữ `frame_idx=0` (static):

```python
def _hit_block_polys(self, n: int = 4) -> list:
    # ... same as above but:
    scroll = 0.0    # static, no animation
    # ... rest unchanged
```

Tile vị trí static cố định ở `z_slots[0] = 3.0`. Khi renderer scroll, tile thực sẽ animation, overlay đứng yên. **Mismatch dynamic** nhưng acceptable cho edit purpose.

Nếu chọn static, KHÔNG cần touch point #2 và #3 (skip set_frame_idx wiring).

**Đề xuất**: bắt đầu với **static (no scroll)** cho V1. Nếu user vẫn thấy mismatch, thêm scroll handling sau.

---

## Test scenarios

### Test 1: Static accuracy (Floor at default 0.86)

```
Setup: hit_frac=0.86, horizon_frac=0.45, near_spread=0.65, fov=55, frame=0.
Bật Edit Layout.
Verify: 4 hit-block polys hiển thị ở y khoảng 564-610 trong widget pixels (cho widget_h=460).
Verify: 4 polys có width đáy ≈ 80% lane spacing (gap nhỏ giữa các tile).
```

### Test 2: So sánh với renderer

```
Setup: cùng config như Test 1, không có scroll (frame=0).
Capture screenshot Edit Layout ON.
Capture screenshot Preview (overlay OFF).
Verify: 4 polys overlay ALIGN PIXEL-BY-PIXEL với 4 tiles renderer (sai số < 2px).
```

### Test 3: Floor handle drag → overlay polys move sync

```
Setup: Edit Layout ON, kéo Floor handle xuống dần.
Verify: 4 hit-block polys di chuyển XUỐNG SYNC với renderer's tiles (cả overlay polys lẫn renderer tiles cùng dịch xuống cùng tốc độ).
```

### Test 4: Near handle drag → polys spread

```
Setup: Edit Layout ON, kéo Near handle ra ngoài.
Verify: 4 polys spread WIDER, match renderer's tile spread.
Verify: Width của mỗi tile cũng tăng (vì step_world tăng → tile_w tăng).
```

### Test 5: Edge case — Floor cao hơn Horizon

```
Setup: hit_frac=0.30, horizon_frac=0.45 (floor ABOVE horizon — invalid).
Verify: _hit_block_polys trả về list rỗng [] (early return at hy <= cy).
Không crash, không vẽ poly nào.
```

### Test 6: Scroll animation (nếu implement touch points 2-3)

```
Setup: live preview đang chạy, Edit Layout ON.
Verify: 4 hit-block polys SCROLL về camera đồng pha với renderer's tiles.
Pause preview.
Verify: polys đứng yên cùng tile renderer.
```

### Test 7: Backward compat — không touch frame_idx

```
Nếu giữ static (frame_idx=0):
Verify: polys không animate, nhưng vẫn match tile renderer ở frame 0.
Khi tile renderer scroll, polys offset dần — nhưng position cốt lõi vẫn gần đúng.
Acceptable cho V1.
```

### Test 8: Resize widget

```
Setup: Edit Layout ON, resize preview window.
Verify: polys resize tỉ lệ theo widget_h, vẫn match renderer's tiles ở scale mới.
```

### Test 9: Different fov (nếu sau này configurable)

```
Hiện tại fov_deg = 55 hardcode trong cả overlay và renderer.
Verify: nếu sau này fov đổi, OVERLAY phải đọc cùng giá trị từ renderer.
Hiện tại không có concern (cả 2 đều 55).
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **Renderer's `_draw_floor_tiles_legacy`** không đổi. Spec này CHỈ sửa overlay.

2. **`PerspectiveCamera` constants** (`Z_NEAR=2.5`, `fov_deg=55`, `tile_len=1.6`, `z_slots=[3.0, 5.5, ...]`) phải KHỚP giữa overlay và renderer. Nếu sau này renderer thay đổi, overlay phải sync — hoặc convert sang Approach B (query renderer trực tiếp).

3. **Comment ở `_hit_block_polys`** cập nhật thành "Pinhole projection matching cam.project() in rhythm.py" để rõ relationship.

4. **Tile clip rule** `wz < Z_NEAR + 0.2 → continue` phải khớp renderer (xem rhythm.py dòng 1196).

5. **Lane spacing math** phải khớp `cam.lane_world_x(i)` — phân bố đều giữa `-LANE_WORLD_X` và `+LANE_WORLD_X`.

6. **`tile_w` clamp** `max(0.25, step_world * 0.80)` khớp renderer (rhythm.py dòng 1171).

7. **n_lanes**: hiện tại renderer dùng N_LANES=4. Overlay default n=4. Nếu thay đổi sau này, sync 2 chỗ.

8. **Project output** dùng widget pixels (cx_pix, cy_pix, fx, fy đều scale theo widget size, không phải renderer canvas size). Đây là KEY insight: nếu widget aspect KHÁC với renderer canvas aspect (16:9), projection vẫn đúng vì overlay được set geometry theo `_rendered_image_rect_global` (letterboxed image area, cùng aspect 16:9).

9. **Scroll animation** (touch point 2-3) là OPTIONAL. Nếu skip, overlay polys static — vẫn close enough.

10. **Math `cam.lane_world_x(i)`** vs lane positions in overlay: cần verify exact formula. PerspectiveCamera dùng:
    ```python
    step = (2 * lane_half_spread) / (n_lanes - 1)
    lane_x_bottom = [cx - lane_half_spread + i * step for i in range(n_lanes)]
    ```
    Project lane_x_bottom (PIXEL value at z=0) back to world: `wx = (lane_x_bottom[i] - cx_pix) * Z_NEAR / fx`. Match `lane_world_x(i)`. Confirmed via:
    ```
    lane_world_x(0) = (-lane_half_spread) * Z_NEAR / fx = -LANE_WORLD_X ✓
    lane_world_x(3) = (+lane_half_spread) * Z_NEAR / fx = +LANE_WORLD_X ✓
    ```

---

## Pattern code hiện có để tham khảo

- **`PerspectiveCamera.project`** (rhythm.py, search "def project"): pinhole formula source of truth.

- **`PerspectiveCamera.__init__`** (rhythm.py dòng ~407-475): tất cả derived constants (fx, fy, FLOOR_WORLD_Y, LANE_WORLD_X).

- **`_draw_floor_tiles_legacy`** (rhythm.py dòng ~1152-1267): tile geometry source.

- **Existing `_floor_footprint_points`** (preview_panel.py dòng ~429-438): tham khảo cách tính trapezoid trong widget pixels.

---

## Thứ tự implement đề xuất

1. **Replace `_hit_block_polys`** với code mới dùng pinhole. Bắt đầu với `frame_idx=0` static, không touch caller. Test 1, 2, 5.

2. **Verify visually**: chạy app, bật Edit Layout, so sánh overlay polys với renderer tiles. Confirm align pixel-by-pixel (Test 2).

3. **Test edge cases**: drag Floor / Near / Far handles, verify overlay polys move sync (Test 3, 4).

4. **Optional — scroll animation**: nếu user vẫn thấy mismatch khi animation scroll, thêm:
   - `set_frame_idx` method vào `FloorWallOverlay`
   - `_cached_frame_idx` attribute
   - Wire trong `_render_live_frame` (touch point 3)
   Test 6.

5. **Cleanup comment**: cập nhật docstring `_hit_block_polys` để phản ánh pinhole logic.

6. **Smoke test toàn bộ Edit Layout**: tất cả handles hoạt động bình thường, không có visual regression khác.

---

## Open questions

(1) **Static vs animated scroll** cho V1: tôi đề xuất static (frame_idx=0). Bạn confirm hay muốn animated luôn?

(2) **fov_deg = 55 hardcode**: nếu sau này có config thì overlay cần đọc từ renderer state. Hiện tại OK hardcode. Bạn confirm?

(3) **Position mismatch khi `Floor` handle xuống quá thấp (hit_frac → 1.0)**: tile front-most có wz lớn dần (vì FLOOR_WORLD_Y tăng → tile có thể vượt xuống dưới screen). Có cần extra clamp không, hay để renderer tự xử lý?

(4) **Có muốn approach B (query renderer) parallel** không, hay chỉ A là đủ? Tôi đề xuất A trước, nếu sau này math drift thì migrate sang B.

(5) **Color/style của overlay polys** sau khi fix có giữ nguyên (`_hit_fill = QColor(90, 70, 20, 65)`, `_hit_edge = QColor(90, 140, 160, 210)`) không? Hay đổi để rõ hơn vì đã match renderer? Tôi đề xuất giữ — translucent là intentional cho overlay.

(6) **Lane indexing**: code mới dùng `range(n)` với `n=4`. Nếu renderer dùng `range(cam.n_lanes)` thì overlay phải đọc cùng giá trị. Hiện đều = 4 hardcode. Nếu sau này sync dynamic, expose qua `live_renderer.n_lanes` property và overlay đọc.
