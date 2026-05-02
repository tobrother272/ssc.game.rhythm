# Drag Mechanics — Offset Fix & Ergonomics Spec

## Mục tiêu

Sửa lỗi UX trong cơ chế kéo (drag) trên timeline. Spec này tập trung vào **mechanics** của drag — cụ thể là cách cursor và item liên hệ với nhau trong khi kéo. Tách biệt với spec [`layer-resize-move-threshold-spec.md`](./layer-resize-move-threshold-spec.md) (vốn nói về threshold-based snap-to-fill cho layer).

Spec này gồm 4 vấn đề:

1. **Issue 1 — Segment drag bỏ qua offset điểm bấm** (bug rõ ràng, phải sửa).
2. **Issue 2 — Layer drag/resize đã đúng pattern, cần verify và làm reference**.
3. **Issue 3 — Cursor feedback chưa nhất quán giữa segment drag và layer drag** (cải thiện).
4. **Issue 4 — Time tooltip trong khi drag** (tính năng mới, nice-to-have).

---

## Issue 1: Segment drag bỏ qua offset điểm bấm

### Triệu chứng

User click vào bất kỳ vị trí nào trên một segment block trên timeline rồi kéo. Ngay khi vượt ngưỡng activate (5px), segment "nhảy" để cursor nằm chính giữa segment, bất kể user click ở đâu trên đó. Cảm giác như segment bị "snap to center" lúc drag bắt đầu.

### Ví dụ cụ thể

Segment dài 10s, render từ scene_x=100 đến scene_x=600 (50 px/s). User click ở scene_x=550 (gần rìa phải) và muốn dịch segment sang phải 4s.

```
Trước khi drag (segment ở 100-600, click tại 550):

           100              550        600
            |================|=========|
            ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
                              ●  ← cursor

Trải nghiệm mong đợi (drag sang phải 200px → cursor=750):

                  300              750        800
                    |================|=========|
                    ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
                                      ●  ← cursor vẫn ở vị trí tương đối cũ
                    
Trải nghiệm hiện tại (cursor mới đi 5px → ngay lập tức nhảy):

                    305              555        805
                    |================|=========|
                    ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
                                      ●  ← cursor ở giữa segment (sai)
                                      
Segment đã nhảy 245 pixel sang trái chỉ vì cursor đi 5px sang phải.
```

### Root cause

**File:** `studio/editor/timeline_panel.py`

3 chỗ tính ghost left edge từ cursor x đều dùng heuristic "căn cursor vào giữa segment":

```python
# _paint_segment_blocks, dòng ~4439
ghost_x = self._drag_ghost_x - w / 2

# _drag_insertion_x, dòng ~5713
return max(0.0, self._drag_ghost_x - seg_width_px / 2.0)

# _commit_segment_drag, dòng ~5759
ghost_left_x = self._drag_ghost_x - seg_width_px / 2.0
```

Heuristic này giả định user luôn grab vào tâm segment — sai.

### Fix — pattern delta-based

Dùng cùng pattern mà `LayerBlockItem` đang dùng (xem Issue 2). Pattern này tự động bảo toàn grab point mà không cần lưu offset rõ ràng:

**Tại moment drag activates** (sau khi vượt ngưỡng 5px), lưu thêm 2 thông số vào `TimelinePanel`:

```python
# Trong TimelinePanel.__init__:
self._drag_press_scene_x: float = 0.0
self._drag_start_seg_start: float = 0.0
```

**Trong `TimelineView.mouseMoveEvent`**, khi activate drag, lưu lại 2 giá trị trên:

```python
# TimelineView.mouseMoveEvent, đoạn cross threshold (dòng ~2087-2096)
if not self._seg_drag_active:
    if abs(delta_px) < 5:
        super().mouseMoveEvent(event)
        return
    self._seg_drag_active = True
    if panel is not None:
        seg = panel._project.get_segment(seg_id) if panel._project else None
        if seg is not None:
            panel._drag_press_scene_x = press_x          # NEW
            panel._drag_start_seg_start = seg.start_time_sec  # NEW
        panel._drag_seg_id = seg_id
        panel._drag_ghost_x = scene_pos.x()
    self.setCursor(Qt.CursorShape.ClosedHandCursor)
```

