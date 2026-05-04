# Relax MIDDLE — Fade Theo Screen Progress 70% Spec

## Mục tiêu

Wall MIDDLE phải solid suốt 70% đầu của hành trình **screen distance** (từ cạnh dưới start gate → cạnh dưới floor / hit zone), chỉ bắt đầu fade-out trong 30% cuối.

User định nghĩa rõ "đoạn đường" = screen distance từ:
- **Start**: cạnh dưới Start Gate (~horizon area, top của visible road)
- **End**: cạnh dưới Floor = hit zone (bottom của visible road)

Spec này thay thế các spec fade trước (`relax-middle-fade-fix-spec.md`, `relax-middle-fade-after-hit-spec.md`, `relax-middle-fade-70-percent-spec.md`). Dùng **z-based fade** thay vì time-based để semantically chính xác.

---

## Toán học

### Mối quan hệ z ↔ screen position

Renderer dùng perspective `(1-z)^1.6` để map z ∈ [0, 1] vào screen y:

```
screen_y(z) = cy_v + (1-z)^1.6 * (y_hit - cy_v)
```

Trong đó:
- `cy_v` = horizon line (y at z=1)
- `y_hit` = hit zone line (y at z=0)

### Screen progress fraction

Block screen progress = % chặng đường visible đã đi từ horizon → hit:

```
screen_progress(z) = (screen_y(z) - cy_v) / (y_hit - cy_v)
                  = (1-z)^1.6
```

| z | Screen progress | Vị trí visual |
|---|---|---|
| 1.00 | 0.000 | Horizon (top of road) |
| 0.80 | 0.072 | Far field |
| 0.50 | 0.330 | Mid field |
| 0.30 | 0.580 | Near (= T = 0.9655 trong time) |
| 0.20 | 0.700 | **70% → fade target** |
| 0.10 | 0.838 | Sát hit zone |
| 0.00 | 1.000 | Hit zone (bottom of road) |

### Tính z khi 70% progress

```
0.70 = (1-z)^1.6
1-z = 0.70^(1/1.6) = 0.70^0.625 ≈ 0.802
z ≈ 0.198 ≈ 0.20
```

→ Wall phải đạt `z ≈ 0.20` (= 70% screen progress) thì mới bắt đầu fade.

### Lưu ý về "start gate bottom"

User nói "đoạn đường = từ cạnh dưới start gate". Mặc định start gate ở y_frac = 0.18 + 0.22 = 0.40, horizon ở 0.45. Gate bottom (0.40H) và horizon (0.45H) gần nhau nhưng không trùng.

Tuy nhiên, **block thực sự spawn từ horizon** (z=1.0 = floor_y(1) = cy_v), KHÔNG phải từ gate bottom. Block path bắt đầu từ horizon trong renderer space.

→ Trong implementation, dùng "70% horizon→hit_zone" làm approximation cho "70% gate_bottom→floor_bottom". Sai số ~5% screen position do gate bottom hơi cao hơn horizon. Acceptable cho default config.

Nếu user thay đổi gate position drastically, spec cần update tham chiếu (xem Open question).

---

## Code patch

**File:** `src/rhythm.py`

**Method:** `RelaxTarget._draw_middle` (khoảng dòng 3437-3446)

**Trước (sau khi đã apply spec relax-middle-fade-fix-spec.md):**

```python
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
```

**Sau (z-based, screen progress = 70%):**

```python
# Fade-out kicks in when wall has visually traveled 70% of the
# screen distance from horizon to hit zone (= start of road →
# end of road in user's terminology).  Using the (1-z)^1.6
# perspective envelope: 70% screen progress ↔ z ≈ 0.198.
# Z-based fade is robust to motion phase tuning — it triggers at
# the same VISUAL position regardless of PHASE_SPLIT_D, etc.
FADE_START_Z = 0.20    # ≈ 1 - 0.70^(1/1.6), wall at 70% screen progress
if z >= 0.0 and z < FADE_START_Z:
    # Linear fade: alpha 1.0 at z = FADE_START_Z, alpha 0 at z = 0
    alpha = z / FADE_START_Z
    alpha = max(0.0, min(1.0, alpha))
    cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
elif z < 0.0:
    # Pass-by (after hit_frame): wall is behind camera, hard cut.
    canvas[:] = base_canvas
```

**Diff:** thay 9 dòng cũ bằng 11 dòng mới (gồm comment).

### Ghi chú implementation

- `z` đã có sẵn ở dòng 3368: `z = self.depth(cur_frame)`. Reuse, không tính lại.
- `z >= 0` check để skip pass-by frames (z âm) → dùng hard-cut.
- `z < FADE_START_Z` (= z < 0.20) → fade window.
- `z >= FADE_START_Z` (= z >= 0.20) → wall fully solid, không vẽ fade.

---

## Bảng so sánh visual

Với `travel = 180 frames`:

| z | Screen progress | Phase | Time p_lin | Alpha (cũ FADE_START=T) | Alpha (mới z-based) |
|---|---|---|---|---|---|
| 0.50 | 33% | Phase 1 drift | 0.69 | 1.0 | 1.0 ✓ |
| 0.40 | 45% | Phase 1 drift | 0.83 | 1.0 | 1.0 ✓ |
| 0.35 | 50% | Phase 1 drift | 0.90 | 1.0 | 1.0 ✓ |
| 0.30 | 58% | Phase 1 end / Phase 2 start | 0.9655 | 1.0 → fade | 1.0 ✓ |
| 0.25 | 64% | Phase 2 vút | 0.972 | 0.81 | 1.0 ✓ |
| 0.20 | 70% | Phase 2 vút | 0.977 | **0.67 ✗** | **1.0 → fade start ✓** |
| 0.15 | 78% | Phase 2 vút | 0.983 | 0.49 | 0.75 |
| 0.10 | 84% | Phase 2 vút | 0.989 | 0.31 | 0.50 |
| 0.05 | 92% | Phase 2 vút | 0.994 | 0.16 | 0.25 |
| 0.0 | 100% | Hit_frame | 1.000 | 0 | 0 |

→ Wall solid suốt drift + đầu vút, chỉ fade trong 30% cuối screen (z = 0.20 → 0). Match user expectation.

---

## Touch points

### `src/rhythm.py` — `_draw_middle` (dòng 3437-3446)

Apply patch trên. Lưu ý: spec này GIẢ ĐỊNH các patch trước (đổi `spawn_frame` → `move_start_frame` cho `p_lin`) ĐÃ apply. Nếu chưa, áp dụng cùng lúc:

```python
# Line 3437-3438 (nếu chưa fix):
move_start = self.move_start_frame
travel_f = max(1, self.hit_frame - move_start)
p_lin = (cur_frame - move_start) / travel_f

# Sau đó replace block FADE bằng z-based logic.
```

Note: với z-based fade, `p_lin` không còn dùng cho fade nữa. Nhưng vẫn giữ tính p_lin cho consistency với existing code (nếu có ai dùng p_lin sau này).

Có thể bỏ p_lin nếu code không cần. Để safe, giữ.

---

## Test scenarios

### Test 1: Wall solid trong drift phase

```
Setup: spawn MIDDLE block, travel = 180f.
At z = 0.50 (33% screen): alpha = 1.0 ✓ wall solid
At z = 0.40 (45% screen): alpha = 1.0 ✓ wall solid
At z = 0.35 (50% screen): alpha = 1.0 ✓ wall solid
At z = 0.30 (58% screen, T): alpha = 1.0 ✓ wall solid (KHÁC với spec cũ)
```

### Test 2: Fade kicks in tại z = 0.20 (70% screen)

```
At z = 0.21 (just above target): alpha = 1.0
At z = 0.20 (target): alpha = 1.0 → fade start
At z = 0.15: alpha = 0.75
At z = 0.10: alpha = 0.50
At z = 0.05: alpha = 0.25
At z = 0.0: alpha = 0
```

### Test 3: Pass-by hard cut

```
At z = -0.5 (after hit, pass-by): canvas[:] = base_canvas (wall gone)
At z = -1.0: same
```

### Test 4: Replay user screenshot scenario

```
User screenshot trước fix: wall purple ở mid-screen (z ≈ 0.35) ĐANG fade.
Sau fix với z-based: ở z = 0.35, alpha = 1.0 (KHÔNG fade).
→ Wall hiển thị solid ở mid-screen, chỉ fade khi đã sát hit zone.
```

### Test 5: Resilient to motion tuning

```
Setup: thay đổi PHASE_SPLIT_D = 0.80 (tune motion).
T = D / (D + (1-D)/ratio) = 0.80 / 0.8167 ≈ 0.9796.
Time when z = 0.20:
  z_split = 0.20, target_z = 0.20 (= z_split, ngay biên Phase 1/2)
  p_lin ≈ 0.98

Verify: với z-based fade, fade vẫn kicks in tại screen 70%, KHÔNG đổi theo PHASE_SPLIT_D.
→ Z-based robust to constant changes.
```

### Test 6: Hole mask alignment

```
Setup: MIDDLE với hole_mask.
Verify: hole position match wall trong toàn approach.
Verify: fade alpha áp đều cho cả wall + hole.
```

### Test 7: Combo segment

```
Setup: combo punch + relax(middle).
Verify: MIDDLE wall behavior correct, không ảnh hưởng punch.
```

### Test 8: Edge case — block spawn ngay tại z=0.20

```
Hypothetical: block spawn với z=0.20 ngay từ đầu (không xảy ra trong practice nhưng test edge).
Verify: alpha = 1.0 → fade start ngay → fade trong 30% remaining z.
KHÔNG crash.
```

### Test 9: Z-based vs time-based comparison

