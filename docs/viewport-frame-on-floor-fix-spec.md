# ViewportFrame — Move 4 Panels Onto Floor Spec

## Mục tiêu

Sửa class `ViewportFrame` trong `src/rhythm.py` để 4 panel "cockpit/visor" hiện ra **TRÊN sàn** (above Floor line in screen space, sát hit zone), thay vì **dưới sàn** (below Floor line, kéo dài tới đáy màn hình) như hiện tại.

Hiện tại 4 panel render ở vùng `z range [≈0.7, Z_NEAR]` (trước Floor line, kéo từ camera tới hit zone). User cảm giác như 4 panel "underground" — không hợp lý vật lý vì Floor line là mặt sàn.

Sau fix: 4 panel render ở vùng `z range [Z_NEAR, Z_NEAR + panel_depth]` (sau Floor line, sát hit zone). 4 panel trông như **landing pads trên sàn** — đúng với vai trò "nơi block đáp xuống".

Spec này độc lập với mọi spec khác trong `docs/`. Chỉ đụng vào 1 method trong `rhythm.py`.

---

## So sánh visual

### Hiện tại (BUG — panels dưới Floor)

```
Screen y:
  0   ┌─────────────────────────────┐  ← Top
      │      Sky / horizon          │
      │                             │
      │ ─── Horizon line ────       │
      │      Floor (chevron)        │
      │       \\\\\///              │
      │        \\\///               │
      │ ─── Floor line ────         │  ← y_hit (z = Z_NEAR)
      │  ┌──┐ ┌──┐ ┌──┐ ┌──┐        │
      │  │V │ │V │ │V │ │V │        │  ← 4 panels dưới Floor
      │  └──┘ └──┘ └──┘ └──┘        │      (z range ~[0.7, Z_NEAR])
   H  └─────────────────────────────┘  ← Bottom
```

Panel back-edge AT Floor line, front-edge sát đáy màn hình → phần lớn panel "dưới sàn".

### Sau fix (panels trên Floor)

```
Screen y:
  0   ┌─────────────────────────────┐  ← Top
      │      Sky / horizon          │
      │                             │
      │ ─── Horizon line ────       │
      │      Floor (chevron)        │
      │       \\\\\///              │
      │        \\\///               │
      │  ┌──┐ ┌──┐ ┌──┐ ┌──┐        │  ← 4 panels TRÊN Floor
      │  │V │ │V │ │V │ │V │        │      (z range [Z_NEAR, Z_NEAR + d])
      │  └──┘ └──┘ └──┘ └──┘        │
      │ ─── Floor line ────         │  ← y_hit, sát đáy panel
      │                             │
   H  └─────────────────────────────┘  ← Bottom
```

Panel front-edge AT Floor line, back-edge lùi vào (cao hơn trên screen, gần horizon hơn).

---

## Code hiện tại

**File:** `src/rhythm.py`

**Method:** `ViewportFrame._build_lane_aligned_panels` (dòng ~4497-4562).

```python
def _build_lane_aligned_panels(self,
                               half_x: float | None = None,
                               z_back: float | None = None,
                               y_target_frac: float = 0.985) -> list:
    cam = self.cam
    hx  = DanceTarget.HALF_X if half_x is None else float(half_x)
    if z_back is None:
        z_back = max(cam.Z_NEAR - DanceTarget.HALF_Z, 0.1)
    else:
        z_back = max(float(z_back), 0.1)

    # Solve z_front from the target bottom-of-screen y.
    y_target = cam.H * y_target_frac    # = 0.985 * H, sát đáy
    denom    = y_target - cam.cy_pix
    if denom > 1.0:
        z_front = cam.fy * cam.FLOOR_WORLD_Y / denom
    else:
        z_front = max(z_back - 0.8, 0.1)
    z_front = max(min(z_front, z_back - 0.05), 0.1)

    panels = []
    for i in range(cam.n_lanes):
        wx = cam.lane_world_x(i)
        corners_w = (
            (wx - hx, cam.FLOOR_WORLD_Y, z_front),  # BL — gần camera
            (wx + hx, cam.FLOOR_WORLD_Y, z_front),  # BR
            (wx + hx, cam.FLOOR_WORLD_Y, z_back),   # TR — xa camera (= Z_NEAR)
            (wx - hx, cam.FLOOR_WORLD_Y, z_back),   # TL
        )
        proj = [cam.project(*p) for p in corners_w]
        ...
```

