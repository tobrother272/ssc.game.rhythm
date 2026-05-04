# Relax Phase Split — Sync Motion + Fade tại 70% Screen Distance Spec

## Mục tiêu

Đồng bộ **vị trí bắt đầu vút** (motion phase 1 → phase 2) **với vị trí bắt đầu fade** tại cùng một mốc: **70% screen distance** từ horizon (= cạnh dưới start gate) xuống hit zone (= cạnh dưới floor).

User logic mong muốn:
> **Di chuyển từ từ ở 70% đoạn đường ban đầu. 30% đoạn đường sau tăng tốc dần VÀ bắt đầu fade out, đến hết đoạn đường (sát màn hình) thì biến mất.**

→ Tại 70% screen, **2 events đồng thời** xảy ra:
1. Wall bắt đầu **vút (tăng tốc)**
2. Wall bắt đầu **fade out**

Cả 2 events đều CHƯA xảy ra trong 70% đầu, ĐỀU xảy ra trong 30% cuối.

---

## Vấn đề hiện tại

### Code hiện tại

```python
PHASE_SPLIT_D = 0.70       # = 70% Z-DISTANCE in Phase 1 (NOT 70% screen)
FADE_START_Z = 0.20        # = 70% screen distance (correct for fade)
```

`PHASE_SPLIT_D = 0.70` được hiểu là "70% z-distance" trong code:
- Phase 1: z 1.0 → z_split = 1.0 - 0.70 = 0.30
- Phase 2: z 0.30 → 0

Convert sang screen distance qua perspective `(1-z)^1.6`:
- z = 0.30 → screen progress = (1-0.30)^1.6 = 0.70^1.6 ≈ **0.580 (58% screen)**

Tức là **vút bắt đầu tại 58% screen**, KHÔNG phải 70% screen.

### Hệ quả (visual)

Wall behavior hiện tại:
- 0% → 58% screen: drift slow ✓
- 58% → 70% screen: vút (đã tăng tốc) nhưng CHƯA fade ✗
- 70% → 100% screen: vút + fade ✓

→ Có vùng 58-70% screen wall vút nhanh nhưng vẫn solid → KHÔNG match user logic.

---

## Phân tích toán

### Tìm PHASE_SPLIT_D để vút khớp 70% screen

Mục tiêu: vút bắt đầu tại z khi screen progress = 70%.

Screen progress formula:
```
screen_progress(z) = (1-z)^1.6
```

Solve cho 70% screen:
```
(1-z)^1.6 = 0.70
1-z = 0.70^(1/1.6) = 0.70^0.625 ≈ 0.802
z ≈ 0.198 ≈ 0.20
```

Để Phase 2 bắt đầu tại z = 0.20:
```
z_split = 1.0 - PHASE_SPLIT_D
0.20 = 1.0 - PHASE_SPLIT_D
PHASE_SPLIT_D = 0.80
```

### So sánh

| Setting | PHASE_SPLIT_D | z_split | Screen progress khi vút bắt đầu |
|---|---|---|---|
| **Hiện tại** | 0.70 | 0.30 | **58% screen** ✗ |
| **Sau fix** | 0.80 | 0.20 | **70% screen** ✓ |

### Time split với D = 0.80

```
T = D / (D + (1-D)/ratio)
  = 0.80 / (0.80 + 0.20/12)
  = 0.80 / 0.8167
  ≈ 0.9796
```

Phase 1 = ~97.96% time, Phase 2 = ~2.04% time.

So với D = 0.70: Phase 1 = 96.55%, Phase 2 = 3.45%.

→ Phase 2 ngắn hơn (2.04% vs 3.45% time) nhưng cover screen distance lớn hơn (30% vs 42% screen). Vút **đậm hơn**.

---

## Patch

**File:** `src/rhythm.py`

**Class:** `RelaxTarget`

**Thay đổi 1 dòng:**

```diff
-    PHASE_SPLIT_D     = 0.70   # fraction of z-distance in Phase 1
+    PHASE_SPLIT_D     = 0.80   # fraction of z-distance in Phase 1
+                                # = 70% SCREEN distance via (1-z)^1.6 perspective
     PHASE_SPEED_RATIO = 12.0   # Phase-2 world-speed / Phase-1 speed
```

