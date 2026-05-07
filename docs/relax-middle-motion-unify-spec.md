# Relax MIDDLE Motion — Unify với LOW/HIGH Spec

## Mục tiêu

Sửa `RelaxTarget.depth()` trong `src/rhythm.py` để MIDDLE kind dùng **cùng phase split (70/30 + 12× ratio)** như LOW và HIGH, thay vì motion riêng "visual-linear" hiện tại (80/20 + ratio nội bộ ≈ 0.5×).

Hiện tại 3 kind có 2 profile chuyển động khác hẳn:
- **LOW + HIGH**: drift chậm 96.55% time → vút mạnh 3.45% time (snap rush cuối)
- **MIDDLE**: drift 66.67% time → approach 33.33% time (visual-linear, không vút)

Sau fix: cả 3 kind dùng cùng motion profile drift-then-vút.

Spec này độc lập với mọi spec khác trong `docs/`. Chỉ đụng 1 method.

---

## Code hiện tại

**File:** `src/rhythm.py`

**Method:** `RelaxTarget.depth()` (dòng ~3119-3184)

```python
def depth(self, cur_frame: int) -> float:
    move_start = self.move_start_frame
    if cur_frame <= move_start:
        return 1.0
    travel_f = max(1, self.hit_frame - move_start)
    p_lin = (cur_frame - move_start) / travel_f

    # ── MIDDLE blocks: visual-linear approach ────────────────────────
    # Middle is a wall to dodge through — it should grow on screen
    # at a roughly CONSTANT rate so the player can read its
    # approach.  Using inverse-Z (1/wz linear in time) makes the
    # block's screen-space size scale linearly...
    if self.kind == 'middle':
        T_M = 2.0 / 3.0
        D_M = 0.8
        z_split = 1.0 - D_M
        if p_lin <= T_M:
            z = 1.0 - D_M * (p_lin / T_M)
        elif p_lin <= 1.0:
            z = z_split * (1.0 - (p_lin - T_M) / (1.0 - T_M))
        else:
            v2 = z_split / max(1e-6, 1.0 - T_M)
            z = -v2 * (p_lin - 1.0)
        return max(-1.2, z)

    # ── LOW / HIGH: two-phase "70% chậm + 30% vút" ──────────────────
    D = self.PHASE_SPLIT_D
    T = self._phase_split_t()
    z_split = 1.0 - D
    if p_lin <= T:
        z = 1.0 - D * (p_lin / T)
    elif p_lin <= 1.0:
        z = z_split * (1.0 - (p_lin - T) / (1.0 - T))
    else:
        v2 = z_split / (1.0 - T)
        z = -v2 * (p_lin - 1.0)
    return max(-1.2, z)
```

## Code mới

Đơn giản: **xoá toàn bộ MIDDLE branch** (dòng 3129-3164) để fall-through xuống LOW/HIGH branch:

```python
def depth(self, cur_frame: int) -> float:
    move_start = self.move_start_frame
    if cur_frame <= move_start:
        return 1.0
    travel_f = max(1, self.hit_frame - move_start)
    p_lin = (cur_frame - move_start) / travel_f

    # All kinds (LOW / HIGH / MIDDLE) share the same two-phase motion:
    # 70% z-distance in Phase 1 (drift slow far field), 30% in Phase 2
    # (vút near-camera).  See PHASE_SPLIT_D / PHASE_SPEED_RATIO for tune.
    D = self.PHASE_SPLIT_D
    T = self._phase_split_t()
    z_split = 1.0 - D
    if p_lin <= T:
        z = 1.0 - D * (p_lin / T)
    elif p_lin <= 1.0:
        z = z_split * (1.0 - (p_lin - T) / (1.0 - T))
    else:
        v2 = z_split / (1.0 - T)
        z = -v2 * (p_lin - 1.0)
    return max(-1.2, z)
```

**Diff cụ thể:** xoá dòng 3129-3164 (block `# ── MIDDLE blocks: visual-linear approach ──` đến hết `return max(-1.2, z)` của MIDDLE).

Cập nhật comment header của LOW/HIGH branch (dòng 3166) thành chung "All kinds":

```python
# ── All kinds: two-phase "70% chậm + 30% vút" ─────────────────────
```

## Hệ quả

### Visual change cho MIDDLE

