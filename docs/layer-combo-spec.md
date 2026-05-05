# Layer Combo — Mirror Countdown Structure Spec

## Mục tiêu

Tách "bộ đếm hit + chữ COMBO" (vùng top-right hiện hardcoded) thành **layer kind riêng `combo`**. Layer này **MIRROR chính xác structure của countdown layer** (16 fields cùng tên/ý nghĩa, đổi prefix `relax_countdown_` → `combo_`), plus 1 extra field `combo_label` để custom text.

User control:
- Màu sắc, font, vị trí, kích thước
- Animation (pop/flash/fade_cross/shake) khi combo number đổi
- Audio (hit sound + milestone sound)
- Border thickness + Glow strength
- **Drag-and-drop overlay trong preview** (giống countdown bbox handles)

---

## Field mapping countdown → combo

| Countdown field | Combo field | Combo semantic |
|---|---|---|
| `relax_countdown_enabled` | `combo_enabled` | Toggle on/off |
| `relax_countdown_color` | `combo_color` | Single color cho cả số + label |
| `relax_countdown_max_sec` | `combo_fade_after_break_sec` | Fade duration sau khi combo bị break (set = 0 → ẩn ngay) |
| `relax_countdown_anim` | `combo_anim` | Animation khi combo number tăng (pop/flash/fade_cross/shake) |
| `relax_countdown_audio_enabled` | `combo_audio_enabled` | Hit sound on each combo increment |
| `relax_countdown_audio_mode` | `combo_audio_mode` | "default" beep / "file" custom |
| `relax_countdown_audio_file` | `combo_audio_file` | Path hit sound |
| `relax_countdown_audio_volume` | `combo_audio_volume` | Volume 0..1 |
| `relax_countdown_audio_last_mode` | `combo_audio_milestone_mode` | Milestone sound mode (default/file/same) |
| `relax_countdown_audio_last_file` | `combo_audio_milestone_file` | Path milestone sound |
| `relax_countdown_x/y/w/h` | `combo_x/y/w/h` | Position bbox (normalized 0..1) |
| `relax_countdown_border_thickness` | `combo_border_thickness` | Độ dày viền số (px) |
| `relax_countdown_glow_strength` | `combo_glow_strength` | Cường độ glow (0..100) |
| **N/A (countdown chỉ có số)** | `combo_label` | **NEW** — text label "COMBO" (default), customizable |

= **16 fields mirror + 1 extra = 17 fields tổng.**

### Semantic adaptation

**`combo_fade_after_break_sec`** (mirror max_sec):
- Default 0.5s. Khi player miss → combo reset về 0 → counter fade out trong N giây.
- Set = 0 → ẩn instant khi combo = 0.
- Set = 5.0 → counter fade chậm 5s sau combo break (visual "vẫn còn dấu vết").

**`combo_audio_milestone_mode/file`** (mirror audio_last):
- Trigger sound khi combo đạt milestone (vd ×10, ×50, ×100).
- Mode "default" = built-in milestone beep.
- Mode "file" = custom sound.
- Mode "same" = dùng cùng file với combo_audio_file.
- Milestone threshold mặc định: mỗi 10 combo. (V2 expose threshold qua config nếu cần.)

**`combo_anim`** = animation **của số combo khi tăng**:
- "pop": scale up rồi settle (default countdown dùng cho mọi tick)
- "flash": brightness boost
- "fade_cross": cross-fade từ số cũ sang số mới
- "shake": rung khi update

---

## Font customization

Thêm 1 field cho font:

```
combo_font_family : enum  default "duplex"
```

Options (mirror cv2 font enum):

| Value | cv2 constant | Visual |
|---|---|---|
| `"simplex"` | `FONT_HERSHEY_SIMPLEX` | Sans-serif đơn giản |
| `"plain"` | `FONT_HERSHEY_PLAIN` | Mảnh, hơi gầy |
| `"duplex"` | `FONT_HERSHEY_DUPLEX` | Sans-serif dày (default) |
| `"complex"` | `FONT_HERSHEY_COMPLEX` | Serif |
| `"triplex"` | `FONT_HERSHEY_TRIPLEX` | Serif dày |
| `"complex_small"` | `FONT_HERSHEY_COMPLEX_SMALL` | Serif compact |
| `"script_simplex"` | `FONT_HERSHEY_SCRIPT_SIMPLEX` | Handwriting style |
| `"script_complex"` | `FONT_HERSHEY_SCRIPT_COMPLEX` | Handwriting bold |

Render code mapping:

```python
_FONT_MAP = {
    "simplex": cv2.FONT_HERSHEY_SIMPLEX,
    "plain": cv2.FONT_HERSHEY_PLAIN,
    "duplex": cv2.FONT_HERSHEY_DUPLEX,
    "complex": cv2.FONT_HERSHEY_COMPLEX,
    "triplex": cv2.FONT_HERSHEY_TRIPLEX,
    "complex_small": cv2.FONT_HERSHEY_COMPLEX_SMALL,
    "script_simplex": cv2.FONT_HERSHEY_SCRIPT_SIMPLEX,
    "script_complex": cv2.FONT_HERSHEY_SCRIPT_COMPLEX,
}

def _get_font(self):
    return _FONT_MAP.get(self._font_family, cv2.FONT_HERSHEY_DUPLEX)
```

V1 chỉ support 8 cv2 fonts built-in. V2 có thể add TTF qua cv2.freetype hoặc PIL bridge (xem Open question).

---

## Tier system (4 milestones)

Khi combo đạt threshold, **chữ label dưới số đếm** đổi sang text tier tương ứng.

### 8 fields mới (4 tier × 2)

```
combo_tier1_threshold : int  default 30
combo_tier1_label     : str  default "Great"
combo_tier2_threshold : int  default 60
combo_tier2_label     : str  default "Superb"
combo_tier3_threshold : int  default 90
combo_tier3_label     : str  default "Perfect"
combo_tier4_threshold : int  default 120
combo_tier4_label     : str  default "Godlike"
```