Cập nhật comment block phía trên (dòng ~2992-3015) để làm rõ semantic mới:

```diff
     # Motion profile (two-phase piecewise) ────────────────────────────
     # The block's spawn→hit travel is split into TWO distinct phases
     # with different world-speeds, per user spec:
-    #   "70% quãng đường đầu tiên chạy chậm từ từ.
-    #    30% còn lại thì vút nhanh."
+    #   "70% SCREEN distance đầu tiên chạy chậm từ từ.
+    #    30% SCREEN distance cuối thì vút nhanh."
     #
-    #   • Phase 1  (drift):  covers the first PHASE_SPLIT_D fraction
-    #                        of z-distance (z from 1.0 to 1-D) at
-    #                        low velocity — the block glides lazily
-    #                        through the far field at the horizon.
-    #   • Phase 2  (vút):    covers the remaining (1-D) of z-distance
-    #                        at PHASE_SPEED_RATIO × the phase-1 speed,
-    #                        so the block snaps into the hit plane.
+    #   • Phase 1  (drift):  covers the first 70% of SCREEN distance
+    #                        from horizon to hit zone.  In z terms,
+    #                        z 1.0 → 0.20 (= PHASE_SPLIT_D=0.80 z fraction)
+    #                        because (1-0.20)^1.6 = 0.70 = 70% screen.
+    #   • Phase 2  (vút):    covers the last 30% of SCREEN distance
+    #                        (z 0.20 → 0) at PHASE_SPEED_RATIO × the
+    #                        phase-1 speed, snapping into hit plane.
+    #     Fade-out also kicks in here (FADE_START_Z = 0.20) so vút
+    #     and dissolve happen simultaneously.
     #
     # With D=0.70 and ratio=4 the time split works out to ~90.3% of
     # the travel window in Phase 1 and only ~9.7% in Phase 2 — the
     # block spends the vast majority of its travel drifting slowly,
     # then zooms through the last 30% of distance in ~0.6s (at
     # travel=180f / 30fps).
```

`FADE_START_Z = 0.20` trong `_draw_middle` đã đúng (= 70% screen). Giữ nguyên, không sửa.

---

## Hệ quả với LOW / HIGH

`PHASE_SPLIT_D` là class constant chia sẻ cho cả 3 kind. Sau fix, LOW và HIGH cũng có Phase 1 = 70% screen.

### Visual impact

**Trước fix (D=0.70, 58% screen split):**
- Drift slow trong 0-58% screen
- Vút trong 58-100% screen (= 42% screen)
- Vút khá rõ rệt, kéo dài

**Sau fix (D=0.80, 70% screen split):**
- Drift slow trong 0-70% screen
- Vút trong 70-100% screen (= 30% screen)
- Vút **đậm hơn nhưng ngắn hơn**

### Dodge timing impact

LOW dùng `DODGE_OFFSET_LOW = +0.01 × travel`, HIGH dùng `DODGE_OFFSET_HIGH = +0.064 × travel`.

Comment trong code (dòng 3034-3041) giải thích:
> HIGH (overhead bar, SQUAT): +0.064 × travel — Per the reference video, the squat only reads well once the bar has ALREADY started to pass overhead and the top ~1/3 of the bar has exited the top of the viewport. From the (1-z)^2.0 pass-by anchor we get exactly 1/3 occlusion at p_lin ≈ 1.063, i.e. 11-12 frames AFTER hit_frame at travel=180.

Calculation cũ dùng D=0.70. Sau đổi D=0.80, time split = 0.9796 (vs 0.9655). Pass-by velocity v2 = z_split / (1-T) = 0.20 / 0.0204 ≈ 9.80 (vs 8.70 cũ).

→ Block pass-by **nhanh hơn** sau fix. HIGH bar đến vị trí 1/3 occlusion sớm hơn → dodge_frame có thể cần điều chỉnh.

**Kiểm tra cần làm**: test LOW/HIGH dodge animation sau fix có còn natural không. Nếu lệch, tune `DODGE_OFFSET_LOW`, `DODGE_OFFSET_HIGH`.

### Pass-by exit time impact

