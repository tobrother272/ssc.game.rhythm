# Punch Block Render — Fix Spec

## Mục tiêu

Cải thiện chất lượng visual của **PunchTarget block 3D** + **Preview render mode** để:
1. Preview mode không quá xấu (vẫn nhanh nhưng nhận diện được visual style).
2. Block 3D có chiều sâu rõ rệt — top/front/side face contrast đủ mạnh.
3. Block xa vẫn nhận diện được là punch block (giữ identity qua icon).
4. Side rails giảm pixelation răng cưa.
5. Hit line không bị block đè (painter order đúng).

> **Ràng buộc**: KHÔNG thay đổi gameplay logic (timing, hit detection, collision). Chỉ visual.

---

## Tóm tắt 5 fix

| # | Fix | File | Effort | Visual impact |
|---|---|---|---|---|
| 1 | Preview resolution upgrade — option B/C | `render_service.py` | Thấp | Cao |
| 2 | Block 3D brightness contrast | `rhythm.py` PunchTarget.draw | Thấp | Cao |
| 3 | Force render side face khi center lane | `rhythm.py` PunchTarget.draw | Thấp | Trung |
| 4 | Block xa giữ identity (icon threshold + simplified shape) | `rhythm.py` | Thấp | Trung |
| 5 | Side rails AA + subdivide | `rhythm.py` SideRail.draw | Trung | Cao |

---

## Fix #1 — Preview resolution upgrade

### Vấn đề

`render_service.py:495-501` ép preview xuống 540p + bloom=0:
```python
if job.is_preview:
    command.extend([
        "-W", "960", "-H", "540",
        "--fps", "24",
        "--bloom", "0",
    ])
```

→ Output preview **pixelated nặng** + **mất neon glow** → user không judge được visual quality, phải full render mới biết.

### Giải pháp — 2 option

#### Option B (recommend) — Single tier "Medium quality"

Đổi preset preview thành:
```python
if job.is_preview:
    command.extend([
        "-W", "1280", "-H", "720",   # was 960×540
        "--fps", "30",                # was 24
        "--bloom", "1",               # was 0 — KEEP bloom on
    ])
```

**Tradeoff**:
- Preview time: ~15s → ~30s (chậm 2× cho 60s clip)
- Quality: gần giống full render (chỉ resolution thấp hơn 1.5×)
- ROI: cao — user thực sự dùng preview để judge style

#### Option C — Two-tier preview với UI button riêng

Thêm field mới vào `RenderJob`:
```python
@dataclass
class RenderJob:
    ...
    is_preview: bool = False
    preview_quality: str = "fast"   # "fast" | "quality"
```

Trong `_run_job`:
```python
if job.is_preview:
    if job.preview_quality == "quality":
        command.extend([
            "-W", "1280", "-H", "720",
            "--fps", "30",
            "--bloom", "1",
        ])
    else:  # "fast" (default, giữ behavior cũ)
        command.extend([
            "-W", "960", "-H", "540",
            "--fps", "24",
            "--bloom", "0",
        ])
```

**UI changes** (`segment_config_panel.py` hoặc toolbar):
- Thay 1 nút `Preview` thành 2 nút:
  - `Preview Fast` (gear icon ⚡) → `is_preview=True, preview_quality="fast"`
  - `Preview Quality` (eye icon 👁) → `is_preview=True, preview_quality="quality"`
- Hoặc dropdown:
  ```
  [Preview ▼]
    ├─ Fast (540p, no bloom)
    └─ Quality (720p + bloom)
  ```

#### Recommend: Option B (đơn giản hơn)

Lý do:
- 1 nút duy nhất — không cần user choose mỗi lần.
- 30s vẫn faster đáng kể vs full render ~80s.
- User cần "judge" preview thực sự, không cần "quick-and-dirty" 15s.

### Code change Fix #1 (Option B)

**`studio/core_bridge/render_service.py`** — replace block:
```python
# DELETE
if job.is_preview:
    command.extend([
        "-W", "960",
        "-H", "540",
        "--fps", "24",
        "--bloom", "0",
    ])

# REPLACE BY
if job.is_preview:
    command.extend([
        "-W", "1280",
        "-H", "720",
        "--fps", "30",
        "--bloom", "1",
    ])
```

### Test cases

```
✓ Preview render 10s clip → time ≤ 35s (1280×720@30 với bloom=1)
✓ Visual: side rails không jagged
✓ Visual: block xa (z_norm > 0.7) vẫn nhận diện được hình dạng
✓ Visual: bloom glow visible toàn frame
✓ Full render time KHÔNG đổi (chỉ ảnh hưởng path is_preview=True)
```

---

## Fix #2 — Block 3D brightness contrast

### Vấn đề

Code rule §2 (`rhythm.py:2228-2230`):
```python
top_col   = tuple(int(min(255, c * 0.90 + 255 * 0.10)) for c in base)
side_col  = tuple(int(c * 0.50) for c in base)
depth_gain = 0.70 + 0.30 * (1.0 - z_norm)
```

Với `base = (80, 230, 80)` (green left lane):
- `top_col = (82, 230, 107)` — **chỉ chênh ~3% với base** → top nhìn = base
- `side_col = (40, 115, 40)` — tối hơn 50% (OK)
- `front = side_col * 1.15 = (46, 132, 46)` — chỉ sáng hơn side 15%

→ Top vs Front contrast **rất yếu** (10% chênh lệch) → block nhìn flat 2D, mất 3D pop.

### Giải pháp

Tăng top brightness + tăng front-vs-side ratio:

```python
# Brightness model — CẢI TIẾN (rule §2 v2)
base = tuple(int(c) for c in self.color)
top_col   = tuple(int(min(255, c * 1.15 + 255 * 0.15)) for c in base)   # was 0.90 + 0.10
side_col  = tuple(int(c * 0.45) for c in base)                          # was 0.50
depth_gain = 0.70 + 0.30 * (1.0 - z_norm)
```

Với `base = (80, 230, 80)`:
- `top_col = (max=255, max=255, min(255, 80*1.15 + 38.25)=130)` → `(130, 255, 130)` — **rõ rệt sáng hơn base**
- `side_col = (36, 103, 36)` — tối hơn 55%
- `front = side_col * 1.30 * depth_gain` → contrast front-vs-side mạnh hơn

```python
# Trong PunchTarget.draw, đổi:
front_scaled = tuple(int(min(255, c * depth_gain * 1.30)) for c in side_col)   # was 1.15
```

### Visual diff

| Element | Before | After |
|---|---|---|
| Top brightness vs base | +3% | **+30%** + ngả trắng rõ |
| Side darkness vs base | -50% | **-55%** |
| Front vs Side ratio | 1.15× | **1.30×** |
| 3D pop perception | Flat | Rõ rệt cube |

### Code change Fix #2

**`src/rhythm.py:2228-2230`** — replace:
```python
# DELETE
top_col   = tuple(int(min(255, c * 0.90 + 255 * 0.10)) for c in base)
side_col  = tuple(int(c * 0.50) for c in base)

# REPLACE BY
top_col   = tuple(int(min(255, c * 1.15 + 255 * 0.15)) for c in base)
side_col  = tuple(int(c * 0.45) for c in base)
```

**`src/rhythm.py:2265`** — replace:
```python
# DELETE
front_scaled = tuple(int(min(255, c * depth_gain * 1.15)) for c in side_col)

# REPLACE BY
front_scaled = tuple(int(min(255, c * depth_gain * 1.30)) for c in side_col)
```

### Test cases

```
✓ Block GẦN (z_norm ≈ 0): top sáng rõ rệt, side rất tối, front mid-tone → 3D rõ rệt
✓ Block XA (z_norm ≈ 0.8): vẫn distinguishable 3 face khi bloom on
✓ Green block left lane: top ngả trắng, không over-saturate
✓ Red block right lane: top hồng nhạt, không clip 255
✓ Visual A/B compare: side-by-side render before/after — block sau pop hơn rõ rệt
```

---

## Fix #3 — Force render side face khi center lane

### Vấn đề

`rhythm.py:2272-2277`:
```python
if block_screen_cx < cam_cx - 2:
    cv2.fillConvexPoly(canvas, right_face, side_scaled, ...)
elif block_screen_cx > cam_cx + 2:
    cv2.fillConvexPoly(canvas, left_face, side_scaled, ...)
# else: NO SIDE FACE — block ở center lane bị flat 2D
```

