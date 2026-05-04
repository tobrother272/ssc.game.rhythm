# Relax MIDDLE — Fade Timing Fix Spec

## Mục tiêu

Sửa 2 bug trong `RelaxTarget._draw_middle` (file `src/rhythm.py`) liên quan tới fade-out timing của MIDDLE wall, gây hiện tượng **wall biến mất ở "nửa đoạn đường"** sau khi đã unify motion với LOW/HIGH.

**Bug 1 (chính):** `FADE_START = 0.9` hardcoded không match phase split của motion mới (`T = 0.9655`). Fade kích hoạt trong drift phase thay vì vút phase → wall mờ dần khi vẫn còn xa camera.

**Bug 2 (pre-existing, lộ rõ hơn sau unify):** `p_lin` trong `_draw_middle` dùng `spawn_frame`, trong khi `depth()` dùng `move_start_frame`. Nếu `RELAX_WAIT_SEC > 0`, 2 timing không sync.

Spec này độc lập với mọi spec khác. Chỉ đụng 4 dòng trong 1 method.

---

## Bug 1: FADE_START không match motion mới

### Vấn đề

`_draw_middle` dòng 3439:

```python
FADE_START = 0.9
```

Hằng số `0.9` được tune cho motion CŨ của MIDDLE (`T_M = 2/3 ≈ 0.667`, Phase 2 từ p_lin = 0.667 → 1.0). Tại p_lin = 0.9, wall đã ở Phase 2 với z = 0.06 (sát camera) → fade ra hợp lý.

Sau khi [unify motion](./relax-middle-motion-unify-spec.md), MIDDLE dùng cùng phase split LOW/HIGH:
- `PHASE_SPLIT_D = 0.70` → Phase 2 bắt đầu tại `T = self._phase_split_t() ≈ 0.9655`
- Tại p_lin = 0.9 → wall vẫn trong Phase 1 drift, z ≈ 0.35 → visually ~50% chiều cao màn hình
- Fade kích hoạt → wall mờ dần khi vẫn ở giữa màn hình

### Hệ quả thực tế

Với travel = 180 frames (6s):

```
p_lin = 0.50:  z = 0.64  (drift, far)         alpha = 1.0   ← wall solid
p_lin = 0.80:  z = 0.42  (drift, mid)         alpha = 1.0   ← wall solid
p_lin = 0.90:  z = 0.35  (drift, ~50% screen) alpha = 1.0   ← FADE BẮT ĐẦU
p_lin = 0.95:  z = 0.32  (drift)              alpha = 0.5   ← wall đã mờ một nửa
p_lin = 1.00:  z = 0     (hit_frame)          alpha = 0     ← wall biến mất
```

User report: "block di chuyển và biến mất khi mới di chuyển nửa đoạn đường".

→ Wall không bao giờ "vút tới mặt" như intent của unify motion. Visual vút hoàn toàn bị fade ẩn đi.

### Fix

Thay hằng số bằng giá trị động `self._phase_split_t()` để fade luôn sync với điểm bắt đầu vút phase:

```python
# CŨ:
FADE_START = 0.9

# MỚI:
FADE_START = self._phase_split_t()    # = 0.9655 với constants hiện tại
```

Sau fix, fade chỉ kích hoạt trong vút phase (~0.21s ở travel=180):

```
p_lin = 0.50:  z = 0.64  (drift)              alpha = 1.0   ← wall solid
p_lin = 0.90:  z = 0.35  (drift)              alpha = 1.0   ← wall vẫn solid
p_lin = 0.95:  z = 0.31  (drift)              alpha = 1.0   ← vẫn solid
p_lin = 0.965: z = 0.30  (vút bắt đầu)        alpha = 1.0   ← FADE BẮT ĐẦU
p_lin = 0.985: z = 0.15  (vút mid + fade)     alpha = 0.43  ← fade nhanh
p_lin = 1.00:  z = 0     (hit_frame)          alpha = 0     ← biến mất ngay tại hit
```

→ Wall solid suốt drift, vút ào tới camera, fade ra trong 0.21s đập vào hit zone, biến mất gọn. Match LOW/HIGH visual semantic.

---

## Bug 2: `p_lin` dùng spawn_frame thay vì move_start_frame

### Vấn đề

Dòng 3437-3438:

```python
travel_f = max(1, self.hit_frame - self.spawn_frame)
p_lin = (cur_frame - self.spawn_frame) / travel_f
```

So sánh với `depth()` dòng 3129-3130:

```python
travel_f = max(1, self.hit_frame - move_start)        # ← move_start_frame
p_lin = (cur_frame - move_start) / travel_f
```

