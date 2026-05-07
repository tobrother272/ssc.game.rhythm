# Layer Start Gate Spec

## Mục tiêu

Thêm một layer kind mới — **`start_gate`** — biểu diễn cổng xuất phát ở đầu xa của floor, nơi các block bắt đầu xuất hiện rồi chạy về phía màn hình. Cổng có thể fill bằng image, video hoặc màu solid (giống pattern của background layer).

Tham khảo screenshot user gửi: cổng dạng scaffold/khung kim loại bao quanh một "màn hình" hiển thị logo "HOLE IN THE WALL" — sát với mép xa của floor (gần horizon). Cổng đứng yên, các block chạy ra từ cổng về camera.

Spec này độc lập với:

- [`layer-countdown-fixes-spec.md`](./layer-countdown-fixes-spec.md) — fix bugs cho countdown layer.
- [`layer-countdown-border-glow-spec.md`](./layer-countdown-border-glow-spec.md) — feature border + glow cho countdown.
- [`layer-resize-move-threshold-spec.md`](./layer-resize-move-threshold-spec.md) — threshold-based snap-to-fill cho layer.
- [`drag-mechanics-offset-and-ergonomics-spec.md`](./drag-mechanics-offset-and-ergonomics-spec.md) — segment offset + cursor + tooltip.

Vì `start_gate` là kind hoàn toàn mới (không có sẵn), spec này touch vào nhiều chỗ hơn (~12 touch points) so với các spec feature khác chỉ thêm field vào kind đã có.

---

## Thiết kế khái niệm

### Hành vi

`start_gate` là layer **visual-only** — không thay đổi logic spawn / movement của block. Cụ thể:

- Cổng đứng yên ở một vị trí cố định trong frame (toạ độ normalized 0..1).
- Mặc định đặt sát đường horizon, căn giữa theo trục X.
- Renders **trước floor panels và blocks** (z-order: nằm sau block, trước background).
- Có thể fill bằng:
  - **Color** — solid color (mặc định, fallback khi không có media).
  - **Image** — ảnh tĩnh, scale fit vào rect cổng.
  - **Video** — video loop / sync với timeline, scale fit vào rect cổng.

### Render order trong main loop

```
Background (segment background layer)
  ↓
Start Gate (mới)        ← rendered HERE
  ↓
Floor panels (perspective lanes)
  ↓
Side rails
  ↓
Blocks (RelaxTarget, PunchTarget, …)
  ↓
Stickman HUD
  ↓
Countdown HUD
```

Đặt sau background, trước floor: cổng "đứng" trong scene 3D nhưng vẽ 2D, không bị floor che.

### Frame visual

Phiên bản V1 (spec này): cổng là một rectangle 2D, fill bằng image/video/color. **Không vẽ frame scaffold riêng** — nếu user muốn frame kim loại như screenshot, họ chuẩn bị image/video PNG có sẵn frame + transparency. Cách này linh hoạt nhất cho V1, không hardcode style frame.

V2 tương lai có thể thêm frame style configurable (scaffold / bar / none + color), nhưng ngoài scope spec này.

---

## Config keys (9 keys)

```
start_gate_enabled     : bool                      default True
start_gate_type        : "color" | "image" | "video"   default "color"
start_gate_color       : hex string                default "#1a1a1a"
start_gate_image       : path string (optional)    default ""
start_gate_video       : path string (optional)    default ""
start_gate_x           : float [0.0, 1.0]          default 0.30
start_gate_y           : float [0.0, 1.0]          default 0.18
start_gate_w           : float [0.02, 1.0]         default 0.40
start_gate_h           : float [0.02, 1.0]         default 0.22
```

**Position keys (`x`, `y`, `w`, `h`)**: normalized 0..1 theo khung hình (cùng convention với countdown bbox). Default đặt cổng ở giữa-trên, kích thước ~40%×22% — phù hợp với layout có floor occupy nửa dưới + horizon ở ~45% chiều cao.

**`start_gate_type`**: enum tương tự `bg_type` của background layer. Quyết định nguồn fill:
- `"color"` → fill bằng `start_gate_color` (solid).
- `"image"` → fill bằng `start_gate_image` (path), scale fit. Fallback color nếu file thiếu/lỗi.
- `"video"` → fill bằng `start_gate_video` (path), play đồng bộ với timeline. Fallback image rồi color nếu lỗi.

User có thể set cả image lẫn video path đồng thời để switch type nhanh không mất dữ liệu, nhưng chỉ một được dùng theo `type`.

---

## Render logic

### Class mới `StartGate` trong `src/rhythm.py`

Pattern giống `SegmentBackgroundLayer` (dòng 565-643) nhưng render vào sub-rect của canvas thay vì full frame:

```python
class StartGate:
    """Visual gate at the far end of the floor, where blocks emerge.
    
    Pattern theo SegmentBackgroundLayer nhưng compose vào sub-rect.
    """

    def __init__(
        self,
        view_w: int,
        view_h: int,
        *,
        gate_type: str = "color",
        color: str = "#1a1a1a",
        image_path: str | None = None,
        video_path: str | None = None,
        bbox: tuple[float, float, float, float] = (0.30, 0.18, 0.40, 0.22),
        fps: float = 30.0,
    ) -> None:
        self._view_w = int(view_w)
        self._view_h = int(view_h)
        self._fps = max(1e-6, float(fps))
        t = str(gate_type or "color").strip().lower()
        self._type = t if t in {"color", "image", "video"} else "color"
        self._solid_bgr = _hex_to_bgr(color, default=(26, 26, 26))
        self.set_bbox(*bbox)

        # Image / video state
        self._image: np.ndarray | None = None
        self._cap: cv2.VideoCapture | None = None
        self._video_fps = self._fps
        self._video_frames = 0
        self._last_src_idx = -1
        self._last_frame: np.ndarray | None = None

        if self._type == "image" and image_path:
            try:
                img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
                if img is not None:
                    self._image = self._fit_to_rect(img)
            except Exception:
                self._image = None
        elif self._type == "video" and video_path:
            try:
                cap = cv2.VideoCapture(str(video_path))
                if cap is not None and cap.isOpened():
                    self._cap = cap
                    vf = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                    if vf > 1e-3:
                        self._video_fps = vf
                    self._video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            except Exception:
                self._cap = None

    def set_bbox(self, x: float, y: float, w: float, h: float) -> None:
        x = max(0.0, min(0.98, float(x)))
        y = max(0.0, min(0.98, float(y)))
        w = max(0.02, min(1.0 - x, float(w)))
        h = max(0.02, min(1.0 - y, float(h)))
        self._bx = x
        self._by = y
        self._bw = w
        self._bh = h
        # Recompute pixel rect
        self._px = int(round(x * self._view_w))
        self._py = int(round(y * self._view_h))
        self._pw = max(1, int(round(w * self._view_w)))
        self._ph = max(1, int(round(h * self._view_h)))
        # Re-fit cached image to new rect
        if self._image is not None:
            # Re-load original logic — simplification: cache original separately
            # In practice need to keep _orig_image for re-fit on bbox change
            pass

    def set_style(self, *, color: str | None = None) -> None:
        """Hot-update solid color (other params require rebuild)."""
        if color is not None:
            self._solid_bgr = _hex_to_bgr(color, default=self._solid_bgr)

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _fit_to_rect(self, img: np.ndarray) -> np.ndarray:
        """Resize image to gate rect dimensions."""
        return cv2.resize(img, (self._pw, self._ph), interpolation=cv2.INTER_AREA)

    def _read_video_frame(self, frame_idx: int) -> np.ndarray | None:
        if self._cap is None:
            return None
        src_idx = int(round((float(frame_idx) / self._fps) * self._video_fps))
        # Loop video
        if self._video_frames > 0:
            src_idx = src_idx % self._video_frames
        if src_idx != self._last_src_idx:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, float(src_idx))
            ok, frm = self._cap.read()
            if ok and frm is not None:
                if frm.shape[1] != self._pw or frm.shape[0] != self._ph:
                    frm = cv2.resize(
                        frm, (self._pw, self._ph), interpolation=cv2.INTER_LINEAR
                    )
                self._last_frame = frm
                self._last_src_idx = src_idx
        return self._last_frame

    def draw(self, canvas: np.ndarray, frame_idx: int) -> None:
        """Composite gate onto canvas at its bbox position."""
        # Determine fill content
        content: np.ndarray | None = None
        if self._type == "image" and self._image is not None:
            content = self._image
        elif self._type == "video":
            content = self._read_video_frame(frame_idx)

        if content is None:
            # Solid color fallback
            content = np.full((self._ph, self._pw, 3), self._solid_bgr, dtype=np.uint8)

        # Composite with alpha if present (for transparent PNGs)
        x0, y0 = self._px, self._py
        x1, y1 = x0 + self._pw, y0 + self._ph
        # Clamp to canvas bounds
        cx0, cy0 = max(0, x0), max(0, y0)
        cx1, cy1 = min(canvas.shape[1], x1), min(canvas.shape[0], y1)
        if cx0 >= cx1 or cy0 >= cy1:
            return  # off-screen

        sx0 = cx0 - x0
        sy0 = cy0 - y0
        src = content[sy0:sy0 + (cy1 - cy0), sx0:sx0 + (cx1 - cx0)]

        if src.shape[2] == 4:
            # RGBA — alpha blend
            alpha = src[:, :, 3:4].astype(np.float32) / 255.0
            rgb = src[:, :, :3].astype(np.float32)
            dst = canvas[cy0:cy1, cx0:cx1].astype(np.float32)
            blended = rgb * alpha + dst * (1.0 - alpha)
            canvas[cy0:cy1, cx0:cx1] = blended.astype(np.uint8)
        else:
            # Plain BGR — overwrite
            canvas[cy0:cy1, cx0:cx1] = src
```

### Gọi từ main render loop

Trong main loop của `Visualizer.render()` (sau khi vẽ background, trước floor panels), thêm:

```python
# ... draw background ...

# Start Gate (NEW)
if self._start_gate is not None and self.START_GATE_ENABLED:
    self._start_gate.draw(canvas, frame_num)

# ... draw floor panels, side rails, blocks, stickman, countdown ...
```

Khởi tạo `self._start_gate` ở đầu `Visualizer.run()`:

```python
self._start_gate = None
if bool(getattr(self, "START_GATE_ENABLED", True)):
    self._start_gate = StartGate(
        view_w=self.WIDTH,
        view_h=self.HEIGHT,
        gate_type=str(getattr(self, "START_GATE_TYPE", "color")),
        color=str(getattr(self, "START_GATE_COLOR", "#1a1a1a")),
        image_path=getattr(self, "START_GATE_IMAGE", None),
        video_path=getattr(self, "START_GATE_VIDEO", None),
        bbox=(
            float(getattr(self, "START_GATE_X", 0.30)),
            float(getattr(self, "START_GATE_Y", 0.18)),
            float(getattr(self, "START_GATE_W", 0.40)),
            float(getattr(self, "START_GATE_H", 0.22)),
        ),
        fps=float(self.FPS),
    )
```

Cleanup video resource khi đoạn render xong:

```python
if self._start_gate is not None:
    self._start_gate.close()
```

---

## 12 touch points end-to-end

### 1. `studio/models/layer.py` — Layer kind registry

a. `LayerKind` Literal (dòng 12): thêm `"start_gate"`.

```python
LayerKind = Literal["background", "side_rails", "floor", "stickman", "countdown", "start_gate"]
```

b. `LAYER_KIND_COLORS` (dòng 15-21): thêm màu cho UI track.

```python
LAYER_KIND_COLORS: dict[str, str] = {
    "background": "#2563eb",
    "side_rails": "#a21caf",
    "floor":      "#0891b2",
    "stickman":   "#ca8a04",
    "countdown":  "#15803d",
    "start_gate": "#ea580c",  # orange — distinguishes from background blue
}
```

c. `_VISUAL_FIELDS_BY_KIND` (dòng 127-160): thêm entry cho `"start_gate"`.

```python
"start_gate": [
    "start_gate_enabled",
    "start_gate_type",
    "start_gate_color",
    "start_gate_image",
    "start_gate_video",
    "start_gate_x",
    "start_gate_y",
    "start_gate_w",
    "start_gate_h",
],
```

d. `resolve_segment_config` for-loop kinds (dòng 233): thêm `"start_gate"` vào tuple iterate.

```python
for kind in ("background", "side_rails", "floor", "stickman", "countdown", "start_gate"):
```

### 2. `studio/models/render_settings.py` — Pydantic schema

Sau các `relax_*` fields hoặc thành section riêng, thêm 9 fields:

```python
start_gate_enabled: bool = True
start_gate_type: Literal["color", "image", "video"] = "color"
start_gate_color: str = "#1a1a1a"
start_gate_image: Optional[str] = None
start_gate_video: Optional[str] = None
start_gate_x: float = Field(default=0.30, ge=0.0, le=1.0)
start_gate_y: float = Field(default=0.18, ge=0.0, le=1.0)
start_gate_w: float = Field(default=0.40, ge=0.02, le=1.0)
start_gate_h: float = Field(default=0.22, ge=0.02, le=1.0)
```

### 3. `studio/editor/timeline_panel.py` — Layer track + button + default config

a. `_LAYER_KINDS` tuple (dòng 4722): thêm `"start_gate"`.

```python
_LAYER_KINDS = ("background", "floor", "side_rails", "stickman", "countdown", "start_gate")
```

Cập nhật `_LAYER_TRACKS_TOTAL_H` constant (dòng 4723) từ `160` (= 5×32) thành `192` (= 6×32). Cập nhật cả `_BEAT_STRIP_Y`, `_WAVE_TRACK_Y`, `_SCENE_H` xuống dưới 32px tương ứng.

b. `_layer_button_pixmaps` (dòng 457-567): thêm icon cho `"start_gate"`. Đề xuất: vẽ rectangle với cổng arch shape (2 trụ + ngang trên).

```python
# Trong _layer_button_pixmaps, sau khối countdown (~dòng 565):

# --- Start Gate: hai trụ + xà ngang ---
pm = _blank()
p = QPainter(pm)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
p.setPen(pen)
p.setBrush(Qt.BrushStyle.NoBrush)
gx0, gy0 = m + s * 0.18, m + s * 0.20
gx1, gy1 = s - m - s * 0.18, s - m
p.drawLine(QPointF(gx0, gy0), QPointF(gx0, gy1))  # left pillar
p.drawLine(QPointF(gx1, gy0), QPointF(gx1, gy1))  # right pillar
p.drawLine(QPointF(gx0 - s * 0.05, gy0),
           QPointF(gx1 + s * 0.05, gy0))           # top beam
p.end()
result["start_gate"] = pm
```

c. `_default_layer_config("start_gate")` (sau dòng 3250 trong `_default_layer_config`):

```python
if kind == "start_gate":
    return {
        "start_gate_enabled": True,
        "start_gate_type": "color",
        "start_gate_color": "#1a1a1a",
        "start_gate_image": "",
        "start_gate_video": "",
        "start_gate_x": 0.30,
        "start_gate_y": 0.18,
        "start_gate_w": 0.40,
        "start_gate_h": 0.22,
    }
```

d. Drop signal mới `start_gate_media_dropped_at` ở `TimelineView` (giống pattern `background_media_dropped_at` dòng 1795):

```python
start_gate_media_dropped_at = Signal(str, float)  # media_id, time_sec
```

Trong `dropEvent` (dòng 1887-1913), thêm nhánh xử lý cho lane "start_gate":

```python
if kind == "start_gate":
    self.start_gate_media_dropped_at.emit(media_id, time_sec)
    event.acceptProposedAction()
    return
```

Và signal `start_gate_media_dropped` ở `TimelinePanel` (dòng 2299-2300):

```python
start_gate_media_dropped = Signal(str, float)
```

Connect ở dòng 3974-3976:

```python
self.view.start_gate_media_dropped_at.connect(self.start_gate_media_dropped.emit)
```

### 4. `studio/editor/layer_edit_dialog.py` — Section UI mới `_StartGateSection`

Class mới đặt giữa `_CountdownSection` và `_LayerEditDialog` (hoặc bất kỳ chỗ nào tương tự pattern):