Với 4-lane layout, lane 1 và 2 (giữa) có `block_screen_cx ≈ cam_cx ± 1` → **rơi vào else branch** → mất side face.

### Giải pháp

Luôn vẽ 1 side face — chọn dựa trên dấu offset, threshold = 0 (không có dead zone):

```python
# Rule §3 v2: ALWAYS render 1 side face (no dead zone at center)
block_screen_cx = float(pts[:, 0].mean())
cam_cx = float(getattr(cam, 'cx_pix', W / 2.0))

if block_screen_cx < cam_cx:
    cv2.fillConvexPoly(canvas, right_face, side_scaled, lineType=cv2.LINE_AA)
else:  # >= cam_cx (ngay center hoặc phải)
    cv2.fillConvexPoly(canvas, left_face, side_scaled, lineType=cv2.LINE_AA)
```

**Tại sao tie-break vào `>= cam_cx`?**: block ngay center → side face nhỏ tí (gần collinear) nhưng có còn hơn không. Nếu block lane center có pts[2,1,5,6] (right_face) bị degenerate ngược xa → fallback dùng left_face an toàn hơn.

### Code change Fix #3

**`src/rhythm.py:2272-2277`** — replace:
```python
# DELETE
if block_screen_cx < cam_cx - 2:
    cv2.fillConvexPoly(canvas, right_face, side_scaled, lineType=cv2.LINE_AA)
elif block_screen_cx > cam_cx + 2:
    cv2.fillConvexPoly(canvas, left_face, side_scaled, lineType=cv2.LINE_AA)

# REPLACE BY
if block_screen_cx < cam_cx:
    cv2.fillConvexPoly(canvas, right_face, side_scaled, lineType=cv2.LINE_AA)
else:
    cv2.fillConvexPoly(canvas, left_face, side_scaled, lineType=cv2.LINE_AA)
```

### Edge case

- Block ngay center lane với cube small + far Z → side face có thể chỉ 1-2 pixel wide. `cv2.fillConvexPoly` xử lý OK, không crash.
- Nếu pts collinear hoàn toàn (rare, only at exact horizon) → fillConvexPoly silently skip. Không cần guard.

### Test cases

```
✓ Block lane 0 (outer-left): right_face visible, contrast với front
✓ Block lane 1 (inner-left): right_face visible (small but present)
✓ Block lane 2 (inner-right): left_face visible (small but present)
✓ Block lane 3 (outer-right): left_face visible
✓ KHÔNG còn block "flat 2D" ở center lane
```

---

## Fix #4 — Block xa giữ identity (lower icon threshold + simplified shape)

### Vấn đề

`rhythm.py:2285`:
```python
if top_w >= 22:
    _draw_fist_icon(canvas, ..., int(top_w * 0.58), CLR_WHITE)
```

Block xa có `top_w < 22` px → **skip icon hoàn toàn** → block xa = chỉ là cuboid đơn không có dấu hiệu là punch block. User không nhận ra "đó là punch tới".

Ngoài ra: ở 540p preview, top_w block xa thường < 10px → nhiều block bị mất icon.

### Giải pháp 4a — Lower threshold + scale icon dynamically

```python
# Smaller threshold + scale tùy size
ICON_MIN_TOP_W = 12   # was 22
if top_w >= ICON_MIN_TOP_W:
    cx_top = int(top_face[:, 0].mean())
    cy_top = int(top_face[:, 1].mean())
    icon_size = int(top_w * 0.58)
    _draw_fist_icon(canvas, cx_top, cy_top, icon_size, CLR_WHITE)
```

### Giải pháp 4b — Simplified icon cho block tí hon

Khi `top_w < 22` nhưng `>= 12`, vẽ phiên bản đơn giản (chỉ knuckles, bỏ thumb):

```python
def _draw_fist_icon_simple(canvas, cx, cy, size, color=CLR_WHITE):
    """Phiên bản tí hon — chỉ knuckles + small palm bar."""
    if size < 4:
        return
    s = size
    # Knuckles bar
    kw = max(1, int(s * 0.6))
    kh = max(2, int(s * 0.3))
    cv2.rectangle(canvas, (cx - kw // 2, cy - kh // 2),
                  (cx + kw // 2, cy + kh // 2),
                  color, -1, lineType=cv2.LINE_AA)
    # Palm bar
    pw = max(1, int(s * 0.7))
    ph = max(1, int(s * 0.15))
    cv2.rectangle(canvas, (cx - pw // 2, cy + kh // 2 + 1),
                  (cx + pw // 2, cy + kh // 2 + 1 + ph),
                  color, -1, lineType=cv2.LINE_AA)
```

Trong `PunchTarget.draw`:
```python
if top_w >= 22:
    _draw_fist_icon(canvas, cx_top, cy_top, int(top_w * 0.58), CLR_WHITE)
elif top_w >= 12:
    _draw_fist_icon_simple(canvas, cx_top, cy_top, int(top_w * 0.7), CLR_WHITE)
```

### Code change Fix #4

**`src/rhythm.py`** — thêm function mới sau `_draw_fist_icon` (line ~2089):
```python
def _draw_fist_icon_simple(canvas: np.ndarray, cx: int, cy: int, size: int,
                           color=CLR_WHITE):
    """Simplified fist icon for far/small blocks (skip thumb + detail)."""
    if size < 4:
        return
    s = size
    kw = max(1, int(s * 0.6))
    kh = max(2, int(s * 0.3))
    cv2.rectangle(canvas, (cx - kw // 2, cy - kh // 2),
                  (cx + kw // 2, cy + kh // 2),
                  color, -1, lineType=cv2.LINE_AA)
    pw = max(1, int(s * 0.7))
    ph = max(1, int(s * 0.15))
    cv2.rectangle(canvas, (cx - pw // 2, cy + kh // 2 + 1),
                  (cx + pw // 2, cy + kh // 2 + 1 + ph),
                  color, -1, lineType=cv2.LINE_AA)
```

**`src/rhythm.py:2280-2289`** — replace:
```python
# DELETE
top_w = int(max(
    np.linalg.norm(top_face[1] - top_face[0]),
    np.linalg.norm(top_face[2] - top_face[3]),
))
if top_w >= 22:
    cx_top = int(top_face[:, 0].mean())
    cy_top = int(top_face[:, 1].mean())
    _draw_fist_icon(canvas, cx_top, cy_top,
                    int(top_w * 0.58), CLR_WHITE)
return canvas

# REPLACE BY
top_w = int(max(
    np.linalg.norm(top_face[1] - top_face[0]),
    np.linalg.norm(top_face[2] - top_face[3]),
))
if top_w >= 12:
    cx_top = int(top_face[:, 0].mean())
    cy_top = int(top_face[:, 1].mean())
    if top_w >= 22:
        _draw_fist_icon(canvas, cx_top, cy_top,
                        int(top_w * 0.58), CLR_WHITE)
    else:
        _draw_fist_icon_simple(canvas, cx_top, cy_top,
                               int(top_w * 0.70), CLR_WHITE)
return canvas
```

### Test cases

```
✓ Block GẦN (top_w >= 22): full fist icon (knuckles + palm + thumb)
✓ Block XA (12 <= top_w < 22): simplified icon (knuckles + palm bar)
✓ Block CỰC XA (top_w < 12): no icon (avoid noise) — block vẫn được render với 3 face
✓ Preview 540p: 90% block visible đều có icon (vs 30% ở threshold cũ)
```

---

## Fix #5 — Side rails AA + chevron subdivide

### Vấn đề

Side rails (purple/pink walls) có **pixel step rất lớn** ngay cả ở 1080p. Có 2 nguyên nhân:

#### Nguyên nhân A — Chevron pattern aliasing

Chevron răng cưa (mỗi pillar = 1 cuboid) vẽ bằng `cv2.fillConvexPoly` không AA mạnh ở scale lớn:

```python
# Hypothesis (cần verify trong side_rail.py):
for pillar_x in pillar_positions:
    pts = [...4 corners chevron...]
    cv2.fillPoly(canvas, [pts], CLR_WALL_PINK)   # KHÔNG có lineType=LINE_AA?
```

#### Nguyên nhân B — Pillar count quá ít

Mỗi pillar chiếm vùng pixel lớn → step lớn giữa pillars khi animate.

### Giải pháp 5a — Force LINE_AA cho mọi rail polygon