### Logic resolve label

```python
def _resolve_label(self, combo: int) -> str:
    """Return label text dựa trên combo count + tier thresholds."""
    if self._tier4_threshold > 0 and combo >= self._tier4_threshold:
        return self._tier4_label
    if self._tier3_threshold > 0 and combo >= self._tier3_threshold:
        return self._tier3_label
    if self._tier2_threshold > 0 and combo >= self._tier2_threshold:
        return self._tier2_label
    if self._tier1_threshold > 0 and combo >= self._tier1_threshold:
        return self._tier1_label
    return self._label   # default "COMBO" khi chưa đạt tier nào
```

**Quy tắc**: threshold = 0 → tier bị disable, skip.

**Thứ tự check**: từ tier cao nhất xuống thấp (tier4 → tier3 → tier2 → tier1) để chọn tier cao nhất đã đạt.

### Ví dụ với defaults

| Combo | Label hiển thị |
|---|---|
| 0 | (ẩn — combo=0) |
| 1 | COMBO |
| 29 | COMBO |
| 30 | **Great** |
| 59 | Great |
| 60 | **Superb** |
| 89 | Superb |
| 90 | **Perfect** |
| 119 | Perfect |
| 120 | **Godlike** |
| 500 | Godlike (mãi mãi nếu không break) |

### Tier transition animation

Khi combo crossing threshold (vd 29 → 30, label đổi "COMBO" → "Great"):
- Reuse `combo_anim` (pop/flash/fade_cross/shake) trigger animation cho cả số + label.
- Hoặc thêm field `combo_tier_transition_anim` riêng (V2). V1 dùng cùng anim.

### UI form layout cho tiers

Trong `_ComboSection`, thêm groupbox "Tier milestones":

```
[Tier milestones groupbox]
  Tier 1 ≥ [spinbox 30]   Label [QLineEdit "Great"]
  Tier 2 ≥ [spinbox 60]   Label [QLineEdit "Superb"]
  Tier 3 ≥ [spinbox 90]   Label [QLineEdit "Perfect"]
  Tier 4 ≥ [spinbox 120]  Label [QLineEdit "Godlike"]

  Hint: Set threshold = 0 to disable a tier.
```

Pseudocode UI:

```python
# Trong _ComboSection.__init__:

self._tier_group = QGroupBox("Tier milestones", self)
tier_form = QFormLayout(self._tier_group)
tier_form.setContentsMargins(8, 10, 8, 8)
tier_form.setSpacing(6)

self._tier_widgets = []  # list of (threshold_spinbox, label_lineedit) tuples

defaults_tiers = [
    (30, "Great"),
    (60, "Superb"),
    (90, "Perfect"),
    (120, "Godlike"),
]

for i, (default_thresh, default_label) in enumerate(defaults_tiers, 1):
    thresh_sp = QSpinBox()
    thresh_sp.setRange(0, 9999)
    thresh_sp.setSingleStep(1)
    thresh_sp.setValue(int(config.get(f"combo_tier{i}_threshold", default_thresh)))
    thresh_sp.setToolTip("Combo count to activate this tier (0 = disabled)")
    thresh_sp.valueChanged.connect(self.changed)
    
    label_edit = QLineEdit(str(config.get(f"combo_tier{i}_label", default_label)))
    label_edit.setMaxLength(20)
    label_edit.setPlaceholderText(f"e.g. {default_label}")
    label_edit.textChanged.connect(self.changed)
    
    # Layout 2 widgets cùng row
    row_widget = QWidget()
    row_layout = QHBoxLayout(row_widget)
    row_layout.setContentsMargins(0, 0, 0, 0)
    row_layout.setSpacing(6)
    row_layout.addWidget(thresh_sp, 1)
    row_layout.addWidget(label_edit, 2)
    
    tier_form.addRow(f"Tier {i} ≥", row_widget)
    self._tier_widgets.append((thresh_sp, label_edit))

# Hint label
hint = QLabel("<i>Set threshold = 0 to disable a tier.</i>")
hint.setStyleSheet("color: #888;")
tier_form.addRow(hint)

form.addRow(self._tier_group)
```

`get_config()` trả thêm 8 keys:

```python
for i, (thresh_sp, label_edit) in enumerate(self._tier_widgets, 1):
    cfg[f"combo_tier{i}_threshold"] = thresh_sp.value()
    cfg[f"combo_tier{i}_label"] = label_edit.text().strip() or f"Tier{i}"
```

---

## Tổng số fields cập nhật: 17 → 26

| Group | Fields | Count |
|---|---|---|
| Mirror countdown core | enabled, color, fade_after_break, anim, x/y/w/h, border, glow | 10 |
| Audio (mirror countdown) | audio_enabled, audio_mode, audio_file, audio_volume, audio_milestone_mode, audio_milestone_file | 6 |
| Combo-specific extras | label (default "COMBO") | 1 |
| **NEW — Font** | **font_family** | **1** |
| **NEW — Tiers** | **tier1/2/3/4 threshold + label** | **8** |
| **TOTAL** | | **26** |

---

## Drag overlay trong preview

Giống Countdown box hiện tại trong `FloorWallOverlay`, thêm **Combo box drag handles**:

```
Khi user mở Edit Layout overlay:
  - Combo box rect hiển thị nếu combo_enabled
  - 4 corner handles: cb_tl, cb_tr, cb_bl, cb_br (resize)
  - Body drag: cb_move
  - Visual: dashed border + 4 corner squares
  - Color: distinguishable từ countdown (vd green vs pink)
```

Implementation pattern giống `_countdown_rect_px` + `_handle_at` + `mouseMove` cb_* logic ở `FloorWallOverlay` (preview_panel.py).

---

## Patch summary (touch points)

### 1. `studio/models/layer.py`

