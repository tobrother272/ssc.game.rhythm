# Inspector Panel — Drag-Drop Media to Layer Config Spec

## Mục tiêu

User kéo file media từ Media panel (trái) **thả vào Inspector panel** (phải, đang hiện config của layer) để **auto-apply** media làm field tương ứng của layer:

- Đang config **Background layer** → kéo image vào → tự động set `bg_type = "image"` + `bg_image = path` + Type combo nhảy sang "Image".
- Cùng layer, kéo video vào → tự động set `bg_type = "video"` + `bg_video = path` + Type combo nhảy sang "Video".
- Cùng cho các layer kind khác (Floor, Start Gate, Side Rails, Countdown, Stickman) tùy field hỗ trợ.

Đường drag-drop hiện có chỉ chấp nhận drop xuống **timeline track** (`background_media_dropped_at`, `floor_media_dropped_at`, `start_gate_media_dropped_at`). Spec này thêm đường thứ 2: drop vào **Inspector panel** áp dụng cho layer đang chọn.

---

## Behavior table per layer kind

| Layer kind | Drop image | Drop video | Drop audio |
|---|---|---|---|
| **Background** | `bg_type=image`, `bg_image=path`, `bg_video=null` | `bg_type=video`, `bg_video=path`, `bg_image=null` | Reject + statusbar warn |
| **Floor** | `floor_panel_image=path` (giữ nguyên các field khác) | Reject + warn | Reject + warn |
| **Side Rails** | `rail_image=path` | Reject + warn | Reject + warn |
| **Start Gate** | `start_gate_type=image`, `start_gate_image=path`, `start_gate_video=null` | `start_gate_type=video`, `start_gate_video=path`, `start_gate_image=null` | Reject + warn |
| **Countdown** | Reject + warn | Reject + warn | `relax_countdown_audio_enabled=true`, `relax_countdown_audio_mode=file`, `relax_countdown_audio_file=path` |
| **Stickman** | Reject + warn ("Stickman doesn't accept media — edit position via spinboxes") | Reject + warn | Reject + warn |

**Reject case**: hiện status bar message ngắn, vd "Background accepts image/video only", overlay border đỏ flash 1s.

---

## Architecture

### Drop source (đã có sẵn)

`MediaLibraryPanel._MediaListView.startDrag` (file `studio/editor/media_library.py` dòng 36-45) đã set mime data:

```python
mime.setData(MEDIA_ID_MIME, str(media_id).encode("utf-8"))
```

`MEDIA_ID_MIME = "application/x-htstudio-media-id"`.

→ Mọi widget có thể trở thành drop target chỉ cần accept mime này, decode media_id, lookup MediaItem qua `project.get_media(media_id)`.

### Drop target (cần thêm)

Inspector panel hiện không accept drop. Cần:

**Option A — Section widgets accept drops**
- Mỗi Section (`_BackgroundSection`, `_FloorPanelSection`, `_StartGateSection`, etc.) implement `dragEnterEvent`, `dragMoveEvent`, `dropEvent`.
- Section biết kind của mình → handle drop tương ứng.

**Option B — Inspector container accepts drops, dispatch theo layer kind**
- `InspectorPanel._layer_section_container` accept drop.
- On drop, đọc `self._current_layer.kind` để dispatch logic tương ứng.

**Đề xuất Option A**: tách concern theo Section. Mỗi Section quản drop logic của chính nó. Dễ test, dễ extend.

### Helper function chung

Mỗi Section sẽ cần kiểm tra media kind. Tách helper:

```python
# studio/editor/inspector_drop_helper.py (file mới)

def get_media_from_drop(event, project) -> tuple[MediaItem, str] | None:
    """Extract MediaItem + kind ("image"|"video"|"audio") from drop event.
    Returns None if drop is not a valid media-id drop."""
    if not event.mimeData().hasFormat(MEDIA_ID_MIME):
        return None
    media_id = bytes(event.mimeData().data(MEDIA_ID_MIME)).decode("utf-8")
    media = project.get_media(media_id)
    if media is None:
        return None
    kind = str(getattr(media.kind, "value", media.kind)).lower()
    return media, kind
```

---

## Section drop implementation pattern

Mỗi Section widget thêm 4 method:

