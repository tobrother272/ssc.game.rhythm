# Relax MIDDLE — Final Implementation Guide

## Mục tiêu

Hợp nhất TẤT CẢ fixes cho `RelaxTarget` kind=`middle` thành 1 spec duy nhất. Cursor implement guide này đủ.

**Logic mong muốn của user:**

> Wall di chuyển từ từ trong **70% đoạn đường ban đầu** (= 70% screen distance từ horizon / cạnh dưới start gate xuống hit zone / cạnh dưới floor). **30% đoạn đường cuối**: wall **tăng tốc dần (vút)** VÀ **bắt đầu fade out** đồng thời. Đến hết đoạn đường (sát màn hình) thì wall biến mất.

→ 2 events đồng thời tại mốc 70% screen:
1. Vút (motion phase 2 bắt đầu)
2. Fade out

---

## Tóm tắt 3 fixes

| Fix | Vị trí | Mô tả |
|---|---|---|
| **1. Motion unify** | `RelaxTarget.depth()` | MIDDLE dùng cùng formula với LOW/HIGH (xoá branch riêng) |
| **2. Phase split = 0.80** | `RelaxTarget.PHASE_SPLIT_D` | Đổi 0.70 → 0.80 để vút bắt đầu tại 70% screen (= z=0.20), không phải 58% |
| **3. Z-based fade** | `RelaxTarget._draw_middle` | Fade kích hoạt tại z=0.20 (sync với vút), linear xuống alpha=0 tại hit |

Spec này thay thế tất cả các spec iteration cũ:
- ~~`relax-middle-motion-unify-spec.md`~~
- ~~`relax-middle-fade-fix-spec.md`~~
- ~~`relax-middle-fade-after-hit-spec.md`~~
- ~~`relax-middle-fade-70-percent-spec.md`~~
- ~~`relax-middle-fade-screen-progress-spec.md`~~
- ~~`relax-middle-implementation-guide.md`~~
- ~~`relax-phase-split-screen-aligned-spec.md`~~

---

## Toán học nền tảng

### Mối quan hệ z ↔ screen position

Renderer dùng perspective `(1-z)^1.6` để map z ∈ [0, 1] vào screen y:

```
screen_y(z) = cy_v + (1-z)^1.6 * (y_hit - cy_v)
screen_progress(z) = (screen_y - cy_v) / (y_hit - cy_v) = (1-z)^1.6
```

| z | Screen progress | Visual position |
|---|---|---|
| 1.00 | 0% | Horizon (top) |
| 0.50 | 33% | Mid-far |
| 0.30 | 58% | Mid |
| **0.20** | **70%** | **Target — vút + fade start** |
| 0.10 | 84% | Near hit zone |
| 0.00 | 100% | Hit zone (bottom) |

### Tính z khi screen = 70%

```
0.70 = (1-z)^1.6
1-z = 0.70^(1/1.6) ≈ 0.802
z ≈ 0.198 ≈ 0.20
```

### Tính PHASE_SPLIT_D để vút bắt đầu tại z=0.20

```
z_split = 1.0 - PHASE_SPLIT_D
0.20 = 1.0 - PHASE_SPLIT_D
PHASE_SPLIT_D = 0.80
```

### Time split với D=0.80, ratio=12

```
T = D / (D + (1-D)/ratio)
  = 0.80 / (0.80 + 0.20/12)
  = 0.80 / 0.8167
  ≈ 0.9796
```

Phase 1 = 97.96% time (drift), Phase 2 = 2.04% time (vút).

---

## File và method bị ảnh hưởng

**1 file:** `src/rhythm.py`

**1 class:** `RelaxTarget`

**3 thay đổi:**
- Class constant `PHASE_SPLIT_D` (dòng ~3014)
- Method `depth()` (dòng ~3122-3184)
- Method `_draw_middle()` (dòng ~3437-3446)

---

## Patch 1 — Đổi `PHASE_SPLIT_D = 0.80`

**Vị trí:** Class constant ở khoảng dòng 3014.