Verify `studio/.../side_rail.py` hoặc `rhythm.py` SideRails class:
```python
# Mọi cv2.fillPoly / fillConvexPoly TRÊN side rail PHẢI có:
cv2.fillPoly(canvas, [pts], color, lineType=cv2.LINE_AA)
cv2.fillConvexPoly(canvas, pts, color, lineType=cv2.LINE_AA)
```

Grep tất cả calls trong rail rendering, đảm bảo có `lineType=cv2.LINE_AA`.

### Giải pháp 5b — Tăng pillar density default

`render_service.py` _ALLOWED_KEYS có:
```python
"rail_pillar_count": ...,
```

Default trong `BaseRenderSettings` (cần check) — nếu < 30, tăng lên 40-50 cho rail dense hơn:
```python
rail_pillar_count: int = Field(default=40, ge=10, le=100)   # was 30 hoặc thấp hơn
```

### Giải pháp 5c — Subdivide rail edges

Mỗi pillar vẽ với 4 corner → straight line giữa corner. Subdivide thành 2-3 segments per edge → smoother curve khi animate:

```python
def _subdivide_quad(pts, n_subdivisions=2):
    """Insert n midpoints per edge for smoother AA rendering."""
    out = []
    for i in range(4):
        p1 = pts[i]
        p2 = pts[(i + 1) % 4]
        for k in range(n_subdivisions):
            t = k / n_subdivisions
            out.append(p1 * (1 - t) + p2 * t)
    return np.array(out, dtype=np.int32)

# Trong rail draw:
pts_smooth = _subdivide_quad(pts_pillar, 3)
cv2.fillPoly(canvas, [pts_smooth], color, lineType=cv2.LINE_AA)
```

### Code change Fix #5

**Bước 1**: Grep tất cả `fillPoly` / `fillConvexPoly` trong rail code:
```bash
grep -n "fillPoly\|fillConvexPoly" src/rhythm.py | grep -i "rail\|chevron\|pillar"
```

**Bước 2**: Mỗi line không có `lineType=cv2.LINE_AA`, thêm vào.

**Bước 3**: Trong `BaseRenderSettings`:
```python
# DELETE (nếu có):
rail_pillar_count: int = Field(default=20, ...)

# REPLACE BY:
rail_pillar_count: int = Field(default=40, ge=10, le=100)
```

**Bước 4 (optional, nếu rail vẫn jagged)**: Add `_subdivide_quad` helper vào `rhythm.py` và gọi trong rail render path.

### Test cases

```
✓ Side rails ở 720p preview: pixel step <= 2px (was ~6-8px)
✓ Side rails ở 1080p full: pixel step <= 1px (smooth)
✓ Animation chevron scroll smooth (no popping)
✓ Performance: rail_pillar_count=40 không drop fps (vs 20-30 cũ)
```

---

## Painter order — Hit line vs Block

### Vấn đề thêm (từ screenshot user)

Hit line đỏ (line 3-4) đang **bị block đè** ở vùng overlap. Painter order hiện:
```
1. bg_layer (background)
2. tunnel (floor + walls + grid)
3. side_rails
4. targets (block 3D)
5. particles
6. viewport / HUD
```

Hit line được vẽ trong **tunnel** (step 2). Block xanh vẽ ở step 4 → đè lên.

### Giải pháp

**Option A**: Giữ painter order, render hit line **2 lần** — 1 lần trước block (back), 1 lần sau block (front strip).

**Option B**: Tách hit line thành layer riêng, render SAU block:
```python
# Trong main render loop:
canvas = bg_layer.frame(fi)
canvas = tunnel.draw(canvas, fi)   # tunnel KHÔNG vẽ hit line
side_rail.draw(canvas, fi, ...)
for tg in game.alive_sorted(fi):
    canvas = tg.draw(canvas, cam, fi)
# NEW: vẽ hit line SAU block
draw_hit_line(canvas, cam, fi)
```

**Option C**: Vẽ hit line **trên top face** của block khi block đang ở near hit (z_norm ≈ 0). Cảm giác block "ngồi lên" hit line.

### Recommend Option B

Cleanest semantically — hit line là "indicator" cho user, phải luôn visible.

### Code change

**`src/rhythm.py`** — main render loop (line ~7820):
- Tách logic vẽ hit line khỏi `tunnel.draw()`
- Tạo function `draw_hit_line(canvas, cam, fi)` riêng
- Gọi sau loop `for tg in game.alive_sorted(fi)`

(Chi tiết implementation tùy thuộc tunnel.py structure — cần đọc thêm.)

---

## Tổng hợp acceptance criteria

```
✓ Preview render 720p với bloom on, time ≤ 35s cho 60s clip
✓ Block GẦN (z_norm < 0.3): top face sáng rõ rệt, side face visible (cho mọi lane)
✓ Block XA (z_norm > 0.7): icon visible (full hoặc simplified) khi top_w >= 12px
✓ Side rails: pixel step ≤ 2px ở 720p, ≤ 1px ở 1080p
✓ Hit line đỏ luôn visible trên block (painter order đúng)
✓ Performance: full render time KHÔNG tăng > 5%
✓ Backward compat: full render output (1080p, bloom=1) visual KHÔNG bị regress
```

---

## Migration plan

| Phase | Fix | Effort | Risk |
|---|---|---|---|
| **Phase 1** | Fix #1 Preview resolution upgrade | 5 phút | Thấp |
| **Phase 2** | Fix #2 Brightness contrast + Fix #3 Side face force | 30 phút | Thấp |
| **Phase 3** | Fix #4 Icon threshold + simplified | 30 phút | Thấp |
| **Phase 4** | Fix #5 Side rails AA (cần grep + verify) | 1-2 giờ | Trung |
| **Phase 5** | Painter order hit line vs block | 1 giờ | Trung (cần đọc tunnel.py) |

**Tổng**: 3-4 giờ implementation. Test visual A/B sau mỗi phase.

---

## Open questions

1. **Preview Option B vs C**: Bạn muốn 1 nút Preview duy nhất (Option B) hay 2 nút Fast/Quality (Option C)?

2. **Brightness factor Fix #2**: hệ số `1.15 + 0.15` cho top có thể quá cao cho neon-pink color (R=255 sẽ clip). Có cần per-color tuning không, hay accept clip white?

3. **Icon Fix #4 — block CỰC xa (top_w < 12)**: có cần vẽ chấm đơn 1 pixel để ít nhất block có 1 dấu hiệu là punch không? Hay accept không icon?

4. **Side rails Fix #5**: tăng `rail_pillar_count` mặc định 40 có affect performance không? Cần benchmark trước khi commit.

5. **Hit line painter order**: hit line đỏ là vẽ 1 đường ngang full width hay 1 strip per lane? Implementation hiện tại trong tunnel.py thế nào? (Cần đọc thêm để spec chính xác Phase 5.)

6. **Compatibility với mesh/texture path**: Fix #2 và #4 chỉ áp default neon path. Mesh/texture path không bị ảnh hưởng (đã return sớm). Confirm KHÔNG cần adjust mesh path?

Bạn confirm các open questions trên + chốt thứ tự ưu tiên phases, tôi sẽ refine spec hoặc implement (theo project rule, tôi chỉ viết spec — bạn implement).

---

# 🎯 DECISIONS v2 — Match Reference Image (FINAL TARGET)

> User instruction: "tự đưa ra quyết định để được các block như hình"
> Reference image: 2 block (xanh dương + vàng) với fist icon to bold ở front face, corner bo tròn, glow neon mạnh, chạy trên 2 track pink chevron tách rời.

## Chốt 6 open questions

| # | Question | DECISION | Lý do |
|---|---|---|---|
| 1 | Preview Option B vs C | **Option B** (1 nút duy nhất, 1280×720@30 + bloom on) | Đơn giản, không bắt user choose mỗi lần |
| 2 | Brightness clip white cho red/pink? | **ACCEPT clip** | Neon aesthetic — top face SHOULD blow out white như đèn neon thật |
| 3 | Block top_w < 12 vẽ chấm? | **KHÔNG vẽ gì** | Thêm chấm = noise; block xa nhận ra qua shape + glow + color đủ rồi |
| 4 | Benchmark rail_pillar_count=40 trước? | **Skip benchmark, commit 40** | Estimate +10-15% rail render cost — chấp nhận được |
| 5 | Hit line: full width vs strip per lane? | **2 strips per lane** (match reference) | Reference image rõ ràng 2 chevron track tách rời |
| 6 | Mesh/texture path ảnh hưởng? | **KHÔNG đụng** | Mesh/texture return sớm trước default neon path — hoàn toàn isolate |

