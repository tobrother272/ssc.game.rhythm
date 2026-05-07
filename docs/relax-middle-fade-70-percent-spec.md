# Relax MIDDLE — Fade Start tại 70% Đoạn Đường Spec

## Mục tiêu

Wall MIDDLE phải **giữ solid trong 70% đầu** của hành trình từ horizon tới hit zone, **chỉ bắt đầu fade** sau mốc 70%. Sau đó fade dần đến khi biến mất tại hit_frame.

Spec này thay thế thiết kế "fade sau hit_frame" của [`relax-middle-fade-after-hit-spec.md`](./relax-middle-fade-after-hit-spec.md). User muốn fade DURING approach (sau 70%) thay vì SAU hit.

---

## Làm rõ "70% đoạn đường"

Có 3 cách diễn giải "70% đoạn đường", mỗi cách cho ra `FADE_START` khác nhau:

### A. 70% TRAVEL TIME (literal time fraction)

`FADE_START = 0.70` → fade kích hoạt tại p_lin = 0.7 (= 70% thời gian travel).

Tại p_lin = 0.70 (motion mới sau unify với T=0.9655):
- Wall đang trong **drift phase**
- z = 1.0 - 0.7 * (0.7/0.9655) ≈ 0.49
- Screen position: ~36% chiều cao từ horizon đến hit zone

Wall ở ~36% screen → fade window = 30% time = 1.8s ở travel=180. Fade chạy SUỐT phần còn lại của drift + toàn bộ vút.

### B. 70% Z-DISTANCE COVERED (đã đi 70% khoảng cách 3D)

Z đi từ 1.0 → 0.30 = covered 0.70 z-units = 70%. Tương ứng `FADE_START = T = 0.9655`.

Tại p_lin = 0.9655:
- Wall ở **vút bắt đầu**
- z = 0.30
- Screen position: ~58% chiều cao

Fade window = 3.45% time = 6 frames ở travel=180 = 0.21s.

**Đây là setting đã thử trong spec `relax-middle-fade-fix-spec.md` và user đã reject** ("còn cách 1 khoảng xa mà đã bắt đầu fadeout").

### C. 70% SCREEN DISTANCE (visual perception)

Screen position = (1-z)^1.6. 70% way down = (1-z)^1.6 = 0.70 → z ≈ 0.20.

Tương ứng p_lin ≈ 0.977.

Tại p_lin = 0.977:
- Wall đang trong **vút phase, sát hit zone**
- z = 0.20
- Screen position: 70% chiều cao từ horizon

Fade window = 2.3% time = 4 frames ở travel=180 = 0.14s.

---

## Tóm tắt 3 lựa chọn

| Interpretation | FADE_START (p_lin) | Z khi fade start | Screen % | Fade window |
|---|---|---|---|---|
| **A. 70% time** | 0.70 | 0.49 | 36% | 30% time (~1.8s) |
| **B. 70% z covered** | 0.9655 | 0.30 | 58% | 3.45% time (~0.21s) |
| **C. 70% screen** | 0.977 | 0.20 | 70% | 2.3% time (~0.14s) |

## Đề xuất chọn

**Cách C** match nhất với "70% đoạn đường" nếu hiểu theo cảm nhận thị giác của user — wall đã visibly đi 70% chặng đường trên màn hình rồi mới fade.

**Cách A** match nếu user nghĩ theo time-fraction (70% thời gian).

**Cách B** match nếu user nghĩ theo world-distance (z-coords).

User đã reject **B**. Còn A và C.

Vì user đã muốn fade **muộn hơn** (rejected B vì "vẫn xa đã fade"), tôi đề xuất **C** (fade khi đã 70% screen distance).

Tuy nhiên implement linh hoạt với hằng số tunable, user có thể thử cả A và C.

---

## Code patch

**File:** `src/rhythm.py`

**Method:** `RelaxTarget._draw_middle` (khoảng dòng 3437-3446)

**Trước:**

```python
travel_f = max(1, self.hit_frame - self.spawn_frame)
p_lin = (cur_frame - self.spawn_frame) / travel_f
FADE_START = 0.9
if p_lin > FADE_START:
    if p_lin >= 1.0:
        canvas[:] = base_canvas
    else:
        alpha = 1.0 - (p_lin - FADE_START) / (1.0 - FADE_START)
        alpha = max(0.0, min(1.0, alpha))
        cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
```

**Sau (Cách C — 70% screen distance, đề xuất):**