**Trước**:
```
travel = 180 frames (6s)
Phase 1: 120f (4s)   z: 1.0 → 0.2  (drift, vel = 1.20 z/time)
Phase 2: 60f  (2s)   z: 0.2 → 0    (approach, vel = 0.60 z/time, slower)
```

MIDDLE wall trôi đều đặn trong 4s, sau đó approach steady trong 2s.

**Sau**:
```
travel = 180 frames (6s)
Phase 1: ~174f (5.79s)  z: 1.0 → 0.30  (drift slow, vel = 0.725 z/time)
Phase 2: ~6f   (0.21s)  z: 0.30 → 0    (vút, vel = 8.7 z/time, ~12×)
```

MIDDLE wall drift chậm 5.79s ở khoảng xa, sau đó **VÚT ÀO** trong 0.21s tới hit zone.

### Gameplay impact

**Player perception cho MIDDLE**:
- TRƯỚC: thấy wall lớn dần đều, có thời gian rộng để ngắm hole → sidestep dễ.
- SAU: wall đứng yên xa lâu, đột ngột vút tới → sidestep timing căng hơn, giống dodge LOW/HIGH.

Đây là behavior change **đáng kể** cho gameplay relax mode. User cần xác nhận chấp nhận:
- (a) MIDDLE khó hơn (player phải ngắm + sidestep nhanh trong window vút)
- (b) Hoặc tăng `RELAX_TRAVEL_SEC` để bù (nhưng làm LOW/HIGH cũng chậm theo)

### DODGE_OFFSET_MIDDLE giữ nguyên

`DODGE_OFFSET_MIDDLE = +0.0` (fire dodge tại đúng hit_frame) giữ nguyên. Spec này chỉ động motion, không động dodge timing.

Có thể sau khi unify motion, user thấy DODGE_OFFSET_MIDDLE cần điều chỉnh để player có cảm giác đúng. Nhưng đó là tinh chỉnh sau, không trong spec này.

### Pass-by velocity

- **TRƯỚC** (MIDDLE): `v2 = 0.2 / (1 - 2/3) = 0.6` → block trôi qua chậm sau hit_frame, lingering.
- **SAU** (MIDDLE = LOW/HIGH): `v2 = 0.3 / (1 - 0.9655) ≈ 8.7` → block vọt qua nhanh, biến mất gần như ngay sau hit.

Visual: MIDDLE wall **biến mất nhanh hơn** sau khi qua camera. Phù hợp với "vút" semantic.

`is_dead` time: tính từ pass-by velocity. Sau unify, MIDDLE die sớm hơn (`exit_pad = travel_f * 1.2 / 8.7` thay vì `travel_f * 1.2 / 0.6`). Block dead sớm → tiết kiệm render frames.

### Hole alignment cho MIDDLE

MIDDLE có optional `hole_mask` để player đi xuyên. Sau unify motion, hole vẫn align cùng wall (cả 2 dùng `depth(z)` cùng formula). Không break.

Nhưng gameplay: trước đây player có 4s để align với hole (drift), sau chỉ có ~5.79s drift + 0.21s vút. Practical effect là vẫn 5.79s align time, nhưng vút cuối tạo cảm giác urgency. Hợp lý.

---

## Touch points

### 1. `src/rhythm.py` — `RelaxTarget.depth()` (dòng 3119-3184)

Xoá dòng 3129-3164 (MIDDLE branch). Cập nhật comment header LOW/HIGH branch thành "All kinds".

**Tổng**: -36 dòng (gồm comments).

### 2. Cập nhật docstring class (optional)

Comment ở `RelaxTarget` class (dòng 2992-3015) mô tả 2 phase motion chỉ áp cho LOW/HIGH. Sau fix, comment nên nói "all kinds use the same two-phase motion".

```python
# CŨ:
# Motion profile (two-phase piecewise) ────────────────────────────
# The block's spawn→hit travel is split into TWO distinct phases
# with different world-speeds, per user spec:
#   "70% quãng đường đầu tiên chạy chậm từ từ.
#    30% còn lại thì vút nhanh."
# ...

# MỚI: thêm note
# Applied to ALL three kinds (LOW, HIGH, MIDDLE).  MIDDLE was
# previously using a separate visual-linear profile but the
# gameplay reads better when its approach also has the late
# "vút" effect, matching LOW/HIGH dodge urgency.
```

### 3. KHÔNG cần touch