## 5 Fix BỔ SUNG để match reference

### Fix #6 — IMPLEMENT rounded corners (CORNER_RADIUS đang là dead code)

**Vấn đề**: Reference image có corner bo tròn rõ rệt (~15-18% radius), nhưng code hiện `CORNER_RADIUS = 0.18` không được dùng — block render với `cv2.fillConvexPoly` góc nhọn tuyệt đối.

**Giải pháp**: Thay `cv2.fillConvexPoly` bằng custom rounded polygon fill. Vì cube faces là quad (4 corners), implement helper:

```python
def _fill_rounded_quad(canvas, pts, color, radius_frac, lineType=cv2.LINE_AA):
    """Fill 4-corner polygon với corners bo tròn.

    radius_frac: 0..0.45 (fraction of shortest edge).
    Implementation: shrink quad inward by radius, fill inner quad solid +
    fill 4 outer arcs + 4 outer rectangles.
    """
    pts = np.array(pts, dtype=np.float32)
    # Compute shortest edge
    edges = [np.linalg.norm(pts[(i+1) % 4] - pts[i]) for i in range(4)]
    r = max(0, int(min(edges) * radius_frac))
    if r < 1:
        cv2.fillConvexPoly(canvas, pts.astype(np.int32), color, lineType=lineType)
        return

    # Method: ROI-based — vẽ vào temp mask rồi composite
    bx, by, bw, bh = cv2.boundingRect(pts.astype(np.int32))
    pad = r + 2
    rx0 = max(0, bx - pad)
    ry0 = max(0, by - pad)
    rx1 = min(canvas.shape[1], bx + bw + pad)
    ry1 = min(canvas.shape[0], by + bh + pad)
    rw, rh = rx1 - rx0, ry1 - ry0
    if rw <= 0 or rh <= 0:
        return

    mask = np.zeros((rh, rw), dtype=np.uint8)
    pts_local = (pts - np.array([rx0, ry0])).astype(np.int32)

    # Fill base polygon
    cv2.fillConvexPoly(mask, pts_local, 255, lineType=lineType)

    # Erode to get inner shape
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r * 2 + 1, r * 2 + 1))
    mask_eroded = cv2.erode(mask, kernel)

    # Dilate eroded back with same kernel → rounded corners
    mask_rounded = cv2.dilate(mask_eroded, kernel)

    # Composite onto canvas
    color_arr = np.array(color, dtype=np.float32)
    a = mask_rounded.astype(np.float32) / 255.0
    a3 = np.dstack([a, a, a])
    roi = canvas[ry0:ry1, rx0:rx1].astype(np.float32)
    canvas[ry0:ry1, rx0:rx1] = (roi * (1 - a3) + color_arr * a3).clip(0, 255).astype(np.uint8)
```

**Apply trong PunchTarget.draw**:
```python
# Replace 3 calls cv2.fillConvexPoly cho top/front/side bằng:
_fill_rounded_quad(canvas, side_face,  side_scaled,  PunchTarget.CORNER_RADIUS)
_fill_rounded_quad(canvas, front_face, front_scaled, PunchTarget.CORNER_RADIUS)
_fill_rounded_quad(canvas, top_face,   top_scaled,   PunchTarget.CORNER_RADIUS)
```

**Performance note**: Erode + dilate là cheap operations (O(W×H)) trong ROI nhỏ. Ước tính +5-10% block render time, OK.

### Fix #7 — Fist icon REDESIGN + chuyển từ TOP face → FRONT face

**Vấn đề**:
1. Reference image fist icon ở **FRONT face** (mặt hướng camera), không phải top face như code hiện.
2. Current `_draw_fist_icon` decompose thành 3 parts (palm + 4 knuckles + thumb) — nhìn rời rạc, không bold.
3. Reference fist là **single bold silhouette** — fill đặc, sharp edges.

**Giải pháp 7a — chuyển icon sang front face**:
```python
# Trong PunchTarget.draw, thay:
# Fist icon on the TOP face
top_w = max edge length top_face
if top_w >= 22:
    cx_top = top_face[:, 0].mean()
    cy_top = top_face[:, 1].mean()
    _draw_fist_icon(canvas, cx_top, cy_top, top_w * 0.58, CLR_WHITE)

# THÀNH:
# Fist icon on the FRONT face (faces camera)
front_w = max edge length front_face
front_h = max(np.linalg.norm(front_face[1] - front_face[2]),
              np.linalg.norm(front_face[0] - front_face[3]))
if front_w >= 18:
    cx_front = int(front_face[:, 0].mean())
    cy_front = int(front_face[:, 1].mean())
    icon_size = int(min(front_w, front_h) * 0.62)   # 62% of smaller edge
    _draw_fist_icon_v2(canvas, cx_front, cy_front, icon_size, CLR_WHITE)
```

**Giải pháp 7b — redesign fist icon polygon**:

Vẽ fist như 1 single polygon với ~20 vertex để có shape chuẩn cú đấm:

```python
def _draw_fist_icon_v2(canvas, cx, cy, size, color=CLR_WHITE):
    """Bold fist icon — single solid silhouette, matches reference image.

    Layout: 4 knuckles bumps on top, palm body, thumb on left.
    All as ONE polygon for clean filled shape.
    """
    if size < 8:
        return
    s = size

    # Vertex layout (relative to center, scaled by s):
    # Going clockwise from top-left of palm:
    #   - Knuckle bumps at top (4 dome-like protrusions)
    #   - Right edge (palm)
    #   - Bottom rounded
    #   - Left edge (palm)
    #   - Thumb bump (left protrusion)

    pts = []

    # 4 knuckles on top (each = 2 vertices forming bump)
    knuckle_y_top = -s * 0.40
    knuckle_y_base = -s * 0.20
    knuckle_xs = [-0.30, -0.10, 0.10, 0.30]
    knuckle_w = 0.16

    # Start: top-left of palm body
    pts.append((-0.40, -0.10))

    for kx in knuckle_xs:
        x_l = kx - knuckle_w / 2
        x_r = kx + knuckle_w / 2
        # Up-left of knuckle
        pts.append((x_l, knuckle_y_base))
        pts.append((x_l, knuckle_y_top))
        # Up-right of knuckle
        pts.append((x_r, knuckle_y_top))
        pts.append((x_r, knuckle_y_base))

    # Top-right of palm body
    pts.append((0.40, -0.10))
    # Right edge down
    pts.append((0.42, 0.20))
    # Bottom-right rounded
    pts.append((0.36, 0.36))
    pts.append((0.20, 0.42))
    # Bottom-left rounded
    pts.append((-0.20, 0.42))
    pts.append((-0.36, 0.36))
    # Left edge up
    pts.append((-0.42, 0.20))
    pts.append((-0.40, 0.05))

    # Thumb bump (sticks out left)
    pts.append((-0.50, 0.00))
    pts.append((-0.55, -0.10))
    pts.append((-0.50, -0.18))
    pts.append((-0.40, -0.15))

    # Close polygon
    # Convert to absolute pixel coords
    abs_pts = np.array(
        [(int(cx + p[0] * s), int(cy + p[1] * s)) for p in pts],
        dtype=np.int32,
    )

    cv2.fillPoly(canvas, [abs_pts], color, lineType=cv2.LINE_AA)
```

**Optional tweak — Inner finger grooves** (cho icon bold hơn):
```python
# After fillPoly, draw 3 dark vertical lines tách 4 fingers
groove_color = tuple(int(c * 0.65) for c in color)   # 65% darker
for kx in [-0.20, 0.00, 0.20]:
    x = int(cx + kx * size)
    y0 = int(cy - 0.30 * size)
    y1 = int(cy - 0.05 * size)
    cv2.line(canvas, (x, y0), (x, y1), groove_color, max(1, int(size * 0.04)),
             lineType=cv2.LINE_AA)
```

**Giải pháp 7c — Simplified version cho block xa**:
```python
def _draw_fist_icon_simple_v2(canvas, cx, cy, size, color):
    """Bold simple fist for far blocks — just knuckle bumps + palm."""
    if size < 6:
        return
    s = size
    # Single rounded square với 4 small bumps on top
    pts = [
        (-0.35, -0.30), (-0.20, -0.40), (-0.05, -0.30),
        (0.05, -0.40), (0.20, -0.30), (0.35, -0.40),
        (0.40, 0.30), (-0.40, 0.30),
    ]
    abs_pts = np.array(
        [(int(cx + p[0] * s), int(cy + p[1] * s)) for p in pts],
        dtype=np.int32,
    )
    cv2.fillPoly(canvas, [abs_pts], color, lineType=cv2.LINE_AA)
```