```python
move_start = self.move_start_frame
travel_f = max(1, self.hit_frame - move_start)
p_lin = (cur_frame - move_start) / travel_f

# Fade-out kicks in when wall has visually traveled 70% of the way
# from horizon to hit zone (= 70% screen distance).  Using the
# (1-z)^1.6 perspective envelope: 0.70 = (1-z)^1.6 → z ≈ 0.20.
# In motion time (with PHASE_SPLIT_D=0.70, ratio=12), z=0.20 occurs
# at p_lin ≈ 0.977.  Fade completes at hit_frame (p_lin=1.0).
FADE_START = 0.977
if p_lin > FADE_START:
    if p_lin >= 1.0:
        canvas[:] = base_canvas
    else:
        alpha = 1.0 - (p_lin - FADE_START) / (1.0 - FADE_START)
        alpha = max(0.0, min(1.0, alpha))
        cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
```

**Sau (Cách A — 70% time literal):**

```python
move_start = self.move_start_frame
travel_f = max(1, self.hit_frame - move_start)
p_lin = (cur_frame - move_start) / travel_f

# Fade-out kicks in at 70% of travel time.  Wall fades over the
# remaining 30% of time, completing at hit_frame.
FADE_START = 0.70
if p_lin > FADE_START:
    if p_lin >= 1.0:
        canvas[:] = base_canvas
    else:
        alpha = 1.0 - (p_lin - FADE_START) / (1.0 - FADE_START)
        alpha = max(0.0, min(1.0, alpha))
        cv2.addWeighted(canvas, alpha, base_canvas, 1.0 - alpha, 0, canvas)
```

Cả 2 đều fix luôn bug `spawn_frame` → `move_start_frame` (đã giải thích ở các spec trước).

---

## Bảng so sánh visual cho 3 lựa chọn (travel = 180 frames = 6s)

### Cách A: FADE_START = 0.70

| p_lin | z | Screen % | Alpha |
|---|---|---|---|
| 0.50 | 0.64 | 30% | 1.0 ✓ |
| 0.70 | 0.49 | 36% | 1.0 → fade start |
| 0.85 | 0.39 | 42% | 0.50 (mid-fade) |
| 0.9655 (T) | 0.30 | 58% | 0.12 (almost gone) |
| 0.99 | 0.087 | 86% | 0.03 (gần invisible) |
| 1.00 | 0 | 100% | 0 |

→ Wall fade nhiều ở **mid-screen** (~36-42% screen), almost gone trước khi vút bắt đầu.

### Cách B: FADE_START = 0.9655 (đã reject)

| p_lin | z | Screen % | Alpha |
|---|---|---|---|
| 0.90 | 0.35 | 50% | 1.0 ✓ |
| 0.9655 (T) | 0.30 | 58% | 1.0 → fade start |
| 0.98 | 0.17 | 75% | 0.58 |
| 0.99 | 0.087 | 86% | 0.29 |
| 1.00 | 0 | 100% | 0 |

→ Wall fade trong **vút phase** ở 58-100% screen. User nói "vẫn còn xa".

### Cách C: FADE_START = 0.977 (đề xuất)

| p_lin | z | Screen % | Alpha |
|---|---|---|---|
| 0.95 | 0.31 | 56% | 1.0 ✓ |
| 0.977 | 0.20 | 70% | 1.0 → fade start |
| 0.985 | 0.13 | 81% | 0.65 |
| 0.99 | 0.087 | 86% | 0.43 |
| 0.995 | 0.043 | 92% | 0.22 |
| 1.00 | 0 | 100% | 0 |

→ Wall solid suốt drift + đầu vút, **chỉ fade ở 70-100% screen** (sát hit zone).

---

## Touch points

### `src/rhythm.py` — `_draw_middle` (dòng 3437-3446)

Apply patch trên. 4 dòng thay đổi (gồm comment + đổi spawn → move_start + đổi FADE_START).

---

## Test scenarios

### Test 1: Wall solid trong drift phase

```
Setup: spawn MIDDLE, travel = 180f.
At p_lin = 0.5 (z = 0.64, 30% screen):
Verify: alpha = 1.0 (fully solid), wall hiển thị rõ.

At p_lin = 0.85 (z = 0.39, 42% screen):
Verify: alpha = 1.0 với cách C, alpha < 1 với cách A.
```

### Test 2: Fade kicks in tại 70% screen (cách C)