- `RelaxTarget.__init__`: kind validation giữ nguyên (`'low'`, `'high'`, `'middle'`).
- `RelaxTarget.draw` / `_draw_middle`: rendering riêng cho MIDDLE giữ nguyên — chỉ depth() đổi.
- `dodge_frame`: vẫn dùng `DODGE_OFFSET_MIDDLE = 0.0` cho MIDDLE.
- `is_dead`: dùng phase split chung từ class constants → tự đồng bộ.
- `RELAX_KIND_RATIO_MIDDLE`, `RELAX_ENABLED_KINDS`: spawn logic giữ nguyên.

---

## Test scenarios

### Test 1: MIDDLE drift slow → vút

```
Setup: spawn MIDDLE block với travel = 180 frames (6s).
Verify tại frame:
  +0:    z = 1.0  (far horizon)
  +50:   z ≈ 0.80 (drift slow)
  +100:  z ≈ 0.60
  +150:  z ≈ 0.40
  +173:  z ≈ 0.30 (cuối Phase 1)
  +175:  z ≈ 0.15 (đang vút)
  +180:  z = 0.0 (hit_frame)
  +185:  z ≈ -0.43 (pass-by vút tiếp)
```

So sánh với LOW/HIGH ở same travel: profile identical.

### Test 2: Visual perception

```
Chạy live preview segment relax với cả 3 kind.
Verify: MIDDLE block có "vút" effect cuối giống LOW/HIGH.
Trước fix: MIDDLE trôi đều, không vút.
Sau fix: MIDDLE drift xa lâu, vút ào tới camera.
```

### Test 3: Hole mask alignment cho MIDDLE

```
Setup: MIDDLE kind với hole_mask, player đi xuyên hole.
Verify: hole position vẫn match wall position trong toàn bộ travel.
Verify: hole texture không bị stretch / glitch khi vút.
```

### Test 4: Dodge timing cho MIDDLE

```
Setup: MIDDLE block, DODGE_OFFSET_MIDDLE = 0.0.
Verify: stickman sidestep pose fire tại đúng hit_frame.
Trong gameplay: player có ~0.21s vút trước hit, có thể cần phản xạ nhanh hơn.
```

### Test 5: Pass-by exit time

```
Trước fix: MIDDLE pass-by v2 = 0.6 → block die khi p_lin > 1 + (1.2 / 0.6) / travel = ... ~3 sec sau hit.
Sau fix: MIDDLE pass-by v2 = 8.7 → block die ~0.14 sec sau hit.

Verify: MIDDLE block không lingering trên screen sau hit_frame.
```

### Test 6: Combo mode với MIDDLE

```
Setup: combo segment có cả relax (kind=middle) và punch.
Verify: MIDDLE motion match LOW/HIGH timing.
Verify: combo cadence vẫn coherent (RELAX_TRAVEL_SEC default cho relax kết hợp với punch travel).
```

### Test 7: Pre-existing tests