```python
LayerKind = Literal[
    "background", "side_rails", "floor", "stickman",
    "countdown", "start_gate", "combo"
]

LAYER_KIND_COLORS["combo"] = "#dc2626"   # red

_VISUAL_FIELDS_BY_KIND["combo"] = [
    "combo_enabled",
    "combo_color",
    "combo_label",
    "combo_font_family",                         # NEW
    "combo_fade_after_break_sec",
    "combo_anim",
    "combo_audio_enabled",
    "combo_audio_mode",
    "combo_audio_file",
    "combo_audio_volume",
    "combo_audio_milestone_mode",
    "combo_audio_milestone_file",
    "combo_x", "combo_y", "combo_w", "combo_h",
    "combo_border_thickness",
    "combo_glow_strength",
    "combo_tier1_threshold", "combo_tier1_label",   # NEW
    "combo_tier2_threshold", "combo_tier2_label",   # NEW
    "combo_tier3_threshold", "combo_tier3_label",   # NEW
    "combo_tier4_threshold", "combo_tier4_label",   # NEW
]

# resolve_segment_config tuple iterate:
for kind in (..., "combo"):
```

### 2. `studio/models/render_settings.py`

```python
combo_enabled: bool = True
combo_color: str = "#FFFFFF"
combo_label: str = "COMBO"
combo_font_family: Literal[
    "simplex", "plain", "duplex", "complex", "triplex",
    "complex_small", "script_simplex", "script_complex"
] = "duplex"
combo_fade_after_break_sec: float = Field(default=0.5, ge=0.0, le=10.0)
combo_anim: Literal["pop", "flash", "fade_cross", "shake"] = "pop"
combo_audio_enabled: bool = False
combo_audio_mode: Literal["default", "file"] = "default"
combo_audio_file: Optional[str] = None
combo_audio_volume: float = Field(default=0.65, ge=0.0, le=1.0)
combo_audio_milestone_mode: Literal["default", "file", "same"] = "default"
combo_audio_milestone_file: Optional[str] = None
combo_x: float = Field(default=0.85, ge=0.0, le=1.0)
combo_y: float = Field(default=0.08, ge=0.0, le=1.0)
combo_w: float = Field(default=0.13, ge=0.05, le=0.5)
combo_h: float = Field(default=0.18, ge=0.03, le=0.3)
combo_border_thickness: float = Field(default=2.0, ge=0.0, le=10.0)
combo_glow_strength: float = Field(default=30.0, ge=0.0, le=100.0)

# Tier milestones — set threshold = 0 to disable tier
combo_tier1_threshold: int = Field(default=30, ge=0, le=9999)
combo_tier1_label: str = "Great"
combo_tier2_threshold: int = Field(default=60, ge=0, le=9999)
combo_tier2_label: str = "Superb"
combo_tier3_threshold: int = Field(default=90, ge=0, le=9999)
combo_tier3_label: str = "Perfect"
combo_tier4_threshold: int = Field(default=120, ge=0, le=9999)
combo_tier4_label: str = "Godlike"
```

### 3. `studio/editor/timeline_panel.py`

a. `_LAYER_KINDS` thêm `"combo"`. Cập nhật `_LAYER_TRACKS_TOTAL_H` 192 → 224.

b. `_layer_button_pixmaps` thêm icon cho `"combo"` (vd "x" + chữ).

c. `_default_layer_config("combo")` trả **26 keys** với defaults (10 mirror + 6 audio + 1 label + 1 font + 8 tier).

```python
if kind == "combo":
    return {
        # Core (mirror countdown)
        "combo_enabled": True,
        "combo_color": "#FFFFFF",
        "combo_label": "COMBO",
        "combo_font_family": "duplex",
        "combo_fade_after_break_sec": 0.5,
        "combo_anim": "pop",
        # Audio
        "combo_audio_enabled": False,
        "combo_audio_mode": "default",
        "combo_audio_file": "",
        "combo_audio_volume": 0.65,
        "combo_audio_milestone_mode": "default",
        "combo_audio_milestone_file": "",
        # Position & size
        "combo_x": 0.85, "combo_y": 0.08,
        "combo_w": 0.13, "combo_h": 0.18,
        # Style
        "combo_border_thickness": 2.0,
        "combo_glow_strength": 30.0,
        # Tier milestones
        "combo_tier1_threshold": 30,  "combo_tier1_label": "Great",
        "combo_tier2_threshold": 60,  "combo_tier2_label": "Superb",
        "combo_tier3_threshold": 90,  "combo_tier3_label": "Perfect",
        "combo_tier4_threshold": 120, "combo_tier4_label": "Godlike",
    }
```

### 4. `studio/editor/layer_edit_dialog.py` — `_ComboSection`

Class mới mirror cấu trúc `_CountdownSection`:

```python
class _ComboSection(QGroupBox):
    """Config section for a Combo counter layer block."""

    changed = Signal()
    _ANIM_OPTIONS = [          # Mirror countdown
        ("Pop", "pop"),
        ("Flash", "flash"),
        ("Fade Cross", "fade_cross"),
        ("Shake", "shake"),
    ]
    _AUDIO_MODE_OPTIONS = [
        ("Default beep", "default"),
        ("Audio file", "file"),
    ]
    _MILESTONE_MODE_OPTIONS = [
        ("Default milestone beep", "default"),
        ("Use another file", "file"),
        ("Same as regular", "same"),
    ]

    def __init__(self, config: dict, parent=None):
        super().__init__("Combo Counter", parent)
        form = QFormLayout(self)
        # ... mirror _CountdownSection layout ...
        
        # Enabled
        self._enabled_cb = QCheckBox("Enabled")
        self._enabled_cb.setChecked(bool(config.get("combo_enabled", True)))
        form.addRow("Combo", self._enabled_cb)
        
        # Color
        self._color = config.get("combo_color") or "#FFFFFF"
        # ... color picker ...
        form.addRow("Color", self._color_btn)
        
        # Label text — NEW (combo-specific)
        self._label_edit = QLineEdit(str(config.get("combo_label", "COMBO")))
        self._label_edit.setMaxLength(20)
        self._label_edit.setPlaceholderText("e.g. COMBO, HITS, STREAK")
        self._label_edit.textChanged.connect(self.changed)
        form.addRow("Label text", self._label_edit)
        
        # Fade after break
        self._fade_sp = QDoubleSpinBox()
        self._fade_sp.setRange(0.0, 10.0)
        self._fade_sp.setSingleStep(0.5)
        self._fade_sp.setDecimals(1)
        self._fade_sp.setValue(float(config.get("combo_fade_after_break_sec", 0.5)))
        self._fade_sp.setSuffix(" s")
        self._fade_sp.setToolTip("Fade duration after combo breaks (set=0 to hide instantly)")
        self._fade_sp.valueChanged.connect(self.changed)
        form.addRow("Fade after break", self._fade_sp)
        
        # Anim
        self._anim_cb = QComboBox()
        for label, val in self._ANIM_OPTIONS:
            self._anim_cb.addItem(label, val)
        # ... set current ...
        form.addRow("Number effect", self._anim_cb)
        
        # Audio (mirror countdown audio block)
        self._audio_enabled_cb = QCheckBox("Enable combo hit sound")
        # ... mirror countdown ...
        
        # Milestone audio
        # ... mirror countdown audio_last ...
        
        # Position & size group
        self._pos_group = QGroupBox("Position & size", self)
        # ... 4 spinbox X, Y, Width, Height ...
        form.addRow(self._pos_group)
        
        # Style group
        self._style_group = QGroupBox("Style", self)
        style_form = QFormLayout(self._style_group)
        
        # Border thickness
        self._border_sp = QDoubleSpinBox()
        self._border_sp.setRange(0.0, 10.0)
        self._border_sp.setSingleStep(0.5)
        self._border_sp.setDecimals(1)
        self._border_sp.setValue(float(config.get("combo_border_thickness", 2.0)))
        style_form.addRow("Border thickness", self._border_sp)
        
        # Glow
        self._glow_sp = QSpinBox()
        self._glow_sp.setRange(0, 100)
        self._glow_sp.setSuffix(" %")
        self._glow_sp.setValue(int(round(float(config.get("combo_glow_strength", 30.0)))))
        style_form.addRow("Glow strength", self._glow_sp)
        
        form.addRow(self._style_group)

    def get_config(self) -> dict:
        return {
            "combo_enabled": self._enabled_cb.isChecked(),
            "combo_color": self._color,
            "combo_label": self._label_edit.text().strip() or "COMBO",
            "combo_fade_after_break_sec": self._fade_sp.value(),
            "combo_anim": self._normalize_anim(self._anim_cb.currentData()),
            "combo_audio_enabled": self._audio_enabled_cb.isChecked(),
            "combo_audio_mode": self._normalize_audio_mode(self._audio_mode_cb.currentData()),
            "combo_audio_file": self._audio_file_edit.get_value(),
            "combo_audio_volume": self._audio_volume_sp.value(),
            "combo_audio_milestone_mode": self._normalize_milestone_mode(...),
            "combo_audio_milestone_file": self._audio_milestone_file_edit.get_value(),
            "combo_x": self._x_sp.value(),
            "combo_y": self._y_sp.value(),
            "combo_w": self._w_sp.value(),
            "combo_h": self._h_sp.value(),
            "combo_border_thickness": self._border_sp.value(),
            "combo_glow_strength": float(self._glow_sp.value()),
        }
```

UI layout cuối cùng:

```
[Main form]
  Combo            [✓] Enabled
  Color            [colored button]
  Font family      [combo: Simplex/Plain/Duplex/Complex/Triplex/
                          Complex Small/Script Simplex/Script Complex]
  Label text       [QLineEdit "COMBO"]
  Fade after break [spinbox 0.5 s]
  Number effect    [combo: Pop/Flash/Fade Cross/Shake]
  Audio            [✓] Enable combo hit sound
  Sound source     [combo]                     (visible if audio on)
  Sound file       [path browse]               (visible if mode=file)
  Sound volume     [spinbox 0.65]              (visible if audio on)
  Milestone sound  [combo]                     (visible if audio on)
  Milestone file   [path browse]               (visible if milestone=file)

[Position & size groupbox]
  X / Y / Width / Height

[Style groupbox]
  Border thickness
  Glow strength

[Tier milestones groupbox]
  Tier 1 ≥ [spinbox 30]   Label [QLineEdit "Great"]
  Tier 2 ≥ [spinbox 60]   Label [QLineEdit "Superb"]
  Tier 3 ≥ [spinbox 90]   Label [QLineEdit "Perfect"]
  Tier 4 ≥ [spinbox 120]  Label [QLineEdit "Godlike"]
  Hint: Set threshold = 0 to disable a tier.
```

### 5. `studio/editor/inspector_panel.py`

Route combo trong `_make_section_for_layer`:

```python
elif kind == "combo":
    from studio.editor.layer_edit_dialog import _ComboSection
    return _ComboSection(cfg, self)
```

### 6. `src/rhythm.py` — Refactor ComboHUD

ComboHUD constructor mirror CountdownHUD:

```python
class ComboHUD:
    _FONT_MAP = {
        "simplex": cv2.FONT_HERSHEY_SIMPLEX,
        "plain": cv2.FONT_HERSHEY_PLAIN,
        "duplex": cv2.FONT_HERSHEY_DUPLEX,
        "complex": cv2.FONT_HERSHEY_COMPLEX,
        "triplex": cv2.FONT_HERSHEY_TRIPLEX,
        "complex_small": cv2.FONT_HERSHEY_COMPLEX_SMALL,
        "script_simplex": cv2.FONT_HERSHEY_SCRIPT_SIMPLEX,
        "script_complex": cv2.FONT_HERSHEY_SCRIPT_COMPLEX,
    }

    def __init__(
        self,
        cam: PerspectiveCamera,
        *,
        enabled: bool = True,
        color: str = "#FFFFFF",
        label: str = "COMBO",
        font_family: str = "duplex",
        fade_after_break_sec: float = 0.5,
        anim: str = "pop",
        bbox: tuple = (0.85, 0.08, 0.13, 0.18),
        border_thickness: float = 2.0,
        glow_strength: float = 30.0,
        # Tier system
        tier1_threshold: int = 30, tier1_label: str = "Great",
        tier2_threshold: int = 60, tier2_label: str = "Superb",
        tier3_threshold: int = 90, tier3_label: str = "Perfect",
        tier4_threshold: int = 120, tier4_label: str = "Godlike",
    ):
        self.cam = cam
        self.combo = 0
        self.rating = ''
        self.rating_frame = -999
        # Config
        self._enabled = bool(enabled)
        self._color_bgr = _hex_to_bgr(color, default=(255,255,255))
        self._label = str(label)
        self._font_family = str(font_family)
        self._fade_break_sec = max(0.0, float(fade_after_break_sec))
        self._anim_mode = self._normalize_anim(anim)
        self._border_thickness = max(0.0, min(10.0, float(border_thickness)))
        self._glow_strength = max(0.0, min(100.0, float(glow_strength)))
        self.set_bbox(*bbox)
        # Tier thresholds + labels
        self._tier1_threshold = max(0, int(tier1_threshold))
        self._tier1_label = str(tier1_label)
        self._tier2_threshold = max(0, int(tier2_threshold))
        self._tier2_label = str(tier2_label)
        self._tier3_threshold = max(0, int(tier3_threshold))
        self._tier3_label = str(tier3_label)
        self._tier4_threshold = max(0, int(tier4_threshold))
        self._tier4_label = str(tier4_label)
        # Track combo break for fade
        self._break_frame = -999
        self._last_combo = 0
        self._last_change_frame = -999

    def _get_font(self) -> int:
        return self._FONT_MAP.get(self._font_family, cv2.FONT_HERSHEY_DUPLEX)

    def _resolve_label(self, combo: int) -> str:
        """Return label text dựa trên combo count + tier thresholds.
        Check từ tier cao nhất xuống thấp nhất.  Threshold = 0 → tier disabled.
        """
        if self._tier4_threshold > 0 and combo >= self._tier4_threshold:
            return self._tier4_label
        if self._tier3_threshold > 0 and combo >= self._tier3_threshold:
            return self._tier3_label
        if self._tier2_threshold > 0 and combo >= self._tier2_threshold:
            return self._tier2_label
        if self._tier1_threshold > 0 and combo >= self._tier1_threshold:
            return self._tier1_label
        return self._label   # default "COMBO"

    def set_tier(self, n: int, threshold: int | None = None, label: str | None = None):
        """Hot-update tier n (1..4) threshold or label."""
        if n not in (1, 2, 3, 4):
            return
        if threshold is not None:
            setattr(self, f"_tier{n}_threshold", max(0, int(threshold)))
        if label is not None:
            setattr(self, f"_tier{n}_label", str(label))

    def set_bbox(self, x, y, w, h):
        # mirror CountdownHUD.set_box
        ...

    def set_style(self, **kwargs):
        # hot-update color, label, anim, border_thickness, glow_strength
        ...

    def register_hit(self, cur_frame: int):
        if self.combo > 0 and self.combo != self._last_combo:
            self._last_change_frame = cur_frame
        self.combo += 1
        self.rating = 'GOOD'
        self.rating_frame = cur_frame
        self._last_combo = self.combo

    def register_miss(self, cur_frame: int):
        if self.combo > 0:
            self._break_frame = cur_frame
        self.combo = 0
        self.rating = 'MISS'
        self.rating_frame = cur_frame

    def draw(self, canvas, cur_frame):
        if not self._enabled:
            return
        # Compute fade alpha if just broken
        alpha = 1.0
        if self.combo == 0 and self._break_frame > 0:
            elapsed = (cur_frame - self._break_frame) / float(self.cam.W) * self._fade_break_sec  # rough fps proxy
            # Better: use fps from elsewhere. Approximate for now.
            if elapsed >= self._fade_break_sec:
                return  # fully faded
            alpha = 1.0 - (elapsed / self._fade_break_sec)
        elif self.combo == 0:
            return  # no combo, no break tracked

        # Draw number + label inside bbox với border + glow
        # Pattern tương tự CountdownHUD._draw_glow_text
        ...
```

ComboHUD draw logic mirror CountdownHUD render structure (border + inner + glow).

### 7-10. Live renderer + main_window + preview_panel + render_service

Cùng pattern các spec layer khác — pass **26 keys** end-to-end:

```python
# render_service.py _ALLOWED_KEYS thêm:
"combo_enabled",
"combo_color",
"combo_label",
"combo_font_family",
"combo_fade_after_break_sec",
"combo_anim",
"combo_audio_enabled",
"combo_audio_mode",
"combo_audio_file",
"combo_audio_volume",
"combo_audio_milestone_mode",
"combo_audio_milestone_file",
"combo_x", "combo_y", "combo_w", "combo_h",
"combo_border_thickness",
"combo_glow_strength",
"combo_tier1_threshold", "combo_tier1_label",
"combo_tier2_threshold", "combo_tier2_label",
"combo_tier3_threshold", "combo_tier3_label",
"combo_tier4_threshold", "combo_tier4_label",
```

`live_renderer.update_render_settings`, `main_window._live_renderer_kwargs`, `preview_panel.update_render_settings`: thêm 26 params optional, forward tương ứng.