```python
class _StartGateSection(QGroupBox):
    """Config section for a Start Gate layer block."""

    changed = Signal()
    _TYPE_OPTIONS = [
        ("Solid color", "color"),
        ("Image", "image"),
        ("Video", "video"),
    ]

    @staticmethod
    def _normalize_type(value: object) -> str:
        raw = str(value or "color").strip().lower()
        return raw if raw in {"color", "image", "video"} else "color"

    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__("Start Gate", parent)
        form = QFormLayout(self)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(8)

        # Enabled checkbox
        self._enabled_cb = QCheckBox("Enabled")
        self._enabled_cb.setChecked(bool(config.get("start_gate_enabled", True)))
        self._enabled_cb.stateChanged.connect(self.changed)
        form.addRow("Start gate", self._enabled_cb)

        # Type selector
        self._type_cb = QComboBox()
        for label, val in self._TYPE_OPTIONS:
            self._type_cb.addItem(label, val)
        cur_type = self._normalize_type(config.get("start_gate_type", "color"))
        idx = next(
            (i for i, (_l, v) in enumerate(self._TYPE_OPTIONS) if v == cur_type),
            0,
        )
        self._type_cb.setCurrentIndex(idx)
        self._type_cb.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Fill type", self._type_cb)

        # Color picker (visible khi type=color)
        self._color: str = config.get("start_gate_color") or "#1a1a1a"
        self._color_btn = QPushButton()
        self._color_btn.setMinimumWidth(90)
        self._refresh_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        self._color_label = QLabel("Color")
        form.addRow(self._color_label, self._color_btn)

        # Image path (visible khi type=image)
        from .segment_config_panel import _PathBrowseWidget
        self._image_edit = _PathBrowseWidget(
            str(config.get("start_gate_image", "") or ""),
            title="Select gate image",
            file_filter="Images (*.png *.jpg *.jpeg *.webp *.bmp);;All files (*.*)",
            placeholder="Optional gate image (transparent PNG ok)",
            parent=self,
        )
        self._image_edit.changed.connect(self.changed)
        self._image_label = QLabel("Image file")
        form.addRow(self._image_label, self._image_edit)

        # Video path (visible khi type=video)
        self._video_edit = _PathBrowseWidget(
            str(config.get("start_gate_video", "") or ""),
            title="Select gate video",
            file_filter="Videos (*.mp4 *.mov *.mkv *.webm);;All files (*.*)",
            placeholder="Optional gate video (loops automatically)",
            parent=self,
        )
        self._video_edit.changed.connect(self.changed)
        self._video_label = QLabel("Video file")
        form.addRow(self._video_label, self._video_edit)

        # Position & size group (cùng pattern Fix 5 của countdown spec)
        self._pos_group = QGroupBox("Position & size", self)
        pos_form = QFormLayout(self._pos_group)
        pos_form.setContentsMargins(8, 10, 8, 8)
        pos_form.setSpacing(6)

        self._x_sp = QDoubleSpinBox()
        self._x_sp.setRange(0.0, 1.0); self._x_sp.setSingleStep(0.01); self._x_sp.setDecimals(3)
        self._x_sp.setValue(float(config.get("start_gate_x", 0.30)))
        self._x_sp.valueChanged.connect(self.changed)
        pos_form.addRow("X", self._x_sp)

        self._y_sp = QDoubleSpinBox()
        self._y_sp.setRange(0.0, 1.0); self._y_sp.setSingleStep(0.01); self._y_sp.setDecimals(3)
        self._y_sp.setValue(float(config.get("start_gate_y", 0.18)))
        self._y_sp.valueChanged.connect(self.changed)
        pos_form.addRow("Y", self._y_sp)

        self._w_sp = QDoubleSpinBox()
        self._w_sp.setRange(0.02, 1.0); self._w_sp.setSingleStep(0.01); self._w_sp.setDecimals(3)
        self._w_sp.setValue(float(config.get("start_gate_w", 0.40)))
        self._w_sp.valueChanged.connect(self.changed)
        pos_form.addRow("Width", self._w_sp)

        self._h_sp = QDoubleSpinBox()
        self._h_sp.setRange(0.02, 1.0); self._h_sp.setSingleStep(0.01); self._h_sp.setDecimals(3)
        self._h_sp.setValue(float(config.get("start_gate_h", 0.22)))
        self._h_sp.valueChanged.connect(self.changed)
        pos_form.addRow("Height", self._h_sp)

        form.addRow(self._pos_group)

        self._update_visibility()

    def _refresh_color_btn(self) -> None:
        c = QColor(self._color)
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        fg = "#000000" if lum > 128 else "#ffffff"
        self._color_btn.setStyleSheet(
            f"background-color:{self._color};color:{fg};border:1px solid #555;"
        )
        self._color_btn.setText(self._color.upper())

    def _pick_color(self) -> None:
        color = _pick_color(self._color, "Start gate color", self)
        if color:
            self._color = color
            self._refresh_color_btn()
            self.changed.emit()

    def _on_type_changed(self) -> None:
        self._update_visibility()
        self.changed.emit()

    def _update_visibility(self) -> None:
        t = self._normalize_type(self._type_cb.currentData())
        self._color_label.setVisible(t == "color")
        self._color_btn.setVisible(t == "color")
        self._image_label.setVisible(t == "image")
        self._image_edit.setVisible(t == "image")
        self._video_label.setVisible(t == "video")
        self._video_edit.setVisible(t == "video")

    def get_config(self) -> dict:
        return {
            "start_gate_enabled": self._enabled_cb.isChecked(),
            "start_gate_type": self._normalize_type(self._type_cb.currentData()),
            "start_gate_color": self._color,
            "start_gate_image": self._image_edit.get_value(),
            "start_gate_video": self._video_edit.get_value(),
            "start_gate_x": self._x_sp.value(),
            "start_gate_y": self._y_sp.value(),
            "start_gate_w": self._w_sp.value(),
            "start_gate_h": self._h_sp.value(),
        }
```

### 5. `studio/editor/inspector_panel.py` — route start_gate

Trong `_make_section_for_layer` (dòng 368-429), thêm nhánh:

```python
elif kind == "start_gate":
    from studio.editor.layer_edit_dialog import _StartGateSection
    return _StartGateSection(cfg, self)
```

### 6. `src/rhythm.py` — Renderer + Visualizer + argparse

**a.** Class `StartGate` mới (xem section "Render logic" bên trên) — đặt sau `SegmentBackgroundLayer` (dòng 643).

**b.** Khởi tạo trong `Visualizer.run` (sau dòng 6062 nơi tạo `bg_layer`):

```python
start_gate = None
if bool(getattr(self, "START_GATE_ENABLED", True)):
    start_gate = StartGate(
        view_w=self.WIDTH,
        view_h=self.HEIGHT,
        gate_type=str(getattr(self, "START_GATE_TYPE", "color")),
        color=str(getattr(self, "START_GATE_COLOR", "#1a1a1a")),
        image_path=getattr(self, "START_GATE_IMAGE", None),
        video_path=getattr(self, "START_GATE_VIDEO", None),
        bbox=(
            float(getattr(self, "START_GATE_X", 0.30)),
            float(getattr(self, "START_GATE_Y", 0.18)),
            float(getattr(self, "START_GATE_W", 0.40)),
            float(getattr(self, "START_GATE_H", 0.22)),
        ),
        fps=float(self.FPS),
    )
```

**c.** Trong main render loop, sau khi composite background, gọi:

```python
if start_gate is not None:
    start_gate.draw(canvas, fi)
```

**d.** Cleanup ở cuối render:

```python
if start_gate is not None:
    start_gate.close()
```

**e.** `Visualizer.__init__` (sau dòng ~5614 nơi RELAX_COUNTDOWN_* được khởi tạo):

```python
self.START_GATE_ENABLED: bool = True
self.START_GATE_TYPE: str = "color"
self.START_GATE_COLOR: str = "#1a1a1a"
self.START_GATE_IMAGE: str | None = None
self.START_GATE_VIDEO: str | None = None
self.START_GATE_X: float = 0.30
self.START_GATE_Y: float = 0.18
self.START_GATE_W: float = 0.40
self.START_GATE_H: float = 0.22
```

**f.** Argparse (sau các `--relax_countdown_*` flags ~dòng 7375):

```python
p.add_argument('--start_gate_enabled', type=int, default=1, metavar='0|1',
               help='Show the start gate at the far end of floor.')
p.add_argument('--start_gate_type', type=str, default='color',
               choices=['color', 'image', 'video'],
               help='Start gate fill type.')
p.add_argument('--start_gate_color', type=str, default='#1a1a1a',
               help='Start gate solid color (used when type=color or fallback).')
p.add_argument('--start_gate_image', type=str, default=None, metavar='PATH',
               help='Start gate image (used when type=image).')
p.add_argument('--start_gate_video', type=str, default=None, metavar='PATH',
               help='Start gate video (used when type=video, loops).')
p.add_argument('--start_gate_x', type=float, default=0.30,
               help='Start gate left position (0..1, normalized).')
p.add_argument('--start_gate_y', type=float, default=0.18,
               help='Start gate top position (0..1, normalized).')
p.add_argument('--start_gate_w', type=float, default=0.40,
               help='Start gate width (0..1, normalized).')
p.add_argument('--start_gate_h', type=float, default=0.22,
               help='Start gate height (0..1, normalized).')
```