**Sau đó 3 chỗ trên đổi từ `cursor - w/2` sang công thức delta-based:**

```python
# _commit_segment_drag (mới)
delta_px = self._drag_ghost_x - self._drag_press_scene_x
delta_t = delta_px / max(0.001, self._effective_pps)
new_start_t = max(0.0, self._drag_start_seg_start + delta_t)

# _paint_segment_blocks (mới) — vẽ ghost ở vị trí mới
delta_px = self._drag_ghost_x - self._drag_press_scene_x
ghost_left_scene_x = self._time_to_x(self._drag_start_seg_start) + delta_px
ghost_x = max(0.0, ghost_left_scene_x)  # clamp at 0

# _drag_insertion_x (mới)
delta_px = self._drag_ghost_x - self._drag_press_scene_x
ghost_left_scene_x = self._time_to_x(self._drag_start_seg_start) + delta_px
return max(0.0, ghost_left_scene_x)
```

### Boundary clamp

Khi user grab xa rìa trái (offset lớn) và kéo cursor về phía trái cho `new_start_t < 0`:

- Clamp `new_start_t = max(0.0, ...)` như cũ.
- Khi clamp đụng 0, cursor không còn nằm trên grab point — segment "kẹt" ở biên còn cursor "đi tiếp". Đây là hành vi chuẩn của mọi editor, user hiểu được.

Không cần thêm logic gì khác cho boundary.

### Reset state

Khi drag commit xong (cuối `_commit_segment_drag`), reset luôn:

```python
self._drag_seg_id = None
self._drag_press_scene_x = 0.0      # NEW
self._drag_start_seg_start = 0.0    # NEW
```

Tương tự reset ở các nhánh early-return trong `_commit_segment_drag` (khi project None, segment None, block None).

---

## Issue 2: Layer drag/resize đã đúng pattern

### Verify

`LayerBlockItem` (file `studio/editor/timeline_panel.py`, class ~dòng 946) hiện đã dùng pattern delta-based đúng:

```python
# LayerBlockItem.mousePressEvent (dòng 1008-1013)
self._drag_start_scene_x = event.scenePos().x()
...
self._drag_start_layer_start = layer.start_time_sec
self._drag_start_layer_end = layer.end_time_sec

# LayerBlockItem.mouseMoveEvent (dòng 1019-1042)
dx = event.scenePos().x() - self._drag_start_scene_x
dt = dx / max(0.001, self._panel._effective_pps)
...
if self._resize_edge == "left":
    new_start = max(0.0, self._drag_start_layer_start + dt)
    ...
elif self._resize_edge == "right":
    new_end = max(..., self._drag_start_layer_end + dt)
    ...
else:  # MOVE
    dur = self._drag_start_layer_end - self._drag_start_layer_start
    new_start = max(0.0, self._drag_start_layer_start + dt)
    layer.start_time_sec = new_start
    layer.end_time_sec = new_start + dur
```

Pattern này tự nhiên bảo toàn grab offset:

- Press tại scene_x = 550, layer start tại 200.
- Cursor di chuyển đến scene_x = 750 → dx = 200.
- `new_start = 200 + 200/pps = layer dịch phải đúng 200px tương đương`.

Cursor vẫn nằm ở vị trí tương đối ban đầu trên layer. Không có "nhảy".

### Không cần thay đổi mechanics

Layer drag/resize hiện đúng về offset. **Không cần sửa gì cho Issue 1.** Spec [`layer-resize-move-threshold-spec.md`](./layer-resize-move-threshold-spec.md) sẽ áp threshold-based commit (revert/snap), nhưng pattern delta-based hiện tại được giữ nguyên làm nền tảng.