Hot-update strategy:
- `font_family`, `label`, `color`, `border_thickness`, `glow_strength`, `tierX_label`: dùng `combo_hud.set_style(**kwargs)`.
- `x/y/w/h`: dùng `combo_hud.set_bbox(...)`.
- `tierX_threshold`: dùng `combo_hud.set_tier(n, threshold=...)`.
- `enabled` toggle: rebuild instance hoặc set flag.

Argparse: thêm 26 CLI flags trong `src/rhythm.py`. Pattern mirror countdown flags hiện có.

### 11. `studio/editor/preview_panel.py` — `FloorWallOverlay` thêm combo box drag

Mirror countdown box drag handles:

```python
class FloorWallOverlay(QWidget):
    # ... existing state ...
    
    # NEW: Combo box state
    self._combo_enabled = False
    self._cb_x = 0.85
    self._cb_y = 0.08
    self._cb_w = 0.13
    self._cb_h = 0.18

    def set_combo_proxy(self, enabled, x, y, w, h):
        """Set combo box position from caller (similar to countdown)."""
        self._combo_enabled = bool(enabled)
        self._cb_x = max(0.0, min(0.98, float(x)))
        ...
        self.update()

    def _combo_rect_px(self) -> QRect:
        # mirror _countdown_rect_px
        ...

    def _handle_at(self, pos):
        # ... existing handlers ...
        if self._combo_enabled:
            cb = self._combo_rect_px()
            corners = {
                "cb_tl": ...,
                "cb_tr": ...,
                "cb_bl": ...,
                "cb_br": ...,
            }
            for kind, rr in corners.items():
                if rr.contains(pos):
                    return kind
            if cb.contains(pos):
                return "cb_move"
        # ... rest ...

    def mouseMoveEvent(self, ev):
        # ... existing ...
        elif self._drag and self._drag.startswith("cb_"):
            # mirror cd_* drag logic
            ...
            self._cb_x, self._cb_y, self._cb_w, self._cb_h = x, y, ww, hh

    def paintEvent(self, _ev):
        # ... existing ...
        if self._combo_enabled:
            cb = self._combo_rect_px()
            p.setBrush(QBrush(QColor(220, 50, 50, 36)))    # red tint
            p.setPen(QPen(QColor(255, 90, 90), 2.0, Qt.PenStyle.DashLine))
            p.drawRect(cb)
            # 4 corners
            ...
            p.drawText(cb.left() + 6, max(14, cb.top() - 6), "Combo")
```

Signal `changing` / `committed` của FloorWallOverlay đã có nhiều floats. Thêm 4 floats cho combo bbox → signal payload tăng từ 12 → 16 floats. Hoặc refactor sang dict signal (xem Open question).

Pattern trong `_on_floor_wall_edit_toggled` (preview_panel) gọi `set_combo_proxy(...)` lấy giá trị từ combo layer config, giống cách gọi `set_start_gate_proxy`.

---

## Render order

Combo HUD vẽ **TRÊN** mọi layer khác (giống countdown):

```
Background → Start Gate → Floor → Side Rails → Blocks → Stickman → Countdown → Combo
```

Combo last để không bị che bởi blocks bay qua.

---

## Backward compat

**Project cũ không có combo layer** → Pydantic fall back default → ComboHUD render top-right với "COMBO" trắng = identical với pre-feature behavior. Không regression.

**Migration**: KHÔNG cần (combo_* fields chưa từng có trong seg.render_settings cũ).

---

## Auto-create policy

V1: KHÔNG auto-create. User chủ động thêm qua nút "+" trên track combo. Combo applicable cho mọi mode game (punch/dance/line/relax) nhưng V1 cho user quyết khi nào hiện.

V2 có thể auto-create cho non-relax modes nếu user feedback muốn.

---

## Test scenarios

### Test 1: Tạo combo layer mới

```
Setup: project mới.
Action: click "+" trên track combo timeline.
Verify: tạo Layer(kind="combo") với 17 keys default.
Verify: render preview thấy "0 COMBO" hoặc ẩn (combo=0) ở top-right (default position).
```

### Test 2: Edit label text

```
Action: trong _ComboSection, đổi label "COMBO" → "HITS".
Verify: layer.config["combo_label"] == "HITS".
Verify: live preview hiện "HITS".
```

### Test 3: Edit color

```
Action: pick color đỏ #FF0000.
Verify: số combo + label đều màu đỏ.
```

### Test 4: Drag combo box trong preview

```
Action: bật Edit Layout, drag combo box (red dashed) tới vị trí mới.
Verify: layer.config["combo_x/y/w/h"] update qua _on_floor_wall_committed.
Verify: render position match drag location.
```

### Test 5: Edit position bbox qua spinbox Inspector

```
Action: đổi X = 0.05, Y = 0.85 (top-right → bottom-left).
Verify: combo HUD render bottom-left.
Verify: drag overlay handle move tương ứng.
```

### Test 6: Animation pop khi combo tăng

```
Action: chạy live preview, có punch hits.
Verify: số combo tăng + visual pop animation (scale up rồi settle).
Action: đổi anim → "shake".
Verify: thay vì pop, số rung khi tăng.
```

### Test 7: Fade sau combo break

```
Setup: combo_fade_after_break_sec = 2.0.
Action: combo đạt 10, sau đó miss.
Verify: số combo "10" fade dần trong 2s rồi biến mất.
Action: đổi fade = 0.
Verify: số combo biến mất ngay khi miss.
```

### Test 8: Audio hit sound

```
Setup: combo_audio_enabled = True, mode = "file", file = "tick.wav".
Action: combo tăng.
Verify: tick sound play mỗi hit.
```

### Test 9: Milestone audio

```
Setup: combo_audio_milestone_mode = "default".
Action: combo đạt 10, 20, 30, ...
Verify: milestone beep play mỗi 10 combo.
```

### Test 10: Border thickness

```
Action: border_thickness 2 → 8.
Verify: viền số dày hơn rõ rệt.
```