**g.** Wire viz attrs (sau ~dòng 7558):

```python
viz.START_GATE_ENABLED = bool(int(args.start_gate_enabled))
viz.START_GATE_TYPE = str(args.start_gate_type or "color")
viz.START_GATE_COLOR = str(args.start_gate_color or "#1a1a1a")
viz.START_GATE_IMAGE = args.start_gate_image or None
viz.START_GATE_VIDEO = args.start_gate_video or None
viz.START_GATE_X = max(0.0, min(1.0, float(args.start_gate_x)))
viz.START_GATE_Y = max(0.0, min(1.0, float(args.start_gate_y)))
viz.START_GATE_W = max(0.02, min(1.0, float(args.start_gate_w)))
viz.START_GATE_H = max(0.02, min(1.0, float(args.start_gate_h)))
```

### 7. `src/live_renderer.py` — Live preview integration

**a.** `LiveFrameRenderer.__init__` thêm 9 params (sau các countdown params):

```python
start_gate_enabled: bool = True,
start_gate_type: str = "color",
start_gate_color: str = "#1a1a1a",
start_gate_image: Optional[str] = None,
start_gate_video: Optional[str] = None,
start_gate_x: float = 0.30,
start_gate_y: float = 0.18,
start_gate_w: float = 0.40,
start_gate_h: float = 0.22,
```

**b.** Lưu state:

```python
self._start_gate_enabled = bool(start_gate_enabled)
self._start_gate_type = str(start_gate_type or "color")
self._start_gate_color = str(start_gate_color or "#1a1a1a")
self._start_gate_image = start_gate_image or None
self._start_gate_video = start_gate_video or None
self._start_gate_x = max(0.0, min(1.0, float(start_gate_x)))
self._start_gate_y = max(0.0, min(1.0, float(start_gate_y)))
self._start_gate_w = max(0.02, min(1.0, float(start_gate_w)))
self._start_gate_h = max(0.02, min(1.0, float(start_gate_h)))
```

**c.** Tạo instance trong scene rebuild (cùng chỗ tạo `_countdown_hud`):

```python
self._start_gate = None
if self._start_gate_enabled:
    from rhythm import StartGate
    self._start_gate = StartGate(
        view_w=self._width,
        view_h=self._height,
        gate_type=self._start_gate_type,
        color=self._start_gate_color,
        image_path=self._start_gate_image,
        video_path=self._start_gate_video,
        bbox=(self._start_gate_x, self._start_gate_y,
              self._start_gate_w, self._start_gate_h),
        fps=float(self._fps),
    )
```

**d.** Trong render frame method (sau composite background, trước floor):

```python
if self._start_gate is not None:
    self._start_gate.draw(canvas, fi)
```

**e.** `update_render_settings` thêm 9 params optional và update logic:

```python
start_gate_enabled: Optional[bool] = None,
start_gate_type: Optional[str] = None,
start_gate_color: Optional[str] = None,
start_gate_image: Optional[str] = None,
start_gate_video: Optional[str] = None,
start_gate_x: Optional[float] = None,
start_gate_y: Optional[float] = None,
start_gate_w: Optional[float] = None,
start_gate_h: Optional[float] = None,
```

**f.** Hot-update strategy:
- `start_gate_color` thay đổi → gọi `self._start_gate.set_style(color=...)`.
- `start_gate_x/y/w/h` thay đổi → gọi `self._start_gate.set_bbox(...)`.
- `start_gate_type/image/video` thay đổi → **rebuild** `_start_gate` instance (vì cần re-load image/video resource). Gọi `self._start_gate.close()` trước rồi tạo lại.
- `start_gate_enabled` toggle → tạo / destroy instance.

```python
needs_rebuild = (
    start_gate_type is not None or
    start_gate_image is not None or
    start_gate_video is not None or
    start_gate_enabled is not None
)
if needs_rebuild:
    if self._start_gate is not None:
        self._start_gate.close()
        self._start_gate = None
    if self._start_gate_enabled:
        self._start_gate = StartGate(...)  # rebuild với state mới
elif self._start_gate is not None:
    if start_gate_color is not None:
        self._start_gate.set_style(color=self._start_gate_color)
    if any(p is not None for p in (start_gate_x, start_gate_y, start_gate_w, start_gate_h)):
        self._start_gate.set_bbox(
            self._start_gate_x, self._start_gate_y,
            self._start_gate_w, self._start_gate_h,
        )
```

### 8. `studio/editor/main_window.py` — Wiring

**a.** `_live_renderer_kwargs` (dòng ~2370): thêm 9 keys vào kwargs dict (sau countdown keys ~dòng 2468):

```python
"start_gate_enabled": bool(_get("start_gate_enabled", True)),
"start_gate_type": str(_get("start_gate_type", "color") or "color"),
"start_gate_color": str(_get("start_gate_color", "#1a1a1a") or "#1a1a1a"),
"start_gate_image": _get("start_gate_image", None) or "",
"start_gate_video": _get("start_gate_video", None) or "",
"start_gate_x": float(_get("start_gate_x", 0.30) or 0.30),
"start_gate_y": float(_get("start_gate_y", 0.18) or 0.18),
"start_gate_w": float(_get("start_gate_w", 0.40) or 0.40),
"start_gate_h": float(_get("start_gate_h", 0.22) or 0.22),
```

**b.** Drop handler mới `_on_start_gate_media_dropped` (cùng pattern `_on_background_media_dropped` dòng 1480-1568):