```python
class _BackgroundSection(QGroupBox):
    # ... existing __init__ ...
    
    def __init__(self, config, project, parent=None):
        super().__init__("Background", parent)
        self._project = project       # NEW: ref tới project để lookup media
        self.setAcceptDrops(True)     # NEW: enable drop
        # ... existing UI setup ...
    
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MEDIA_ID_MIME):
            event.acceptProposedAction()
            self._set_drop_highlight(True)
        else:
            event.ignore()
    
    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(MEDIA_ID_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()
    
    def dragLeaveEvent(self, event):
        self._set_drop_highlight(False)
    
    def dropEvent(self, event):
        from studio.editor.inspector_drop_helper import get_media_from_drop
        result = get_media_from_drop(event, self._project)
        self._set_drop_highlight(False)
        if result is None:
            event.ignore()
            return
        media, kind = result
        # Section-specific logic:
        self._apply_dropped_media(media, kind)
        event.acceptProposedAction()
    
    def _apply_dropped_media(self, media, kind):
        """Background: accept image + video, reject audio."""
        if kind == "image":
            self._type_cb.setCurrentIndex(self._type_index_for("image"))
            self._image_edit.set_value(media.source_path)
            self._video_edit.set_value("")
            self.changed.emit()
            self._notify("Background → image: " + media.display_name)
        elif kind == "video":
            self._type_cb.setCurrentIndex(self._type_index_for("video"))
            self._video_edit.set_value(media.source_path)
            self._image_edit.set_value("")
            self.changed.emit()
            self._notify("Background → video: " + media.display_name)
        else:  # audio
            self._notify_reject(f"Background does not accept audio.")
    
    def _set_drop_highlight(self, on: bool):
        """Visual: dashed border when drag hover."""
        if on:
            self.setStyleSheet("QGroupBox { border: 2px dashed #00aaff; }")
        else:
            self.setStyleSheet("")
    
    def _notify(self, msg: str):
        """Bubble to parent (Inspector → MainWindow → status bar)."""
        # Use Qt signal or parent().window().statusBar()
        ...
    
    def _notify_reject(self, msg: str):
        """Show reject feedback: red flash + status bar."""
        ...
```

---

## Visual feedback trong khi drag

### Drag hover state

Khi user kéo media qua Section:

```
QGroupBox {
    border: 2px dashed #00aaff;     /* cyan dashed border */
    background-color: rgba(0, 170, 255, 25);  /* faint cyan tint */
}
```

Khi drag rời (dragLeaveEvent) hoặc drop done: clear stylesheet về default.

### Reject animation

Khi drop loại media không hỗ trợ (vd audio drop vào Background):

```python
def _notify_reject(self, msg: str):
    # Flash red border 0.8s
    self.setStyleSheet("QGroupBox { border: 2px solid #ff4444; }")
    QTimer.singleShot(800, lambda: self.setStyleSheet(""))
    # Status bar message
    self._notify(msg)
```

---

## Touch points

### Files mới

1. **`studio/editor/inspector_drop_helper.py`**
   - Function `get_media_from_drop(event, project)` (helper chung).
   - Function `notify_drop_status(widget, msg, is_error=False)` (signal bubble lên Main).
   - Constants for highlight stylesheet.

### Files modified

#### 2. **`studio/editor/layer_edit_dialog.py`**

Cho 3 Section trong file này:

**`_BackgroundSection`** (~dòng 50-200):
- Constructor thêm `project` param.
- Thêm `setAcceptDrops(True)`.
- Thêm 4 event handlers (dragEnter/Move/Leave/dropEvent).
- Thêm `_apply_dropped_media(media, kind)` cho Background semantic.

**`_StickmanSection`** (~dòng 50-235):
- Constructor thêm `project` param.
- `setAcceptDrops(True)`.
- 4 event handlers.
- `_apply_dropped_media`: reject all media types (Stickman không accept media qua drop).

**`_CountdownSection`** (~dòng 242-460):
- Constructor thêm `project` param.
- `setAcceptDrops(True)`.
- 4 event handlers.
- `_apply_dropped_media`: chỉ accept audio → set audio fields. Reject image/video.

**`_StartGateSection`** (sẽ có sau khi apply layer-start-gate-spec):
- Constructor thêm `project` param.
- `setAcceptDrops(True)`.
- 4 event handlers.
- `_apply_dropped_media`: accept image + video, reject audio (giống Background).