**Vấn đề**: `z_front` được giải từ `y_target_frac = 0.985` (98.5% screen height) → z_front rất nhỏ (gần camera). Panels trải dài từ Z_NEAR xuống tới ~0.7.

---

## Code mới

Đảo ngược logic: panels nằm **TRÊN** Floor line, kéo từ Z_NEAR (front) ra sau (z_back > Z_NEAR).

```python
def _build_lane_aligned_panels(self,
                               half_x: float | None = None,
                               z_front: float | None = None,
                               panel_depth: float = 0.6) -> list:
    """Lane-aligned panels — each panel sits ON THE FLOOR at hit zone,
    extending slightly backward toward horizon.

    Parameters
    ----------
    half_x : float | None
        Panel half-width in world units. ``None`` = ``DanceTarget.HALF_X``.
    z_front : float | None
        World depth of the panel's FRONT edge (closer to camera, lower
        on screen). ``None`` = ``Z_NEAR`` (panel front sits exactly at
        the Floor line / hit zone).
    panel_depth : float
        World depth from front to back. Default 0.6 = nửa tile_len, đủ
        để panel có visual depth nhưng không lấn quá xa horizon.
    """
    cam = self.cam
    hx = DanceTarget.HALF_X if half_x is None else float(half_x)
    if z_front is None:
        z_front = float(cam.Z_NEAR)         # NEW: front AT Floor line
    else:
        z_front = max(float(z_front), 0.1)
    z_back = z_front + max(0.1, float(panel_depth))   # NEW: back BEHIND front

    panels = []
    for i in range(cam.n_lanes):
        wx = cam.lane_world_x(i)
        # Corner order = (front-left, front-right, back-right, back-left)
        # Front = closer to camera (lower y on screen, near hit zone)
        # Back  = farther (higher y on screen, toward horizon)
        corners_w = (
            (wx - hx, cam.FLOOR_WORLD_Y, z_front),  # FL — sát Floor line
            (wx + hx, cam.FLOOR_WORLD_Y, z_front),  # FR
            (wx + hx, cam.FLOOR_WORLD_Y, z_back),   # BR — lùi vào sàn
            (wx - hx, cam.FLOOR_WORLD_Y, z_back),   # BL
        )
        proj = [cam.project(*p) for p in corners_w]
        if any(p is None for p in proj):
            continue
        panels.append(np.array(
            [(int(round(p[0])), int(round(p[1]))) for p in proj],
            dtype=np.int32))
    return panels
```

**Khác biệt**:

| Aspect | Cũ | Mới |
|---|---|---|
| `z_front` | Solved từ `y_target_frac=0.985` (~0.7) | `Z_NEAR` (= 2.5) |
| `z_back` | Z_NEAR hoặc Z_NEAR - HZ (~2.18-2.5) | Z_NEAR + panel_depth (~3.1) |
| Panel position (screen) | Below Floor line, sát đáy màn hình | At/above Floor line, on floor |
| Vai trò semantic | Cockpit/visor cản tầm nhìn | Landing pad trên sàn |

### Caller cần sửa

`ViewportFrame.__init__` gọi `_build_lane_aligned_panels` với:

```python
# CŨ:
if mode == 'dance':
    half_x = DanceTarget.HALF_X
    z_back = max(cam.Z_NEAR - DanceTarget.HALF_Z, 0.1)
else:
    if cam.n_lanes > 1:
        lane_step_world = abs(cam.lane_world_x(1) - cam.lane_world_x(0))
    else:
        lane_step_world = 0.40
    half_x = 0.40 * lane_step_world
    z_back = cam.Z_NEAR
panels = self._build_lane_aligned_panels(half_x=half_x, z_back=z_back)
```

