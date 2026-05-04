# Relax MIDDLE — Implementation Guide

## Mục tiêu

Hợp nhất 2 fix cho `RelaxTarget` kind=`middle` thành 1 hướng dẫn implement duy nhất:

1. **Motion unify**: MIDDLE dùng cùng phase split (drift slow → vút) như LOW/HIGH.
2. **Fade theo screen progress**: wall fade-out chỉ kích hoạt khi đã đi 70% screen distance từ horizon (cạnh dưới start gate) tới hit zone (cạnh dưới floor).

Spec này thay thế các spec iteration trước cho MIDDLE:
- ~~`relax-middle-motion-unify-spec.md`~~
- ~~`relax-middle-fade-fix-spec.md`~~
- ~~`relax-middle-fade-after-hit-spec.md`~~
- ~~`relax-middle-fade-70-percent-spec.md`~~
- ~~`relax-middle-fade-screen-progress-spec.md`~~

Cursor implement guide này đủ, không cần đọc 5 spec trên.

---

## File và method bị ảnh hưởng

**File duy nhất:** `src/rhythm.py`

**Class:** `RelaxTarget`

**2 method:**
- `depth(self, cur_frame: int) -> float` (khoảng dòng 3122-3184)
- `_draw_middle(self, canvas, cam, cur_frame)` (khoảng dòng 3367-3447)

---

## Patch 1 — Motion unify trong `depth()`

### Diff

Trong `depth()`, **xoá hoàn toàn** block MIDDLE-specific (khoảng 36 dòng):

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
-    # approach.  Using inverse-Z (1/wz linear in time) makes the
-    # block's screen-space size scale linearly...
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
+    # ── All kinds (LOW / HIGH / MIDDLE): two-phase "70% chậm + 30% vút" ─
+    # MIDDLE was previously using a separate visual-linear profile
+    # but unified motion gives all kinds the same drift-then-vút
+    # urgency, making dodge timing consistent.
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

### Hệ quả

Cả 3 kind (LOW, HIGH, MIDDLE) sau đó dùng cùng motion:
- Phase 1 (drift): ~96.55% time, z 1.0 → 0.30
- Phase 2 (vút): ~3.45% time, z 0.30 → 0
- Pass-by: z âm tới -1.2 rồi clamp

Travel duration không đổi (vẫn `RELAX_TRAVEL_SEC`).

---

## Patch 2 — Z-based fade trong `_draw_middle`

### Diff

Trong `_draw_middle`, replace block fade ở cuối method (khoảng dòng 3437-3446):

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
+    # Fade-out kicks in only when the wall has visually traveled 70%
+    # of the screen distance from horizon (≈ start-gate bottom) to
+    # hit zone (= floor bottom).  Using the (1-z)^1.6 perspective
+    # envelope: 70% screen progress ↔ z ≈ 0.198 ≈ 0.20.  Z-based
+    # fade is independent of motion phase tuning — it always triggers
+    # at the same VISUAL position regardless of PHASE_SPLIT_D, etc.
+    FADE_START_Z = 0.20    # ≈ 1 - 0.70^(1/1.6), wall at 70% screen progress
+    if z >= 0.0 and z < FADE_START_Z:
+        # Linear fade: alpha 1.0 at z = FADE_START_Z, alpha 0 at z = 0
+        alpha = z / FADE_START_Z
+        alpha = max(0.0, min(1.0, alpha))
+        cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
+    elif z < 0.0:
+        # Pass-by (after hit_frame): wall is behind camera, hard cut.
+        canvas[:] = base_canvas
     return canvas
