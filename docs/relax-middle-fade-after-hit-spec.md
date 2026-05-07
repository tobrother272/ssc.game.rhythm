# Relax MIDDLE — Move Fade Sau Hit_Frame Spec

## Mục tiêu

Sửa tiếp `RelaxTarget._draw_middle` để wall MIDDLE **giữ solid suốt approach** (cả drift + vút) và **chỉ fade sau khi đã chạm hit_frame**, không phải fade trong lúc còn đang bay tới.

User report: sau khi apply spec [`relax-middle-fade-fix-spec.md`](./relax-middle-fade-fix-spec.md) (đổi `FADE_START = self._phase_split_t() = 0.9655`), wall vẫn fade khi còn cách camera một khoảng xa (xem screenshot user gửi: wall purple ở mid-screen đã bắt đầu transparent, chưa tới yellow chevron hit zone).

Root cause: tại `T = 0.9655`, wall ở `z = 0.30` — visually ~58% chiều cao màn hình từ horizon → still mid-tunnel, chưa "ở camera face". Fade starts here vẫn thấy "fade từ xa".

Spec này độc lập với mọi spec khác. Chỉ đụng 4 dòng trong `_draw_middle`.

---

## Phân tích vị trí z trong từng phase

Với `PHASE_SPLIT_D = 0.70`, `PHASE_SPEED_RATIO = 12.0`, `T = 0.9655`:

| p_lin | z value | Screen y (% from horizon to hit) | Cảm giác |
|---|---|---|---|
| 0.50 | 0.64 | 30% | Mid-far drift |
| 0.80 | 0.42 | 45% | Mid drift |
| 0.90 | 0.35 | 50% | Late drift |
| 0.9655 (T) | 0.30 | 58% | **Vút bắt đầu** |
| 0.98 | 0.17 | 75% | Vút mid |
| 0.99 | 0.087 | 86% | Vút near hit zone |
| 0.995 | 0.043 | 92% | Sát hit zone |
| 1.0 | 0 | 100% | **Hit zone** |

Tại `T = 0.9655` (FADE_START hiện tại), wall ở **58% screen** — chưa thực sự "ở camera". Fade kích hoạt ở đây gây cảm giác "biến mất từ xa".

Wall thực sự "ở camera face" chỉ khi **z gần 0** (~p_lin > 0.99).

---

## Fix: Move fade sang sau hit_frame

### Thiết kế mới

Wall behaviour mong muốn:
1. **Drift phase** (p_lin = 0 → T): wall solid, drift chậm từ horizon.
2. **Vút phase** (T → 1.0): wall solid, vút mạnh tới hit zone.
3. **Hit_frame** (p_lin = 1.0): wall ở camera face (z=0).
4. **Pass-by** (p_lin > 1.0): z âm trong depth() nhưng `_draw_middle` clamp về 0 → wall freeze visual ở z=0. Đây là lúc wall **fade out** dissolve dần.
5. Sau fade complete: wall removed entirely.

→ Fade chỉ chạy **sau** hit_frame, ngắn (~0.1-0.3s) để wall dissolve gọn.

### Code mới

Replace toàn bộ block fade hiện tại (dòng 3437-3446 sau apply spec previous):

```python
# CŨ (sau spec fix-fix-spec):
move_start = self.move_start_frame
travel_f = max(1, self.hit_frame - move_start)
p_lin = (cur_frame - move_start) / travel_f
FADE_START = self._phase_split_t()
if p_lin > FADE_START:
    if p_lin >= 1.0:
        canvas[:] = base_canvas
    else:
        alpha = 1.0 - (p_lin - FADE_START) / (1.0 - FADE_START)
        alpha = max(0.0, min(1.0, alpha))
        cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)

# MỚI:
move_start = self.move_start_frame
travel_f = max(1, self.hit_frame - move_start)
p_lin = (cur_frame - move_start) / travel_f
# Fade-out kicks in ONLY AFTER hit_frame (p_lin > 1.0).  Wall stays
# fully solid throughout approach (drift + vút), reaches camera face
# at hit_frame, then dissolves quickly during pass-by.  This matches
# the visual intent: wall flies in, hits camera, fades out — instead
# of fading while still mid-tunnel.
FADE_DURATION = 0.05   # fade complete in 5% travel after hit (~0.3s @ travel=180)
if p_lin > 1.0:
    fade_progress = (p_lin - 1.0) / FADE_DURATION
    if fade_progress >= 1.0:
        canvas[:] = base_canvas
    else:
        alpha = 1.0 - fade_progress
        alpha = max(0.0, min(1.0, alpha))
        cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
```