`is_dead` dùng `exit_pad = travel_f * 1.2 / v2`.

- Trước: v2 = 8.7 → exit_pad ≈ 0.138 × travel_f = 25 frames ở travel=180.
- Sau: v2 = 9.8 → exit_pad ≈ 0.122 × travel_f = 22 frames ở travel=180.

→ Block dead sớm hơn ~3 frames (~0.1s). Không đáng kể.

---

## Visual sau fix (travel = 180f)

| Time (s) | p_lin | z | Screen % | Phase | Alpha | Sự kiện |
|---|---|---|---|---|---|---|
| 0.0 | 0.00 | 1.00 | 0% | Drift | 1.0 | Spawn |
| 1.5 | 0.25 | 0.83 | 6% | Drift | 1.0 | Drift slow |
| 3.0 | 0.50 | 0.66 | 18% | Drift | 1.0 | Drift slow |
| 4.5 | 0.75 | 0.49 | 36% | Drift | 1.0 | Drift slow |
| 5.5 | 0.917 | 0.32 | 56% | Drift | 1.0 | Drift cuối |
| **5.875** | **0.9796 (T)** | **0.20** | **70%** | **Vút bắt đầu** | **1.0 → fade start** | **Sync event** |
| 5.93 | 0.9886 | 0.10 | 84% | Vút | 0.50 | Vút + fade |
| 5.97 | 0.9943 | 0.05 | 92% | Vút | 0.25 | Vút + fade |
| 6.0 | 1.000 | 0 | 100% | Hit zone | 0 | **Biến mất** |

**Vút phase = ~0.125s** (vs 0.21s cũ). Rất nhanh. Wall snap-vút từ 70% → 100% screen + fade out đồng thời.

---

## Touch points

### 1. `src/rhythm.py` — `RelaxTarget.PHASE_SPLIT_D` constant

Đổi 1 dòng (xem patch). Dòng ~3014.

### 2. `src/rhythm.py` — Comment block (dòng ~2992-3015)

Cập nhật mô tả semantic mới. Optional nhưng recommend.

### 3. KHÔNG cần touch

- `_draw_middle` `FADE_START_Z = 0.20`: đã đúng, giữ nguyên.
- `_phase_split_t()` classmethod: tự động tính lại với D=0.80.
- `is_dead`: tự động pickup PHASE_SPLIT_D mới.
- `dodge_frame`: dùng DODGE_OFFSET constants riêng, không depend on PHASE_SPLIT_D trực tiếp. Nhưng pass-by velocity đổi → có thể cần tune offsets sau (xem Test 4).
- `depth()`: dùng PHASE_SPLIT_D class constant, tự động pickup giá trị mới.

---

## Test scenarios

### Test 1: MIDDLE motion + fade sync tại 70% screen

```
Setup: spawn MIDDLE, travel = 180f.
At z = 0.30 (58% screen): 
  - Phase 1 (drift slow) ✓
  - Alpha = 1.0 ✓
At z = 0.21 (just above target):
  - Phase 1 ending ✓
  - Alpha = 1.0 ✓
At z = 0.20 (70% screen):
  - Phase 2 vút bắt đầu ✓
  - Alpha = 1.0 → fade start ✓
  - Hai events đồng bộ.
At z = 0.15 (78% screen):
  - Vút active ✓
  - Alpha = 0.75 ✓
At z = 0:
  - Hit zone ✓
  - Alpha = 0 ✓
```

### Test 2: LOW kind motion change

```
Setup: spawn LOW (jump obstacle), travel = 180f.
Verify Phase 1 covers 0-70% screen với drift slow.
Verify Phase 2 covers 70-100% screen với vút (0.125s).
Verify dodge animation (camera bob + stickman jump) vẫn fire ở dodge_frame.
Verify visual jump timing acceptable. Nếu lệch, tune DODGE_OFFSET_LOW.
```

### Test 3: HIGH kind motion change