#### 3. **`studio/editor/segment_config_panel.py`**

Cho 2 Section trong file này:

**`_FloorPanelSection`**:
- Constructor thêm `project` param.
- `setAcceptDrops(True)`.
- 4 event handlers.
- `_apply_dropped_media`: chỉ accept image → set `floor_panel_image=path`. Reject video/audio.

**`_SideRailSection`**:
- Constructor thêm `project` param.
- `setAcceptDrops(True)`.
- 4 event handlers.
- `_apply_dropped_media`: chỉ accept image → set `rail_image=path`. Reject video/audio.

#### 4. **`studio/editor/inspector_panel.py`**

`_make_section_for_layer` (dòng 368-429):
- Pass `project` ref vào mỗi Section constructor:
```python
elif kind == "background":
    return _BackgroundSection(cfg, self._project, self)
# ... tương tự cho các kind khác
```

Cần ensure `self._project` available (đã có qua `set_project()` đã tồn tại).

### Files KHÔNG đụng

- **Timeline drop logic** (`background_media_dropped_at`, etc.): giữ nguyên. 2 đường drop song song, không xung đột.
- **MediaLibraryPanel** drag source logic: giữ nguyên.
- **Layer model**: giữ nguyên.
- **Renderer**: không liên quan UI.

---

## Notify status bar pattern

Section không có direct access tới MainWindow's status bar. Dùng Qt signal bubble:

**Trong mỗi Section:**

```python
class _BackgroundSection(QGroupBox):
    changed = Signal()              # existing
    media_dropped = Signal(str, bool)   # NEW: (message, is_error)
    
    def _notify(self, msg: str):
        self.media_dropped.emit(msg, False)
    
    def _notify_reject(self, msg: str):
        self.media_dropped.emit(msg, True)
```

**Trong InspectorPanel khi tạo Section:**

```python
section = _BackgroundSection(cfg, self._project, self)
section.changed.connect(...)         # existing
section.media_dropped.connect(self._on_section_media_dropped)   # NEW
```

**`InspectorPanel._on_section_media_dropped`:**

```python
def _on_section_media_dropped(self, msg: str, is_error: bool):
    # Bubble to MainWindow via existing pattern
    main_window = self.window()
    if main_window and hasattr(main_window, "statusBar"):
        main_window.statusBar().showMessage(msg, 3000)
```

---

## Test scenarios

### Test 1: Drop image vào Background section

```
Setup: Inspector hiện _BackgroundSection.
Action: drag 9.jpg từ Media panel, thả vào Background config area.
Verify:
  - Type combo tự động nhảy sang "Image"
  - Image file path = absolute path tới 9.jpg
  - Video file path = "" (cleared)
  - Color field giữ nguyên
  - layer.config update qua debounce
  - Status bar: "Background → image: 9.jpg"
  - Live preview update với background image mới
```

### Test 2: Drop video vào Background section

```
Setup: Background section.
Action: drag 1.mp4 thả vào Background.
Verify: Type → "Video", Video path = 1.mp4 path, Image path cleared.
```

### Test 3: Drop audio vào Background → reject

```
Action: drag audioFull.MP3 thả vào Background.
Verify:
  - layer.config KHÔNG đổi
  - Border flash đỏ 0.8s
  - Status bar: "Background does not accept audio."
```

### Test 4: Drop image vào Floor → set floor_panel_image

```
Setup: chọn Floor layer trong timeline → Inspector hiện _FloorPanelSection.
Action: drag 9.jpg thả vào.
Verify:
  - Floor panel image field = path
  - Các field khác (color, opacity, chevron) giữ nguyên
  - Status bar: "Floor panel image set: 9.jpg"
```

### Test 5: Drop video vào Floor → reject

```
Action: drag 1.mp4 thả vào Floor section.
Verify: reject + status bar message.
```

### Test 6: Drop image vào Start Gate

```
Setup: Inspector hiện _StartGateSection.
Action: drag 6.mp4 thả vào.
Verify: start_gate_type → "video", start_gate_video = path.
```

### Test 7: Drop audio vào Countdown → enable audio + set file