### Test 11: Glow strength

```
Action: glow 30 → 100.
Verify: glow mạnh hơn.
Action: glow → 0.
Verify: không glow.
```

### Test 12: Disable combo

```
Action: bỏ check Enabled.
Verify: combo HUD biến mất khỏi render.
```

### Test 13: CLI export pipeline

```
Setup: project có combo layer custom.
Action: render export.
Verify: CLI args bao gồm 17 --combo_* flags.
Verify: rendered video có combo HUD theo config.
```

### Test 14: Backward compat

```
Setup: project cũ không có combo layer.
Verify: render với defaults (top-right, white, "COMBO").
Verify: identical với pre-feature.
```

### Test 15: Pydantic validation

```
BaseRenderSettings(combo_x=1.5) → ValidationError.
BaseRenderSettings(combo_glow_strength=200) → ValidationError.
BaseRenderSettings(combo_anim="invalid") → ValidationError.
```

### Test 16: Drop media reject (qua spec inspector-drop)

```
Action: drop image vào _ComboSection.
Verify: REJECT — combo không accept media.
```

### Test 17: Hot-update qua live preview

```
Setup: live preview đang chạy.
Action: edit label "COMBO" → "STREAK". Đợi debounce.
Verify: frame kế tiếp hiện "STREAK", không flicker, không rebuild scene.
```

### Test 18: Font family change

```
Setup: combo_font_family = "duplex" (default).
Action: đổi sang "script_complex" trong Inspector.
Verify: chữ COMBO + số render với font handwriting bold.
Action: đổi sang "plain".
Verify: chữ render với font mảnh, gầy.
Verify: tất cả 8 cv2 fonts work.
```

### Test 19: Tier transition theo combo count

```
Setup: defaults — tiers [30, 60, 90, 120] với labels [Great, Superb, Perfect, Godlike].
Action: chạy live preview, có punch hits liên tục.
Verify ở các combo count:
  - combo = 1: label "COMBO"
  - combo = 29: label "COMBO"
  - combo = 30: label "Great" (transition + animation pop)
  - combo = 59: label "Great"
  - combo = 60: label "Superb" (transition + anim)
  - combo = 90: label "Perfect"
  - combo = 120: label "Godlike"
  - combo = 500: label vẫn "Godlike"
```

### Test 20: Tier threshold custom

```
Setup: tier1=10, tier2=25, tier3=50, tier4=100 (lower thresholds).
Verify combo transitions tại các giá trị mới.
```

### Test 21: Disable tier (threshold = 0)

```
Setup: tier1=0 (disabled), tier2=50, tier3=0 (disabled), tier4=200.
Verify:
  - combo 1-49: "COMBO"
  - combo 50: "Superb" (tier2, vì tier1 disabled)
  - combo 100: "Superb" (skip tier3 disabled)
  - combo 200: "Godlike" (tier4)
```

### Test 22: Tier label custom text

```
Action: đổi tier1_label "Great" → "Hot streak!".
Verify: ở combo 30, label hiển thị "Hot streak!" thay vì "Great".
```

### Test 23: Tier ngược (threshold ngẫu nhiên)

```
Setup: tier1=100, tier2=50, tier3=200, tier4=30 (không monotone).
Action: combo tăng từ 1 → 250.
Verify resolve theo logic tier4 → tier3 → tier2 → tier1:
  - combo 30 → tier4 hit → "Godlike" (vì check tier4 trước)
  - combo 50 → vẫn "Godlike" (tier4 vẫn match 30)
  - combo 100 → vẫn "Godlike"
  - combo 200 → "Perfect" (tier3 hit, but tier4 has 30 cũng match... wait check thứ tự)

Lưu ý: logic check từ tier4 cao nhất xuống tier1. Nếu tier4=30 và combo=50:
  - tier4 check: 30 > 0 và 50 >= 30 → return tier4_label "Godlike"
  - Không check tier3 trở xuống.

→ Nếu user set threshold ngược thứ tự (tier4 < tier1), tier4 sẽ thắng vì check trước.
   Đây là behavior expected — user nên set threshold tăng dần (tier1 < tier2 < tier3 < tier4).
   
Có thể add Inspector validation warn user nếu tier không monotone (V2 polish).
```

### Test 24: Tier reset khi combo break

