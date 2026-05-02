# Layer Resize / Move + Fill Menu — Threshold-Based Override Spec

## Mục tiêu

Thay thế hành vi snap-to-fill hiện tại bằng pattern "intent commit" có ngưỡng 20%. Hành vi hiện tại quá hung hăng (chớm 1 pixel là expand full segment + xóa silent các layer khác, không undo được). Spec này áp dụng cho:

1. **Resize** (kéo cạnh trái/phải layer block) — threshold-based commit.
2. **Move** (kéo thân layer block) — threshold-based commit.
3. **Context menu — "Fill to next segment" / "Fill to previous segment"** — explicit action, không cần threshold, mở rộng layer 1 segment kế.

Cả 3 luồng đều dùng chung override rule cho layer cùng kind (trim partial, xóa fully contained), undo command, và status bar message.

---

## Hành vi hiện tại cần loại bỏ

**File:** `studio/editor/timeline_panel.py`

**Hàm cần thay đổi:** `TimelinePanel._on_layer_move_finished` (khoảng dòng 2507-2551)

**Vấn đề:**

1. Khối snap-to-fill (dòng ~2519-2545) chỉ cần layer overlap 1 pixel với segment là expand layer ra `[min(seg.start), max(seg.end)]` của TẤT CẢ segment overlap.
2. Code đồng thời xóa các layer cùng kind nằm trọn trong vùng mới — silent, không có thông báo, không có undo.
3. Không phân biệt move vs resize — cả hai đều đi qua cùng một path.
4. Không push undo command, nên `Ctrl+Z` không khôi phục được.

**Code chết cần dọn (optional):**

- `TimelinePanel._on_layer_moved` (dòng ~2497) — định nghĩa nhưng không nơi nào gọi.
- `TimelinePanel._compute_drag_insert_idx` (dòng ~5675), `_repack_segments` (dòng ~5715), `_sorted_others` (dòng ~5669), thuộc tính `_drag_insert_idx` — di sản từ implementation cũ, không còn dùng.

---

## Hành vi mới — Tổng quan

### Drag (resize hoặc move)

Khi user kéo (move hoặc resize) một `LayerBlockItem`, không tự động snap-fill nữa. Thay vào đó:

1. **Trong khi kéo** (live drag): layer follow cursor như bình thường, có visual feedback rõ ràng cho biết user đang ở trạng thái "sẽ commit" hay "sẽ revert".
2. **Khi release**: tính ngưỡng commit dựa trên độ vượt vào segment kế cận (= 20% chiều rộng segment đó). Nếu vượt threshold thì commit + snap, không vượt thì revert.
3. **Khi commit và có layer cùng kind trong vùng bị nuốt**: override (xóa nếu chứa trọn, trim nếu overlap một phần). Push undo command. Hiện status bar message.

### Context menu — Fill to next / previous segment

Right-click vào layer block → menu mới có 2 item:

- **Fill to next segment**: mở rộng `end_time_sec` đến cuối segment kế tiếp (theo quy tắc "segment có end nhỏ nhất nhưng > end hiện tại"). Không cần threshold. Vẫn áp override rule và undo.
- **Fill to previous segment**: đối xứng — mở rộng `start_time_sec` về đầu segment liền trước.

Các item disable đúng lúc nếu không có segment để fill.

---

## Threshold = 20% pure (không clamp)

Với mỗi segment kế cận đang bị cursor lấn vào:

```python
threshold_seconds = 0.20 * segment.duration_sec
```

Không có min/max clamp. Pure 20%. Tính lại mỗi frame trong lúc drag (vì PPS có thể đổi do zoom giữa drag).

---

## Hành vi cho RESIZE (kéo cạnh layer)

Đã biết hướng resize qua `LayerBlockItem._resize_edge` ("left" / "right" / None).

### RESIZE rìa phải (`_resize_edge == "right"`)

**Trong khi drag:**

- Layer right edge follow cursor.
- Tính các segment mà cursor đã lấn vào: với mỗi segment X có `X.start_time_sec >= original_layer.end_time_sec` (segment ở phía bên phải của layer ban đầu) và `cursor_x_in_seconds > X.start_time_sec`:
  - `penetration = min(cursor_x, X.end) - X.start`
  - `threshold = 0.20 * (X.end - X.start)`
  - X được "commit-armed" nếu `penetration >= threshold`
- Visual feedback (xem section "Visual Feedback" bên dưới).