```diff
-    PHASE_SPLIT_D     = 0.70   # fraction of z-distance in Phase 1
+    PHASE_SPLIT_D     = 0.80   # = 70% SCREEN distance via (1-z)^1.6 perspective
+                                #   Phase 1 covers z 1.0 → 0.20 (=70% screen)
+                                #   Phase 2 covers z 0.20 → 0  (=last 30% screen)
     PHASE_SPEED_RATIO = 12.0   # Phase-2 world-speed / Phase-1 speed
```

Optional: cập nhật comment block phía trên (dòng ~2992-3015):

```diff
     # Motion profile (two-phase piecewise) ────────────────────────────
     # The block's spawn→hit travel is split into TWO distinct phases
-    # with different world-speeds, per user spec:
-    #   "70% quãng đường đầu tiên chạy chậm từ từ.
-    #    30% còn lại thì vút nhanh."
+    # with different world-speeds, per user spec:
+    #   "70% SCREEN distance đầu chạy chậm.  30% SCREEN distance
+    #    cuối thì vút nhanh và đồng thời fade out tới hit zone."
+    #
+    # IMPORTANT: PHASE_SPLIT_D is in Z fraction (3D world).  Due to
+    # perspective (1-z)^1.6, z fraction != screen fraction.  D=0.80
+    # means 80% Z covered in Phase 1 = 70% screen distance via the
+    # perspective envelope.  Fade at FADE_START_Z=0.20 syncs with
+    # this — both vút and fade kick in at z=0.20 = 70% screen.
```

---

## Patch 2 — Motion unify trong `depth()`

**Vị trí:** Method `depth()` khoảng dòng 3122-3184.

**Xoá hoàn toàn** block MIDDLE-specific (~36 dòng):

```diff
 def depth(self, cur_frame: int) -> float:
     move_start = self.move_start_frame
     if cur_frame <= move_start:
         return 1.0
     travel_f = max(1, self.hit_frame - move_start)
     p_lin = (cur_frame - move_start) / travel_f

-    # ── MIDDLE blocks: visual-linear approach ────────────────────────
-    # Middle is a wall to dodge through — it should grow on screen
-    # at a roughly CONSTANT rate so the player can read its
-    # approach.  Using inverse-Z (1/wz linear in time) makes the...
-    if self.kind == 'middle':
-        T_M = 2.0 / 3.0
-        D_M = 0.8
-        z_split = 1.0 - D_M
-        if p_lin <= T_M:
-            z = 1.0 - D_M * (p_lin / T_M)
-        elif p_lin <= 1.0:
-            z = z_split * (1.0 - (p_lin - T_M) / (1.0 - T_M))
-        else:
-            v2 = z_split / max(1e-6, 1.0 - T_M)
-            z = -v2 * (p_lin - 1.0)
-        return max(-1.2, z)
-
-    # ── LOW / HIGH: two-phase "70% chậm + 30% vút" ──────────────────
+    # ── All kinds (LOW / HIGH / MIDDLE): two-phase motion ───────────
+    # Phase 1 (drift slow): z 1.0 → 0.20 (= 70% screen via (1-z)^1.6).
+    # Phase 2 (vút fast):    z 0.20 → 0   (= last 30% screen).
+    # Fade-out (in _draw_middle) also kicks in at z=0.20 so vút and
+    # dissolve happen simultaneously.
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

---

## Patch 3 — Z-based fade trong `_draw_middle`

**Vị trí:** Method `_draw_middle` khoảng dòng 3437-3446.

**Replace block fade cũ:**

```diff
     # Middle block now renders as a solid obstacle by default.
     # Only apply cutout when user explicitly provides a mask path.
     if self.hole_mask_path:
         self._punch_hole(canvas, wall_poly, base_canvas)