```
Setup: combo đạt 95 → label "Perfect".
Action: miss → combo reset 0.
Verify: label về "COMBO" ngay (sau khi fade hoàn tất).
Action: combo tăng lại.
Verify: tier transition lại từ đầu.
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **Existing ComboHUD logic** (combo, rating, register_hit, register_miss): giữ 100%. Chỉ refactor `__init__` + `draw` để dùng config.

2. **Rating badge** (GOOD/GREAT/SUPERB/PERFECT): giữ nguyên hardcoded (V2 nếu user muốn config riêng cho rating thì spec sau).

3. **Pattern mirror countdown**: mọi field tương ứng dùng cùng valid range, default convention. Audio mode enums mirror chính xác.

4. **Layer track height update** 192 → 224 (= 7 lanes × 32px): cẩn thận update `_BEAT_STRIP_Y`, `_WAVE_TRACK_Y`, `_SCENE_H`.

5. **Drag overlay signal payload**: hiện tăng từ 12 → 16 floats. Có thể refactor sang dict (xem Open question 1) hoặc tiếp tục tuple.

6. **`FloorWallOverlay` không phá countdown drag**: thêm combo state riêng `_cb_x/y/w/h`, không đụng `_cd_x/y/w/h`.

7. **Default position** (0.85, 0.08, 0.13, 0.18): match gần với hardcode top-right cũ.

8. **Auto-create**: KHÔNG auto-create combo. Test `test_auto_create_does_not_include_*` cần extend.

9. **Pydantic schema**: thêm 17 fields mới, không xoá fields cũ.

10. **Inspector drop**: combo Section reject mọi media drop (theo spec inspector-drop-media).

---

## Pattern code hiện có để tham khảo

- **`_CountdownSection`** trong layer_edit_dialog.py: mirror UI pattern.
- **`CountdownHUD`** trong rhythm.py (~dòng 4522): mirror class structure (set_box, set_style, draw_glow_text).
- **`_VISUAL_FIELDS_BY_KIND["countdown"]`** trong layer.py: mirror visual fields list.
- **Countdown bbox drag** trong `FloorWallOverlay` (preview_panel.py): mirror handle hit-test + drag logic, đổi prefix `cd_` → `cb_`.
- **`set_start_gate_proxy`** pattern trong FloorWallOverlay: mirror để có `set_combo_proxy`.
- **Audio playback** cho countdown trong preview_panel.py (`_play_countdown_tick_sound`): mirror cho combo hit + milestone sounds.

---

## Thứ tự implement đề xuất

### Phase 1: Backend

1. Pydantic schema (17 fields).
2. Layer model: kind + colors + visual fields.
3. Default config trong timeline_panel.

### Phase 2: Renderer refactor

4. Refactor ComboHUD constructor + draw + set_style + set_bbox.
5. Argparse 17 flags + viz attrs + wire.
6. Manual test CLI: chạy với `--combo_label HITS --combo_color "#FF0000"` verify visual.

### Phase 3: Live preview pipeline

7. live_renderer params + state + ComboHUD instance + hot-update.
8. main_window _live_renderer_kwargs.
9. preview_panel forward params.
10. render_service _ALLOWED_KEYS.

### Phase 4: UI

11. Tạo `_ComboSection` (mirror _CountdownSection).
12. Inspector route combo.
13. Timeline lane height update + icon + default config.

### Phase 5: Drag overlay

14. FloorWallOverlay: thêm combo state, hit-test, drag logic, paint.
15. Update signal payload (12 → 16 floats hoặc refactor dict).
16. preview_panel `_on_floor_wall_edit_toggled` gọi `set_combo_proxy`.
17. preview_panel `_on_floor_wall_committed` persist combo bbox vào layer.

### Phase 6: Audio (optional V1)

18. Audio playback cho hit sound + milestone (mirror countdown audio).
19. Milestone threshold detection (vd combo % 10 == 0).

### Phase 7: Tests + smoke

20. Unit tests cho layer kind + Pydantic.
21. Smoke test: tạo layer, edit Inspector, drag overlay, render export — full pipeline.

---

## Acceptance criteria

✓ Layer kind "combo" registered đầy đủ (model, schema, timeline lane, inspector route)  
✓ 26 fields total: 16 mirror countdown + 1 label + 1 font_family + 8 tier (4×{threshold,label})  
✓ User edit label, color, font, anim, audio, position, border, glow qua Inspector  
✓ Font family selector với 8 cv2 fonts hoạt động  
✓ Tier system 4 milestones với threshold + label customizable  
✓ Tier transition tự động khi combo crosses threshold (kèm anim)  
✓ Tier disable khi threshold = 0  
✓ Drag combo box trong preview overlay (giống countdown)  
✓ Hot-update real-time qua live preview (bao gồm font, tier)  
✓ Render export pass đủ 26 CLI flags  
✓ Backward compat: project cũ render với defaults  
✓ Pydantic validation hoạt động (font enum + tier ranges)  
✓ Combo audio: hit sound + milestone sound  
✓ Combo fade after break smooth  
✓ Auto-create policy: V1 KHÔNG auto-create  
✓ Inspector drop reject media (theo spec inspector-drop)

---

## Open questions

(1) **Signal payload size**: FloorWallOverlay `changing`/`committed` hiện 12 floats. Thêm combo bbox → 16 floats. Có nên refactor sang `Signal(dict)` hoặc tách signal riêng cho combo (`combo_box_changing`, `combo_box_committed`)? Tôi đề xuất **tách signal riêng** cho clarity.

(2) **`combo_fade_after_break_sec`**: semantic mirror countdown's `max_sec` không hoàn toàn match. Có nên rename thành `combo_fade_duration_sec` cho rõ? Hay giữ name "max_sec" để đồng bộ field countdown?

(3) **Milestone threshold**: hardcoded 10? Hay add field `combo_milestone_every_n: int` (default 10)? V1 đề xuất hardcoded, V2 expose nếu cần.

(4) **Single color** vs **separate number/label colors**: hiện single (mirror countdown). Nhưng combo có 2 elements (số + label), thường style khác nhau. Có muốn V1 thêm `combo_label_color` không? Tôi đề xuất V1 single color (đơn giản, mirror countdown). Nếu user complaint, V2 thêm.

(5) **Font scale**: mirror countdown chỉ có `relax_countdown_max_sec` ảnh hưởng size visible window, không có font_scale. Combo cần customize size không? Đề xuất add `combo_font_scale` (NOT trong countdown nhưng combo cần) — total 18 fields. Hoặc skip vì auto-fit theo bbox.

(6) **Drag overlay handle color**: red (CLR_WALL_PINK area) có conflict với countdown pink? Đề xuất green (#22c55e) hoặc orange (#ea580c) cho combo box phân biệt.

(7) **Auto-create cho mọi mode**: combo applicable hơn countdown (countdown chỉ relax, combo mọi mode). Có nên auto-create cho mọi segment không? V1 đề xuất KHÔNG. V2 cho mọi mode trừ pure-relax.

(8) **Reset combo on segment change**: combo carry over giữa segments hay reset khi switch? Hiện carry over (existing behavior). User có muốn reset không?

(9) **Combo break event**: hiện chỉ có miss. Có sự kiện nào khác break combo không? (vd timing too late). Game logic existing.

(10) **Replace existing layer-combo-spec**: spec này thay thế version trước (11 fields). Có cần lưu version cũ làm history hay xóa? Đề xuất xóa để clean.