`move_start_frame = spawn_frame + wait_frames`. 2 công thức:

- **Same** nếu `wait_frames = 0` (mặc định, hầu hết case).
- **Khác** nếu `wait_frames > 0` (RELAX_WAIT_SEC > 0):
  - Block "đợi" tại horizon `wait_frames` frames trước khi bắt đầu di chuyển.
  - `_draw_middle` p_lin tăng đều từ spawn → hit, kể cả lúc đang đợi.
  - `depth()` p_lin chỉ tăng từ move_start → hit.
  - Fade timing trong `_draw_middle` không match motion phase trong `depth()`.

### Hệ quả

Với `wait_frames = 60` (2s wait) và `travel_f` configured = 180:
- spawn_frame → hit_frame = 60 + 180 = 240 frames
- Trong `_draw_middle`: travel_f = 240
- p_lin tại move_start_frame = 60/240 = 0.25 (nhưng motion thực sự chưa bắt đầu)
- p_lin tại hit_frame = 240/240 = 1.0 ✓
- Fade kích hoạt tại p_lin = 0.9 → cur_frame = spawn + 216 = move_start + 156 → motion p_lin = 156/180 ≈ 0.867 → drift phase

→ Fade kích hoạt trong drift phase ngay cả nếu đã fix Bug 1. Vì 2 base time khác nhau.

### Fix

Sync với `depth()` dùng `move_start_frame`:

```python
# CŨ:
travel_f = max(1, self.hit_frame - self.spawn_frame)
p_lin = (cur_frame - self.spawn_frame) / travel_f

# MỚI:
move_start = self.move_start_frame
travel_f = max(1, self.hit_frame - move_start)
p_lin = (cur_frame - move_start) / travel_f
```

Sau fix: `_draw_middle` p_lin = `depth()` p_lin trong mọi case (wait_frames bất kỳ).

---

## Patch tổng hợp

**File:** `src/rhythm.py`

**Method:** `RelaxTarget._draw_middle` (khoảng dòng 3437-3439)

**Trước:**

```python
# Fade-out across the last 1/10 of travel time.  Alpha goes
# 1.0 → 0.0 as p_lin moves through [9/10, 1].  We blend the block
# back toward `base_canvas`; non-block pixels are identical in
# both buffers so they pass through unchanged.
travel_f = max(1, self.hit_frame - self.spawn_frame)
p_lin = (cur_frame - self.spawn_frame) / travel_f
FADE_START = 0.9
```

**Sau:**

```python
# Fade-out across the vút phase (Phase 2) of the unified motion.
# Alpha goes 1.0 → 0.0 as p_lin crosses [T, 1] where T is the
# phase 1→2 hand-off (≈ 0.9655 with PHASE_SPLIT_D=0.70 and
# PHASE_SPEED_RATIO=12).  Using `move_start_frame` as the time
# base keeps fade timing synchronised with depth()'s motion
# phase even when RELAX_WAIT_SEC > 0.
move_start = self.move_start_frame
travel_f = max(1, self.hit_frame - move_start)
p_lin = (cur_frame - move_start) / travel_f
FADE_START = self._phase_split_t()
```

**Diff:** 3 dòng cũ → 5 dòng mới (3 logic + 2 dòng thêm cho readability với `move_start` local var).

---

## Touch points

### 1. `src/rhythm.py` — `_draw_middle` (dòng 3437-3439)

Apply patch ở trên. Đây là toàn bộ thay đổi cần làm.

### 2. KHÔNG cần touch

- `RelaxTarget.depth()`: đã unified ở spec trước, không đụng.
- Constants `PHASE_SPLIT_D`, `PHASE_SPEED_RATIO`: giữ nguyên.
- `_phase_split_t()` classmethod: giữ nguyên (đang dùng).
- `is_dead`, `dodge_frame`, `dodge_end_frame`: không phụ thuộc fade timing.
- `_relax_camera_dy`, stickman pose engine: skip MIDDLE, không liên quan.
- LOW / HIGH rendering: dùng `_draw_low` / `_draw_high` riêng, không có fade logic này.

---

## Test scenarios

### Test 1: MIDDLE wall solid suốt drift, fade chỉ trong vút