```python
def _on_start_gate_media_dropped(self, media_id: str, time_sec: float) -> None:
    """Drop video/image onto Start Gate track -> apply to start_gate layer."""
    media = self.project.get_media(media_id)
    if media is None:
        return

    media_kind = str(getattr(media.kind, "value", media.kind)).lower()
    if media_kind not in {"video", "image"}:
        return  # audio doesn't make sense for gate

    eps = 1e-6
    target_segment = next(
        (s for s in self.project.sorted_segments()
         if s.start_time_sec - eps <= time_sec < s.end_time_sec + eps),
        None,
    )
    if target_segment is None:
        self.statusBar().showMessage(
            "Drop onto a segment range in the Start Gate track.", 3000
        )
        return

    gate_type = "video" if media_kind == "video" else "image"
    gate_cfg = {
        "start_gate_enabled": True,
        "start_gate_type": gate_type,
        "start_gate_color": "#1a1a1a",
        "start_gate_image": media.source_path if gate_type == "image" else None,
        "start_gate_video": media.source_path if gate_type == "video" else None,
        "start_gate_x": 0.30,
        "start_gate_y": 0.18,
        "start_gate_w": 0.40,
        "start_gate_h": 0.22,
    }

    # Find or create start_gate layer for the target segment range
    candidates = [
        la for la in self.project.layers
        if la.kind == "start_gate"
        and la.start_time_sec <= target_segment.start_time_sec + eps
        and la.end_time_sec >= target_segment.end_time_sec - eps
    ]

    if candidates:
        target_layer = max(candidates, key=lambda la: la.z_index)
        target_layer.config = dict(gate_cfg)
    else:
        from studio.models.layer import Layer
        overlap = [
            la for la in self.project.layers
            if la.kind == "start_gate"
            and la.overlaps(target_segment.start_time_sec, target_segment.end_time_sec)
        ]
        z_index = (max((la.z_index for la in overlap), default=-1) + 1)
        target_layer = Layer(
            kind="start_gate",
            start_time_sec=target_segment.start_time_sec,
            end_time_sec=target_segment.end_time_sec,
            z_index=z_index,
            name="Start Gate",
            config=dict(gate_cfg),
        )
        self.project.layers.append(target_layer)

    self._on_layer_changed()
    self.segment_panel.set_selection(InspectorPanel.KIND_LAYER, target_layer)
    self.statusBar().showMessage(
        f"Start gate set to {gate_type}: {media.display_name}", 3000
    )
```

**c.** Connect signal trong `__init__` (cùng chỗ connect `background_media_dropped` ~dòng 421):

```python
self.timeline_panel.start_gate_media_dropped.connect(
    self._on_start_gate_media_dropped
)
```

### 9. `studio/editor/preview_panel.py` — Forward params

`update_render_settings` (dòng ~2431): thêm 9 params optional.

```python
start_gate_enabled: Optional[bool] = None,
start_gate_type: Optional[str] = None,
start_gate_color: Optional[str] = None,
start_gate_image: Optional[str] = None,
start_gate_video: Optional[str] = None,
start_gate_x: Optional[float] = None,
start_gate_y: Optional[float] = None,
start_gate_w: Optional[float] = None,
start_gate_h: Optional[float] = None,
```

Forward tới live_renderer:

```python
start_gate_enabled=start_gate_enabled,
start_gate_type=start_gate_type,
start_gate_color=start_gate_color,
start_gate_image=start_gate_image,
start_gate_video=start_gate_video,
start_gate_x=start_gate_x,
start_gate_y=start_gate_y,
start_gate_w=start_gate_w,
start_gate_h=start_gate_h,
```

### 10. `studio/core_bridge/render_service.py` — CLI export args

`_ALLOWED_KEYS` (dòng 716-744): thêm 9 keys mới vào set.

```python
"start_gate_enabled",
"start_gate_type",
"start_gate_color",
"start_gate_image",
"start_gate_video",
"start_gate_x",
"start_gate_y",
"start_gate_w",
"start_gate_h",
```

### 11. Test file `tests/studio/test_layers.py` (nice-to-have)

Thêm test:

- `test_layer_kind_colors_includes_start_gate`
- `test_default_start_gate_layer_creation`
- `test_resolve_start_gate_overrides_render_settings`
- `test_migration_extracts_start_gate` (nếu cần migrate from legacy render_settings)

### 12. Auto-create policy (decision)

`auto_create_default_layers` (`studio/models/layer.py` dòng 78-114) hiện tạo background/floor/stickman cho mọi segment mới. Có nên auto-create start_gate luôn không?

**Đề xuất KHÔNG auto-create**, giống countdown:

- Start gate là visual decoration, không phải core game element.
- Mặc định không phải segment nào cũng cần gate (segment punch/dance không có "blocks emerge" semantic).
- User chủ động thêm khi cần qua nút "+" trên track start_gate.

Test `test_auto_create_does_not_include_side_rails_countdown` cần update tên / extend để cover start_gate nếu giữ chính sách này.

---

## Layout UI Section

`_StartGateSection` cuối cùng:

```
[Main form]
  Start gate     [✓] Enabled
  Fill type      [combo: Solid color / Image / Video]
  Color          [colored button]              (visible khi type=color)
  Image file     [path browse]                  (visible khi type=image)
  Video file     [path browse]                  (visible khi type=video)

[Position & size groupbox]
  X        [spinbox 0.30]
  Y        [spinbox 0.18]
  Width    [spinbox 0.40]
  Height   [spinbox 0.22]
```

Pattern visibility (color/image/video field) giống cách countdown audio mode đã làm trong `_CountdownSection._update_audio_visibility`.

---

## Backward compatibility

**Project cũ không có start_gate layer:** không có effect — `resolve_segment_config` không thêm key nào vào effective. Renderer constructor `start_gate_enabled=True` trên Pydantic default → hiện cổng với solid color default `#1a1a1a` (xám rất tối, gần như invisible trên background đen).

**Đánh giá visual regression:** với default `enabled=True + type=color + #1a1a1a`, một hình chữ nhật xám tối nhỏ sẽ xuất hiện ở vị trí (0.30, 0.18, 0.40, 0.22). Trên background tối quen thuộc, có thể khó thấy nhưng vẫn LÀ regression.

**Hai lựa chọn cho default:**

(a) `start_gate_enabled = False` mặc định → không hiện gì cho project cũ. User phải explicit enable. **An toàn nhất, đề xuất.**

(b) `start_gate_enabled = True` mặc định → hiện cổng xám tối ngay cho project cũ. Có regression nhỏ nhưng "discoverability" tốt hơn (user thấy ngay tính năng mới).

**Đề xuất chốt: (a) — default disabled.** User mới sẽ thấy nút "+" trên track start_gate và tự enable khi cần. Project cũ không bị thay đổi visual.

Sửa default trong:
- `studio/models/render_settings.py`: `start_gate_enabled: bool = False`
- `studio/editor/timeline_panel.py` `_default_layer_config`: `"start_gate_enabled": False`
- `src/rhythm.py` Visualizer init: `self.START_GATE_ENABLED = False`
- `src/rhythm.py` argparse: `default=0`