```
Implement cả 2 versions (time-based FADE_START = 0.977 vs z-based FADE_START_Z = 0.20).
Verify: ở default config, 2 versions cho ra fade timing TƯƠNG ĐƯƠNG (sai số nhỏ).
Verify: nếu tune PHASE_SPLIT_D, time-based bị lệch, z-based vẫn correct.
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **`depth()` motion**: KHÔNG đụng. Phase split giữ nguyên.

2. **`is_dead`, `dodge_frame`**: không liên quan fade.

3. **LOW / HIGH rendering**: không có fade logic này.

4. **Constants `PHASE_SPLIT_D`, `PHASE_SPEED_RATIO`**: giữ nguyên. Z-based fade ĐỘC LẬP với 2 constants này.

5. **`base_canvas` blend pattern** (`cv2.addWeighted`): giữ nguyên.

6. **`canvas[:] = base_canvas` hard-cut sau hit**: giữ nguyên cho z < 0.

7. **`spawn_frame` → `move_start_frame`** trong p_lin (nếu chưa fix): vẫn cần fix song song.

8. **Hole mask alignment**: depend on `depth(z)`, tự sync.

9. **`(1-z)^1.6` perspective formula**: hardcoded trong `floor_y()` của PerspectiveCamera. Nếu sau này đổi thành `(1-z)^k` với k khác, FADE_START_Z mapping sẽ lệch. Cần update spec nếu k thay đổi.

10. **Wall freeze tại z=0**: do `_draw_middle` clamp `wz = cam.z_from_norm(max(0, ...))`, wall freeze tại y_hit khi z=0. Điều này không vấn đề vì alpha = 0 ngay tại z=0 → wall đã hidden. Tiếp z<0 → hard cut.

---

## Pattern code hiện có để tham khảo

- **`z = self.depth(cur_frame)`** ở line 3368: source of z, reuse.
- **`cv2.addWeighted` blend**: existing pattern.
- **`(1-z)^1.6` perspective**: ở `floor_y(z)` của PerspectiveCamera.

---

## Thứ tự implement đề xuất

1. **Backup screenshot** trước fix ở z = 0.5, 0.30, 0.20, 0.10, 0.0.

2. **Apply patch** thay block fade cũ bằng z-based.

3. **Test 1-3**: verify wall solid trong drift, fade tại z=0.20, hard cut sau.

4. **Test 4**: replay user screenshot scenario, confirm wall solid ở mid-screen.

5. **Test 5**: tune PHASE_SPLIT_D, verify fade vẫn correct (robustness).

6. **Test 6, 7**: hole mask + combo, không break.

7. **Smoke test**: tất cả modes.

8. **Tune nếu cần**: nếu user thấy fade quá sớm/muộn, đổi `FADE_START_Z`:
   - `0.25` (60% screen): fade sớm hơn
   - `0.20` (70% screen): default
   - `0.15` (78% screen): fade muộn hơn
   - `0.10` (84% screen): rất muộn

---

## Open questions

(1) **Approximation "horizon ≈ gate bottom"**: với default gate config (gate_bottom = 0.40H, horizon = 0.45H), sai số ~5% screen. Bạn có chấp nhận không, hay muốn implement chính xác (cần access start_gate config trong `_draw_middle`)?

(2) **`FADE_START_Z = 0.20`** (= 70% screen) OK không? Hay muốn:
   - 0.25 (= 64% screen, fade sớm hơn)
   - 0.15 (= 78% screen, fade muộn hơn)

(3) **Linear fade vs ease curve**: hiện tại `alpha = z / FADE_START_Z` linear. Có muốn ease-out (vd `alpha = (z / FADE_START_Z)^2` quadratic) để fade slow start, fast end?

(4) **Z-based vs time-based**: spec này dùng z-based (robust to motion tuning). Có ưu tiên nào cho time-based không?

(5) **Pass-by behavior**: hiện `z < 0` → hard cut. Có muốn fade thêm trong pass-by (e.g., fade từ z=0 xuống z=-0.1 với alpha tiếp tục giảm nhẹ) không? Hay hard cut là đủ?

(6) **Visual effect khác**: alpha fade simple. Có muốn add motion blur, particle dissolve, glow flash gì không?

---

## So sánh với spec previous

| Spec | FADE_START | Z khi fade start | Screen progress | Fade window |
|---|---|---|---|---|
| `relax-middle-fade-fix-spec` | 0.9655 (T) | 0.30 | 58% | 0.21s |
| `relax-middle-fade-after-hit-spec` | 1.0 | 0 | 100% | 0.3s sau hit |
| `relax-middle-fade-70-percent-spec` (Cách C) | 0.977 | 0.20 | 70% | 0.14s |
| **Spec này (z-based)** | **z = 0.20** | **0.20** | **70%** | **Z range [0, 0.20] linear** |

Spec này EFFECTIVELY giống Cách C của spec 70-percent nhưng:
- Z-based (robust)
- Comment giải thích rõ "70% screen progress = từ cạnh dưới start gate xuống cạnh dưới floor"
- Linear alpha based on z (cleaner formula)