```
Setup: spawn MIDDLE block với travel = 180f (6s), wait_frames = 0.
Capture frame at:
  +0:    z = 1.0    alpha = 1.0   ← spawn, far horizon
  +90:   z ≈ 0.65   alpha = 1.0   ← drift mid
  +160:  z ≈ 0.40   alpha = 1.0   ← drift near end (BUG 1: trước fix alpha = 1.0 vì 160/180=0.89 < 0.9)
  +172:  z ≈ 0.32   alpha = 1.0   ← cuối drift (TRƯỚC fix: alpha ~= 0.78, đã mờ. SAU fix: alpha = 1.0)
  +174:  z ≈ 0.30   alpha = 1.0   ← T = 0.9655, vút bắt đầu, FADE_START
  +178:  z ≈ 0.10   alpha ≈ 0.4   ← vút + fade
  +180:  z = 0      alpha = 0     ← hit_frame, wall biến mất

Verify: wall solid (alpha=1) cho đến p_lin = 0.965, KHÔNG mờ giữa màn hình.
Verify: vút effect visible (~0.21s), wall snap to camera trước khi fade.
```

### Test 2: MIDDLE wall fade timing chính xác

```
Setup: travel = 180f, frame = hit_frame - 5 (= 175).
Verify: p_lin = (175 - move_start) / 180 ≈ 0.972
       FADE_START = self._phase_split_t() ≈ 0.9655
       alpha = 1.0 - (0.972 - 0.9655) / (1 - 0.9655) ≈ 1.0 - 0.187 ≈ 0.81

Setup: frame = hit_frame - 1 (= 179).
Verify: p_lin ≈ 0.994, alpha ≈ 0.17 (gần fully transparent).
```

### Test 3: Sync với RELAX_WAIT_SEC > 0

```
Setup: spawn_frame = 0, wait_frames = 60 (2s wait), travel = 180.
       move_start_frame = 60, hit_frame = 240.

Frame 60 (move_start): p_lin = 0/180 = 0   → motion bắt đầu, z = 1.0
Frame 174 (move_start + 0.9655 * 180 = 234): wait! Hmm 60 + 174 = 234, kế đó +6 = 240.

Let me recompute. p_lin = 0.9655 → cur_frame - move_start = 174 → cur_frame = 234.
At frame 234: p_lin = 0.9655, FADE_START = 0.9655 → alpha = 1.0 (vừa bắt đầu fade)
At frame 240: p_lin = 1.0, alpha = 0 (hit_frame, biến mất)

Verify: SAU fix, fade kích hoạt tại frame 234 (vào vút), biến mất tại frame 240.
        TRƯỚC fix (Bug 2): _draw_middle dùng spawn_frame, p_lin = (cur_frame - 0) / 240.
        FADE_START = 0.9 → cur_frame = 0.9 * 240 = 216.
        Frame 216 → p_lin trong depth() = (216 - 60) / 180 = 0.867 (drift phase, z=0.39).
        → Fade kích hoạt tại drift, không sync với vút. Bug.
```

### Test 4: `_phase_split_t()` returns đúng giá trị

```
Setup: PHASE_SPLIT_D = 0.70, PHASE_SPEED_RATIO = 12.0.
Compute: T = D / (D + (1-D)/ratio) = 0.7 / (0.7 + 0.3/12) = 0.7 / 0.725 ≈ 0.9655.
Verify: self._phase_split_t() == 0.9655 (gần đúng float).
```

### Test 5: Backward compat — wall vẫn fade out trước hit

```
Setup: bất kỳ travel, wait. Tại p_lin = 1.0:
Verify: canvas[:] = base_canvas (full reset, wall hoàn toàn gone).
Verify: KHÔNG có "frozen wall" sau hit_frame (state vẫn 'flying' nhưng draw trả base_canvas).
```

### Test 6: Tune-friendly với constants change

```
Setup: đổi PHASE_SPLIT_D = 0.80 (drift dài hơn, vút ngắn hơn).
Compute: T = 0.8 / (0.8 + 0.2/12) ≈ 0.980.
Verify: FADE_START tự cập nhật = 0.980 (vì dùng self._phase_split_t()).
Verify: Fade kích hoạt sau drift dài hơn, đồng bộ với vút mới.

→ Spec này future-proof: nếu user tune motion, fade tự follow.
```

### Test 7: Hole mask alignment với wall mới

```
Setup: MIDDLE với hole_mask_path.
Verify: hole position vẫn align với wall position trong toàn bộ travel.
Verify: alpha fade áp dụng cho toàn wall (kể cả vùng hole) — base_canvas blend đều.
```

### Test 8: Smoke — combo segment