```

### Note quan trọng

- `z` đã được tính ở dòng 3368: `z = self.depth(cur_frame)`. Reuse, không tính lại.
- KHÔNG cần `p_lin`, `move_start`, `travel_f` trong block fade nữa. Z-based đơn giản hơn.
- 2 dòng `travel_f = max(1, self.hit_frame - self.spawn_frame)` và `p_lin = (cur_frame - self.spawn_frame) / travel_f` (dòng 3437-3438) **xoá luôn** — fade không dùng tới.

---

## Tóm tắt visual sau khi apply 2 patches

Với `travel = 180 frames` (6s) và default config:

| Time (s) | p_lin | z | Screen % | Phase | Alpha |
|---|---|---|---|---|---|
| 0.0 | 0.00 | 1.00 | 0% | Spawn (horizon) | 1.0 ✓ |
| 1.5 | 0.25 | 0.82 | 7% | Drift (slow) | 1.0 ✓ |
| 3.0 | 0.50 | 0.64 | 33% | Drift (mid) | 1.0 ✓ |
| 4.5 | 0.75 | 0.46 | 39% | Drift | 1.0 ✓ |
| 5.5 | 0.917 | 0.34 | 53% | Drift (cuối) | 1.0 ✓ |
| 5.79 | 0.9655 | 0.30 | 58% | **Vút bắt đầu** | 1.0 ✓ |
| 5.85 | 0.975 | 0.22 | 67% | Vút | 1.0 ✓ |
| 5.86 | 0.977 | 0.20 | 70% | Vút | **1.0 → fade start** |
| 5.92 | 0.989 | 0.10 | 84% | Vút (sát hit) | 0.50 |
| 5.97 | 0.995 | 0.05 | 92% | Vút (rất sát) | 0.25 |
| 6.0 | 1.000 | 0.00 | 100% | **Hit zone** | 0 |
| 6.0+ | > 1 | < 0 | (pass-by) | Pass-by | 0 (hard cut) |

→ Wall hiển thị solid suốt 5.86s đầu (drift + đầu vút), fade dần trong 0.14s cuối khi đã đi 70% screen progress.

---

## Test scenarios

### Test 1: Wall solid trong drift

Pause ở các frame:
- p_lin = 0.50 (z = 0.64): alpha = 1.0, wall hiển thị rõ
- p_lin = 0.85 (z = 0.39): alpha = 1.0, wall hiển thị rõ
- p_lin = 0.95 (z = 0.31): alpha = 1.0, wall hiển thị rõ (KHÁC behavior cũ)

### Test 2: Fade tại 70% screen progress

Verify alpha tại các z:
- z = 0.21 (just above target): alpha = 1.0
- z = 0.20: alpha = 1.0 → fade start
- z = 0.15 (78% screen): alpha = 0.75
- z = 0.10 (84% screen): alpha = 0.50
- z = 0.05 (92% screen): alpha = 0.25
- z = 0.00 (hit): alpha = 0

### Test 3: Pass-by hard cut

Sau hit_frame (z âm): wall biến mất ngay (canvas[:] = base_canvas), không có frozen wall lingering.

### Test 4: Replay user screenshot

Setup giống screenshot user gửi (wall purple ở mid-screen).
Verify: ở vị trí đó (z ≈ 0.35), wall ALPHA = 1.0 (solid), KHÔNG fade.

### Test 5: Robust khi tune motion

Đổi `PHASE_SPLIT_D = 0.80`. Verify: fade vẫn kích hoạt tại z=0.20 (= 70% screen). Z-based KHÔNG bị ảnh hưởng motion tuning.

### Test 6: RELAX_WAIT_SEC > 0

Spawn = 0, wait_frames = 60, travel = 180. Verify fade timing sync với move_start (qua `z = depth(cur_frame)`).

### Test 7: Hole mask alignment

Wall + hole_mask: verify hole position match wall trong toàn approach. Fade alpha áp đều cho cả wall + hole.

### Test 8: Combo mode

Combo punch + relax(middle). Verify MIDDLE behavior đúng, không ảnh hưởng punch.

### Test 9: Existing tests vẫn pass

Run pytest suite. Test relax-related vẫn pass (motion tests có thể cần update expected z values).

---

## Quan trọng: KHÔNG được phá vỡ

1. **`PHASE_SPLIT_D = 0.70`, `PHASE_SPEED_RATIO = 12.0`**: class constants giữ nguyên.

2. **`_phase_split_t()` classmethod**: giữ nguyên, dùng cho cả 3 kind sau unify.

3. **LOW / HIGH rendering** (`_draw_low`, `_draw_high`): KHÔNG đụng. Motion vẫn dùng formula chung qua `depth()`.

4. **MIDDLE rendering** (`_draw_middle`): KHÔNG đụng phần build wall_poly + texture + hole. Chỉ thay block fade ở cuối method.

5. **`is_dead`, `dodge_frame`, `dodge_end_frame`**: giữ nguyên.

6. **Camera bob `_relax_camera_dy`**: skip MIDDLE intentionally (line 201-202). Giữ nguyên.

7. **Stickman pose engine**: skip MIDDLE intentionally (line 6572-6573). Giữ nguyên.

8. **Spawn logic** (`_spawn_target` mode='relax'): không đụng.

9. **`(1-z)^1.6` perspective formula** trong `floor_y()`: nếu sau này đổi exponent (vd 1.5 hoặc 2.0), `FADE_START_Z = 0.20` sẽ lệch. Khi đó update mapping.

10. **Wall freeze tại z=0**: do `_draw_middle` clamp `wz = cam.z_from_norm(max(0, ...))`, wall freeze tại y_hit khi z=0. Không vấn đề vì alpha=0 ngay tại z=0 → wall đã invisible. Tiếp z<0 → hard cut.

---

## Pattern code hiện có để tham khảo

- **`z = self.depth(cur_frame)`** ở line 3368: source of z, reuse cho fade.
- **`cv2.addWeighted` blend** ở line 3446 cũ: pattern giữ nguyên cho alpha fade.
- **`canvas[:] = base_canvas`** ở line 3442 cũ: pattern giữ nguyên cho hard cut.
- **`_phase_split_t()` classmethod** dòng 3110-3113: dùng trong `is_dead`, không đụng.

---

## Thứ tự implement đề xuất

1. **Backup screenshot** trước fix ở các thời điểm:
   - z = 0.5 (mid-drift)
   - z = 0.30 (vút start)
   - z = 0.20 (target fade start)
   - z = 0.10 (vút near)
   - z = 0.0 (hit)

2. **Apply Patch 1** (motion unify trong `depth()`):
   - Xoá block `if self.kind == 'middle': ... return max(-1.2, z)` (36 dòng).
   - Cập nhật comment header LOW/HIGH branch thành "All kinds".
   - Run test motion: verify z values theo formula chung.

3. **Apply Patch 2** (z-based fade trong `_draw_middle`):
   - Xoá 2 dòng tính `travel_f` + `p_lin` ở line 3437-3438.
   - Replace block fade (line 3439-3446) bằng z-based code.
   - Run test fade: verify alpha tại các z.

4. **Test 1-4**: visual sanity (Test 1, 2, 3) + replay user scenario (Test 4).

5. **Test 5-7**: robustness (Test 5), edge cases (Test 6), hole mask (Test 7).

6. **Test 8-9**: integration (Test 8 combo, Test 9 existing tests).

7. **Smoke test** toàn bộ live preview với segments có MIDDLE block.

8. **Optional tuning** nếu user thấy fade quá sớm/muộn:
   - `FADE_START_Z = 0.25` → fade sớm hơn (60% screen progress)
   - `FADE_START_Z = 0.15` → fade muộn hơn (78% screen progress)
   - `FADE_START_Z = 0.10` → fade rất muộn (84% screen progress)

---

## Code mẫu cuối cùng

Sau khi apply cả 2 patches, đoạn code quan trọng:

### `RelaxTarget.depth()`

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

### `RelaxTarget._draw_middle()` (đoạn cuối, sau hole_mask)

```python
def _draw_middle(self, canvas, cam, cur_frame):
    z = self.depth(cur_frame)
    if z < -1.0:
        return canvas
    base_canvas = canvas.copy()
    # ... existing code: build wall_poly, render texture/stripes, hole_mask ...

    if self.hole_mask_path:
        self._punch_hole(canvas, wall_poly, base_canvas)

    # Fade-out kicks in only when the wall has visually traveled 70%
    # of the screen distance from horizon (≈ start-gate bottom) to
    # hit zone (= floor bottom).  Using the (1-z)^1.6 perspective
    # envelope: 70% screen progress ↔ z ≈ 0.198 ≈ 0.20.
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

## Acceptance criteria

Spec coi như implement xong khi:

✓ MIDDLE motion match LOW/HIGH (cùng formula trong `depth()`)
✓ Wall MIDDLE solid alpha=1.0 cho mọi z >= 0.20 (= 70% screen progress)
✓ Wall MIDDLE fade linear từ alpha=1.0 (z=0.20) → alpha=0 (z=0)
✓ Wall MIDDLE biến mất ngay khi z < 0 (pass-by)
✓ Replay user screenshot: wall ở mid-screen (z=0.35) hiển thị SOLID, không fade
✓ Hole mask alignment đúng
✓ Combo modes (punch + middle) work
✓ Existing tests pass (hoặc update expected values)

Nếu user vẫn không hài lòng visual, tune `FADE_START_Z` (xem Optional tuning).