```
Setup: spawn HIGH (duck obstacle), travel = 180f.
Verify Phase 1, Phase 2 split correct.
Verify squat animation timing. Comment cũ nói squat fire khi bar đã pass 1/3 overhead.
Sau đổi D, pass-by velocity v2 đổi từ 8.7 → 9.8.
Verify squat fires ~11-12 frames after hit_frame (DODGE_OFFSET_HIGH = +0.064).
Calculate: với p_lin=1.063 ⇒ z = -v2 * 0.063 = -0.617. Floor_y(-0.617) extrapolate xuống dưới screen → bar đã thoát top viewport.
Verify visually: bar passes overhead by 1/3 OK.
```

### Test 4: Dodge offset re-tuning (nếu cần)

```
Nếu Test 2/3 cho thấy dodge animation lệch:
- Tune DODGE_OFFSET_LOW (hiện +0.01 × travel)
- Tune DODGE_OFFSET_HIGH (hiện +0.064 × travel)
Goal: stickman pose fire đúng moment user expect.
```

### Test 5: Replay user screenshot

```
Setup: replay frame user gửi (wall purple ở mid-screen ≈ 50% screen).
Verify: ở vị trí đó, motion = drift slow (Phase 1), alpha = 1.0 (chưa fade).
Verify: wall hiển thị solid trong toàn drift, chỉ vút + fade khi đã sát hit zone (70%+ screen).
```

### Test 6: Pass-by exit time

```
Verify: is_dead trả True ở ~22 frames sau hit_frame (travel=180), giảm 3 frames so với cũ.
Verify: KHÔNG có target lingering.
```

### Test 7: Tune-friendly với constants change

```
Đổi PHASE_SPEED_RATIO = 8.0 (vút chậm hơn).
T = 0.80 / (0.80 + 0.20/8) = 0.80 / 0.825 = 0.9697.
Phase 2 = 3.03% time.
Verify: motion vẫn correct, phase boundary vẫn ở z = 0.20 = 70% screen.
```

### Test 8: Combo segment

```
Setup: combo punch + relax(low/high/middle).
Verify: tất cả relax kinds dùng D=0.80, motion consistent.
Verify: punch không bị ảnh hưởng (PunchTarget không dùng PHASE_SPLIT_D).
```

### Test 9: Existing tests