-    # Fade-out across the last 1/10 of travel time.  Alpha goes
-    # 1.0 → 0.0 as p_lin moves through [9/10, 1].  We blend the block
-    # back toward `base_canvas`; non-block pixels are identical in
-    # both buffers so they pass through unchanged.
-    travel_f = max(1, self.hit_frame - self.spawn_frame)
-    p_lin = (cur_frame - self.spawn_frame) / travel_f
-    FADE_START = 0.9
-    if p_lin > FADE_START:
-        if p_lin >= 1.0:
-            canvas[:] = base_canvas
-        else:
-            alpha = 1.0 - (p_lin - FADE_START) / (1.0 - FADE_START)
-            alpha = max(0.0, min(1.0, alpha))
-            cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
+    # Fade-out kicks in when wall has visually traveled 70% of the
+    # screen distance from horizon (≈ start-gate bottom) to hit zone
+    # (= floor bottom).  Synced with motion Phase 2 (vút) which also
+    # starts at z=0.20 (= 70% screen).  Wall + fade simultaneously
+    # accelerate and dissolve in last 30% screen distance.
+    FADE_START_Z = 0.20    # = 1 - 0.70^(1/1.6), wall at 70% screen progress
+    if z >= 0.0 and z < FADE_START_Z:
+        # Linear fade: alpha 1.0 at z=FADE_START_Z, alpha 0 at z=0
+        alpha = z / FADE_START_Z
+        alpha = max(0.0, min(1.0, alpha))
+        cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
+    elif z < 0.0:
+        # Pass-by (after hit_frame): wall behind camera, hard cut.
+        canvas[:] = base_canvas
     return canvas
```

**Lưu ý:**
- `z` đã có sẵn ở dòng 3368: `z = self.depth(cur_frame)`. Reuse.
- KHÔNG cần `p_lin`, `travel_f` trong fade nữa.

---

## Visual sau khi apply 3 patches (travel = 180f)

| Time (s) | p_lin | z | Screen % | Phase | Alpha | Sự kiện |
|---|---|---|---|---|---|---|
| 0.0 | 0.00 | 1.00 | 0% | Drift | 1.0 | Spawn (horizon) |
| 1.5 | 0.25 | 0.83 | 6% | Drift | 1.0 | Drift slow |
| 3.0 | 0.50 | 0.66 | 18% | Drift | 1.0 | Drift slow |
| 4.5 | 0.75 | 0.49 | 36% | Drift | 1.0 | Drift slow |
| 5.5 | 0.917 | 0.32 | 56% | Drift | 1.0 | Drift cuối |
| **5.875** | **0.9796 (T)** | **0.20** | **70%** | **Vút + Fade START** | **1.0 → fade** | **Sync event** |
| 5.93 | 0.989 | 0.10 | 84% | Vút + fade | 0.50 | Tăng tốc + mờ |
| 5.97 | 0.994 | 0.05 | 92% | Vút + fade | 0.25 | Sát hit zone |
| 6.0 | 1.000 | 0 | 100% | Hit | 0 | **Biến mất** |
| 6.0+ | > 1 | < 0 | (pass-by) | Pass-by | 0 (hard cut) | Cleanup |

→ Wall:
- **Solid** suốt 0% → 70% screen (drift slow)
- **Vút + fade đồng thời** trong 70% → 100% screen (~0.125s)
- **Biến mất** tại hit zone

---

## Hệ quả với LOW / HIGH

`PHASE_SPLIT_D` là class constant chia sẻ. Sau fix:
- LOW (jump) và HIGH (duck) cũng có Phase 1 = 70% screen
- Phase 2 ngắn hơn (~0.125s thay vì ~0.21s)
- Vút **đậm hơn nhưng nhanh hơn**

### Dodge timing

`DODGE_OFFSET_LOW = +0.01 × travel`, `DODGE_OFFSET_HIGH = +0.064 × travel` không đổi.

Pass-by velocity v2 đổi từ 8.7 → 9.8 (do D=0.80 thay 0.70):

| Setting | v2 |
|---|---|
| Cũ (D=0.70) | 8.70 |
| Mới (D=0.80) | 9.80 |

→ Block pass-by **nhanh hơn** ~13%. HIGH bar đến vị trí 1/3 occlusion sớm hơn ~13% time.

**Có thể cần re-tune** `DODGE_OFFSET_LOW`, `DODGE_OFFSET_HIGH` nếu visual jump/squat timing không khớp. Test trước, tune sau.

### Pass-by exit

`is_dead` exit_pad = travel_f * 1.2 / v2 ≈ 22 frames (vs 25 cũ). Block dead sớm hơn ~3 frames (~0.1s). Negligible.

---

## Code mẫu cuối cùng

### Class constants

```python
class RelaxTarget(Target):
    # ... (other constants) ...
    
    PHASE_SPLIT_D     = 0.80   # = 70% SCREEN distance via (1-z)^1.6
                                #   Phase 1: z 1.0 → 0.20 (=70% screen)
                                #   Phase 2: z 0.20 → 0  (=last 30% screen)
    PHASE_SPEED_RATIO = 12.0