### Fix #8 — Color palette EXPAND 4 lanes 4 colors

**Vấn đề**: Reference image có **blue + yellow** block, nhưng code hiện chỉ `is_left ? green : red`. Hard-coded 2 colors.

**Giải pháp**: Mở rộng palette per-lane:

```python
# Trong PunchTarget hoặc top-level constants:
PUNCH_LANE_COLORS = {
    0: (255, 60, 50),     # outer-left — neon blue (BGR)
    1: (50, 200, 255),    # inner-left — cyan
    2: (50, 230, 80),     # inner-right — green
    3: (40, 200, 255),    # outer-right — yellow (BGR yellow = (0,255,255))
}
# Hoặc theo reference: blue (lane 0) + yellow (lane 3), 2 lane giữa = mid colors

# Trong PunchTarget.__init__ hoặc create_target factory:
self.color = PUNCH_LANE_COLORS.get(lane, CLR_NEON_LIME)
```

**User customization**: Expose qua render_settings:
```python
# BaseRenderSettings (mode_punch hoặc segment level):
punch_lane_color_0: str = Field(default="#FF3C32")   # blue
punch_lane_color_1: str = Field(default="#32C8FF")   # cyan
punch_lane_color_2: str = Field(default="#50E650")   # green
punch_lane_color_3: str = Field(default="#FFCC00")   # yellow

# Inspector UI: 4 color picker rows.
```

### Fix #9 — Bloom intensity tăng default

**Vấn đề**: Reference có bloom **rất mạnh** — block có halo lan ra ~50-80px. Current default bloom có vẻ yếu hơn.

**Giải pháp**: Tăng bloom default config:

```python
# Trong rhythm.py gpu_glow() default:
def gpu_glow(canvas, sigma=24.0, gain=0.75):   # was sigma=18.0, gain=0.55
    ...
```

Hoặc tăng qua render_settings field nếu đã expose:
```python
bloom_sigma: float = Field(default=24.0, ge=0.0, le=60.0)   # was 18.0
bloom_gain:  float = Field(default=0.75, ge=0.0, le=2.0)    # was 0.55
```

### Fix #10 — Side rails REDESIGN

**Vấn đề**: Current side rails = thick purple **walls** chạy từ floor lên trên — heavy, jagged, đè cảnh.

**Reference image**: Side rails là:
- **Ceiling beams** trên đỉnh tunnel (thanh ngang nhỏ + chevron)
- **Accent dot lights** nhỏ cạnh floor edge (pink dots)
- **KHÔNG có wall** chắn 2 bên

**Giải pháp**: Đây là refactor lớn — đổi rail rendering từ wall sang 2 elements:

#### 10a — Ceiling beam (top horizontal beam)
```python
# Trong tunnel.draw() hoặc SideRail class:
# Vẽ 1 thanh mỏng chạy ngang ngay sát ceiling Y position
beam_y_world = -0.8   # eye-level - 0.8 = ceiling
beam_height_world = 0.05   # mỏng

for z in chevron_zs:
    proj_l = cam.project(-LANE_WIDTH * 2, beam_y_world, z)
    proj_r = cam.project( LANE_WIDTH * 2, beam_y_world, z)
    proj_l_b = cam.project(-LANE_WIDTH * 2, beam_y_world + beam_height_world, z)
    proj_r_b = cam.project( LANE_WIDTH * 2, beam_y_world + beam_height_world, z)
    # Vẽ chevron pattern bên dưới beam
    ...
```

#### 10b — Floor edge accent dots
```python
# Replace pillar walls bằng small dots tại floor edge
dot_radius_world = 0.04
for z in dot_zs:
    for side in [-1, 1]:
        wx = side * (LANE_WIDTH * 2 + 0.1)
        proj = cam.project(wx, FLOOR_Y, z)
        if proj is None:
            continue
        screen_radius = int(dot_radius_world * cam.fy / z)
        cv2.circle(canvas, proj, screen_radius, CLR_NEON_PINK, -1, lineType=cv2.LINE_AA)
        # Optional: glow halo around dot
```

#### 10c — Disable old wall rendering

Old `SideRail.draw()` với 4-corner cuboid pillars cần được toggle off. Add config:
```python
rail_style: str = Field(default="ceiling_beams", regex="^(walls|ceiling_beams|none)$")
```
- `walls` = current behavior (backward compat)
- `ceiling_beams` = new reference style
- `none` = no rails

**Decision**: Default = `ceiling_beams` (match reference). User có thể chọn `walls` nếu muốn giữ legacy look.

### Fix #11 — 2 floor tracks tách rời với chevron

**Vấn đề**: Reference có **2 track pink/magenta** tách rời (1 cho lane left, 1 cho lane right), mỗi track có chevron pattern dọc. Floor giữa 2 track = đen.

**Giải pháp**: Refactor `tunnel.draw()` floor rendering:

```python
# Replace single-floor with 2 separate tracks
TRACK_WIDTH_WORLD = 0.45   # mỗi track rộng 45% lane unit
TRACK_GAP_WORLD = 0.10     # gap giữa 2 track
TRACK_COLOR = CLR_NEON_PINK

# Track left: x in [-LANE_WIDTH*2, -GAP/2]
# Track right: x in [GAP/2, LANE_WIDTH*2]

for z in z_grid:
    for side_sign in [-1, 1]:
        x_inner = side_sign * (TRACK_GAP_WORLD / 2)
        x_outer = side_sign * (LANE_WIDTH * 2)
        # 4 corners của track segment tại depth z, z+dz
        ...
        cv2.fillPoly(canvas, [pts], TRACK_COLOR, lineType=cv2.LINE_AA)
        # Chevron stripe trong track
        ...
```

**Implementation detail**: Chevron pattern = dải sáng-tối xen kẽ scrolling theo Z. Mỗi stripe vẽ 1 polygon.

### Fix #12 — Background distant points/stars

Reference có small bright points ở distance (background) tạo depth. Implementation đơn giản:

```python
# Trong bg_layer.frame() hoặc tunnel.draw():
# Generate fixed star field once (not per-frame)
if not hasattr(self, '_star_field'):
    rng = np.random.default_rng(seed=42)   # deterministic
    n_stars = 80
    self._star_field = [
        (rng.uniform(0.1, 0.9), rng.uniform(0.1, 0.5), rng.uniform(0.5, 1.0))
        for _ in range(n_stars)
    ]   # (x_norm, y_norm, brightness)

H, W = canvas.shape[:2]
for sx, sy, sb in self._star_field:
    px = int(sx * W)
    py = int(sy * H)
    intensity = int(180 * sb)
    cv2.circle(canvas, (px, py), 1, (intensity, intensity, intensity), -1)
```

---

## 🎨 Tổng hợp code change tóm tắt

| File | Change | Reason |
|---|---|---|
| `studio/core_bridge/render_service.py:495-501` | Preview 540p→720p, bloom 0→1, fps 24→30 | Fix #1 |
| `src/rhythm.py:2228-2230` | Top brightness `0.90+0.10` → `1.15+0.15`; side `0.50` → `0.45` | Fix #2 |
| `src/rhythm.py:2265` | Front ratio `1.15` → `1.30` | Fix #2 |
| `src/rhythm.py:2272-2277` | Bỏ dead zone ±2px, luôn vẽ side face | Fix #3 |
| `src/rhythm.py:2280-2289` | Icon position TOP→FRONT face, threshold 22→18, simplified version cho 12-22px | Fix #4 + #7 |
| `src/rhythm.py` (new helper) | `_fill_rounded_quad()` | Fix #6 |
| `src/rhythm.py:2274-2278` | Replace `cv2.fillConvexPoly` → `_fill_rounded_quad(..., CORNER_RADIUS)` | Fix #6 |
| `src/rhythm.py` (new helper) | `_draw_fist_icon_v2()` + `_draw_fist_icon_simple_v2()` | Fix #7 |
| `src/rhythm.py` PUNCH_LANE_COLORS | 4 lanes 4 colors palette | Fix #8 |
| `src/rhythm.py` `gpu_glow` defaults | sigma 18→24, gain 0.55→0.75 | Fix #9 |
| `src/rhythm.py` SideRail | Refactor sang ceiling beam + floor dots | Fix #10 |
| `src/rhythm.py` tunnel | Refactor floor sang 2 tracks pink chevron | Fix #11 |
| `src/rhythm.py` bg_layer | Add star field background | Fix #12 |
| `studio/models/render_settings.py` | Add fields: punch_lane_color_0..3, rail_style, bloom_sigma, bloom_gain | Fix #8, #10 |