```
Run pytest. Test có hardcoded z values cho relax có thể fail.
Ví dụ: nếu test verify "z at p_lin=0.5 == 0.65", giá trị mới với D=0.80:
  z = 1.0 - 0.80 * (0.5 / 0.9796) = 1.0 - 0.408 = 0.592.
→ Update expected values.
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **`PHASE_SPEED_RATIO = 12.0`**: giữ nguyên. Chỉ đổi `PHASE_SPLIT_D`.

2. **`FADE_START_Z = 0.20`** trong `_draw_middle`: giữ nguyên (đã đúng = 70% screen).

3. **`_phase_split_t()` classmethod**: KHÔNG đổi formula. Tự động tính T với D mới.

4. **`depth()` method**: KHÔNG đổi logic. Dùng `self.PHASE_SPLIT_D` class constant.

5. **MIDDLE rendering** (`_draw_middle`): KHÔNG đổi. Fade vẫn z-based với z=0.20.

6. **LOW / HIGH rendering** (`_draw_low`, `_draw_high`): KHÔNG đổi.

7. **Camera bob, stickman pose**: pattern giữ nguyên. Có thể cần tune DODGE_OFFSET nếu visual không khớp sau fix (xem Test 4).

8. **`is_dead` formula**: tự động pickup D mới qua `_phase_split_t()`.

9. **`(1-z)^1.6` perspective formula** trong `floor_y()`: KHÔNG đổi. Mapping screen ↔ z fix với D=0.80.

10. **Combo modes**: `PHASE_SPLIT_D` chia sẻ cho mọi RelaxTarget. Không break combo.

11. **PunchTarget, DanceTarget, StepTarget, LineTarget**: KHÔNG dùng `PHASE_SPLIT_D`. Không bị ảnh hưởng.

---

## Pattern code hiện có để tham khảo

- **`PHASE_SPLIT_D`, `PHASE_SPEED_RATIO`** ở dòng 3014-3015: class constants. Spec chỉ đổi 1 giá trị.
- **`_phase_split_t()`** dòng 3110-3113: helper compute T từ D + ratio. Tự động làm lại.
- **`(1-z)^1.6` perspective**: trong `cam.floor_y(z)`. Reference cho mapping screen ↔ z.

---

## Thứ tự implement đề xuất

1. **Backup screenshots** trước fix:
   - z = 0.50 (mid-drift)
   - z = 0.30 (current vút start, sẽ thành cuối drift sau fix)
   - z = 0.20 (new vút start, also fade start)
   - z = 0.10 (mid vút + fade)
   - z = 0 (hit)

2. **Apply patch**: đổi `PHASE_SPLIT_D = 0.80`. Cập nhật comment block (optional).

3. **Test 1**: MIDDLE motion + fade sync tại 70% screen.

4. **Test 2, 3**: LOW + HIGH motion change. Verify dodge animation OK.

5. **Test 4**: nếu LOW/HIGH dodge timing lệch, tune DODGE_OFFSET.

6. **Test 5**: replay user screenshot scenario.

7. **Test 6**: pass-by exit time.

8. **Test 7**: tune-friendly verification.

9. **Test 8**: combo segment.

10. **Test 9**: existing tests, update expected values nếu fail.

11. **Smoke test**: tất cả modes (solo low, solo high, solo middle, combo).

12. **Optional**: nếu LOW/HIGH visual không thoả, có thể split MIDDLE-specific D (xem Open question 2).

---

## Open questions

(1) **Đổi `PHASE_SPLIT_D` ảnh hưởng cả LOW/HIGH**: bạn OK không? Hay muốn:
   - (a) Đổi global = 0.80, accept LOW/HIGH motion thay đổi (đề xuất, đơn giản)
   - (b) Re-introduce kind-specific: `PHASE_SPLIT_D_MIDDLE = 0.80`, giữ `PHASE_SPLIT_D = 0.70` cho LOW/HIGH (cần modify `depth()` thêm branch)

(2) **`DODGE_OFFSET_LOW` (+0.01) và `DODGE_OFFSET_HIGH` (+0.064)** có cần re-tune sau khi đổi D?
   - `+0.01 × travel` ở D=0.80: dodge fires 2 frames sau hit. Tại p_lin=1.01, z = -v2*0.01 = -0.098. Block đã pass-by xa hơn (vì v2=9.8 thay 8.7). Visual có thể OK hoặc cần tune.
   - `+0.064 × travel` ở D=0.80: dodge fires 12 frames sau hit. p_lin=1.064, z = -0.627. Bar passed 1/3 occlusion check.
   - Đề xuất: test trước, tune sau nếu cần.

(3) **Phase 2 = 0.125s** sau D=0.80 (vs 0.21s cũ): có nhanh quá không? Nếu cảm giác wall vút "snap" quá đột ngột:
   - Giảm `PHASE_SPEED_RATIO` từ 12 xuống 8 → Phase 2 = 3.03% time, vẫn 30% screen nhưng chậm hơn.
   - Hoặc accept 0.125s nếu visual OK.

(4) **Có nên đặt comment định nghĩa rõ "PHASE_SPLIT_D = z fraction"**: hiện comment nói "fraction of z-distance in Phase 1". Sau fix có thể confusing vì user nghĩ "70% đoạn đường" = screen distance. Khuyến nghị thêm comment giải thích semantic rõ.

(5) **Cleanup `relax-middle-implementation-guide.md`**: spec implementation guide trước viết FADE_START_Z = 0.20 (đúng) nhưng motion lúc đó D=0.70 (sai). Sau apply spec này, guide đó vẫn correct về fade nhưng cần update note "PHASE_SPLIT_D giờ là 0.80". Update guide hoặc tham chiếu spec này.

---

## Acceptance criteria

✓ MIDDLE motion + fade ĐỒNG BỘ start tại z = 0.20 (= 70% screen)
✓ Wall solid suốt 0-70% screen
✓ Wall vút + fade out simultaneously trong 70-100% screen
✓ Wall biến mất tại hit zone (z=0)
✓ Replay user screenshot: wall ở 50% screen → solid (drift)
✓ LOW/HIGH motion change OK (hoặc tune DODGE_OFFSET nếu cần)
✓ Combo modes work
✓ Existing tests pass (hoặc update expected)