Đổi thành:

```python
# MỚI — panel front AT Floor line, back lùi sau
if mode == 'dance':
    # Dance: panel rộng = DanceTarget tile width. Depth = tile depth
    # so that DanceTarget at hit_frame land FLUSH on top of panel.
    half_x = DanceTarget.HALF_X
    panel_depth = 2.0 * DanceTarget.HALF_Z   # = tile depth
else:
    # Punch: panel = 80% lane step width.
    if cam.n_lanes > 1:
        lane_step_world = abs(cam.lane_world_x(1) - cam.lane_world_x(0))
    else:
        lane_step_world = 0.40
    half_x = 0.40 * lane_step_world
    panel_depth = 0.6                         # default depth
panels = self._build_lane_aligned_panels(
    half_x=half_x, z_front=cam.Z_NEAR, panel_depth=panel_depth,
)
```

---

## Hệ quả tới các phần khác

### 1. DanceTarget landing — vẫn flush

Comment cũ ở `ViewportFrame.__init__`:
> ``mode='dance'`` — panel back-edge coincides with ``DanceTarget`` front-edge at hit_frame, so the stomp tile lands flush onto its panel.

Sau fix, panel **front-edge** ở Z_NEAR. DanceTarget tại hit_frame có center ở Z_NEAR, front ở Z_NEAR - HZ (~2.18) → DanceTarget front edge nằm TRƯỚC panel front edge.

Để DanceTarget vẫn "land flush" trên panel, đặt `panel_depth = 2 * DanceTarget.HALF_Z` (= tile depth = 0.64) thì panel back-edge = Z_NEAR + 0.64 = 3.14. DanceTarget back ở Z_NEAR + HZ = 2.82. Tile back nằm giữa panel front và panel back → tile NẰM TRÊN panel. ✓

Visual: DanceTarget tile vẫn trông như "đặt lên" panel khi land. Không break.

### 2. PunchTarget rail — vẫn match

Comment cũ:
> ``mode='punch'`` — panel back-edge = ``Z_NEAR`` (the hit line), panel half-width = ``0.40 × lane_step_world``... flying cubes then visibly enter the panel along its rail.

Trong design cũ, PunchTarget bay từ horizon về camera, ENTER vào panel khi cube reach Z_NEAR (= panel back-edge). Cube travel qua panel từ back tới front, ra ngoài camera.

Sau fix, panel front = Z_NEAR. Cube reach Z_NEAR → "enter" panel front. Cube tiếp tục bay tới z=0 (camera) → "exit" panel front... wait, cube depth tại hit_frame = Z_NEAR. Sau hit, cube không vẽ nữa (state='hit'). Vậy cube không bao giờ enter panel.

**Lệch ý đồ**: PunchTarget visual cũ "fly into panel" sẽ không còn vì cube biến mất ngay khi reach panel front. 

Có 2 lựa chọn:
- (a) Giữ nguyên fix cho mode='dance', riêng mode='punch' vẫn dùng z range [Z_NEAR - tiny, Z_NEAR] để cube "enter" panel.
- (b) Punch panels cũng lên trên floor. Cube bay tới Z_NEAR → land. Bỏ "fly through panel" effect. Đơn giản hơn.

**Đề xuất (b)**: cả 2 mode dùng cùng logic on-floor. Mất "fly through" effect nhưng UX nhất quán (panel ở đúng vị trí vật lý).

### 3. ViewportFrame.draw — không đổi

Method `draw()` (dòng 4575-4647) chỉ vẽ trên `self.panels` đã build. Logic vẽ (dark fill, neon border, accent, shake jitter) không depends on z range. Giữ nguyên.

### 4. Floor handle alignment

Trong overlay (`FloorWallOverlay`), handle "Floor" hiện là cyan dashed line at y_hit. Sau fix, panel front-edge AT y_hit → aligned. User kéo Floor handle → panel di chuyển sync.