```
Setup: combo có cả relax (kind=middle) và punch.
Verify: MIDDLE wall behavior đúng, không ảnh hưởng punch rendering.
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **`_phase_split_t()` classmethod** (dòng ~3110-3113): giữ nguyên. Spec dùng existing API.

2. **`PHASE_SPLIT_D`, `PHASE_SPEED_RATIO` class constants**: giữ nguyên. Spec lấy giá trị runtime từ `_phase_split_t()`, future-proof nếu constants đổi.

3. **`depth()` method**: KHÔNG đụng. Đã unified ở spec trước.

4. **`spawn_frame`, `move_start_frame`, `hit_frame`** properties: giữ nguyên.

5. **`base_canvas` blend logic** (dòng 3441-3446): giữ nguyên — chỉ fix base time của p_lin.

6. **Hole mask `_punch_hole`** (dòng 3449+): không liên quan fade, không đụng.

7. **`is_dead`, `check_hit`**: không đụng.

8. **LOW / HIGH rendering** (`_draw_low`, `_draw_high`): không có fade logic này. Không break.

9. **Pre-fix-bug-1 visual** (wall fade trong drift): nếu user đã quen visual cũ, fix sẽ làm wall hiện rõ rệt hơn ở vút. Đây là intent của unify, expected change.

10. **Comment update**: cập nhật comment "Fade-out across the last 1/10 of travel time" thành "Fade-out across the vút phase (Phase 2)" để match logic mới.

---

## Pattern code hiện có để tham khảo

- **`_phase_split_t()` classmethod** (dòng ~3110-3113): pattern compute T từ class constants.
- **`depth()` method** (dòng 3122-3154): pattern dùng `move_start = self.move_start_frame` làm time base. Spec mirror.
- **`is_dead()` method** (dòng 3196+): cũng dùng `_phase_split_t()`. Pattern tương tự.

---

## Thứ tự implement đề xuất

1. **Backup screenshot trước fix** ở các thời điểm: p_lin = 0.5, 0.85, 0.95, 1.0. Để so sánh sau.

2. **Apply patch 3 dòng** ở `_draw_middle`.

3. **Optional**: cập nhật comment header phía trên patch (dòng 3433-3436) để mô tả fade trong vút phase.

4. **Test 1**: chạy live preview MIDDLE block, verify wall solid suốt drift, fade ngay vút phase.

5. **Test 2**: pause ở frame cụ thể, verify alpha calc.

6. **Test 3 (nếu có config)**: set RELAX_WAIT_SEC > 0, verify fade sync với move_start.

7. **Test 7**: với hole_mask, verify alignment.

8. **Smoke test**: combo modes, không break gì khác.

9. **(Optional cleanup)**: nếu sau test thấy default `PHASE_SPEED_RATIO = 12.0` làm vút quá ngắn (~0.21s tại travel=180) → consider tune nhỏ hơn (vd 8.0) cho MIDDLE dễ đọc. Nhưng đó là tinh chỉnh sau, không thuộc spec này.

---

## Open questions

(1) **Comment update**: có cần cập nhật comment block (dòng 3433-3436) không? Hiện comment cũ nói "last 1/10 of travel time" — sau fix là "vút phase (~3.45% of travel)". Tôi đề xuất update để comment match code. Bạn confirm?

(2) **Vút phase ngắn (~0.21s)**: với MIDDLE wall, 0.21s fade có thể quá nhanh để player kịp nhận biết. Có 2 lựa chọn nếu user thấy quá nhanh:
   - (a) Mở rộng fade window ra trước vút: `FADE_START = self._phase_split_t() - 0.05` (fade bắt đầu 5% trước vút).
   - (b) Tune `PHASE_SPEED_RATIO` xuống (vd 8.0) để vút dài hơn — nhưng ảnh hưởng cả LOW/HIGH.
   
Đề xuất (a) làm fallback nếu cần. Spec này dùng `_phase_split_t()` thuần để start.

(3) **MIDDLE-specific FADE_START constant**: thay vì gọi `_phase_split_t()` mỗi frame (cheap nhưng có overhead), có thể cache thành class constant `FADE_START_MIDDLE`. Tôi đề xuất KHÔNG cache vì:
   - `_phase_split_t()` cheap (vài phép chia)
   - Cache sẽ stale nếu PHASE_SPLIT_D / PHASE_SPEED_RATIO đổi runtime
   - Code rõ hơn
Bạn confirm OK không cache?

(4) **Local var `move_start`**: có thể inline `self.move_start_frame` vào trong `travel_f` và `p_lin` để giảm 1 dòng. Tôi đề xuất giữ local var cho readability + tránh gọi property 2 lần. Bạn confirm.

(5) **Pre-existing bug 2 (spawn_frame vs move_start_frame)**: bạn có quan tâm fix luôn không, hay chỉ cần fix bug 1 (FADE_START)? Tôi đề xuất fix cả 2 (4 dòng change) vì cùng method và liên quan.