```

### `depth()` method (sau Patch 2)

```python
def depth(self, cur_frame: int) -> float:
    move_start = self.move_start_frame
    if cur_frame <= move_start:
        return 1.0
    travel_f = max(1, self.hit_frame - move_start)
    p_lin = (cur_frame - move_start) / travel_f

    # All kinds (LOW / HIGH / MIDDLE): two-phase motion.
    # Phase 1 (drift slow): z 1.0 → 0.20 (= 70% screen).
    # Phase 2 (vút fast):    z 0.20 → 0   (= last 30% screen).
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

### `_draw_middle()` đoạn cuối (sau Patch 3)

```python
def _draw_middle(self, canvas, cam, cur_frame):
    z = self.depth(cur_frame)
    if z < -1.0:
        return canvas
    base_canvas = canvas.copy()
    # ... existing code: build wall_poly, render texture/stripes ...
    
    if self.hole_mask_path:
        self._punch_hole(canvas, wall_poly, base_canvas)

    # Fade-out kicks in at 70% screen progress (= vút start).
    # Synced with motion Phase 2 — both vút and fade in last 30%.
    FADE_START_Z = 0.20
    if z >= 0.0 and z < FADE_START_Z:
        alpha = z / FADE_START_Z
        alpha = max(0.0, min(1.0, alpha))
        cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
    elif z < 0.0:
        canvas[:] = base_canvas
    return canvas
```

---

## Test scenarios

### Test 1: Motion + fade SYNC tại 70% screen

```
Setup: spawn MIDDLE, travel = 180f.
At z = 0.21 (just above 70% screen):
  - Phase 1 (drift) ✓
  - Alpha = 1.0 ✓
At z = 0.20 (= 70% screen):
  - Phase 2 (vút) bắt đầu ✓
  - Alpha = 1.0 → fade start ✓
  - 2 events đồng bộ.
At z = 0.10 (84% screen):
  - Vút active + alpha = 0.50 ✓
At z = 0:
  - Hit zone, alpha = 0 ✓
```

### Test 2: Wall solid trong 70% đầu screen

```
At z = 0.50 (33% screen): alpha = 1.0, wall solid
At z = 0.40 (45% screen): alpha = 1.0, wall solid
At z = 0.30 (58% screen): alpha = 1.0, wall solid (KHÁC behavior cũ — alpha trước khi fix là chưa fade nhưng wall đã vút)
At z = 0.25 (64% screen): alpha = 1.0, wall solid
At z = 0.21 (just under 70%): alpha = 1.0, wall solid
```

### Test 3: Wall vút + fade trong 30% cuối screen

```
At z = 0.20 (70%): alpha = 1.0, vút bắt đầu
At z = 0.15 (78%): alpha = 0.75, vút active
At z = 0.10 (84%): alpha = 0.50
At z = 0.05 (92%): alpha = 0.25
At z = 0 (100%): alpha = 0, biến mất
```

### Test 4: Pass-by hard cut

```
Sau hit_frame (z âm): canvas[:] = base_canvas, wall biến mất ngay.
Verify: KHÔNG có frozen wall lingering.
```

### Test 5: Replay user screenshot scenario

```
Setup: replay frame user gửi (wall purple ở mid-screen, z ≈ 0.35).
Verify: ở vị trí đó, alpha = 1.0 (solid), KHÔNG fade. ✓ Match expectation.
```

### Test 6: LOW kind motion change

```
Setup: spawn LOW, travel = 180f.
Verify Phase 1 covers 0-70% screen (drift).
Verify Phase 2 covers 70-100% screen (vút, ~0.125s).
Verify camera bob + stickman jump fire ở dodge_frame.
Visual jump timing acceptable. Nếu lệch, tune DODGE_OFFSET_LOW.
```

### Test 7: HIGH kind motion change