---

## Touch points

### 1. `src/rhythm.py` — `ViewportFrame._build_lane_aligned_panels` (dòng ~4497-4562)

Replace toàn bộ method theo "Code mới" ở trên.

### 2. `src/rhythm.py` — `ViewportFrame.__init__` (dòng ~4438-4495)

Đoạn build call (dòng ~4462-4475): đổi từ `z_back` param sang `z_front + panel_depth` params. Code đã viết ở trên.

### 3. Cập nhật docstring

`ViewportFrame.__init__` docstring (dòng ~4441-4456):

```python
# CŨ:
"""...
* ``mode='dance'`` — panel back-edge coincides with
  ``DanceTarget`` front-edge at hit_frame, so the stomp tile
  lands flush onto its panel.
* ``mode='punch'`` — panel back-edge = ``Z_NEAR`` (the hit
  line), panel half-width = ``0.40 × lane_step_world`` so
  ...flying cubes then visibly enter the panel along its rail.
"""

# MỚI:
"""...
Both modes now place panels ON the floor at hit zone:
* ``mode='dance'`` — panel front-edge = ``Z_NEAR``, depth = tile
  depth (2 × DanceTarget.HALF_Z) so the stomp tile lands FLUSH
  ON TOP of its panel.
* ``mode='punch'`` — panel front-edge = ``Z_NEAR``, half_width =
  ``0.40 × lane_step_world``, depth = 0.6 world units. Flying
  cubes land at the panel front and burst there.
"""
```

### 4. Class `ViewportFrame` doc (dòng ~4429-4436)

Update class docstring:

```python
# CŨ:
"""Four neon-outlined blocks floating at eye-level.

Static decorative HUD that represents the player's "visor" / cockpit.
On each punch hit, all four blocks receive a brief random jitter that
decays over ~0.25s, selling the illusion of a real impact shaking the
camera/viewport.
"""

# MỚI:
"""Four neon-outlined landing pads ON THE FLOOR at the hit zone.

Each panel sits flush on the floor plane at z = Z_NEAR (the hit
line), extending slightly backward toward the horizon. Visually
represents the per-lane "drum pad" where flying targets land.

On each punch hit, all four pads receive a brief random jitter
that decays over ~0.25s, selling the illusion of a real impact
shake. The jitter is purely visual — pad geometry stays anchored
on the floor between hits.
"""
```

### 5. Comment trong `_build_lane_aligned_panels`

Update header comment:

```python
"""Lane-aligned landing pads on the floor — each panel is lane *i*'s
floor footprint at the hit zone.

The panels sit FLUSH on the floor plane (FLOOR_WORLD_Y), with their
front edge at z = Z_NEAR (= the Floor line in overlay) and back edge
at z = Z_NEAR + panel_depth. This is opposite to the legacy "cockpit
visor" interpretation where panels extended FORWARD from Z_NEAR
toward the camera (z range [near 0, Z_NEAR]).
"""
```

---

## Test scenarios

### Test 1: Visual position — panels TRÊN Floor line

```
Setup: chạy live preview, không có target nào ở hit_frame.
Verify: 4 panels hiển thị trên màn hình, ở screen y range:
  - Front edge (closer to camera, lower y) = y_hit (= Floor line)
  - Back edge (farther, higher y) ở y < y_hit (above Floor line)
Verify: KHÔNG còn vùng panel nằm DƯỚI y_hit.
Verify: Trong overlay Edit Layout, panels match _hit_block_polys hoặc nằm sát đó.
```

### Test 2: Compare với screenshot user gửi

```
Trước fix: 4 V panels nằm sát đáy màn hình, dưới Floor line.
Sau fix: 4 panels nằm sát Floor line, trên sàn.
```

### Test 3: DanceTarget land flush

```
Setup: chạy dance mode, chờ DanceTarget reach hit_frame.
Verify: tile DanceTarget sit FLUSH on panel surface (tile back edge ≈ panel back edge).
Verify: Không có gap visible giữa tile và panel.
```