### Vai trò làm reference cho segment fix

Pattern của `LayerBlockItem` chính là template cho segment fix ở Issue 1. Implementer nên đọc kỹ `LayerBlockItem.mouseMoveEvent` trước khi sửa segment drag để có cảm nhận đúng về "delta-based offset preservation".

---

## Issue 3: Cursor feedback chưa nhất quán

### Hiện trạng

**Segment drag** (`TimelineView`):

- Khi activate drag (vượt 5px): set `Qt.CursorShape.ClosedHandCursor` (dòng ~2096).
- Khi release: set `Qt.CursorShape.ArrowCursor` (dòng ~2137).

**Layer drag/resize** (`LayerBlockItem`):

- `hoverMoveEvent` (dòng ~1059-1065): set `SizeHorCursor` cho cạnh, `SizeAllCursor` cho thân khi hover.
- Trong khi drag thực tế: KHÔNG set cursor. Cursor giữ nguyên từ hover, hoặc bị Qt đổi sang gì đó tuỳ default.
- `hoverLeaveEvent`: `unsetCursor()`.

### Vấn đề

Layer drag không có cursor feedback rõ ràng trong lúc drag — user không biết đang ở chế độ move hay resize chỉ qua cursor (phải nhìn vị trí block). Khi drag ra khỏi vùng layer block (cursor đi sang scene area), cursor thậm chí đổi về Arrow vì hover handler không còn.

### Fix — chuẩn hóa

**Trong `LayerBlockItem.mousePressEvent`** (sau khi xác định `_resize_edge`):

```python
# Set cursor rõ ràng cho cả phiên drag (không phụ thuộc hover)
if self._resize_edge in ("left", "right"):
    self.setCursor(Qt.CursorShape.SizeHorCursor)
else:
    self.setCursor(Qt.CursorShape.ClosedHandCursor)
```

Lưu ý: `SizeAllCursor` (4 mũi tên) phù hợp khi item có thể di chuyển 2D, nhưng layer chỉ di chuyển 1D (theo trục thời gian) — dùng `ClosedHandCursor` đúng semantic hơn.

**Trong `LayerBlockItem.mouseReleaseEvent`** (đầu hàm):

```python
self.unsetCursor()
```

Để hover handler tiếp quản lại sau khi release.

**Trong `LayerBlockItem.hoverMoveEvent`** (dòng 1059-1065):

```python
# Đổi SizeAllCursor → OpenHandCursor cho thân (đối xứng với ClosedHand khi drag)
if pos.x() <= self.EDGE_HIT_W or pos.x() >= r.width() - self.EDGE_HIT_W:
    self.setCursor(Qt.CursorShape.SizeHorCursor)
else:
    self.setCursor(Qt.CursorShape.OpenHandCursor)
```

Pattern hover (open hand) → press (closed hand) là chuẩn UI cho draggable item.

### Đối với segment drag

Hiện đã đúng (`ClosedHandCursor` trong drag). Bổ sung `OpenHandCursor` khi hover:

**Trong `TimelineView.mouseMoveEvent`** (đoạn hover detection ~dòng 2113-2126), khi cursor nằm trên segment block và không có drag đang chạy:

```python
# Pseudocode
if hovering_over_segment_block and not self._dragging_playhead and not self._seg_drag_active:
    if cursor_in_resize_zone:  # Nếu sau này thêm resize cho segment
        self.setCursor(Qt.CursorShape.SizeHorCursor)
    else:
        self.setCursor(Qt.CursorShape.OpenHandCursor)
```

Hiện tại `SegmentRectItem` không có resize (xem comment dòng 882-885 — drag bị disable, segment fixed về duration). Nên chỉ cần `OpenHandCursor` cho hover thân.

### Bảng tổng hợp cursor