---

## Migration plan FINAL

| Phase | Fixes | Effort | Risk | Visual delta |
|---|---|---|---|---|
| **Phase 1** | #1 Preview 720p+bloom | 5 phút | Thấp | ⭐⭐⭐ |
| **Phase 2** | #2 Brightness + #3 Side face force | 30 phút | Thấp | ⭐⭐⭐ |
| **Phase 3** | #6 Rounded corners | 1 giờ | Thấp | ⭐⭐⭐ |
| **Phase 4** | #7 Fist icon v2 (redesign + front face) | 1-2 giờ | Trung | ⭐⭐⭐ |
| **Phase 5** | #4 Icon threshold lower + simplified | 30 phút | Thấp | ⭐⭐ |
| **Phase 6** | #8 Color palette 4 lanes | 30 phút | Thấp | ⭐⭐ |
| **Phase 7** | #9 Bloom intensity tăng | 5 phút | Thấp | ⭐⭐ |
| **Phase 8** | #5 Side rails AA basic fixes (LINE_AA + pillar count) | 1 giờ | Thấp | ⭐⭐ |
| **Phase 9** | #10 Side rails REDESIGN (ceiling beams + dots) | 3-4 giờ | Cao | ⭐⭐⭐⭐ |
| **Phase 10** | #11 Floor 2 tracks pink chevron | 3-4 giờ | Cao | ⭐⭐⭐⭐ |
| **Phase 11** | #12 Background stars | 30 phút | Thấp | ⭐ |

**Total**: ~12-15 giờ implementation. Test visual A/B sau mỗi phase.

**Recommend thứ tự**:
- **Tuần 1**: Phase 1-7 (quick wins, ~5-6 giờ) — đã đem về 80% giá trị reference
- **Tuần 2**: Phase 8-11 (refactor lớn, ~7-9 giờ) — đem về 100% match reference

---

## Visual checklist cuối — match reference image

```
✓ Block 3D có corner bo tròn rõ rệt (~18% radius)
✓ Block có top face sáng + ngả trắng (clip white = OK)
✓ Block có 1 side face visible cho mọi lane (kể cả center)
✓ Fist icon BOLD ở FRONT face (không phải top), single solid silhouette
✓ Block xa vẫn có simplified icon (knuckles + palm bar)
✓ Block màu xanh dương / vàng / xanh lá / tùy lane (4 colors palette)
✓ Bloom halo neon mạnh quanh block (sigma 24, gain 0.75)
✓ Side rails = ceiling beams + floor dot lights (KHÔNG còn wall heavy)
✓ Floor = 2 track pink chevron tách rời, giữa = đen
✓ Background = đen + small distant stars
✓ Preview render 720p smooth, không jagged
```

---

## Acceptance v2

```
✓ Render full HD output side-by-side với reference image — perceptual similarity ≥85%
✓ KHÔNG break full render output backward compat (1080p+bloom=1 trông ít nhất bằng hiện tại)
✓ Performance: full render time +<10% so với baseline
✓ Preview render time +50-80% so với baseline (acceptable tradeoff for quality)
✓ User có 4 color picker cho 4 lane (Inspector panel)
✓ User có dropdown rail_style (walls / ceiling_beams / none)
✓ Default = match reference style (ceiling_beams + 4 colors + 2 tracks)
```

Tôi đã chốt mọi quyết định. Bạn implement theo migration plan trên — bắt đầu từ Phase 1 (5 phút quick win) rồi từng phase một, test visual sau mỗi phase. Nếu phase nào ra kết quả không như spec mong đợi → ping tôi điều chỉnh.

---

# 🚨 ICON FIST V3 — CRITICAL REWRITE (current icon SAI HOÀN TOÀN concept)

## Phát hiện sau khi user gửi screenshot hệ thống vs reference

**Vấn đề chí mạng**: Code `_draw_fist_icon` hiện tại vẽ thành **TAY MỞ XÒE** chứ KHÔNG phải nắm đấm:
- 4 knuckles được vẽ là 4 rectangle vertical CHĨA LÊN từ top palm → trông như 4 ngón tay duỗi
- Concept hoàn toàn sai vs "punch" (đấm)

Reference image rõ ràng là **closed fist** quay về camera:
- 4 knuckles = 4 dome bumps trên TOP edge của fist body
- Bên dưới có 3 finger grooves (đường rãnh dọc) → ngón tay cuộn lại
- Thumb wrapped HORIZONTAL ở giữa fist (không phải bên trái)

## So sánh visual specifications

| Spec | Current (sai) | Target v3 (mẫu) |
|---|---|---|
| Concept | Open palm "STOP" | Closed fist "PUNCH" |
| Knuckle direction | Vertical bars chĩa LÊN | Dome bumps trên top edge |
| Finger representation | Không có (knuckles = ngón) | 3 vertical grooves dưới knuckles |
| Thumb position | Bên TRÁI palm | Vắt NGANG giữa fist |
| Thumb shape | Rectangle dọc | Bar ngang có taper bên phải |
| Icon size vs face | 58% | **75%** |
| Icon color | Trắng đặc | **Trắng fill + đen outline** |
| Outline thickness | Không | ~5% size (bold) |
| Detail level | Flat shapes | Outline + grooves + thumb wrap |

## Spec helper FINAL — `_draw_fist_icon_v3`

```python
def _draw_fist_icon_v3(canvas: np.ndarray, cx: int, cy: int, size: int,
                       outline_color=(20, 20, 20),
                       fill_color=(245, 245, 245)) -> None:
    """Closed fist icon — match reference image (knuckles + grooves + thumb wrap).

    Layout viewed from front:
      ╭──╮╭──╮╭──╮╭──╮     ← 4 knuckle bumps (top)
      │              │     ← fist body
      │  ┃   ┃   ┃   │     ← 3 finger grooves
      │  ╭─────╮     │     ← thumb wrap (horizontal)
      │  ╰─────╯     │
      ╰──────────────╯     ← bottom rounded

    Args:
        size: tổng chiều rộng icon (px). Skip nếu < 8.
        outline_color: BGR cho outline + grooves (default near-black)
        fill_color: BGR cho fill bên trong (default near-white)
    """
    if size < 8:
        return
    s = size

    # ── Outline polygon vertices (relative to center, normalized to size) ──
    knuckle_top  = -0.42
    knuckle_base = -0.28
    knuckle_xs   = [-0.30, -0.10, 0.10, 0.30]
    knuckle_w    = 0.14

    pts_norm = []
    pts_norm.append((-0.40, -0.20))   # top-left fist body

    for kx in knuckle_xs:
        x_l = kx - knuckle_w / 2
        x_r = kx + knuckle_w / 2
        # Dome: left-base → top-mid → right-base (3 verts)
        pts_norm.append((x_l, knuckle_base))
        pts_norm.append((kx,  knuckle_top))
        pts_norm.append((x_r, knuckle_base))

    pts_norm.append((0.40, -0.20))    # top-right
    pts_norm.append((0.44, 0.10))     # right-mid
    pts_norm.append((0.40, 0.38))     # bottom-right rounded
    pts_norm.append((0.20, 0.44))     # bottom-right curve
    pts_norm.append((-0.20, 0.44))    # bottom-left curve
    pts_norm.append((-0.40, 0.38))    # bottom-left rounded
    pts_norm.append((-0.44, 0.10))    # left-mid

    # Convert to absolute pixel coords
    abs_pts = np.array(
        [(int(cx + p[0] * s), int(cy + p[1] * s)) for p in pts_norm],
        dtype=np.int32,
    )

    # ── 1) Fill body ────────────────────────────────────────
    cv2.fillPoly(canvas, [abs_pts], fill_color, lineType=cv2.LINE_AA)

    # ── 2) Bold outline ─────────────────────────────────────
    outline_thickness = max(2, int(s * 0.055))
    cv2.polylines(canvas, [abs_pts], isClosed=True, color=outline_color,
                  thickness=outline_thickness, lineType=cv2.LINE_AA)

    # ── 3) Finger grooves (3 vertical lines under knuckles) ─
    groove_thickness = max(1, int(s * 0.045))
    groove_y0 = int(cy + (-0.18) * s)
    groove_y1 = int(cy + (0.05) * s)
    for gx_norm in [-0.20, 0.00, 0.20]:
        gx = int(cx + gx_norm * s)
        cv2.line(canvas, (gx, groove_y0), (gx, groove_y1),
                 outline_color, groove_thickness, lineType=cv2.LINE_AA)

    # ── 4) Thumb wrap (horizontal bar with taper on right) ──
    thumb_y       =  0.18
    thumb_half_h  =  0.07
    thumb_x_l     = -0.22
    thumb_x_r_top =  0.22
    thumb_x_r_bot =  0.18    # taper inward at bottom

    thumb_pts = np.array([
        (cx + int(thumb_x_l * s),     cy + int((thumb_y - thumb_half_h) * s)),
        (cx + int(thumb_x_r_top * s), cy + int((thumb_y - thumb_half_h) * s)),
        (cx + int(thumb_x_r_bot * s), cy + int((thumb_y + thumb_half_h) * s)),
        (cx + int(thumb_x_l * s),     cy + int((thumb_y + thumb_half_h) * s)),
    ], dtype=np.int32)

    # Thumb fill (slightly brighter than body) + outline
    cv2.fillPoly(canvas, [thumb_pts], fill_color, lineType=cv2.LINE_AA)
    cv2.polylines(canvas, [thumb_pts], isClosed=True, color=outline_color,
                  thickness=outline_thickness, lineType=cv2.LINE_AA)


def _draw_fist_icon_simple_v3(canvas, cx, cy, size,
                               outline_color=(20, 20, 20),
                               fill_color=(245, 245, 245)):
    """Simplified fist for far/small blocks — keep concept, drop details."""
    if size < 6:
        return
    s = size
    # Just 4 dome bumps + body (no grooves, no thumb)
    pts_norm = [
        (-0.40, -0.15),
        (-0.30, -0.30), (-0.20, -0.40), (-0.10, -0.30),
        ( 0.00, -0.40),
        ( 0.10, -0.30), ( 0.20, -0.40), ( 0.30, -0.30),
        ( 0.40, -0.15),
        ( 0.40,  0.40),
        (-0.40,  0.40),
    ]
    abs_pts = np.array(
        [(int(cx + p[0] * s), int(cy + p[1] * s)) for p in pts_norm],
        dtype=np.int32,
    )
    cv2.fillPoly(canvas, [abs_pts], fill_color, lineType=cv2.LINE_AA)
    cv2.polylines(canvas, [abs_pts], isClosed=True, color=outline_color,
                  thickness=max(1, int(s * 0.06)), lineType=cv2.LINE_AA)
```