```
Setup: Inspector hiện _CountdownSection.
Action: drag audioFull.MP3 thả vào.
Verify:
  - relax_countdown_audio_enabled = True
  - relax_countdown_audio_mode = "file"
  - relax_countdown_audio_file = path
  - Audio file widget visibility update (hiện ra nếu trước đang ẩn)
```

### Test 8: Drop image vào Stickman → reject

```
Setup: Inspector hiện _StickmanSection.
Action: drag image vào.
Verify: reject với message "Stickman doesn't accept media — edit position via spinboxes".
```

### Test 9: Drop image vào Side Rails → set rail_image

```
Action: drag 11.jpg thả vào _SideRailSection.
Verify: rail_image = path.
```

### Test 10: Visual feedback drag hover

```
Setup: drag bất kỳ media qua Section đang hỗ trợ kind đó.
Verify khi cursor hover trong Section bounds:
  - Border dashed cyan xuất hiện
  - Background tint cyan nhạt
Verify khi cursor rời Section:
  - Border + tint biến mất
```

### Test 11: Drop ngoài Section, không bị accept

```
Action: drag media qua Inspector container nhưng KHÔNG vào trong Section nào.
Verify: không có visual highlight, drop ignored.
```

### Test 12: Switch layer trong khi đang drag

```
Setup: đang drag media.
Action: hover qua timeline track của layer khác (chưa chọn) — đặc tính cũ trigger track drop.
Verify: track drop logic vẫn work (background_media_dropped_at signal).
Verify: section drop và track drop KHÔNG xung đột (mỗi cái target khác nhau).
```

### Test 13: Drop khi project chưa có media

```
Setup: project không có media nào.
Verify: không có gì để drag → không cần test edge case này.

OR Setup: media bị xóa khỏi project sau khi drag started.
Action: complete drop.
Verify: get_media_from_drop trả None → drop ignored, no crash.
```

### Test 14: Persistence sau drop

```
Action: drop image vào Background.
Action: save project → close → reopen.
Verify: layer.config['bg_image'] vẫn = path đã drop.
```

### Test 15: Undo

```
Action: drop image vào Background.
Verify: undo có push command để revert.
Action: Ctrl+Z.
Verify: layer.config restore về trạng thái trước drop.
```

(Nếu Inspector hiện chưa có undo support cho config edit, cần implement riêng — xem Open question.)

---

## Quan trọng: KHÔNG được phá vỡ

1. **Timeline drop signals** (`background_media_dropped_at`, `floor_media_dropped_at`, `start_gate_media_dropped_at`): giữ nguyên. Inspector drop là đường thứ 2 song song.

2. **MediaLibraryPanel drag source**: KHÔNG đụng. Mime type giữ `application/x-htstudio-media-id`.

3. **Section UI structure**: chỉ THÊM event handlers, không sửa form layout / fields hiện có.

4. **`changed` signal pattern**: drop logic emit `changed` để Inspector debounce commit như cũ.

5. **Layer model**: KHÔNG đụng. Drop chỉ update `layer.config` qua existing pipeline.

6. **Inspector debounce + edit-session compound undo**: drop nên trigger debounce như spinbox change. Tự động được nếu emit `changed`.

7. **`_on_layer_section_changed` flow**: drop → section emit `changed` → debounce → `layer.config = section.get_config()` → renderer update. Pattern existing.

8. **Multiple Section subclasses**: mỗi Section tự quản drop logic của mình. Không có super class chung (theo design hiện tại).

9. **Path format**: `media.source_path` là absolute path. Inspector path widgets accept absolute path. Không cần convert relative.

10. **Status bar message**: dùng existing pattern qua signal bubble. Không tạo notification system mới.

11. **Section stylesheet drop highlight**: dùng `setStyleSheet("QGroupBox { ... }")`, KHÔNG override các style khác. Khi clear, dùng `setStyleSheet("")` để reset.

---

## Pattern code hiện có để tham khảo

- **`MediaLibraryPanel._MediaListView.startDrag`** (`media_library.py` dòng 36-45): drag source pattern.
- **`TimelineView.dragEnterEvent` / `dropEvent`** (`timeline_panel.py` dòng 1875-1913): drop target pattern, mime check, decode media_id.
- **`MainWindow._on_background_media_dropped`** (`main_window.py` dòng 1480-1568): dispatch logic theo media kind, layer config update.
- **`_BackgroundSection`** trong `layer_edit_dialog.py`: pattern Section với type selector + visibility toggle.
- **`_PathBrowseWidget.set_value`** trong `segment_config_panel.py`: pattern set path từ external code (drop sẽ gọi).
- **Inspector `_commit_pending_edit`**: pattern apply config debounced.