```
Setup: spawn HIGH, travel = 180f.
Verify Phase 1, Phase 2 split correct.
Verify squat fires ~12 frames sau hit (DODGE_OFFSET_HIGH = +0.064).
With v2 = 9.8 (vs 8.7 cũ), bar pass-by nhanh hơn.
Visually: squat timing OK?  Nếu lệch, tune DODGE_OFFSET_HIGH.
```

### Test 8: Dodge offset re-tuning (nếu cần)

```
Nếu Test 6/7 thấy dodge animation lệch:
- LOW: tune DODGE_OFFSET_LOW (hiện +0.01)
- HIGH: tune DODGE_OFFSET_HIGH (hiện +0.064)
Goal: stickman pose fire đúng moment user expect.
```

### Test 9: RELAX_WAIT_SEC > 0 sync

```
Setup: spawn = 0, wait = 60, travel = 180.
move_start = 60, hit_frame = 240.
Verify: depth() và fade dùng cùng base time (move_start).
Verify: fade kicks in tại z=0.20 = move_start + 0.9796*180 ≈ frame 236.
```

### Test 10: Hole mask alignment

```
Setup: MIDDLE với hole_mask.
Verify: hole position match wall trong toàn approach.
Verify: fade alpha áp đều cho cả wall + hole region.
```

### Test 11: Combo segment

```
Setup: combo punch + relax(low/high/middle).
Verify: tất cả 3 relax kinds dùng D=0.80, motion consistent.
Verify: punch không bị ảnh hưởng (PunchTarget không dùng PHASE_SPLIT_D).
```

### Test 12: Robust khi tune motion

```
Đổi PHASE_SPEED_RATIO = 8.0 (vút chậm hơn).
T = 0.80 / (0.80 + 0.20/8) ≈ 0.9697.
Phase 2 = 3.03% time.
Verify: motion vẫn correct, phase boundary vẫn ở z=0.20=70% screen.
Verify: fade vẫn kích hoạt tại z=0.20 (z-based, robust).
```

### Test 13: Existing tests