```
                      Segment block       Layer block
Hover thân:           OpenHandCursor      OpenHandCursor
Hover cạnh resize:    (n/a)               SizeHorCursor
Press + drag thân:    ClosedHandCursor    ClosedHandCursor
Press + drag cạnh:    (n/a)               SizeHorCursor
Release:              ArrowCursor         (unset, hover lại)
Khác / scene trống:   ArrowCursor         ArrowCursor
```

---

## Issue 4: Time tooltip trong khi drag

### Mục đích

Trong khi user kéo segment hoặc layer, hiện một tooltip nhỏ gần cursor (hoặc gần item đang kéo) cho biết thời gian hiện tại của item. Giúp user biết chính xác mình đang ở đâu mà không phải đoán theo ruler.

### Format tooltip

```
Segment drag:
    [start_time → end_time]    duration: X.XXs
    
    Ví dụ: [00:05.234 → 00:13.567]    duration: 8.33s

Layer move:
    [start_time → end_time]    duration: X.XXs
    
Layer resize rìa trái:
    start: 00:05.234    duration: 8.33s
    
Layer resize rìa phải:
    end: 00:13.567    duration: 8.33s
```

Format số: `MM:SS.mmm` (giây có 3 chữ số mili) hoặc fallback `XX.XXXs` cho time < 60s.

### Vị trí render

Tooltip nên render trong scene (không phải QToolTip native) để theo cursor mượt và không bị flicker. Có 2 lựa chọn:

**Option A: QGraphicsSimpleTextItem** thêm vào scene khi drag activates, set position sát cursor mỗi `mouseMoveEvent`, remove khi release.

**Option B: Vẽ trong `_paint_segment_blocks` / `_paint_layer_blocks`** (background pass) bằng `painter.drawText()` ở scene coordinate gần cursor.

Tôi đề xuất Option B vì đã có infrastructure paint sẵn. Thêm 1 helper:

```python
def _paint_drag_time_tooltip(self, painter: QPainter, item_kind: str) -> None:
    """Render time tooltip near cursor during drag.
    
    item_kind: "segment", "layer-move", "layer-resize-left", "layer-resize-right"
    """
    # Tính text dựa trên kind + state
    # Vẽ background hộp + text gần self._drag_ghost_x
```

Gọi từ `drawBackground` của `TimelineScene` sau khi vẽ ghost.

### Khi nào hiện

- Segment drag: khi `_seg_drag_active = True`.
- Layer drag/resize: khi `LayerBlockItem._drag_moved = True`.

Ẩn ngay khi release (state về False).

### Style

Hộp text nhỏ:

- Background: `rgba(0, 0, 0, 200)` (đen mờ)
- Text color: `#ffffff`
- Font: monospace (số dễ đọc, không nhảy width khi đổi giá trị)
- Padding: 4px ngang, 2px dọc
- Border radius: 3px
- Vị trí: cách cursor 16px sang phải + 8px lên trên (tránh che cursor)

### Nếu tooltip vượt rìa view

Auto-flip sang trái khi sát rìa phải view. Tương tự cho rìa trên/dưới.

---

## Optional — Issue H: Segment ripple-right không clamp end-of-project

### Triệu chứng

Sau drag, segment bị đẩy bởi ripple-right có thể có `end_time_sec` vượt qua tổng độ dài audio. Project hiện không có khái niệm "project length" rõ ràng nên không có chỗ clamp.

### Quyết định cần lấy

Có 3 cách:

(1) Để vậy, không clamp. Đơn giản nhất, không thay đổi logic. Risk: segment "ma" ở vùng ngoài audio, render có thể lỗi hoặc tạo black frame.

(2) Clamp end vào `max(audio_path.duration for all segments)` — tổng audio dài nhất. Đơn giản, có thể implement.

(3) Cảnh báo qua status bar khi ripple đẩy segment ra ngoài, vẫn cho commit. Linh hoạt nhất.