```
Setup: cách C, FADE_START = 0.977.
At p_lin = 0.97 (z ≈ 0.21, ~69% screen):
Verify: alpha = 1.0 (chưa fade).

At p_lin = 0.977 (z ≈ 0.20, 70% screen):
Verify: alpha = 1.0 (vừa bắt đầu fade).

At p_lin = 0.985 (z ≈ 0.13, 81% screen):
Verify: alpha ≈ 0.65 (đang fade).

At p_lin = 1.0 (hit_frame, z = 0, 100% screen):
Verify: alpha = 0, wall biến mất.
```

### Test 3: User screenshot scenario (replay)

```
Setup: replay frame user gửi (wall purple ở mid-screen).
Verify với cách C: wall ở ~50% screen → alpha = 1.0 (solid).
Verify với cách C: wall chỉ fade khi đã tới sát yellow chevron.
```

### Test 4: Hole mask alignment

```
Setup: MIDDLE với hole_mask.
Verify: hole position match wall trong toàn approach.
Verify: fade alpha áp đều cho cả wall + hole.
```

### Test 5: Sync với RELAX_WAIT_SEC > 0

```
Setup: spawn = 0, wait = 60, travel = 180.
move_start = 60, hit_frame = 240.

At cur_frame = 60 + 0.977 * 180 = 235.86 (≈ frame 236):
Verify: p_lin = (236 - 60) / 180 ≈ 0.978 → fade start.

→ Sync với move_start, không bị off-by-wait.
```

### Test 6: So sánh 3 lựa chọn

```
Implement cả 3 (FADE_START = 0.70, 0.9655, 0.977).
Run live preview với mỗi setting.
User feedback chọn cái nào match expectation.
```

### Test 7: Combo segment

```
Setup: combo punch + relax(middle).
Verify: MIDDLE wall behavior consistent với spec.
Verify: Punch không bị ảnh hưởng.
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **`depth()` motion**: KHÔNG đụng. Phase split giữ nguyên.

2. **`is_dead`, `dodge_frame`**: không phụ thuộc fade timing.

3. **LOW / HIGH rendering**: không có fade logic này.

4. **Constants `PHASE_SPLIT_D`, `PHASE_SPEED_RATIO`**: giữ nguyên. Chỉ FADE_START là tunable.

5. **`base_canvas` blend pattern** (cv2.addWeighted): pattern giữ nguyên.

6. **Bug fix `spawn_frame` → `move_start_frame`**: vẫn cần (đã làm trong patch).

7. **Wall freeze tại z=0 sau hit**: vẫn dùng `canvas[:] = base_canvas` cut, không thay đổi behavior này.

8. **Hole mask alignment**: depend on `depth(z)`, tự sync.

---

## Đề xuất implement

1. **Implement cách C** (FADE_START = 0.977) trước. Đây là "70% screen" interpretation match user expectation tốt nhất.

2. **Test live preview** xem có khớp ý user không.

3. **Nếu user vẫn thấy fade quá sớm/muộn**, tune FADE_START:
   - Tăng (ví dụ 0.985, 0.99): fade muộn hơn, window ngắn hơn.
   - Giảm (ví dụ 0.95, 0.93): fade sớm hơn, window dài hơn.

4. **Nếu user thực sự muốn cách A** (literal 70% time): đổi `FADE_START = 0.70`. Nhưng warn user rằng wall sẽ fade từ mid-screen, có thể không match expectation.

---

## Open questions

(1) **Interpretation đúng**: user muốn cách A, B, hay C? Tôi đề xuất **C** dựa trên user previously rejected B ("còn xa đã fade"), và A còn xa hơn nữa. Nhưng confirm giúp.

(2) **FADE_START tunable qua config**: có muốn expose qua `RELAX_MIDDLE_FADE_START` config không? Tôi đề xuất KHÔNG cần (one-size-fits-all giá trị).

(3) **Fade window có cần dài hơn**: với cách C, fade window = 0.14s rất nhanh. Có muốn extend fade window xuống dưới hit_frame (ví dụ fade kéo dài tới 1.05 thay vì 1.0) không?

(4) **Visual effect khác trong fade**: alpha fade đơn giản. Có muốn add motion blur, particle dissolve, glow flash gì không? Đó là feature riêng.

(5) **Cách C vs B**: chỉ chênh lệch 0.012 trong p_lin (= 2 frames) nhưng visual khác biệt rõ. Bạn confirm muốn LATER hơn B nhỉ?