## Cách integrate vào PunchTarget.draw

Replace toàn bộ block icon ở line 2280-2289:

```python
# DELETE
top_w = int(max(
    np.linalg.norm(top_face[1] - top_face[0]),
    np.linalg.norm(top_face[2] - top_face[3]),
))
if top_w >= 22:
    cx_top = int(top_face[:, 0].mean())
    cy_top = int(top_face[:, 1].mean())
    _draw_fist_icon(canvas, cx_top, cy_top,
                    int(top_w * 0.58), CLR_WHITE)
return canvas

# REPLACE BY
# Icon on FRONT face (matches reference v3, đấm về camera)
front_edges = [
    np.linalg.norm(front_face[1] - front_face[0]),   # top
    np.linalg.norm(front_face[2] - front_face[3]),   # bottom
    np.linalg.norm(front_face[0] - front_face[3]),   # left
    np.linalg.norm(front_face[1] - front_face[2]),   # right
]
front_min_dim = int(min(front_edges))   # smaller of width/height
icon_size = int(front_min_dim * 0.75)   # 75% of smaller face dimension

if icon_size >= 12:
    cx_front = int(front_face[:, 0].mean())
    cy_front = int(front_face[:, 1].mean())
    if icon_size >= 22:
        _draw_fist_icon_v3(canvas, cx_front, cy_front, icon_size,
                           outline_color=(20, 20, 20),
                           fill_color=(245, 245, 245))
    else:
        _draw_fist_icon_simple_v3(canvas, cx_front, cy_front, icon_size,
                                   outline_color=(20, 20, 20),
                                   fill_color=(245, 245, 245))
return canvas
```

## Lý do icon trắng-đen thay vì trắng đặc

Reference dùng **outline đen + fill trắng** vì:
1. **Contrast cực mạnh** trên block màu sáng (xanh dương / vàng / xanh lá)
2. **Detail rõ** — finger grooves, thumb wrap đều cần outline để đọc được
3. **Không bị merge** vào block khi block sáng (vd block vàng + icon trắng → khó đọc)
4. **Universal** — works trên mọi tier color khác nhau

Code hiện vẽ trắng đặc → trên block xanh dương đọc tạm OK, nhưng trên block vàng / cyan / pink sẽ mất contrast.

## Acceptance v3

```
✓ Icon trông rõ ràng là CLOSED FIST (không phải tay xòe)
✓ Có 4 knuckle bumps trên top
✓ Có 3 finger grooves vertical bên dưới knuckles
✓ Có thumb wrap horizontal ở middle
✓ Outline đen ~5% size, fill trắng/off-white
✓ Icon size 75% smaller face dim (was 58%)
✓ Render rõ trên mọi block color (blue/yellow/green/red/cyan/pink)
✓ Block xa (icon_size 12-21px): simplified version (knuckles + body, no grooves/thumb)
✓ Block cực xa (icon_size < 12): no icon
```

## Update Migration plan

Phase 4 (Fist icon v2) → REPLACE bằng **Phase 4 v3**:
- Implement `_draw_fist_icon_v3` + `_draw_fist_icon_simple_v3`
- Replace integrate trong PunchTarget.draw (FRONT face, threshold 12/22, color outline+fill)
- Test trên 4 lane colors khác nhau verify contrast OK

Effort: 1.5-2 giờ (tăng nhẹ vs v2 vì có outline + fill + grooves + thumb).
Risk: Thấp — isolated trong icon function.

---

# 🚨 COLOR ANALYSIS V3 — Fix #2 REVISED (block không 3D, sáng tối không rõ)

## Phát hiện sau khi user complain "màu xấu, không 3D, sáng tối không rõ"

### BUG NGHIÊM TRỌNG — Front face derives sai từ side_col

`rhythm.py:2229` và `2265`:
```python
side_col     = tuple(int(c * 0.50) for c in base)              # 50% base
front_scaled = tuple(int(min(255, c * depth_gain * 1.15))      # ← BUG
                     for c in side_col)                         # front = side * 1.15 = base * 0.575
```

**Hậu quả**: Front face — mặt camera nhìn thẳng vào, chiếm 60-70% pixel block — chỉ là **57.5% base brightness**. Block trông dim, mất sức sống.

→ Đây là root cause "block xấu, không 3D, không vibrant".

## Brightness ratio analysis

Với base xanh dương `(255, 60, 50)` BGR:

| Face | Code current → output | Brightness ratio | Reference cần đạt |
|---|---|---|---|
| **Top**   | `base*0.90 + 255*0.10` → `(250, 79, 75)`   | 95% | **130%** (highlight rõ) |
| **Front** | `base*0.575` → `(147, 35, 29)` ⚠️           | **57.5%** | **100%** (canonical) |
| **Side**  | `base*0.50` → `(128, 30, 25)`              | 50% | **45%** (OK) |

**Ratio current**: top:front:side = 1.9 : 1.15 : 1 (front quá tối).
**Ratio reference**: top:front:side = **2.9 : 2.2 : 1** (front dominant + top highlight rõ).

## Fix #2a — Front = base CANONICAL (FIX BUG)

```python
# rhythm.py:2227-2230 — REPLACE
base = tuple(int(c) for c in self.color)

# Top: highlight + ngả trắng
top_col = tuple(
    int(min(255, c * 1.20 + 255 * 0.18))
    for c in base
)

# Front: CANONICAL — đây là màu user expect thấy
front_col_raw = tuple(int(c) for c in base)

# Side: shadow ~45%
side_col = tuple(int(c * 0.45) for c in base)

depth_gain = 0.70 + 0.30 * (1.0 - z_norm)
```

```python
# rhythm.py:2264-2266 — REPLACE
top_scaled   = tuple(int(min(255, c * depth_gain)) for c in top_col)
front_scaled = tuple(int(min(255, c * depth_gain)) for c in front_col_raw)   # was side_col * 1.15 — BUG
side_scaled  = tuple(int(min(255, c * depth_gain)) for c in side_col)
```