**Trong spec này không bao gồm fix cho H.** Đánh dấu là open question — bạn quyết định trước khi implement spec này. Nếu chọn (1) thì không cần làm gì. Nếu (2) hoặc (3) thì spec riêng.

---

## Test scenarios

### Test 1: Segment drag offset — grab giữa segment

```
Setup: segment A ở 0-10s. PPS = 50 (segment width 500px, từ scene_x=0 đến 500).
Action: click tại scene_x=250 (giữa segment), kéo đến scene_x=350.
Expect: segment dịch sang phải đúng 100px = 2s. New range: 2-12s.
Cursor vẫn ở giữa segment trong toàn bộ drag (visually).
```

### Test 2: Segment drag offset — grab rìa phải

```
Setup: segment A ở 0-10s, PPS = 50.
Action: click tại scene_x=480 (gần rìa phải), kéo đến scene_x=580.
Expect: 
- Khi vượt ngưỡng 5px (cursor=485): segment ở 1-11 (dịch +5px).
  Tuyệt đối KHÔNG nhảy về vị trí "cursor giữa segment".
- Cuối drag (cursor=580): segment ở 2-12s (dịch +100px).
- Cursor vẫn ở vị trí tương đối ~96% chiều rộng segment trong toàn drag.
```

### Test 3: Segment drag offset — grab rìa trái + kéo về 0

```
Setup: segment A ở 5-15s, PPS = 50 (scene_x 250-750).
Action: click tại scene_x=270 (cách rìa trái 20px), kéo cursor đến scene_x=10 (kéo về phía trái 260px).
Expect:
- delta_t = -260/50 = -5.2s.
- new_start = max(0, 5 + (-5.2)) = max(0, -0.2) = 0.
- Segment clamped tại 0-10s.
- Cursor ở scene_x=10 nhưng segment rìa trái ở scene_x=0 — không còn align (boundary case, chấp nhận).
```

### Test 4: Layer drag — grab offset bảo toàn (verify, không cần fix)

```
Setup: layer A ở 5-15s. PPS = 50.
Action: click thân layer tại scene_x=350 (offset 100px từ rìa trái), drag đến scene_x=400.
Expect: layer thành 6-16s (dịch +1s = +50px).
Cursor vẫn ở vị trí 100px tính từ rìa trái layer.
Đây là hành vi sẵn có, test này chỉ để regression check.
```

### Test 5: Layer resize — grab offset không có ý nghĩa cho cạnh

```
Setup: layer A ở 5-15s. Click cạnh phải tại scene_x=750.
Action: kéo đến scene_x=800.
Expect: layer.end_time_sec += 1s. Layer thành 5-16s.
Resize chỉ quan tâm delta của cursor, không có khái niệm "grab offset" trên cạnh.
Test này verify pattern delta-based hoạt động đúng cho resize.
```

### Test 6: Cursor feedback — segment drag

```
Setup: segment trên timeline.
Hover thân segment: cursor = OpenHandCursor.
Press: cursor giữ OpenHand.
Move > 5px (drag activates): cursor = ClosedHandCursor.
Release: cursor = ArrowCursor (hoặc OpenHand nếu vẫn hover trên segment).
Hover ra ngoài segment: cursor = ArrowCursor.
```

### Test 7: Cursor feedback — layer block

```
Hover thân layer: OpenHandCursor.
Hover cạnh layer (≤ 8px từ biên): SizeHorCursor.
Press thân: ClosedHandCursor (giữ trong toàn drag).
Press cạnh: SizeHorCursor (giữ trong toàn drag).
Release: unset, để hover handler tiếp quản.
Hover sang block khác hoặc scene trống: cursor đổi tương ứng.
```

### Test 8: Time tooltip — segment drag

```
Setup: segment 5-15s.
Bắt đầu drag.
Trong drag: tooltip hiện gần cursor, format "[00:05.234 → 00:15.234]    duration: 10.00s".
Số start/end cập nhật real-time theo cursor.
Release: tooltip biến mất ngay.
```