### Diff cụ thể

```diff
 move_start = self.move_start_frame
 travel_f = max(1, self.hit_frame - move_start)
 p_lin = (cur_frame - move_start) / travel_f
-FADE_START = self._phase_split_t()
-if p_lin > FADE_START:
-    if p_lin >= 1.0:
+FADE_DURATION = 0.05
+if p_lin > 1.0:
+    fade_progress = (p_lin - 1.0) / FADE_DURATION
+    if fade_progress >= 1.0:
         canvas[:] = base_canvas
     else:
-        alpha = 1.0 - (p_lin - FADE_START) / (1.0 - FADE_START)
+        alpha = 1.0 - fade_progress
         alpha = max(0.0, min(1.0, alpha))
         cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
```

5 dòng thay đổi. Logic flip từ "fade trong [FADE_START, 1.0]" sang "fade trong [1.0, 1.0 + FADE_DURATION]".

---

## Kết quả

Với `travel = 180 frames` (6s), `FADE_DURATION = 0.05` = 9 frames (~0.3s):

| p_lin | z | alpha (cũ) | alpha (mới) | Visual |
|---|---|---|---|---|
| 0.50 | 0.64 | 1.0 | 1.0 | Solid drift |
| 0.90 | 0.35 | 1.0 | 1.0 | Solid drift |
| 0.9655 | 0.30 | 1.0 → fade | 1.0 ✓ | Solid, vút bắt đầu |
| 0.98 | 0.17 | 0.42 ✗ | 1.0 ✓ | Solid, vút mid |
| 0.99 | 0.087 | 0.29 ✗ | 1.0 ✓ | Solid, sát camera |
| 1.0 | 0 | 0 (cut) | 1.0 → fade | **Hit, wall AT camera** |
| 1.025 | 0 (clamp) | base_canvas | 0.5 | Pass-by, dissolving |
| 1.05 | 0 (clamp) | base_canvas | 0 (cut) | Wall removed |

→ Wall solid suốt approach, dừng ở hit zone tại hit_frame, dissolve trong 0.3s pass-by.

User thấy: wall purple bay từ xa, vút tới yellow chevron area, dừng và fade out tại đó. Matches "block hits camera face then disappears" visual.

---

## Tham số FADE_DURATION

`FADE_DURATION = 0.05` = 5% travel time. Với travel khác nhau:

| travel_f (frames) | FADE duration (frames) | At 30 fps |
|---|---|---|
| 90 (3s) | 4-5 | 0.15s |
| 180 (6s) | 9 | 0.3s |
| 240 (8s) | 12 | 0.4s |
| 360 (12s) | 18 | 0.6s |

0.3s ở default travel cảm giác smooth, không quá chậm/quá nhanh. User có thể tune nếu cần:

- **0.02** (1.2% travel) = ~0.07s ở 6s travel: snap-fast, gần như cut đột ngột.
- **0.05** (5% travel) = ~0.3s: smooth dissolve, **đề xuất**.
- **0.10** (10% travel) = ~0.6s: chậm dần, có thể wall lingering quá lâu.

---

## Hệ quả về `is_dead` và lifecycle

`is_dead` (dòng 3196+) dùng `exit_pad ≈ 25 frames` ở travel=180. Block dead 25 frames sau hit_frame (~0.83s).

So với fade duration mới (9 frames = 0.3s):
- p_lin = 1.0 (hit): wall starts fading
- p_lin = 1.05 (= 1.0 + 9/180): wall fully gone
- p_lin = 1.139 (= 1.0 + 25/180): is_dead = True, target removed from active list

→ Có **gap 16 frames (0.53s)** giữa "wall fully invisible" và "target dead". Trong gap này, `_draw_middle` được gọi nhưng `canvas[:] = base_canvas` → KHÔNG render gì.

Không lãng phí render (hard cut at p_lin > FADE_DURATION). Chỉ lãng phí cycle gọi method draw vô ích. Không big deal.

Nếu muốn tối ưu, có thể short-circuit ở đầu `_draw_middle`:

```python
def _draw_middle(self, canvas, cam, cur_frame):
    z = self.depth(cur_frame)
    if z < -1.0:
        return canvas
    # NEW: skip render entirely if past fade
    move_start = self.move_start_frame
    travel_f = max(1, self.hit_frame - move_start)
    if (cur_frame - move_start) / travel_f > 1.05:   # past FADE_DURATION
        return canvas
    # ... rest of method
```

Nhưng đây là micro-optimization, không phải mục tiêu chính của spec này.

---

## Touch points

### 1. `src/rhythm.py` — `_draw_middle` (dòng ~3437-3446)

Apply diff ở trên. Đây là toàn bộ thay đổi.

### 2. KHÔNG cần touch

- `RelaxTarget.depth()`: KHÔNG đụng. Motion logic giữ nguyên.
- `_phase_split_t()`: vẫn dùng cho `is_dead`, không liên quan fade nữa.
- `is_dead`, `dodge_frame`: không phụ thuộc fade timing.
- LOW / HIGH rendering: không có fade logic này.
- Constants `PHASE_SPLIT_D`, `PHASE_SPEED_RATIO`: giữ nguyên.

---

## Test scenarios

### Test 1: Wall solid suốt approach

```
Setup: spawn MIDDLE, travel = 180f, no wait.
Capture frames at p_lin = 0.5, 0.7, 0.9, 0.95, 0.99.
Verify: alpha = 1.0 ở TẤT CẢ frame trong khoảng này.
Verify: wall hiển thị solid, KHÔNG mờ giữa đường.
```

### Test 2: Wall đứng tại hit zone moment of hit

```
Setup: tại p_lin = 1.0 (= hit_frame).
Verify: z = 0, wall projects ở y_hit (sát yellow chevron).
Verify: alpha = 1.0 (vẫn solid tại moment of hit).
```

### Test 3: Fade kicks in ngay sau hit

```
Setup: cur_frame = hit_frame + 1 (= p_lin ≈ 1.006).
Verify: fade_progress = 0.006 / 0.05 ≈ 0.11 → alpha ≈ 0.89.
Verify: wall slightly transparent.

Setup: cur_frame = hit_frame + 5.
Verify: fade_progress = 5/9 ≈ 0.56 → alpha ≈ 0.44.

Setup: cur_frame = hit_frame + 9.
Verify: fade_progress = 1.0 → canvas = base_canvas (wall gone).
```

### Test 4: Pass-by fade duration scaling

```
Setup: travel = 90f (3s).
Compute FADE_DURATION = 0.05 → 4-5 frames.
Verify: wall fade complete trong ~0.15s sau hit.

Setup: travel = 360f (12s).
Compute FADE_DURATION = 0.05 → 18 frames.
Verify: wall fade complete trong ~0.6s sau hit.
```

### Test 5: Sync với RELAX_WAIT_SEC > 0

```
Setup: spawn = 0, wait_frames = 60, travel = 180.
move_start = 60, hit_frame = 240.

Frame 240 (hit_frame): p_lin = 180/180 = 1.0 → alpha = 1.0
Frame 241: p_lin = 181/180 ≈ 1.006 → fade_progress ≈ 0.11 → alpha ≈ 0.89
Frame 249 (= 240 + 9): p_lin = 1.05 → wall removed

Verify: fade kicks in chính xác tại move_start + travel_f, không bị off-by-wait.
```

### Test 6: User screenshot scenario

```
Setup: replay scenario user gửi (wall purple ở mid-screen).
Verify: SAU fix, wall ở vị trí p_lin ≈ 0.7 (mid screen) HOÀN TOÀN SOLID.
Verify: wall chỉ bắt đầu fade khi đã tới sát yellow chevron (z gần 0).
```

### Test 7: Hole mask alignment

```
Setup: MIDDLE với hole_mask_path.
Verify: hole position match wall trong toàn approach.
Verify: fade alpha áp đều cho cả wall + hole region (base_canvas blend uniform).
```

### Test 8: Combo segment