```
Run pytest. Test với hardcoded z values cho relax có thể fail.
Update expected values theo formula mới (D=0.80).
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **`PHASE_SPEED_RATIO = 12.0`**: giữ nguyên. Chỉ đổi `PHASE_SPLIT_D`.

2. **`_phase_split_t()` classmethod**: KHÔNG đổi formula. Tự động tính T với D mới.

3. **`is_dead`, `dodge_frame`, `dodge_end_frame`** properties: tự động pickup `PHASE_SPLIT_D` mới qua class constant.

4. **MIDDLE rendering** (`_draw_middle`): KHÔNG đụng phần build wall_poly + texture + hole. Chỉ replace block fade ở cuối.

5. **LOW / HIGH rendering** (`_draw_low`, `_draw_high`): KHÔNG đụng. Motion vẫn dùng formula chung qua `depth()`.

6. **Camera bob `_relax_camera_dy`** (dòng 181-219): skip MIDDLE intentionally. KHÔNG đụng.

7. **Stickman pose engine** (dòng 6572-6573): skip MIDDLE intentionally. KHÔNG đụng.

8. **Spawn logic** (`_spawn_target` mode='relax'): không đụng.

9. **PunchTarget, DanceTarget, StepTarget, LineTarget**: KHÔNG dùng `PHASE_SPLIT_D`. Không bị ảnh hưởng.

10. **`(1-z)^1.6` perspective formula** trong `cam.floor_y(z)`: KHÔNG đổi. Mapping screen ↔ z fix với D=0.80.

11. **`spawn_frame`, `move_start_frame`, `hit_frame`** properties: KHÔNG đụng.

12. **Wall freeze tại z=0**: do clamp `wz = cam.z_from_norm(max(0, ...))`, wall freeze tại y_hit khi z=0. Không vấn đề vì alpha=0 ngay khi z=0 → wall đã invisible. z<0 → hard cut.

13. **Hole mask alignment**: depend on `depth(z)`, tự sync.

14. **Combo modes**: shared PHASE_SPLIT_D không break combo.

---

## Pattern code hiện có để tham khảo

- **`PHASE_SPLIT_D`, `PHASE_SPEED_RATIO`** ở dòng ~3014-3015: class constants. Spec chỉ đổi 1 giá trị.
- **`_phase_split_t()` classmethod** dòng ~3110-3113: helper compute T. Tự động.
- **`z = self.depth(cur_frame)`** ở line 3368: source of z, reuse cho fade.
- **`cv2.addWeighted` blend** pattern existing.
- **`(1-z)^1.6` perspective** trong `cam.floor_y(z)`.

---

## Thứ tự implement đề xuất

### Phase 1: Apply patches

1. **Backup screenshots** trước fix ở các thời điểm:
   - z = 0.50 (mid-drift, 33% screen)
   - z = 0.30 (cũ vút start, mới vẫn drift, 58% screen)
   - z = 0.20 (mới vút + fade start, 70% screen)
   - z = 0.10 (mid vút + fade, 84% screen)
   - z = 0 (hit zone)

2. **Apply Patch 1** (đổi `PHASE_SPLIT_D = 0.80`).

3. **Apply Patch 2** (xoá MIDDLE branch trong `depth()`, cập nhật comment).

4. **Apply Patch 3** (z-based fade trong `_draw_middle`, xoá time-based code).

### Phase 2: Test core behavior

5. **Test 1**: motion + fade sync tại z=0.20.

6. **Test 2, 3**: wall solid trong drift, vút + fade trong 30% cuối.

7. **Test 4**: pass-by hard cut.

8. **Test 5**: replay user screenshot — confirm wall solid ở mid-screen.

### Phase 3: Test side effects

9. **Test 6, 7**: LOW + HIGH motion change.

10. **Test 8**: nếu LOW/HIGH dodge timing lệch, tune `DODGE_OFFSET_LOW`, `DODGE_OFFSET_HIGH`.

11. **Test 9**: RELAX_WAIT_SEC > 0 sync.

12. **Test 10**: hole mask alignment.

13. **Test 11**: combo segments.

14. **Test 12**: robust khi tune `PHASE_SPEED_RATIO`.

15. **Test 13**: existing pytest, update expected values.

### Phase 4: Tuning (optional)

16. Nếu user vẫn thấy fade quá sớm/muộn:
    - `FADE_START_Z = 0.25` (60% screen, fade sớm hơn)
    - `FADE_START_Z = 0.15` (78% screen, fade muộn hơn)

17. Nếu vút quá nhanh (0.125s):
    - `PHASE_SPEED_RATIO = 8` → vút chậm hơn (~0.18s)
    - Lưu ý: cần update FADE_START_Z theo nếu muốn vẫn sync với z_split.

---

## Acceptance criteria

Spec coi như implement xong khi:

✓ MIDDLE motion + fade ĐỒNG BỘ start tại z=0.20 (= 70% screen distance)  
✓ Wall solid alpha=1.0 cho mọi z >= 0.20 (= 70% screen progress)  
✓ Wall vút + fade simultaneous trong z [0.20, 0]  
✓ Wall fade linear từ alpha=1.0 (z=0.20) → alpha=0 (z=0)  
✓ Wall biến mất ngay khi z < 0 (pass-by hard cut)  
✓ Replay user screenshot: wall ở mid-screen (z=0.35) hiển thị SOLID  
✓ Hole mask alignment đúng  
✓ Combo modes (punch + relax) work  
✓ LOW/HIGH motion OK (hoặc tune DODGE_OFFSET nếu cần)  
✓ Existing tests pass (hoặc update expected values)

---

## Open questions

(1) **Đổi `PHASE_SPLIT_D` ảnh hưởng cả LOW/HIGH**: bạn OK chấp nhận, hay muốn split thành 2 constants (`PHASE_SPLIT_D_MIDDLE = 0.80`, giữ `PHASE_SPLIT_D = 0.70` cho LOW/HIGH)?

(2) **`DODGE_OFFSET_LOW = +0.01`, `DODGE_OFFSET_HIGH = +0.064`** có cần re-tune không sau khi v2 đổi từ 8.7 → 9.8?

(3) **Phase 2 = 0.125s** (vs 0.21s cũ) có quá nhanh? Nếu có, giảm `PHASE_SPEED_RATIO`.

(4) **`FADE_START_Z = 0.20`** OK hay tune (0.15 muộn hơn, 0.25 sớm hơn)?

(5) **Existing iteration spec files** có cần xóa khỏi `docs/` không, để chỉ giữ guide cuối cùng này?