### Test 9: Time tooltip — layer resize rìa phải

```
Setup: layer 5-15s.
Drag rìa phải đến vị trí mới.
Tooltip hiện format "end: 00:17.500    duration: 12.50s".
Số cập nhật real-time.
Release: biến mất.
```

### Test 10: Tooltip auto-flip khi sát rìa phải view

```
Setup: drag segment đến gần rìa phải viewport.
Tooltip mặc định ở phía phải cursor.
Khi tooltip sẽ vượt rìa phải view → auto-flip sang phía trái cursor (giữ trong viewport).
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **Segment drag activation threshold (5px):** giữ nguyên trong `TimelineView.mouseMoveEvent`. Việc lưu `_drag_press_scene_x` xảy ra TẠI thời điểm activate, không phải tại press, để không phá flow click-không-drag hiện tại.

2. **`_seg_drag_pending` semantics:** `(seg_id, press_x)` — giữ nguyên. Spec chỉ thêm 2 attribute mới ở `TimelinePanel`, không thay đổi cấu trúc của `_seg_drag_pending`.

3. **Click-không-drag (single click on segment để select):** vẫn phải work. Khi user click segment và release mà không vượt 5px, `_seg_drag_active = False` → bỏ qua toàn bộ drag logic, không động gì đến segment position.

4. **Ctrl+click (join partner selection):** vẫn phải work. Logic `Ctrl+ClickModifier` ở `TimelineView.mousePressEvent` (dòng ~2059) chặn drag activation cho Ctrl+click. Giữ nguyên.

5. **Focus mode (drag disabled):** `panel._focus_segment_id is None` check (dòng ~2061) vẫn chặn drag khi đang ở focus mode. Giữ nguyên.

6. **Layer ownership detection trong `_commit_segment_drag`:** logic nhận diện layer "thuộc về" segment nào dựa trên pre-drag positions (dòng ~5803-5824). Không động vì đây là concern khác (Issue I tôi đã nêu, để spec sau).

7. **Ripple-right logic** (dòng ~5777-5784): giữ nguyên. Spec này không sửa ripple, chỉ sửa cách tính ghost position.

8. **Layer block hover EDGE_HIT_W = 8.0:** giữ nguyên.

9. **Layer block MIN_DURATION_SEC = 0.1:** giữ nguyên.

10. **Layer drag/resize threshold spec** (`layer-resize-move-threshold-spec.md`): hai spec phải compatible. Spec threshold sẽ thêm threshold-based commit/revert cho layer; spec này chỉ chuẩn hóa cursor/tooltip + verify offset đúng. Hai spec không xung đột — implement song song được.

11. **`_paint_segment_blocks` và `_paint_layer_blocks` infrastructure:** chỉ thêm code mới (paint tooltip + dùng công thức delta cho ghost), không phá structure hiện tại.

12. **Undo stack:** segment offset fix KHÔNG thay đổi gì về undo (segment drag đã có undo `"Move Segment"` ở dòng 5855). Layer cursor/tooltip cũng không tạo undo mới (vì không phải mutation persistant). Spec này không động đến undo.

---

## Pattern code hiện có để tham khảo

- **Delta-based offset (đúng):** `LayerBlockItem.mouseMoveEvent` dòng 1019-1042. Đây là template cho segment fix.

- **Cursor set/unset:**
  ```python
  self.setCursor(Qt.CursorShape.ClosedHandCursor)
  self.unsetCursor()
  ```

- **Scene paint pass:** `TimelineScene.drawBackground` (tìm trong file) gọi `_paint_segment_blocks`, `_paint_layer_blocks`, etc. Tooltip render thêm vào sau cùng.

- **Time format helper:** `format_seconds` (dòng ~54), `format_ruler_time` (dòng ~60). Có thể tái sử dụng hoặc viết helper riêng cho format tooltip.

- **Scene invalidate cho repaint:**
  ```python
  panel.scene.invalidate(
      panel.scene.sceneRect(),
      QGraphicsScene.SceneLayer.BackgroundLayer,
  )
  ```
  Đã được gọi mỗi mouseMoveEvent của cả segment drag (dòng ~2102-2107) và layer drag (dòng ~1031-1037). Tooltip sẽ tự repaint theo.

---

## Cleanup sau khi implement

1. **Xóa heuristic `cursor - w/2`** trong 3 chỗ ở `TimelinePanel`:
   - `_paint_segment_blocks` dòng ~4439
   - `_drag_insertion_x` dòng ~5713
   - `_commit_segment_drag` dòng ~5759

2. **Comment tại 3 chỗ trên** (nếu còn) ghi rõ "delta-based offset preservation, see drag-mechanics-offset-and-ergonomics-spec.md".

3. **Update comment trong `_commit_segment_drag`** mô tả thuật toán: ghost position = original_seg_left + cursor_delta.

---

## Thứ tự implement đề xuất

1. **Skeleton:** thêm 2 attribute mới vào `TimelinePanel.__init__` (`_drag_press_scene_x`, `_drag_start_seg_start`). Khởi tạo về 0.

2. **Segment offset fix — capture state** trong `TimelineView.mouseMoveEvent` (đoạn cross threshold). Verify không phá Test 6 (cursor feedback) và click-không-drag.

3. **Segment offset fix — apply công thức delta** trong 3 chỗ paint/insertion/commit. Test 1, 2, 3.

4. **Reset state** ở `_commit_segment_drag` cuối hàm và các nhánh early-return.

5. **Cursor feedback chuẩn hóa:**
   - `LayerBlockItem.mousePressEvent` set cursor theo `_resize_edge`.
   - `LayerBlockItem.mouseReleaseEvent` unset cursor.
   - `LayerBlockItem.hoverMoveEvent` đổi `SizeAllCursor` → `OpenHandCursor`.
   - `TimelineView` thêm OpenHand cho hover segment (nếu có infrastructure hover sẵn).
   - Test 6, 7.

6. **Time tooltip:**
   - Helper `_format_time_for_tooltip(seconds: float) -> str`.
   - Helper `_paint_drag_time_tooltip(painter, kind, ...)` trong `TimelinePanel`.
   - Gọi từ `_paint_segment_blocks` (khi `_drag_seg_id is not None`) và `_paint_layer_blocks` (khi có layer block đang drag — cần track `_active_layer_drag_id` mới).
   - Auto-flip logic.
   - Test 8, 9, 10.

7. **Smoke test toàn bộ:**
   - Segment select không drag (single click).
   - Ctrl+click segment (join partner).
   - Focus mode drag disabled.
   - Layer Edit / Duplicate / Delete menu.
   - Layer double-click zoom-to-layer.
   - Drag-drop từ Media Library lên layer track.
   - Toàn bộ phải work nguyên.

8. **Cleanup** comment và code dư.

---

## Open questions

(1) **Issue H (segment ripple end-of-project clamp)**: spec này đánh dấu open. Bạn quyết định trước implement: (a) để vậy, (b) clamp vào audio dài nhất, (c) chỉ status bar warning.

(2) **Hover cursor cho segment trong `TimelineView`**: hiện chưa có hover handler riêng cho segment block ở view level. Có cần thêm không, hay accept rằng segment hover không có cursor feedback (chỉ có khi drag)?

(3) **Tooltip có cần khi pan/scroll viewport trong lúc drag không**: Qt có thể auto-scroll khi cursor chạm rìa viewport. Tooltip cần follow cursor đúng — verify trong test.

(4) **Format tooltip MM:SS.mmm vs XX.XXXs**: chọn 1 hoặc cho phép cả 2 (ví dụ short cho < 60s, long cho ≥ 60s). Tôi đề xuất: < 60s dùng `X.XXXs`, ≥ 60s dùng `MM:SS.mmm`. Bạn thấy ổn?