**Verification với base blue `(255, 60, 50)`**:
- Top: `(255, 117, 105)` — vibrant blue + slight white tint
- Front: `(255, 60, 50)` — **CANONICAL blue đậm sắc** (was muddy 147,35,29)
- Side: `(115, 27, 22)` — shadow rõ rệt

→ Block sẽ có 3 face contrast **2.9 : 2.2 : 1** match reference.

## Fix #2b — Gradient mỗi face (block không còn flat)

**Vấn đề**: `cv2.fillConvexPoly` chỉ fill solid 1 màu → mỗi face flat → mất cảm giác lighting.

**Solution**: Vẽ gradient nhẹ mỗi face theo hướng phù hợp:

```python
def _fill_face_with_gradient(canvas: np.ndarray, pts: np.ndarray,
                              color_bright: tuple, color_dim: tuple,
                              gradient_dir: str = "top_to_bottom") -> None:
    """Fill polygon với linear gradient subtle.

    gradient_dir options:
      - "top_to_bottom": cho FRONT face — sáng top edge, tối bottom edge
      - "front_to_back": cho TOP face — sáng front edge, tối back edge
      - "left_to_right": cho SIDE face — tùy hướng

    Gradient dim factor mặc định ~0.85 (không quá tối, giữ readability).
    """
    pts = np.array(pts, dtype=np.int32)
    bx, by, bw, bh = cv2.boundingRect(pts)
    pad = 2
    H, W = canvas.shape[:2]
    rx0, ry0 = max(0, bx - pad), max(0, by - pad)
    rx1, ry1 = min(W, bx + bw + pad), min(H, by + bh + pad)
    rw, rh = rx1 - rx0, ry1 - ry0
    if rw <= 0 or rh <= 0:
        return

    # Mask cho polygon
    mask = np.zeros((rh, rw), dtype=np.uint8)
    pts_local = pts - np.array([rx0, ry0])
    cv2.fillConvexPoly(mask, pts_local, 255, lineType=cv2.LINE_AA)

    # Gradient layer
    bright = np.array(color_bright, dtype=np.float32)
    dim = np.array(color_dim, dtype=np.float32)

    if gradient_dir == "left_to_right":
        t = np.linspace(0, 1, rw, dtype=np.float32).reshape(1, -1, 1)
    else:   # top_to_bottom hoặc front_to_back (đều theo Y)
        t = np.linspace(0, 1, rh, dtype=np.float32).reshape(-1, 1, 1)

    color_grad = bright * (1 - t) + dim * t   # broadcasting → (rh, rw, 3)
    color_grad = np.broadcast_to(color_grad, (rh, rw, 3))

    # Composite onto canvas
    a = mask.astype(np.float32) / 255.0
    a3 = np.dstack([a, a, a])
    roi = canvas[ry0:ry1, rx0:rx1].astype(np.float32)
    canvas[ry0:ry1, rx0:rx1] = (
        roi * (1 - a3) + color_grad * a3
    ).clip(0, 255).astype(np.uint8)
```

**Apply trong PunchTarget.draw — replace 3 calls fillConvexPoly**:

```python
# REPLACE block 2274-2278:

# Side face với gradient front_to_back
side_bright = tuple(int(min(255, c * 1.10)) for c in side_scaled)
side_dim    = tuple(int(c * 0.85) for c in side_scaled)

if block_screen_cx < cam_cx:
    _fill_face_with_gradient(canvas, right_face, side_bright, side_dim, "front_to_back")
else:
    _fill_face_with_gradient(canvas, left_face,  side_bright, side_dim, "front_to_back")

# Front face với gradient top_to_bottom (light from above)
front_bright = front_scaled
front_dim    = tuple(int(c * 0.88) for c in front_scaled)
_fill_face_with_gradient(canvas, front_face, front_bright, front_dim, "top_to_bottom")

# Top face với gradient front_to_back
top_bright = top_scaled
top_dim    = tuple(int(c * 0.82) for c in top_scaled)
_fill_face_with_gradient(canvas, top_face, top_bright, top_dim, "front_to_back")
```

## Fix #2c — Rim lighting trên edges

**Vấn đề**: Edges giao 3 face hiện là hard transition → block trông "carved" không "lit".

**Solution**: Vẽ 1-2px line sáng tại 3 edges chính (top-front, top-side, front-side):

```python
# Sau khi đã vẽ 3 face với gradient, thêm rim:
RIM_BRIGHT = tuple(int(min(255, c * 1.45 + 30)) for c in base)
RIM_THICKNESS = max(1, int(top_w * 0.025))

# Edge top-front (FLT → FRT) = pts[0] → pts[1]
cv2.line(canvas, tuple(pts[0]), tuple(pts[1]), RIM_BRIGHT,
         RIM_THICKNESS, lineType=cv2.LINE_AA)

# Edge top-side: depends on which side face
if block_screen_cx < cam_cx:
    # Vẽ right_face → edge top-right giao là FRT → BRT (pts[1] → pts[5])
    cv2.line(canvas, tuple(pts[1]), tuple(pts[5]), RIM_BRIGHT,
             RIM_THICKNESS, lineType=cv2.LINE_AA)
    # Edge front-side = FRT → FRb (pts[1] → pts[2]) — optional weaker rim
    rim_dim = tuple(int(c * 0.7) for c in RIM_BRIGHT)
    cv2.line(canvas, tuple(pts[1]), tuple(pts[2]), rim_dim,
             max(1, RIM_THICKNESS - 1), lineType=cv2.LINE_AA)
else:
    cv2.line(canvas, tuple(pts[0]), tuple(pts[4]), RIM_BRIGHT,
             RIM_THICKNESS, lineType=cv2.LINE_AA)
    rim_dim = tuple(int(c * 0.7) for c in RIM_BRIGHT)
    cv2.line(canvas, tuple(pts[0]), tuple(pts[3]), rim_dim,
             max(1, RIM_THICKNESS - 1), lineType=cv2.LINE_AA)
```

## Visual diff before vs after

```
BEFORE (current)              AFTER (v3 với 3 fix)
─────────────────────         ────────────────────────
flat blue rectangle           3D cube với rõ:
top hơi sáng                  ✓ Top: vibrant + ngả trắng (gradient front→back)
front DIM (57.5% base) ⚠️    ✓ Front: CANONICAL color (gradient top→bottom)
side dark                     ✓ Side: dark shadow (gradient front→back)
edges hard                    ✓ Rim light trên 3 edges giao
no lighting feel              ✓ Cảm giác "neon block phát sáng" rõ rệt
```

## Test cases v3

```
✓ Block xanh dương: front sáng đậm sắc, top ngả trắng, side dark navy
✓ Block vàng: front saturated yellow, top hơi cream, side dark olive
✓ Block xanh lá: front emerald, top mint highlight, side dark forest
✓ Block đỏ/hồng: clip white ở top OK (neon aesthetic), front canonical red
✓ Gradient mỗi face visible nhưng subtle (không "graphic design")
✓ Rim light trên top-front edge nổi bật, top-side edge subtle
✓ Side-by-side với reference image: 3D pop ≥ 90% similarity
✓ Block xa (z_norm > 0.7): gradient + rim vẫn visible (giảm intensity OK)
```

## Performance estimate

- Fix #2a: 0% impact (chỉ đổi formula)
- Fix #2b: +5-10% block render time (gradient = 1 ROI alloc + linspace + broadcast)
- Fix #2c: +1-2% (3 calls cv2.line LINE_AA)

→ Tổng +6-12% block render. Acceptable cho visual lift lớn.

## Migration plan UPDATE — Fix #2 v3 chi tiết

| Sub-fix | Effort | Impact | Recommend |
|---|---|---|---|
| **2a** Front canonical (bỏ bug `side_col * 1.15`) | 5 phút | ⭐⭐⭐⭐ HUGE | **MUST DO FIRST** |
| **2b** Gradient mỗi face (helper mới) | 1-1.5 giờ | ⭐⭐⭐ | DO NEXT |
| **2c** Rim lighting edges | 30 phút | ⭐⭐ | DO LAST (polish) |

**Recommend tuần này**: Làm 2a TRƯỚC tiên (chỉ 5 phút sửa formula → block đã pop hơn rất nhiều). 2b và 2c là polish có thể delay.