Khi user click "+" trên track start_gate qua `_on_add_layer_clicked("start_gate")`, layer mới được tạo với config từ `_default_layer_config("start_gate")` → enabled=False. Cần override thành `enabled=True` cho explicit-add path:

```python
# Trong _create_layer hoặc _on_add_layer_clicked, special case cho start_gate:
if kind == "start_gate":
    config = self._default_layer_config(kind)
    config["start_gate_enabled"] = True  # explicit add → enable
```

Hoặc đơn giản hơn: giữ default `enabled=True` trong `_default_layer_config` nhưng `False` trong Pydantic schema. Layer mới qua nút "+" → enabled. Project cũ không có layer → effective không có key → Pydantic default False → invisible.

**Chọn lựa chọn 2 (sạch hơn):**

```
Pydantic schema:        start_gate_enabled = False    (project cũ → False)
_default_layer_config:  "start_gate_enabled": True    (layer mới qua + → True)
Visualizer.__init__:    self.START_GATE_ENABLED = False
Argparse default:       default=0
```

---

## Test scenarios

### Test 1: Tạo start_gate layer mới

```
Setup: project mới, không có layer start_gate.
Action: click nút "+" trên track start_gate trong timeline.
Verify: tạo Layer(kind="start_gate") với config 9 keys, enabled=True, type=color.
Verify: render preview thấy hình chữ nhật xám tối ở vị trí (0.30, 0.18, 0.40, 0.22).
```

### Test 2: Drop image onto Start Gate track

```
Setup: project có 1 segment relax 0-30s. Media library có 1 image "gate.png".
Action: drag "gate.png" thả vào track start_gate trong khoảng segment range.
Verify: tạo (hoặc update) start_gate layer cho segment range đó.
Verify: layer.config["start_gate_type"] == "image".
Verify: layer.config["start_gate_image"] == path("gate.png").
Verify: render preview thấy ảnh ở vị trí cổng.
Verify: status bar hiện "Start gate set to image: gate.png".
```

### Test 3: Drop video onto Start Gate track

```
Tương tự Test 2 nhưng với "gate_loop.mp4".
Verify: layer.config["start_gate_type"] == "video", _video set.
Verify: render preview play video ở rect cổng (loop).
```

### Test 4: Drop audio bị từ chối

```
Setup: media library có "song.mp3".
Action: drag thả vào track start_gate.
Verify: KHÔNG tạo layer. Status bar không có thông báo lỗi (silently ignore — hoặc thông báo "Start gate accepts image/video only").
```

### Test 5: Inspector edit fill type

```
Setup: start_gate layer type=color.
Action: mở Inspector, đổi Fill type → "Image". Đợi debounce.
Verify: UI hiện Image file row, ẩn Color row.
Action: chọn file ảnh.
Verify: layer.config update, preview render ảnh.
Action: đổi Fill type → "Video". Chọn video.
Verify: live preview play video ngay (rebuild HUD instance vì đổi type).
```

### Test 6: Position & size spinbox

```
Setup: start_gate layer (vị trí default).
Action: trong Inspector, đổi X = 0.5. Đợi debounce.
Verify: layer.config["start_gate_x"] == 0.5.
Verify: live preview cổng dịch sang phải ngay (set_bbox hot-update, không rebuild).
Verify: KHÔNG flicker / video không restart.
```

### Test 7: Disable / enable toggle

```
Setup: start_gate layer enabled=True, đang hiện trong preview.
Action: bỏ check Enabled trong Inspector.
Verify: cổng biến mất ngay frame kế (rebuild với enabled=False, instance set None).
Action: tick lại Enabled.
Verify: cổng xuất hiện trở lại ở vị trí cũ với content cũ.
```

### Test 8: Render order — gate sau background, trước blocks

```
Setup: segment relax có background image (đỏ), start_gate image (xanh).
Render frame có blocks active.
Verify: pixel ở vị trí cổng = xanh (gate đè background).
Verify: nếu block đi qua trước cổng (z gần camera), pixel block đè cổng.
Verify: nếu block còn ở phía sau cổng (z xa), pixel cổng đè block phần dưới gate.
```

### Test 9: CLI export pipeline

```
Setup: project có start_gate layer type=video.
Action: trigger render export qua MainWindow.
Verify: CLI command có flags --start_gate_enabled 1 --start_gate_type video --start_gate_video <path> + 4 position flags.
Verify: rendered video có cổng video play ở đúng vị trí.
```

### Test 10: Backward compat (default disabled)

```
Setup: project cũ không có start_gate layer.
Load → effective config không có start_gate_* keys → Pydantic fall back enabled=False.
Render preview / export.
Verify: KHÔNG có cổng nào hiện. Visual identical với pre-feature behavior.
```

### Test 11: Pydantic validation

```
BaseRenderSettings(start_gate_x=1.5) → ValidationError (out of range 0..1).
BaseRenderSettings(start_gate_w=0.01) → ValidationError (below 0.02).
BaseRenderSettings(start_gate_type="solid") → ValidationError (not in enum).
```

### Test 12: Persistence round-trip