**Khi release:**

- Đi qua các segment phía phải, từ gần đến xa.
- Segment cuối cùng được commit-armed → đặt `layer.end_time_sec = X.end_time_sec`.
- Nếu không có segment nào commit-armed → revert layer về kích thước trước drag.
- Áp dụng override rule cho layer cùng kind trong vùng `[old_layer.end, new_layer.end]`.

### RESIZE rìa trái (`_resize_edge == "left"`)

Đối xứng hoàn toàn:

- Tính các segment có `X.end_time_sec <= original_layer.start_time_sec` (phía bên trái), check `penetration` và `threshold` tương tự.
- Khi commit: `layer.start_time_sec = X.start_time_sec` (segment xa nhất bên trái mà commit-armed).
- Override rule áp cho vùng `[new_layer.start, old_layer.start]`.

### Edge cases cho RESIZE

- **Không có segment kế cận** (layer ở rìa cuối/đầu project): cho free resize, không snap, không threshold.
- **Resize làm layer < `MIN_DURATION_SEC` (0.1s)**: clamp tại `MIN_DURATION_SEC`.
- **Cursor chưa vượt biên segment hiện tại**: thuần resize trong segment hiện tại → revert (hoặc commit về biên hiện tại nếu user đã vượt biên cũ một chút).

---

## Hành vi cho MOVE (kéo thân layer, `_resize_edge is None`)

Layer giữ nguyên duration, toàn bộ range shift theo cursor.

### Trong khi drag

- Layer follow cursor; cả `start_time_sec` và `end_time_sec` shift đều theo `dx`.
- Track `original_layer_start = self._drag_start_layer_start` đã có sẵn ở `LayerBlockItem`.

### Khi release — luật threshold cho MOVE

Tính theo **rìa trái mới** của layer (`new_layer.start_time_sec`):

**Nếu MOVE sang phải** (cursor đi phải, `dx > 0`):