### Test 4: PunchTarget land

```
Setup: chạy punch mode, cube bay tới hit_frame.
Verify: cube biến mất hoặc burst tại panel front edge (z = Z_NEAR).
Verify: shake effect vẫn trigger (panel jitter ~0.25s).
```

### Test 5: Shake animation

```
Setup: trigger ViewportFrame.trigger(intensity=1.0).
Verify: 4 panels jitter random trong ~0.25s, sau đó decay về idle.
Verify: Position cốt lõi (z_front, z_back) KHÔNG đổi — chỉ jitter offset 2D screen.
```

### Test 6: Edge case — Z_NEAR rất gần horizon

```
Setup: hit_frac thấp (panels gần horizon).
panel_depth = 0.6 → panel back ở z = Z_NEAR + 0.6 ≈ 3.1.
Project: sy_back = cy_pix + (y_hit - cy_pix) * Z_NEAR / 3.1
Nếu y_hit gần cy_pix, sy_back gần cy_pix → panel rất nhỏ.
Verify: KHÔNG crash, KHÔNG project None.
Có thể panel vẽ rất nhỏ (acceptable cho extreme camera).
```

### Test 7: Edge case — Z_NEAR overflow

```
Setup: panel_depth = 5.0 (large) → panel back z = 7.5, gần horizon.
Verify: panel projects OK, không bị clip.
Verify: panel back chiều rộng nhỏ rõ rệt (perspective converge).
```

### Test 8: Lane alignment

```
Setup: 4 lanes, n_lanes = 4.
Verify: panel I cho lane I căn chính giữa lane (xc = lane_world_x(i)).
Verify: panel rộng = 2 * half_x.
Verify: 4 panels không overlap (gap nhỏ giữa panels).
```

### Test 9: Multi-mode consistency

```
Setup mode='dance': panel half_x = DanceTarget.HALF_X = 0.20.
Setup mode='punch': panel half_x = 0.40 * lane_step_world.
Verify: cả 2 mode panels nằm trên floor (z range [Z_NEAR, Z_NEAR + depth]).
Verify: Width khác nhau theo mode (dance hẹp hơn punch).
```

### Test 10: Backward compat — render khi không có hit