```
Setup: combo punch + relax(middle).
Verify: MIDDLE wall behavior consistent với spec.
Verify: Punch không bị ảnh hưởng.
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **`depth()` motion**: KHÔNG đụng. Motion phase split giữ nguyên (LOW/HIGH/MIDDLE shared profile).

2. **`_phase_split_t()` classmethod**: vẫn còn, vẫn dùng cho `is_dead`. Chỉ KHÔNG dùng cho fade nữa.

3. **`is_dead` exit_pad**: giữ nguyên. Block dead ~25 frames sau hit (vẫn track lifecycle đúng).

4. **`base_canvas` blend pattern** (dòng 3441-3446): giữ nguyên cách blend, chỉ đổi điều kiện trigger.

5. **`hole_mask` `_punch_hole`**: không đụng. Hole vẫn align với wall.

6. **LOW / HIGH rendering**: không có fade logic này, không break.

7. **`canvas[:] = base_canvas`**: vẫn dùng làm hard-cut sau fade complete. Pattern giữ nguyên.

8. **`_draw_middle` cấu trúc**: tổng thể giữ nguyên (project corners → fill poly → texture/stripes → hole → fade). Chỉ block fade ở cuối thay đổi logic.

9. **`spawn_frame`, `move_start_frame`, `hit_frame` properties**: KHÔNG đụng.

10. **Pre-fix-bug fade behavior** (wall fades during last 10% time): sau fix sẽ KHÔNG xảy ra nữa. Đây là intent. User test verify.

---

## Pattern code hiện có để tham khảo

- **`cv2.addWeighted` blend pattern**: existing in line 3446. Spec reuse.
- **Hard-cut `canvas[:] = base_canvas`**: existing pattern, spec reuse.
- **`_phase_split_t()`** (dòng ~3110): vẫn dùng ở `is_dead`. Spec không touch.
- **`move_start_frame` property**: spec dùng làm time base, đã sync với `depth()`.

---

## Thứ tự implement đề xuất

1. **Backup screenshot trước fix** ở các p_lin: 0.5, 0.85, 0.95, 1.0, 1.025. Để verify hiệu quả.

2. **Apply diff 5 dòng** ở `_draw_middle`.

3. **Test 1**: pause ở p_lin = 0.5, 0.7, 0.9, 0.95 — verify wall fully solid.

4. **Test 2**: pause ngay tại hit_frame — verify wall AT yellow chevron, alpha=1.

5. **Test 3**: pause ngay sau hit_frame — verify wall starts fading.

6. **Test 6**: replay scenario user gửi — verify wall không còn fade từ giữa đường.

7. **Test 7**: với hole mask — verify hole vẫn align.

8. **Smoke test**: combo modes, tất cả relax kinds, không break gì.

9. **Optional**: nếu user thấy 0.3s fade quá chậm hoặc nhanh, tune `FADE_DURATION`:
   - 0.02 = 0.07s (snap)
   - 0.05 = 0.3s (default đề xuất)
   - 0.10 = 0.6s (smooth)

---

## Open questions

(1) **`FADE_DURATION = 0.05`** OK không hay muốn tune khác?
   - 0.02 (snap, gần như cut)
   - 0.05 (default đề xuất, ~0.3s ở travel=180)
   - 0.10 (smooth, ~0.6s)

(2) **Wall freezes at z=0 during fade**: vì `_draw_middle` clamp z ≥ 0, wall stay tại hit zone visual trong 0.3s fade. Có vẻ kỳ vì wall lẽ ra phải bay qua. Có 2 options:
   - (a) Accept, wall freeze tại hit zone fade dissolve. Đơn giản.
   - (b) Bỏ z clamp, cho phép wall render với z < 0 (bay xuyên qua camera). Phức tạp hơn — projection có thể blow up.
Đề xuất (a) cho V1, (b) là feature mở rộng nếu cần.

(3) **`_phase_split_t()` còn dùng cho fade nữa không?** Sau fix, fade dùng thời gian sau hit_frame fixed (FADE_DURATION). Không phụ thuộc phase split. Có thể giữ `self._phase_split_t()` import in case sau muốn quay lại, nhưng không gọi.
Đề xuất: bỏ luôn nếu sau fix không dùng. Cleaner code.

(4) **Optimize: skip render khi past fade**: thêm early return `if p_lin > 1.0 + FADE_DURATION: return canvas` ở đầu method. Tiết kiệm cycles trong gap 16 frames giữa fade complete và is_dead. Bạn quan tâm tối ưu không?

(5) **Có cần expose FADE_DURATION qua config** (vd `RELAX_MIDDLE_FADE_SEC`)? Tôi đề xuất KHÔNG cần — 0.3s là one-size-fits-all reasonable. Nếu sau user yêu cầu, dễ expose.

(6) **Visual effect khác trong fade window**: có muốn add dissolve effect (vd particle burst, glow flash) thay vì plain alpha fade không? Đó là feature riêng, không trong scope.