---

## Thứ tự implement đề xuất

### Phase 1: Helper + Background

1. Tạo `inspector_drop_helper.py` với `get_media_from_drop()`.
2. Thêm `setAcceptDrops(True)` + 4 event handlers vào `_BackgroundSection`.
3. Implement `_apply_dropped_media` cho Background (image + video + reject audio).
4. Wire status bar notify qua signal.
5. Test 1, 2, 3.

### Phase 2: Floor + Side Rails (image-only)

6. Áp pattern tương tự cho `_FloorPanelSection` (image only).
7. `_SideRailSection` (image only).
8. Test 4, 5, 9.

### Phase 3: Start Gate + Countdown

9. `_StartGateSection` (image + video, sau khi spec layer-start-gate-spec đã apply).
10. `_CountdownSection` (audio only).
11. Test 6, 7.

### Phase 4: Stickman reject + visual feedback

12. `_StickmanSection` (reject all).
13. Polish visual feedback (dashed border, red flash).
14. Test 8, 10, 11.

### Phase 5: Edge cases + tests

15. Test 12 (concurrent timeline + inspector drop).
16. Test 13 (defensive null).
17. Test 14 (persistence).
18. Test 15 (undo, nếu có).

### Phase 6: Optional polish

19. Tooltip "Drop image/video here to set background" cho Section khi empty state.
20. Audio waveform thumbnail trong Countdown audio widget khi drop audio.

---

## Acceptance criteria

✓ Drag image từ Media panel → drop vào Background section → bg_type=image, bg_image=path  
✓ Drag video → drop vào Background → bg_type=video, bg_video=path  
✓ Drop audio vào Background → reject + status bar message + red flash  
✓ Drop image vào Floor → floor_panel_image=path  
✓ Drop image vào Start Gate → start_gate_type=image, start_gate_image=path  
✓ Drop audio vào Countdown → audio enabled + file set  
✓ Drop image vào Stickman → reject + helpful message  
✓ Visual highlight (cyan dashed border) khi drag hover  
✓ Live preview update sau drop (qua existing changed signal + debounce)  
✓ Timeline track drop vẫn work (không xung đột)  
✓ Persistence: layer.config saved correctly sau drop  

---

## Open questions

(1) **Drop vào empty area của Inspector** (chưa có Section hiện): silently ignore, hay show message "Select a layer first"?

(2) **Multi-file drop**: hiện chỉ support 1 media drop một lúc. Có cần support drop nhiều file (vd 3 image cùng lúc) không? V1 đề xuất 1 file only.

(3) **External file drop** (file từ Windows Explorer kéo vào, KHÔNG qua Media panel): có cần support không? Nếu có, cần auto-import file vào project trước khi apply. Phức tạp hơn. V1 đề xuất KHÔNG support, chỉ Media panel drop.

(4) **Replace-confirm**: nếu Background đã có image, drop video sẽ thay thế. Có cần confirm dialog không? V1 đề xuất KHÔNG, replace silently (user có Ctrl+Z nếu nhầm).

(5) **Clear field bằng cách drop "empty"**: ví dụ kéo một "trash" icon vào để clear field. Không có pattern hiện tại. V1 đề xuất KHÔNG.

(6) **Undo/Redo**: drop có push undo command không? Nếu Inspector hiện đã có compound undo cho config edit, drop cũng nên flow vào đó. Cần verify.

(7) **Drop indicator inside `_PathBrowseWidget`** (cụ thể vào field path thay vì cả Section): có hợp lý không? Hiện spec đặt drop target là cả Section. Có thể refine sau nếu user muốn precision.

(8) **Sound feedback**: có cần audio click confirm khi drop success không? V1 đề xuất KHÔNG (ít user expect).

(9) **Drop reject border color**: đỏ #ff4444 OK không, hay tone-down sang #cc6644?

(10) **Status bar message timeout**: 3000ms cho success, 4000ms cho reject (lâu hơn để user đọc kịp lý do)? Đề xuất tách riêng.