```
Setup: idle scene, no target at hit_frame, no shake.
Verify: 4 panels hiển thị faint grey outline (idle state).
Verify: Position vẫn correct (trên floor, không dưới).
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **`ViewportFrame.draw`** logic (dòng 4575-4647): KHÔNG đổi. Chỉ position panels thay đổi, vẽ logic giữ nguyên (dark fill, neon border, shake, accent).

2. **`trigger()` / `update()`** methods: giữ nguyên. Shake mechanism không liên quan position.

3. **`PerspectiveCamera`** không động. Math `cam.project()` giữ nguyên.

4. **`DanceTarget` / `PunchTarget` / `StepTarget`** rendering không động. Targets vẫn render độc lập.

5. **`floor_panel_image`** behavior: nếu user dùng custom V image, image vẫn render trên FLOOR TILES (qua `_draw_floor_tiles_legacy`), không phải trên ViewportFrame panels. ViewportFrame và floor tiles là 2 layer riêng biệt.

6. **`_hit_block_polys`** trong overlay (preview_panel.py): nếu sau fix panel position đổi, overlay's hit-block visualization có thể không match nữa. Cần update overlay để vẽ ở vị trí mới (hoặc nếu apply approach A của hit-block-overlay-pinhole-fix-spec, math tự đồng bộ).

7. **Shake `max_offset`** (dòng 4481): pixel-based, không depend on z range. Giữ nguyên.

8. **Comment compatibility**: dù docstring nói "lane_aligned" legacy flag, không impact functionality.

9. **PunchTarget "fly through" semantic**: spec đề xuất bỏ vì panel mới ở behind hit zone. User cần confirm có chấp nhận lose effect này không (xem Open question).

10. **Param signature**: `_build_lane_aligned_panels` hiện có param `z_back`, `y_target_frac`. Đổi sang `z_front`, `panel_depth`. Caller (chỉ `ViewportFrame.__init__`) phải update theo. Verify không có caller khác qua grep.

---

## Pattern code hiện có để tham khảo

- **`PerspectiveCamera.project`** (rhythm.py): pinhole projection. Spec dùng giống.
- **`DanceTarget._draw_flat_tile`** (rhythm.py dòng ~2764): pattern project tile lên floor plane. Tham khảo cho panel placement.
- **Existing `ViewportFrame.draw`** (dòng ~4575): pattern shake jitter + neon border. Giữ.
- **`grep -n "ViewportFrame" src/rhythm.py`** để tìm caller của `_build_lane_aligned_panels`. Hiện chỉ có `__init__` gọi. Nếu có caller khác, update.

---

## Thứ tự implement đề xuất

1. **Backup screenshot** trước khi fix để so sánh visual sau.

2. **Update docstring** class + method (touch points 3, 4, 5). Pure documentation, không break gì.

3. **Replace `_build_lane_aligned_panels`** với code mới (touch point 1).

4. **Update `ViewportFrame.__init__` build call** (touch point 2). Verify không có caller khác qua `grep _build_lane_aligned_panels src/`.

5. **Run live preview**. Verify 4 panels hiện trên Floor line (Test 1, 2).

6. **Test mode='dance'**: chạy segment dance, verify DanceTarget land flush trên panel (Test 3).

7. **Test mode='punch'**: chạy segment punch, verify PunchTarget land/burst tại panel front. Note xem có acceptable "lose fly-through" không (Test 4).

8. **Test shake**: punch hit → verify panels jitter (Test 5).

9. **Edge cases**: Test 6, 7 với extreme floor_hit_frac.

10. **Update overlay's `_hit_block_polys`** nếu có (xem KHÔNG được phá vỡ #6) — tạm thời skip nếu chưa apply spec hit-block-overlay-pinhole-fix-spec.

11. **Smoke test toàn bộ live preview**: tất cả modes (punch, dance, line, relax, combo) hoạt động bình thường.

---

## Open questions

(1) **PunchTarget "fly-through" effect**: spec đề xuất bỏ (panel ở sau hit zone, cube không enter panel). Bạn confirm chấp nhận lose effect, hay muốn keep mode='punch' giữ nguyên (cockpit) và chỉ sửa mode='dance'?

(2) **`panel_depth` default**:
- Dance mode: `2 * DanceTarget.HALF_Z` = 0.64 (= tile depth)
- Punch mode: 0.6 (default)
Bạn confirm 2 giá trị này, hay muốn tune khác (vd 0.4, 1.0)?

(3) **Visual semantic**: 4 panels mới sẽ trông như "drum pads" trên sàn. Bạn muốn:
- (a) Giữ neon border + shake jitter pattern (như cockpit cũ)
- (b) Đổi style sang "stomp pad" rõ rệt hơn (vd thêm corner tick, add icon footprint giống DanceTarget)
- (c) Tone down để panels mờ hơn, không che blocks bay tới

Tôi đề xuất (a) — giữ nguyên style để minimize change. Bạn quyết.

(4) **Floor handle alignment**: sau fix, panel front-edge AT y_hit. Floor handle line trùng với front edge của panels. Verify visual đúng (Test 1). Nếu user thấy lệch, có thể cần tinh chỉnh `z_front = Z_NEAR ± epsilon`.

(5) **Có cần fallback**: nếu user không thích thay đổi, có thể keep cũ qua flag config `viewport_frame_on_floor: bool = True` để toggle 2 mode? Tôi đề xuất KHÔNG cần (single behavior, đơn giản hơn).

(6) **V mark đỏ**: đến từ source khác (chevron texture / floor_panel_image / target letter), KHÔNG phải `ViewportFrame.draw`. Spec này không động đến V mark. Sau fix vị trí panels, V mark vẫn render ở vị trí cũ — bạn cần xác định source riêng nếu muốn align V với panel mới.