```
Setup: project có start_gate layer config x=0.5, y=0.3, type=video, video="gate.mp4".
Action: save project → close → reopen.
Verify: layer.config khôi phục đúng 9 keys.
Verify: render preview hiển thị giống trước save.
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **Layer kind hiện có** (background, floor, side_rails, stickman, countdown): KHÔNG đổi behavior. Spec này thêm kind mới, không sửa kind cũ.

2. **`_LAYER_TRACKS_TOTAL_H`** phải cập nhật khi thêm 1 lane (160 → 192). Cẩn thận tính lại `_BEAT_STRIP_Y`, `_WAVE_TRACK_Y`, `_SCENE_H` để layout không bị overlap.

3. **`auto_create_default_layers`**: KHÔNG thêm start_gate vào defaults (xem section 12). Test `test_auto_create_does_not_include_side_rails_countdown` cần extend hoặc rename để cover start_gate.

4. **Drag-drop từ Media Library**: thêm signal mới, không thay đổi 2 signal cũ (`background_media_dropped`, `floor_media_dropped`).

5. **`resolve_segment_config`**: chỉ thêm `"start_gate"` vào tuple iterate, không sửa logic strip / update.

6. **`SegmentBackgroundLayer`**: KHÔNG đụng. `StartGate` là class riêng, dù pattern tương tự.

7. **Render order**: gate phải vẽ SAU background, TRƯỚC floor. Sai thứ tự → gate bị floor che hoặc gate che blocks hoàn toàn.

8. **Pydantic default `enabled=False`**: bắt buộc cho backward compat. Nếu set True, project cũ render khác visual.

9. **Hot-update strategy**: color + bbox dùng setter (không rebuild). Type / image / video / enabled cần rebuild instance vì phải re-load resource. Sai → memory leak (`cv2.VideoCapture` không close) hoặc resource conflict.

10. **Cleanup video resource**: `StartGate.close()` phải gọi khi rebuild hoặc khi render xong. Live renderer + main render đều cần.

11. **Inspector `_make_section_for_layer`**: chỉ thêm nhánh `elif kind == "start_gate"`. Không đổi logic existing branches.

12. **`_VISUAL_FIELDS_BY_KIND`**: thêm entry "start_gate" với 9 keys. Migration sẽ tự nhận và extract đúng.

---

## Pattern code hiện có để tham khảo

- **`SegmentBackgroundLayer`** (`src/rhythm.py` dòng 565-643): template gần nhất cho `StartGate`. Cùng pattern color/image/video fill, video frame indexing.

- **`CountdownHUD.set_box`** (dòng 4562-4570): template cho `StartGate.set_bbox` clamp logic.

- **`_BackgroundSection`** trong `layer_edit_dialog.py` (search "_BackgroundSection"): template cho `_StartGateSection` UI với type selector + visibility toggling.

- **`_PathBrowseWidget`** trong `segment_config_panel.py`: widget chọn file path, dùng cho image/video paths.

- **`_on_background_media_dropped`** trong `main_window.py` (dòng 1480-1568): template cho `_on_start_gate_media_dropped`.

- **`background_media_dropped_at` signal** trong `timeline_panel.py` (dòng 1795, 1903, 3975): pattern để add `start_gate_media_dropped_at`.

- **`_layer_button_pixmaps`** trong `timeline_panel.py` (dòng 457-567): vẽ icon cho lane mới.

---

## Thứ tự implement đề xuất

1. **Touch point 1 — `studio/models/layer.py`.** Thêm "start_gate" vào `LayerKind`, `LAYER_KIND_COLORS`, `_VISUAL_FIELDS_BY_KIND`, `resolve_segment_config` tuple. Verify import không break, mọi test cũ pass.

2. **Touch point 2 — Pydantic schema.** Thêm 9 fields với `start_gate_enabled = False` default. Run Test 11.

3. **Touch point 3 — Timeline panel.** Thêm vào `_LAYER_KINDS`, cập nhật `_LAYER_TRACKS_TOTAL_H` + Y offsets, thêm icon + default config + drop signal. Verify lane mới hiện trong timeline.

4. **Touch point 6 — Renderer (rhythm.py).** Thêm class `StartGate`, init/draw/close trong main loop, viz attrs, argparse, wire args. Manual test CLI: chạy với `--start_gate_enabled 1 --start_gate_color "#ff0000"` → verify cổng đỏ hiện.

5. **Touch point 10 — render_service `_ALLOWED_KEYS`.** Thêm 9 keys. Verify CLI args truyền đúng (Test 9).

6. **Touch point 7 — live_renderer.** Thêm params, state, instance, `update_render_settings`, hot-update / rebuild logic. Verify live preview update mượt (Test 6, 7).

7. **Touch point 8 — main_window.** `_live_renderer_kwargs` thêm 9 keys. Drop handler `_on_start_gate_media_dropped` + connect signal. Verify drop image (Test 2), drop video (Test 3).

8. **Touch point 9 — preview_panel.** Forward `update_render_settings` 9 params.

9. **Touch point 4 — `_StartGateSection` UI.** Tạo class, thêm 4 spinbox + color + 2 path widgets + visibility toggle. `get_config` trả 9 keys.

10. **Touch point 5 — Inspector route.** Thêm nhánh `elif kind == "start_gate"`. Test Test 5.

11. **Touch point 11 — Tests** (optional): viết unit tests cho layer kind, default config, migration.

12. **Smoke test toàn bộ:** create project → add segment → click "+" trên start_gate → drop image → edit Inspector → save → reopen → render export. Mọi pipeline hoạt động.

---

## Open questions

(1) **Default `enabled = False` (an toàn) hay `True` (discoverable)?** Spec đề xuất False. Bạn quyết định.

(2) **Render order: gate trước hay sau floor panels?** Hiện đề xuất "trước floor" (gate đè background, floor đè gate). Cách này floor "hide" mép dưới của gate khi gate vượt xuống dưới horizon — phù hợp visual của screenshot. Bạn xác nhận?

(3) **Block render order:** block vẽ TRÊN gate khi block ở gần camera (z nhỏ), DƯỚI gate khi block ở xa (z lớn, sau gate). Hiện block code không có z-test với gate. Có 2 lựa chọn:
   - (a) Gate luôn vẽ trước blocks → block luôn đè gate kể cả khi spawn.
   - (b) Gate vẽ trước blocks, block render check z-test riêng.
   Lựa chọn (a) đơn giản hơn nhưng visually không "đúng" — block spawn từ sau gate sẽ "popup" qua gate. Lựa chọn (b) phức tạp hơn. **Đề xuất (a) cho V1, V2 sau xử lý z-test.**

(4) **Frame styling decoration** (scaffold/bar quanh gate): KHÔNG bao gồm V1 — user dùng image transparent có sẵn frame. Bạn OK không, hay muốn thêm option built-in?

(5) **Auto-fit width to floor far-end:** thay vì dùng bbox normalized, có thể auto-tính width = floor far-end width từ `PerspectiveCamera`. Phức tạp hơn nhưng "đúng" hơn về phối cảnh. Hiện spec dùng bbox normalized cho đơn giản và consistency với countdown. Bạn muốn auto-fit không?

(6) **Layer `start_gate` có nên auto-fit kéo theo segment range** giống background/floor (snap-to-fill spec)? Hiện spec không nói, mặc định inherit hành vi snap-to-fill chung của layer system.

(7) **Z-index conflict** khi 2 start_gate layer cùng segment: lấy max z_index như background. OK?

(8) **Preview panel drag-to-position** cho gate giống countdown box: V1 KHÔNG có (chỉ chỉnh qua spinbox Inspector). V2 có thể thêm overlay edit handle. Bạn OK?