- Tìm các segment X có `X.start > original_layer.start` (segment nằm bên phải vị trí ban đầu của layer's left edge).
- Với mỗi X: `penetration = new_layer.start - X.start` (chỉ valid nếu `new_layer.start >= X.start`).
- `threshold = 0.20 * (X.end - X.start)`.
- X được commit-armed nếu `penetration >= threshold`.
- Segment XA NHẤT (rightmost) trong các X được commit-armed → snap `layer.start = X.start`, `layer.end = X.start + duration`.

**Nếu MOVE sang trái** (cursor đi trái, `dx < 0`): đối xứng.

- Tìm các segment X có `X.end <= original_layer.start` (segment nằm bên trái).
- `penetration = X.end - new_layer.start` (chỉ valid nếu `new_layer.start <= X.end`).
- `threshold = 0.20 * (X.end - X.start)`.
- Commit-armed nếu `penetration >= threshold`.
- Segment XA NHẤT (leftmost) commit-armed → snap `layer.start = X.start`.

**Nếu không có segment nào commit-armed**: revert layer về vị trí trước drag.

**Nếu không có segment kế cận để snap** (đã ở rìa cuối/đầu): revert.

### Edge cases cho MOVE

- **MOVE chỉ shift trong cùng segment hiện tại** (không crossing): luôn revert. Free positioning trong segment không support trong spec này (tránh layer lệch lung tung).
- **Layer width > segment width**: vẫn áp luật trên, layer mới có thể spans nhiều segment. Override rule sẽ xử lý.
- **Project rỗng**: không có segment để snap → revert.

---

## Override rule cho layer cùng kind

Sau khi xác định được `[new_layer.start, new_layer.end]`, duyệt qua các layer khác cùng kind (bỏ qua layer đang được kéo):

```python
for B in project.layers:
    if B.id == A.id:                # skip self
        continue
    if B.kind != A.kind:            # khác kind, không động
        continue

    overlap_start = max(new_layer.start, B.start_time_sec)
    overlap_end   = min(new_layer.end,   B.end_time_sec)

    if overlap_end <= overlap_start:
        continue                    # không overlap

    # Fully contained?
    if new_layer.start <= B.start_time_sec and B.end_time_sec <= new_layer.end:
        # Xóa B hoàn toàn
        deleted_layers.append(B)
    else:
        # Partial overlap → trim B
        if B.start_time_sec < new_layer.start:
            # B thò ra bên trái new_layer → cắt phải B
            new_B_end = new_layer.start
            trimmed_layers.append((B, B.start_time_sec, B.end_time_sec))
            B.end_time_sec = new_B_end
        else:
            # B thò ra bên phải new_layer → cắt trái B
            new_B_start = new_layer.end
            trimmed_layers.append((B, B.start_time_sec, B.end_time_sec))
            B.start_time_sec = new_B_start

        # Nếu sau trim B < MIN_DURATION_SEC → xóa luôn
        if (B.end_time_sec - B.start_time_sec) < LayerBlockItem.MIN_DURATION_SEC:
            deleted_layers.append(B)
            # (nếu trim đã apply thì cần undo trim trước khi delete trong cùng command)

# Apply deletes
for B in deleted_layers:
    project.layers.remove(B)
```

**Quan trọng:** chỉ áp luật override khi A thực sự commit move/resize. Nếu user kéo dưới threshold và revert, KHÔNG động đến layer B nào.

---

## Undo command

Push **một** `_Cmd` duy nhất vào `self.undo_stack` cho mỗi commit.

**Label:** `"Resize {kind} layer"` cho resize, `"Move {kind} layer"` cho move.

**Snapshot trước mutate:**

```python
old_state = {
    "a_id": A.id,
    "a_old_start": A.start_time_sec,
    "a_old_end": A.end_time_sec,
    "deleted_layers": [
        {"layer": copy.deepcopy(B), "index": project.layers.index(B)}
        for B in <fully-contained Bs>
    ],
    "trimmed_layers": [
        {"id": B.id, "old_start": B.start_time_sec, "old_end": B.end_time_sec}
        for B in <partially-overlapping Bs>
    ],
}
```

**`_undo`:**

- Restore `A.start_time_sec, A.end_time_sec`.
- Insert lại từng B đã xóa vào `project.layers` ở `index` cũ.
- Restore `start_time_sec, end_time_sec` cho từng B đã trim.
- `self.refresh()` + `self.layer_changed.emit()`.

**`_redo`:**

- Apply lại mutation: set A's new range, xóa lại các B đã delete, trim lại các B đã trim.
- `self.refresh()` + `self.layer_changed.emit()`.

Pattern tham khảo: xem `_do_delete_layer` (dòng ~2615), `_do_duplicate_layer` (dòng ~2641), `_commit_segment_drag` (dòng ~5736) — tất cả đều đã dùng pattern `self.undo_stack.push(_Cmd(label, _undo, _redo))`.

---

## Status bar message

Sau khi commit thành công, gọi message qua main window status bar.

`TimelinePanel` không có ref trực tiếp đến status bar, nên emit signal mới hoặc dùng `self.layer_changed` + `MainWindow` lấy info từ command. Cách đơn giản nhất: thêm signal `layer_replaced = Signal(int)  # số layer bị ảnh hưởng` và `MainWindow` connect để show message.

**Quy tắc:**

```
Không có B nào bị ảnh hưởng (deleted + trimmed = 0):
    Không hiện message.

Có B bị ảnh hưởng:
    n_affected = len(deleted_layers) + len(trimmed_layers)
    self.statusBar().showMessage(
        f"Resized {kind} layer; replaced {n_affected} layer(s)",
        3000  # ms
    )
```

(Cho `move`: dùng `"Moved {kind} layer; replaced {n_affected} layer(s)"`.)

---

## Visual feedback trong khi drag

Đây là phần mấu chốt để pattern không gây bất ngờ. Cần repaint mỗi mouseMoveEvent.

### State tính toán mỗi frame

```python
class _ThresholdState:
    cursor_in_segment_id: Optional[str]   # segment cursor đang nằm trong
    will_commit_to_segment_id: Optional[str]  # segment xa nhất sẽ commit nếu release ngay
    affected_layers_b_ids: list[str]      # các B sẽ bị xóa/trim nếu commit
```

### Vẽ ghost layer (layer A đang kéo)

```
Cursor chưa vượt threshold của bất kỳ segment kế nào:
    fill alpha = 0.4
    border = QPen("#cccccc", 1, DashLine)
    tooltip near cursor: "Release to revert"

Cursor đã vượt threshold ít nhất 1 segment:
    fill alpha = 0.8
    border = QPen("#00e5ff", 2, SolidLine)  # cyan, giống insertion indicator hiện tại
    tooltip near cursor: 
        nếu affected_layers_b_ids rỗng:  "Release to fill {seg_name}"
        nếu có:                          "Release — replaces {n} layer(s)"
```

### Vẽ các B sẽ bị ảnh hưởng

Trong vòng paint của `_paint_layer_blocks`, kiểm tra mỗi B layer:

```
Nếu B.id in affected_layers_b_ids và will_commit_to_segment_id is not None:
    Nếu B fully contained in (committed range):
        fill alpha = 0.2
        border = QPen("#ff4444", 1.5, DashLine)  # đỏ
        vẽ chữ "✕" hoặc icon trash giữa block
    Nếu B partial overlap:
        fill alpha = 0.5
        border = QPen("#ff8800", 1.5, DashLine)  # cam
        vẽ vạch dọc tại đường cắt (B.new_start hoặc B.new_end) — đường này màu cam đậm

Nếu cursor rút lùi lại không còn vượt threshold:
    B trở về visual bình thường
```

### Cập nhật mỗi frame

Trong `LayerBlockItem.mouseMoveEvent` hiện đã invalidate scene background mỗi tick — cách này vẫn dùng được. Bổ sung lưu `_ThresholdState` vào panel (`self._panel._drag_threshold_state`) để paint pass đọc.

---

## Context menu — Fill to next / previous segment

### Vị trí thêm

**File:** `studio/editor/timeline_panel.py`

**Hàm:** `TimelinePanel._on_layer_block_context_menu` (khoảng dòng 2575-2593).

Menu hiện tại: `Edit…` / `Duplicate` / `---` / `Delete`. Bổ sung 2 item mới và 1 separator giữa nhóm sửa và nhóm fill:

```
Edit…
Duplicate
---
Fill to previous segment    (tiền tệ tay khi không khả dụng)
Fill to next segment        (tiền tệ tay khi không khả dụng)
---
Delete
```

### Logic "next segment" — định nghĩa duy nhất

Sau khi đã loại bỏ A/B/C diễn giải mơ hồ, dùng quy tắc thống nhất:

> **Next segment** = segment có `end_time_sec` nhỏ nhất nhưng `> A.end_time_sec`.
>
> **Previous segment** = segment có `start_time_sec` lớn nhất nhưng `< A.start_time_sec`.

Quy tắc này tự nhiên xử lý cả 2 case:

- Layer phủ trọn segment → fill sang segment kế.
- Layer phủ dở dang một segment (overlap một phần) → hoàn thiện segment đó trước khi sang segment kế.

### Pseudo code

```python
def _find_next_segment_for_fill(self, layer):
    """Segment có end nhỏ nhất nhưng > layer.end_time_sec. None nếu không có."""
    if self._project is None:
        return None
    candidates = [
        s for s in self._project.segments
        if s.end_time_sec > layer.end_time_sec
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.end_time_sec)

def _find_previous_segment_for_fill(self, layer):
    """Segment có start lớn nhất nhưng < layer.start_time_sec. None nếu không có."""
    if self._project is None:
        return None
    candidates = [
        s for s in self._project.segments
        if s.start_time_sec < layer.start_time_sec
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.start_time_sec)
```

### Hành vi khi click "Fill to next segment"

```python
def _do_fill_layer_to_next_segment(self, layer_id):
    layer = self._get_layer(layer_id)
    if layer is None:
        return
    next_seg = self._find_next_segment_for_fill(layer)
    if next_seg is None:
        return  # menu item lẽ ra đã disable, defensive

    new_end = next_seg.end_time_sec
    if new_end <= layer.end_time_sec:
        return  # nothing to do

    # Snapshot để undo
    old_end = layer.end_time_sec
    new_range = (layer.start_time_sec, new_end)

    # Apply override rule cho vùng [old_end, new_end] với layer cùng kind
    deleted, trimmed = self._apply_override_rule(layer, new_range)

    # Mutate
    layer.end_time_sec = new_end

    # Push undo (label: "Fill {kind} layer to next segment")
    # Status bar message nếu deleted+trimmed > 0
    # refresh + emit layer_changed (deferred qua QTimer.singleShot(0, ...))
```

`_do_fill_layer_to_previous_segment` đối xứng:

- Tìm `prev_seg` theo `_find_previous_segment_for_fill`.
- `new_start = prev_seg.start_time_sec`.
- Override vùng `[new_start, old_start]`.
- Mutate `layer.start_time_sec = new_start`.

### Khi nào disable menu item

```python
# Khi build menu trong _on_layer_block_context_menu:
fill_next_act = menu.addAction("Fill to next segment")
fill_prev_act = menu.addAction("Fill to previous segment")

next_seg = self._find_next_segment_for_fill(layer)
prev_seg = self._find_previous_segment_for_fill(layer)

fill_next_act.setEnabled(next_seg is not None)
fill_prev_act.setEnabled(prev_seg is not None)

if next_seg is None:
    fill_next_act.setToolTip("No segment after this layer")
if prev_seg is None:
    fill_prev_act.setToolTip("No segment before this layer")
```

### Override rule áp y nguyên

Cùng logic ở section "Override rule cho layer cùng kind" — apply cho range bị mở rộng:

- Fill to next: range affected = `[layer.end_time_sec_old, layer.end_time_sec_new]`.
- Fill to previous: range affected = `[layer.start_time_sec_new, layer.start_time_sec_old]`.

Cả 2 đều: B fully contained → xóa, B partial overlap → trim, B sau trim < `MIN_DURATION_SEC` → xóa.

### Undo command

Cùng pattern với resize/move. Label:

- `"Fill {kind} layer to next segment"`
- `"Fill {kind} layer to previous segment"`

Snapshot:

```python
{
    "a_id": layer.id,
    "a_old_start": layer.start_time_sec,    # giữ cùng dạng với resize/move
    "a_old_end": layer.end_time_sec,
    "deleted_layers": [...],
    "trimmed_layers": [...],
}
```

`_undo` / `_redo` hoàn toàn giống resize/move — restore A range cũ + restore deleted Bs + restore trimmed Bs.

### Status bar message

```
Không có B affected:
    "Filled {kind} layer to next segment"   (3000ms)

Có B affected:
    n = len(deleted) + len(trimmed)
    "Filled {kind} layer to next segment; replaced {n} layer(s)"   (3000ms)
```

Tương tự cho previous.

### Edge cases riêng cho menu

**Layer đã phủ tới rìa cuối segment cuối cùng**: `_find_next_segment_for_fill` trả về None → menu item disable.

**Layer đã phủ tới rìa đầu segment đầu tiên**: `_find_previous_segment_for_fill` trả về None → menu item disable.

**Project không có segment nào**: cả 2 menu item disable.

**Layer overlap với segment kế nhưng không phủ trọn nó** (vd layer 0-7s, S2 = 5-10s): `_find_next_segment_for_fill` vẫn trả về S2 (S2.end = 10 > 7). `new_end = 10`. Layer thành 0-10s. Đây chính là case "hoàn thiện segment dở dang" — đúng ý đồ.

**Click "Fill to next segment" liên tục**: mỗi click commit thêm 1 segment kế. Mỗi click là 1 undo step độc lập.

**Layer đang fill có cùng kind với một B đã spans nhiều segment**: B sẽ bị trim hoặc xóa theo override rule — y như resize. Không có exception.

### Không cần threshold, không cần visual preview

Đây là explicit action (user chủ động click menu), không có "thử rồi rút lại" như drag. Cứ commit ngay, dựa vào undo để khôi phục nếu sai.

---

## Edge cases tổng hợp

**Layer ở rìa project (không có segment kế).** Cho free resize/move; threshold không áp dụng; không snap. Vẫn được commit (không revert), nhưng không có override.

**Threshold tính trên segment có duration rất nhỏ (vd 0.5s).** 20% = 0.1s = `MIN_DURATION_SEC`. Vẫn áp đúng, không cần special case.

**Threshold tính trên segment rất dài (vd 60s).** 20% = 12s. User phải kéo 12s mới commit. **Đây là chủ ý theo quyết định của bạn (pure 20%, không clamp)**. Nếu sau này muốn dễ hơn thì add clamp sau.

**Layer width > tổng width của tất cả segment.** Khi MOVE sang phải, layer's right edge có thể tràn ra ngoài segment cuối. Cho phép, không clamp right edge. Nhưng commit chỉ tính trên left edge.

**Multiple segment cross trong 1 drag.** Đã spec trong section RESIZE/MOVE: progressive — commit segment xa nhất mà threshold thoả mãn.

**Drag rất nhanh, cursor nhảy qua nhiều segment giữa 2 mouseMoveEvent.** Không sao, mỗi mouseMoveEvent tính lại từ đầu dựa trên `_drag_start_layer_*` và `event.scenePos()` hiện tại.

**Project chỉ có 1 segment, layer phủ trọn segment đó.** RESIZE: không có segment kế → free resize. MOVE: không có segment để snap → revert.

**Resize làm overlap với chính layer A?** Không xảy ra (A chỉ là 1 layer, không tự overlap).

---

## Kiểm thử (test scenarios)

### Test 1: RESIZE rìa phải dưới threshold → revert

```
Setup: layer A phủ S1 (0-5s). Segments: S1(0-5), S2(5-10).
Action: kéo rìa phải A đến 5.5s (lấn S2 0.5s = 10% < 20%).
Release.
Expect: A trở về 0-5s. Không có undo command push. Không có status message.
```

### Test 2: RESIZE rìa phải vượt threshold → commit fill S2

```
Setup: layer A phủ S1 (0-5s). S1, S2 như trên.
Action: kéo rìa phải đến 7s (lấn S2 2s = 40% > 20%).
Release.
Expect: A thành 0-10s. Undo stack có command "Resize {kind} layer". Status bar không hiện (không có B).
```

### Test 3: RESIZE vượt threshold + có B fully contained → xóa B

```
Setup: A phủ S1 (0-5s). B (cùng kind A) phủ S2 (5-10s). S1, S2 như trên.
Action: kéo rìa phải A đến 7s.
Release.
Expect: A thành 0-10s. B đã bị remove khỏi project.layers. Undo command snapshot có B (deepcopy) ở deleted_layers. Status bar: "Resized {kind} layer; replaced 1 layer(s)".
Ctrl+Z → A về 0-5s, B được restore vào project.layers.
```

### Test 4: RESIZE + B partial overlap → trim B

```
Setup: A phủ S1 (0-5s). B (cùng kind) ở 7-15s (spans S2 phần sau + S3). Segments: S1(0-5), S2(5-10), S3(10-15).
Action: kéo rìa phải A đến 12s (lấn S3 2s = 40% > 20%).
Release.
Expect: A thành 0-15s. B bị trim, B.start_time_sec = 15s — nhưng B.end_time_sec = 15s, duration = 0 → xóa luôn.
Hoặc nếu B = 7-20s ban đầu: A thành 0-15, B trim thành 15-20s, vẫn tồn tại.
Status bar: "Resized {kind} layer; replaced 1 layer(s)".
```

### Test 5: MOVE dưới threshold → revert

```
Setup: A phủ S2 (5-10s). Segments: S1(0-5), S2(5-10), S3(10-15).
Action: kéo thân A sang phải, layer thành 6-11s (left edge lấn S3 1s = 20% chính xác).
Release.

Tuỳ chọn so sánh '>' vs '>=' — spec dùng >= nên 20% chính xác commit.

Nếu kéo đến 5.8-10.8s (lấn S3 0.8s = 16%): revert về 5-10s.
Nếu kéo đến 6.5-11.5s (lấn S3 1.5s = 30%): commit. A snap to 10-15s (S3.start = 10).
```

### Test 6: MOVE crossing 2 segments

```
Setup: A phủ S2 (5-10s). Segments: S1(0-5), S2(5-10), S3(10-15), S4(15-20).
Action: kéo thân A sang phải đến 17-22s (left edge ở 17, lấn S4 2s = 40%).
Release.
Expect: A snap to S4.start = 15. A becomes 15-20s.
```

### Test 7: MOVE sang trái symmetric

```
Setup: A phủ S2 (5-10s). 
Action: kéo thân sang trái, layer thành 1-6s (left edge ở 1, lùi vào S1 1s + còn 4s đến S1.start... thực ra cần check penetration đúng cách).

Lại tính: original A.start = 5, S1 (0-5). Layer mới A.start = 1.
S1.end = 5 >= original A.start (5) → S1 là candidate.
penetration = S1.end - new_A.start = 5 - 1 = 4s.
threshold = 0.20 * 5 = 1s.
4 >= 1 → commit. snap A.start = S1.start = 0. A becomes 0-5s.
```

### Test 8: MOVE chỉ shift trong cùng segment → revert

```
Setup: A phủ một phần S2 (vd 6-8s). 
Action: kéo thân A đến 7-9s. Cả original và new đều trong S2, không cross.
Release.
Expect: revert về 6-8s.
```

### Test 9: Visual feedback states

```
Setup: A phủ S1, B (cùng kind) phủ S2.
Action: bắt đầu kéo rìa phải A từ 5s.

State 1 (cursor ở 5.5s, lấn S2 = 10% < 20%):
- Ghost A: alpha 0.4, viền xám đứt
- B: visual bình thường
- Tooltip: "Release to revert"

State 2 (cursor ở 7s, lấn S2 = 40% >= 20%):
- Ghost A: alpha 0.8, viền cyan
- B: alpha 0.2, viền đỏ đứt, dấu ✕
- Tooltip: "Release — replaces 1 layer(s)"

State 3 (cursor lùi về 5.3s, lấn 6%):
- Trở lại State 1
```

### Test 10: Undo / Redo round-trip

```
Setup: bất kỳ commit nào ở Test 3 / 4.
Ctrl+Z: phải khôi phục đúng nguyên trạng pre-drag (A range cũ, B layers restored).
Ctrl+Shift+Z (Redo): phải apply lại đúng mutation (A range mới, B layers deleted/trimmed).
```

### Test 11: Fill to next segment — case cơ bản

```
Setup: layer A phủ S1 (0-5s). Segments: S1(0-5), S2(5-10), S3(10-15).
Action: right-click A → "Fill to next segment".
Expect: A thành 0-10s. Undo command "Fill {kind} layer to next segment". Status bar: "Filled {kind} layer to next segment".
```

### Test 12: Fill to next segment — hoàn thiện segment dở dang

```
Setup: A phủ 0-7s (overlap S1 + 2s đầu của S2). Segments: S1(0-5), S2(5-10), S3(10-15).
Action: right-click → "Fill to next segment".
Expect: A thành 0-10s (next_seg = S2 vì S2.end=10 > A.end=7). 
Click lần nữa → A thành 0-15s (next_seg = S3).
Mỗi click là 1 undo step.
```

### Test 13: Fill to next segment + override B

```
Setup: A phủ S1 (0-5s). B (cùng kind) phủ S2 (5-10s). 
Action: right-click A → "Fill to next segment".
Expect: A thành 0-10s. B bị xóa (fully contained). Undo command snapshot có B. Status bar: "Filled {kind} layer to next segment; replaced 1 layer(s)".
Ctrl+Z: A về 0-5s, B restored.
```

### Test 14: Fill to previous segment — symmetric

```
Setup: A phủ S2 (5-10s). Segments: S1(0-5), S2(5-10), S3(10-15).
Action: right-click → "Fill to previous segment".
Expect: A thành 0-10s. prev_seg = S1 (S1.start=0 < A.start=5). Status: "Filled {kind} layer to previous segment".
```

### Test 15: Fill to next/previous — disable state

```
Setup A: layer A phủ 0-15s (toàn bộ project). Segments: S1(0-5), S2(5-10), S3(10-15).
Right-click A:
- "Fill to next segment" → disabled, tooltip "No segment after this layer"
- "Fill to previous segment" → disabled, tooltip "No segment before this layer"

Setup B: layer A phủ S3 (10-15s).
Right-click A:
- "Fill to next segment" → disabled
- "Fill to previous segment" → enabled (sẽ fill thành 5-15s)

Setup C: project rỗng (không segment nào).
Cả 2 menu item disabled.
```

### Test 16: Fill to previous + B partial overlap → trim

```
Setup: A phủ S3 (10-15s). B (cùng kind) phủ 0-7s (S1 + 2s đầu S2). Segments: S1(0-5), S2(5-10), S3(10-15).
Action: right-click A → "Fill to previous segment".
Expect: prev_seg = S2 (S2.start=5 < A.start=10, lớn nhất trong các candidate). 
A thành 5-15s.
B (0-7s) overlap với A's new range (5-15s) ở vùng 5-7s.
B không fully contained → trim: B.end = 5 (cắt phải B). B sau trim = 0-5s.
Status: "Filled {kind} layer to previous segment; replaced 1 layer(s)".
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **Phím tắt và menu hiện có** vẫn phải work: Edit / Duplicate / Delete trong context menu (`_on_layer_block_context_menu`), Delete key xóa selected layer (line ~2170-2174). Khi thêm "Fill to next/previous segment" KHÔNG được làm hỏng 3 item cũ — chèn vào giữa group sửa (Edit/Duplicate) và group xóa (Delete), giữ nguyên thứ tự và xử lý handler của 3 item cũ.
2. **Inspector selection** khi single-click layer block: vẫn gọi `_on_layer_block_clicked` ngay trong `mousePressEvent` (đã có ở dòng 1002-1003). Không thay đổi.
3. **Double-click zoom-to-layer** trong `_on_layer_block_double_clicked`: không động.
4. **Hover cursor** (SizeHorCursor cho cạnh, SizeAllCursor cho thân): giữ nguyên trong `hoverMoveEvent`.
5. **Resize edge detection** dùng `EDGE_HIT_W = 8.0`: giữ nguyên.
6. **MIN_DURATION_SEC = 0.1**: giữ nguyên, áp dụng làm clamp dưới cho cả layer A và B sau trim.
7. **Pattern defer refresh qua `QTimer.singleShot(0, ...)`** trong `_on_layer_move_finished` hiện tại: phải giữ vì lý do Qt event ordering (xem comment trong code dòng 2510-2513). Bất kỳ refresh nào sau commit cũng phải defer như cũ.
8. **Drag-drop từ Media Library** (`media_dropped_at` / `background_media_dropped_at` / `floor_media_dropped_at` signals): KHÔNG đi qua flow này. Giữ nguyên `_on_background_media_dropped` và `_on_floor_media_dropped` trong `main_window.py`.
9. **Segment drag** (`_commit_segment_drag` ở `TimelinePanel`): hoàn toàn riêng biệt với layer drag, không động.

---

## Pattern code hiện có để tham khảo

- **Push undo command:** xem `_do_delete_layer` dòng 2615-2640. Pattern:
  ```python
  def _undo(): ...
  def _redo(): ...
  self.undo_stack.push(_Cmd(label, _undo, _redo))
  ```

- **Refresh sau mutation:** `self.refresh()` + `self.layer_changed.emit()`.

- **Defer qua QTimer:** `QTimer.singleShot(0, _do_refresh)` — xem `_on_layer_move_finished` dòng 2547-2551.

- **Status bar:** access qua `self.statusBar().showMessage(text, ms)` từ `MainWindow`. `TimelinePanel` cần emit signal mới để báo lên main_window.

- **Scene invalidate cho live repaint:**
  ```python
  self._panel.scene.invalidate(
      self._panel.scene.sceneRect(),
      QGraphicsScene.SceneLayer.BackgroundLayer,
  )
  ```

- **`_paint_layer_blocks`** trong `TimelinePanel` (tìm hàm này) — nơi vẽ visual cho layer blocks. Visual feedback của ghost A và B affected sẽ thêm vào pass này.

---

## Cleanup sau khi implement

Sau khi spec này được implement và tested:

1. Xóa khối snap-to-fill cũ trong `_on_layer_move_finished` (dòng 2519-2545).
2. Xóa `_on_layer_moved` (dòng 2497) — code chết.
3. Xóa `_compute_drag_insert_idx`, `_repack_segments`, `_sorted_others`, `_drag_insert_idx` — code chết từ implementation cũ.
4. Cập nhật comment ở dòng 2458-2462 (mô tả `_drag_insert_idx`) cho đúng với code mới.

---

## Thứ tự implement đề xuất

1. **Skeleton:** thêm các attribute mới vào `TimelinePanel.__init__` (`_drag_threshold_state`, etc.). Stub các helper function (`_compute_threshold_state`, `_apply_layer_commit_with_override`, `_find_next_segment_for_fill`, `_find_previous_segment_for_fill`).
2. **Override rule helper** (`_apply_override_rule(layer, new_range)` trả về `(deleted, trimmed)`) — dùng chung cho cả 3 luồng (resize, move, fill menu). Tách ra trước để không lặp code.
3. **Context menu Fill to next/previous segment** (test 11, 12, 13, 14, 15, 16) — đây là luồng đơn giản nhất (không có drag, không có visual feedback, chỉ explicit action). Implement trước để verify override rule + undo + status bar message hoạt động đúng. Sau đó tái sử dụng pattern cho drag.
4. **Logic threshold + commit cho RESIZE rìa phải** (test 1, 2). Chưa cần visual feedback.
5. **Override rule integration vào RESIZE** (test 3, 4). Push undo. Status bar message. Tái dùng helper bước 2.
6. **RESIZE rìa trái** (đối xứng).
7. **MOVE** (test 5, 6, 7, 8).
8. **Visual feedback** (test 9) — chỉ áp cho drag, không cho menu.
9. **Cleanup code chết.**
10. **Smoke test toàn bộ:** Edit / Duplicate / Delete menu, Fill next/previous menu, double-click zoom, drag-drop từ Media Library, segment drag — phải work nguyên.