```
Run unit tests:
- test_relax_motion (nếu có): có thể fail vì MIDDLE motion đổi.
  → Update expected values nếu test có hardcoded z values cho MIDDLE.
- test_relax_dodge_timing: KHÔNG đổi, vẫn pass.
- test_relax_kind_spawn: KHÔNG đổi.
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **`PHASE_SPLIT_D = 0.70`, `PHASE_SPEED_RATIO = 12.0`**: class constants giữ nguyên. Đổi sau (nếu user muốn tune chậm/nhanh khác) áp dụng đồng thời cho cả 3 kind.

2. **`_phase_split_t()` classmethod**: giữ nguyên, MIDDLE giờ cũng dùng.

3. **Kind validation** (`'low'`, `'high'`, `'middle'`) trong `__init__`: giữ nguyên.

4. **Rendering MIDDLE** (`_draw_middle`, `_y_band` với MIDDLE_HORIZON_OFFSET_FRAC, hole mask): KHÔNG đụng. Chỉ motion (depth()) đổi.

5. **CountdownHUD timing**: countdown đếm tới `move_start_frame` (= block bắt đầu di chuyển). Không phụ thuộc motion profile. Giữ nguyên.

6. **`RELAX_TRAVEL_SEC`, `RELAX_WAIT_SEC`**: config giữ nguyên. Travel duration không đổi, chỉ profile trong travel đổi.

7. **Camera bob `_relax_camera_dy`**: trigger tại `dodge_frame` của block active. MIDDLE dodge fire tại hit_frame như cũ. Bob window giữ nguyên.

8. **Stickman pose** RELAX_STAND ↔ SIDESTEP cho MIDDLE: pose timing giữ nguyên (controlled bởi dodge_frame và dodge_end_frame).

9. **Pass-by clamp `max(-1.2, z)`**: giữ nguyên cho cả 3 kind. MIDDLE giờ vọt tới z=-1.2 nhanh hơn, dead sớm hơn → OK.

10. **Live renderer / render_service**: không touch. Spec chỉ trong `RelaxTarget` class.

---

## Pattern code hiện có để tham khảo

- **`RelaxTarget.depth()`** dòng 3119: source of truth cho motion. Sau fix có 1 path duy nhất.
- **`_phase_split_t()`** classmethod: helper tính T (time at hand-off) từ D + ratio.
- **LOW/HIGH motion code** dòng 3166-3184: copy pattern, MIDDLE giờ dùng cùng.
- **`RelaxTarget._draw_middle`**: rendering MIDDLE, không liên quan motion.

---

## Thứ tự implement đề xuất

1. **Backup screenshot** mode relax với MIDDLE block ở các thời điểm khác nhau (z=1.0, z=0.5, z=0.2, z=0). Để so sánh visual sau fix.

2. **Xoá MIDDLE branch** trong `depth()` (dòng 3129-3164).

3. **Cập nhật comment header** "LOW / HIGH" → "All kinds" (dòng 3166).

4. **Chạy live preview** segment có MIDDLE.

5. **Test 1**: pause ở các frame, verify z values match formula chung.

6. **Test 2**: visual perception — MIDDLE giờ có "vút" effect.

7. **Test 3**: hole mask alignment vẫn đúng.

8. **Test 4-5**: dodge timing + pass-by exit OK.

9. **Optional**: cập nhật class docstring (touch point #2) cho consistency.

10. **Smoke test**: tất cả mode (solo low, solo high, solo middle, combo) hoạt động bình thường.

---

## Open questions

(1) **DODGE_OFFSET_MIDDLE = 0.0 có cần điều chỉnh?** Sau unify motion, MIDDLE có vút effect. Player cần phản xạ tại hit_frame đúng lúc vút. Có thể cần shift `DODGE_OFFSET_MIDDLE` để fire pose hơi sớm hơn (vd -0.02 × travel). Bạn muốn:
   - (a) Giữ 0.0 (fire đúng hit_frame, player perception cùng lúc với vút)
   - (b) Đổi -0.02 (anticipate, fire ngay khi vút bắt đầu)
   - (c) Đổi giống LOW (+0.01)
Tôi đề xuất (a) — giữ nguyên, test feel trước rồi điều chỉnh sau.

(2) **`RELAX_TRAVEL_SEC` có cần tăng cho gameplay easier?** Sau fix, MIDDLE chỉ có ~0.21s vút window — sidestep timing căng. Nếu tester thấy quá khó, có thể:
   - (a) Giữ travel hiện tại, accept khó hơn
   - (b) Tăng default `RELAX_TRAVEL_SEC` lên 1.5×
   - (c) Per-kind travel: thêm `RELAX_TRAVEL_SEC_MIDDLE` riêng
Tôi đề xuất (a), test gameplay trước rồi quyết.

(3) **`PHASE_SPLIT_D` / `PHASE_SPEED_RATIO` constants có nên tách per-kind?** Hiện tại unify dùng chung. Nếu tương lai muốn LOW vút mạnh hơn MIDDLE, có thể expose `PHASE_SPLIT_D_MIDDLE`, `PHASE_SPEED_RATIO_MIDDLE`. Bạn cần ngay không?
Đề xuất: KHÔNG cần — giữ unified cho đơn giản. Mở rộng khi có yêu cầu cụ thể.

(4) **Visual cảm giác "vút" có hợp với MIDDLE wall semantic?** MIDDLE wall thiết kế để player ngắm hole + sidestep. Vút ở khoảng cuối tạo cảm giác urgency, có thể làm sidestep timing thú vị hơn. Nhưng nếu user thấy "wall xuất hiện đột ngột" gây frustration, có thể revert. Bạn confirm intent.

(5) **Có nên xóa hardcoded `T_M`, `D_M` constants** trong code cũ ngay không? Nếu giữ để có thể fallback dễ, có thể comment-out thay vì xóa. Tôi đề xuất xóa hẳn — nếu cần fallback, dùng git history.
