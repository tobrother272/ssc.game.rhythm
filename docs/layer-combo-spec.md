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

## Visual style guide — Match reference image

### Reference

```
   ┌──────────────────────────┐
   │            40            │  ← Number  (large, bold white)
   │           COMBO          │  ← COMBO label (small, white)
   │    ┌──────────────┐      │
   │    │    GREAT     │      │  ← Tier pill (green bg, white text, glow)
   │    └──────────────┘      │
   └──────────────────────────┘
```

### Diff với current implementation

Code hiện tại (`ComboHUD._draw_tier_badge` ở rhythm.py ~dòng 5023-5108) render badge style **tilted -8° + dark fill + neon border + heavy glow**. Mẫu user yêu cầu **straight (no tilt) + solid color fill + clean white text + outer glow**.

3 thay đổi visual cần làm:

| Aspect | Current | Match reference |
|---|---|---|
| Tilt | `-8°` | `0°` (straight) |
| Pill background | Dark `(18,18,18)` + colored border | **Solid colored fill** (no dark BG, color = tier color) |
| Text style | Per-tier color text + heavy glow | **Solid white text**, light shadow only |
| Glow | Heavy multi-pass blur on border | Single subtle outer glow halo |

### 7 fields mới

Để user customize 3 parts riêng + tier colors:

```
combo_number_font_scale   : float  default 0.0   (0 = auto-fit theo bbox; > 0 = override)
combo_label_font_scale    : float  default 0.0   (auto-fit)
combo_tier_font_scale     : float  default 0.0   (auto-fit theo badge_h)

combo_tier1_color         : hex    default "#22c55e"   (green, "Great")
combo_tier2_color         : hex    default "#3b82f6"   (cyan-blue, "Superb")
combo_tier3_color         : hex    default "#a855f7"   (purple, "Perfect")
combo_tier4_color         : hex    default "#f59e0b"   (gold, "Godlike")
```

→ Tổng spec layer combo: **26 + 7 = 33 fields**.

### Render logic mới

#### Part 1: Number

```python
# Font: combo_font_family (existing) — đề xuất "triplex" cho bold serif feel
font_face = self._get_font()    # default duplex; reference look = triplex
target_h = int(bh * 0.62) if self._number_fs <= 0 else None   # auto vs override
if self._number_fs > 0:
    num_fs = float(self._number_fs)
else:
    # auto-fit
    (ref_nw, ref_nh), _ = cv2.getTextSize(txt_num, font_face, 1.0, 4)
    num_fs = max(0.3, target_h / max(1, ref_nh))

# Color: combo_color (default white)
# Border thickness: combo_border_thickness (default 2.0) — vẽ outline đen trước
# Glow: combo_glow_strength (default 30) — bao quanh number
self._draw_glow_text(canvas, text=txt_num, ..., color_bgr=self._color_bgr)
```

#### Part 2: "COMBO" label

```python
# Same font as number (combo_font_family)
# Smaller scale (auto-fit ~0.18 × bh, hoặc override qua combo_label_font_scale)
target_lbl_h = int(bh * 0.18) if self._label_fs <= 0 else None
if self._label_fs > 0:
    lbl_fs = float(self._label_fs)
else:
    (ref_lw, ref_lh), _ = cv2.getTextSize(self._label, font_face, 1.0, 1)
    lbl_fs = max(0.2, target_lbl_h / max(1, ref_lh))

# Color: dim white (80% của combo_color)
lbl_color = tuple(int(c * 0.85) for c in self._color_bgr)
self._draw_glow_text(canvas, text=self._label, ..., color_bgr=lbl_color, alpha=...)
```

#### Part 3: Tier badge (pill) — **rewrite `_draw_tier_badge`**

```python
def _draw_tier_badge(
    self,
    canvas, text, cx, cy, badge_w, badge_h,
    color_bgr,           # tier color (vd green for tier 1)
    alpha=1.0,
):
    """Solid-color pill badge với clean white text — match reference."""
    if alpha <= 1e-4 or badge_w < 8 or badge_h < 8:
        return
    
    pad = max(8, int(badge_h * 0.5))   # outer glow padding
    buf_w = badge_w + 2 * pad
    buf_h = badge_h + 2 * pad
    rx0, ry0 = pad, pad
    rx1, ry1 = pad + badge_w, pad + badge_h
    rcx = (rx0 + rx1) // 2
    rcy = (ry0 + ry1) // 2
    rr = max(8, badge_h // 2)   # rounded corners ~50% height = full pill
    
    # Buffer
    badge_buf = np.zeros((buf_h, buf_w, 3), dtype=np.uint8)
    
    # 1) Outer glow halo (subtle, single pass)
    if self._glow_strength > 0:
        glow_buf = np.zeros_like(badge_buf)
        self._draw_rounded_rect(glow_buf, (rx0, ry0), (rx1, ry1),
                                color_bgr, rr, -1)
        ksize = max(7, int(badge_h * 0.4)) | 1
        glow_buf = cv2.GaussianBlur(glow_buf, (ksize, ksize), 0)
        glow_norm = self._glow_strength / 100.0
        badge_buf = cv2.addWeighted(badge_buf, 1.0, glow_buf, 0.6 * glow_norm, 0)
    
    # 2) Pill SOLID FILL (tier color, no dark BG)
    self._draw_rounded_rect(badge_buf, (rx0, ry0), (rx1, ry1),
                            color_bgr, rr, -1)
    
    # 3) Optional dark border (~1-2px) cho contrast với background
    border_color = tuple(int(c * 0.4) for c in color_bgr)
    self._draw_rounded_rect(badge_buf, (rx0, ry0), (rx1, ry1),
                            border_color, rr, 2)
    
    # 4) Tier label text — uppercase, bold, white
    txt_upper = text.upper()
    font_b = cv2.FONT_HERSHEY_DUPLEX   # hoặc TRIPLEX cho bold hơn
    if self._tier_fs > 0:
        tfs = float(self._tier_fs)
    else:
        # Auto-fit: text height ~58% badge height
        target_th = int(badge_h * 0.58)
        target_tw = int(badge_w * 0.85)   # 85% width with margin
        (ref_w, ref_h), _ = cv2.getTextSize(txt_upper, font_b, 1.0, 2)
        tfs = max(0.3, min(target_th / max(1, ref_h),
                          target_tw / max(1, ref_w)))
    (tw, th), _ = cv2.getTextSize(txt_upper, font_b, tfs, 2)
    tx = rcx - tw // 2
    ty = rcy + th // 2
    
    # Subtle shadow (light depth)
    cv2.putText(badge_buf, txt_upper, (tx + 1, ty + 1), font_b, tfs,
                (0, 0, 0), 2, cv2.LINE_AA)
    
    # Main text — white
    cv2.putText(badge_buf, txt_upper, (tx, ty), font_b, tfs,
                (255, 255, 255), 2, cv2.LINE_AA)
    
    # Composite onto canvas (NO TILT — straight)
    x0 = cx - buf_w // 2
    y0 = cy - buf_h // 2
    self._blit_with_alpha(canvas, badge_buf, x0, y0, alpha)


def _blit_with_alpha(self, canvas, src, x0, y0, alpha):
    """Composite src onto canvas at (x0, y0) with alpha blend."""
    # Same compositing logic as existing code, but no tilt rotation
    H_c, W_c = canvas.shape[:2]
    h_s, w_s = src.shape[:2]
    sx0 = max(0, -x0)
    sy0 = max(0, -y0)
    sx1 = w_s - max(0, x0 + w_s - W_c)
    sy1 = h_s - max(0, y0 + h_s - H_c)
    dx0 = max(0, x0)
    dy0 = max(0, y0)
    dx1 = min(W_c, x0 + w_s)
    dy1 = min(H_c, y0 + h_s)
    if dx1 <= dx0 or dy1 <= dy0 or sx1 <= sx0 or sy1 <= sy0:
        return
    
    c_roi = canvas[dy0:dy1, dx0:dx1].astype(np.float32)
    src_s = src[sy0:sy1, sx0:sx1].astype(np.float32)
    
    # Mask: pixels có content (non-zero) = visible
    mask = np.clip(src_s.max(axis=2, keepdims=True) / 80.0, 0, 1) * alpha
    c_roi = c_roi * (1 - mask) + src_s * mask
    canvas[dy0:dy1, dx0:dx1] = np.clip(c_roi, 0, 255).astype(np.uint8)
```

### Tier color resolution (sửa `_TIER_COLORS_BGR`)

Code cũ có hardcoded `_TIER_COLORS_BGR` dict. Sửa thành **dynamic from config**:

```python
def _resolve_tier_color(self, tier_idx: int) -> tuple:
    """Return BGR tuple cho tier color, đọc từ config."""
    if tier_idx == 4:
        return _hex_to_bgr(self._tier4_color, default=(11, 158, 245))   # gold
    if tier_idx == 3:
        return _hex_to_bgr(self._tier3_color, default=(247, 85, 168))   # purple
    if tier_idx == 2:
        return _hex_to_bgr(self._tier2_color, default=(246, 130, 59))   # cyan-blue
    if tier_idx == 1:
        return _hex_to_bgr(self._tier1_color, default=(94, 197, 34))    # green
    return self._color_bgr   # fallback
```

(Lưu ý BGR order: hex `#22c55e` (green) → BGR `(94, 197, 34)`.)

### Config defaults summary

```python
combo_tier1_color = "#22c55e"   # green   (BGR 94, 197, 34)
combo_tier2_color = "#3b82f6"   # blue    (BGR 246, 130, 59)
combo_tier3_color = "#a855f7"   # purple  (BGR 247, 85, 168)
combo_tier4_color = "#f59e0b"   # gold    (BGR 11, 158, 245)
```

Inspector cho phép user pick custom color từng tier.

### Recommended fonts cho match mẫu

- Number: `triplex` (cv2.FONT_HERSHEY_TRIPLEX) — bold serif, đậm hơn duplex
- COMBO label: `simplex` (sans-serif clean)
- Tier badge text: `duplex` (sans-serif bold)

→ User có thể đổi qua `combo_font_family` (single field áp cho tất cả) hoặc V2 thêm 3 font_family riêng. V1 dùng 1 font shared.

### UI Section update

```
[Style groupbox]
  Border thickness     [spinbox 2.0]
  Glow strength        [spinbox 30 %]
  Number font scale    [spinbox 0.0]   tooltip "0 = auto-fit"
  Label font scale     [spinbox 0.0]   tooltip "0 = auto-fit"
  Tier font scale      [spinbox 0.0]   tooltip "0 = auto-fit"

[Tier milestones groupbox]
  Tier 1 ≥ [30]   Label [Great]    Color [#22c55e button]
  Tier 2 ≥ [60]   Label [Superb]   Color [#3b82f6 button]
  Tier 3 ≥ [90]   Label [Perfect]  Color [#a855f7 button]
  Tier 4 ≥ [120]  Label [Godlike]  Color [#f59e0b button]
```

3 row tier giờ có 3 cột: threshold + label text + color picker.

### Pydantic schema additions

```python
combo_number_font_scale: float = Field(default=0.0, ge=0.0, le=10.0)
combo_label_font_scale: float = Field(default=0.0, ge=0.0, le=5.0)
combo_tier_font_scale: float = Field(default=0.0, ge=0.0, le=5.0)
combo_tier1_color: str = "#22c55e"
combo_tier2_color: str = "#3b82f6"
combo_tier3_color: str = "#a855f7"
combo_tier4_color: str = "#f59e0b"
```

### `_VISUAL_FIELDS_BY_KIND["combo"]` thêm 7 keys

```python
"combo_number_font_scale",
"combo_label_font_scale",
"combo_tier_font_scale",
"combo_tier1_color",
"combo_tier2_color",
"combo_tier3_color",
"combo_tier4_color",
```

### Constructor updates

```python
def __init__(
    self,
    cam,
    *,
    # ... existing params ...
    number_font_scale: float = 0.0,
    label_font_scale: float = 0.0,
    tier_font_scale: float = 0.0,
    tier1_color: str = "#22c55e",
    tier2_color: str = "#3b82f6",
    tier3_color: str = "#a855f7",
    tier4_color: str = "#f59e0b",
):
    # ...
    self._number_fs = max(0.0, float(number_font_scale))
    self._label_fs = max(0.0, float(label_font_scale))
    self._tier_fs = max(0.0, float(tier_font_scale))
    self._tier1_color = str(tier1_color)
    self._tier2_color = str(tier2_color)
    self._tier3_color = str(tier3_color)
    self._tier4_color = str(tier4_color)
```

### Test cases bổ sung

**Test 25**: Match reference visual
```
Setup: defaults — number=auto, label=auto, tier=auto, tier1_color=#22c55e.
Action: trigger combo 31, screenshot frame.
Verify side-by-side với reference image:
  - Number "31" white bold ở top
  - "COMBO" white nhỏ dưới
  - Pill green chứa "GREAT" trắng (NO TILT, solid green)
  - Glow halo subtle quanh pill
```

**Test 26**: Per-tier color
```
Setup: tier1_color=#FF0000 (red), combo=35.
Verify: tier badge "Great" với background đỏ.
```

**Test 27**: Override font_scale
```
Setup: number_font_scale=4.0 (large override), bbox bình thường.
Verify: số combo render rất to (vượt qua bbox). User accept overflow.
Setup: number_font_scale=0.0 (auto).
Verify: số fit gọn trong bbox.
```

**Test 28**: Tier badge size
```
Setup: tier_font_scale=0.0 (auto-fit ~58% badge_h).
Verify: text "GREAT" fit gọn trong pill.
Setup: tier_font_scale=2.0 (large override).
Verify: text quá lớn, có thể tràn pill (acceptable cho user override).
```

### Migration từ implementation hiện tại

Code hiện đang dùng `_TIER_COLORS_BGR` hardcoded dict + tilt -8° + dark bg + neon border. Sau apply spec:

1. Xóa `_TIER_COLORS_BGR` class constant.
2. Thêm 4 instance attrs `_tier{N}_color`.
3. Thêm method `_resolve_tier_color(tier_idx)` đọc từ config.
4. Rewrite `_draw_tier_badge`: bỏ `tilt_deg`, bỏ `bg_buf`, dùng `_blit_with_alpha` (no rotate).
5. Pill fill = solid tier color, text = solid white, glow = single-pass outer halo.

### Acceptance bổ sung

```
✓ Visual match reference image (no tilt, solid green pill, clean white text)
✓ 3 font_scale fields work (number, label, tier — auto khi = 0)
✓ 4 tier color fields configurable qua Inspector color picker
✓ Default tier colors: #22c55e, #3b82f6, #a855f7, #f59e0b (Tailwind palette)
✓ Pill render straight (no tilt) by default; tilt là hardcoded 0 (V2 expose nếu cần)
```

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

---

## NEON GLOW STYLE — Match reference image v2 (FINAL TARGET)

> **Bối cảnh**: User cung cấp 2 ảnh so sánh — hình 1 (output hiện tại) và hình 2 (style mong muốn). 2 hình KHÁC NHAU 1 TRỜI 1 VỰC. Section này là spec định nghĩa lại để render đúng 100% như hình 2.

### So sánh side-by-side

| Element | Hình 1 (HIỆN TẠI) | Hình 2 (TARGET) |
|---|---|---|
| **Số combo** | "36" — bold trắng, size MEDIUM | "40" — bold trắng, size **EXTRA LARGE** (~1.6× hiện tại) |
| **Label "COMBO"** | Letter spacing thường, sát số | Letter spacing **WIDE** (giãn rộng), nhỏ, đặt giữa số và badge |
| **Badge shape** | Pill (oval, fully rounded) | **Rectangle với corner radius nhỏ** (~16-20px) — KHÔNG phải pill |
| **Badge size** | Nhỏ — vừa fit chữ | **LỚN** — padding rộng cả ngang lẫn dọc |
| **Badge fill** | Solid green flat | **Solid green** + cảm giác sáng hơn ở giữa |
| **Badge border** | Không có / mỏng | **Border neon dày** ~6-10px, màu xanh sáng cực gắt |
| **Glow / halo** | Không / nhẹ | **Halo neon CỰC MẠNH** — multi-pass blur, xanh lá phát sáng ra ngoài 20-40px |
| **Text trong badge** | "GREAT" trắng, regular | "GREAT" **trắng đậm, bold heavy**, không tilt |
| **Tilt** | Không | Không (giữ thẳng) |
| **Tổng thể** | UI flat, đơn giản | **Neon sign aesthetic** — như biển hiệu LED phát sáng |

### 4 điểm phải fix CHÍNH

#### Fix 1: Badge SHAPE — chuyển từ pill sang rounded rectangle

```python
# HIỆN TẠI (sai)
rr = max(8, badge_h // 2)   # full pill (radius = nửa chiều cao)

# TARGET (đúng)
rr = max(12, int(badge_h * 0.18))   # rounded rect, radius ~18% chiều cao
# Ví dụ badge_h=80 → rr=14-16px (KHÔNG phải 40)
```

#### Fix 2: NEON BORDER — viền dày màu sáng

```python
# Border thickness scale theo badge size
border_thickness = max(4, int(badge_h * 0.08))   # ~6-10px cho badge_h=80-120

# Border color = tier color SÁNG HƠN fill (lighten +30%)
def _lighten_bgr(bgr, factor=0.3):
    """Pha trắng vào màu để tạo neon highlight."""
    b, g, r = bgr
    return (
        int(min(255, b + (255 - b) * factor)),
        int(min(255, g + (255 - g) * factor)),
        int(min(255, r + (255 - r) * factor)),
    )

fill_color = color_bgr                         # vd green #22c55e BGR
border_color = _lighten_bgr(color_bgr, 0.35)   # green sáng hơn, gần neon white-green
```

#### Fix 3: HALO GLOW — multi-pass blur cực mạnh

```python
def _draw_neon_halo(self, canvas, badge_rect, color_bgr, intensity=1.0):
    """Vẽ halo neon ngoài badge bằng multi-pass gaussian blur.

    intensity 0..1 — controlled bởi self.combo_glow_strength / 100.
    """
    x, y, w, h = badge_rect
    H, W = canvas.shape[:2]

    # Mask trắng cho vùng badge (slightly larger)
    pad = int(min(w, h) * 0.15)
    mask = np.zeros((H, W), dtype=np.uint8)
    cv2.rectangle(
        mask,
        (max(0, x - pad), max(0, y - pad)),
        (min(W, x + w + pad), min(H, y + h + pad)),
        255,
        -1,
    )

    # Multi-pass blur — mỗi pass blur radius lớn hơn, alpha thấp hơn
    glow_canvas = np.zeros_like(canvas)
    glow_color = np.array(color_bgr, dtype=np.float32)
    passes = [
        # (blur_radius, alpha_multiplier)
        (int(min(w, h) * 0.20), 0.55),
        (int(min(w, h) * 0.40), 0.40),
        (int(min(w, h) * 0.70), 0.25),
        (int(min(w, h) * 1.10), 0.15),
    ]
    for blur_r, alpha_m in passes:
        if blur_r % 2 == 0:
            blur_r += 1
        blur_r = max(3, blur_r)
        blurred_mask = cv2.GaussianBlur(mask, (blur_r, blur_r), 0)
        # Multiply mask normalized → alpha layer per pixel
        alpha = (blurred_mask.astype(np.float32) / 255.0) * alpha_m * intensity
        # Blend glow color in
        for c in range(3):
            glow_canvas[:, :, c] = np.clip(
                glow_canvas[:, :, c] + alpha * glow_color[c],
                0,
                255,
            ).astype(np.uint8)

    # Composite glow onto canvas (additive — neon nổi bật trên dark BG)
    canvas[:] = cv2.addWeighted(canvas, 1.0, glow_canvas, 1.0, 0)
```

#### Fix 4: SỐ COMBO PHẢI TO HƠN nhiều

```python
# HIỆN TẠI: number_font_scale auto-fit theo bbox height
# user_scale = self.combo_number_font_scale or auto

# TARGET: scale up ~1.6×
NUMBER_SIZE_BOOST = 1.6

if self.combo_number_font_scale > 0:
    number_scale = self.combo_number_font_scale
else:
    # Auto-fit nhưng aggressive hơn — chiếm ~60-70% chiều cao bbox
    number_scale = self._auto_number_scale(bbox_h) * NUMBER_SIZE_BOOST
```

### Code FULL refactor `_draw_tier_badge`

```python
def _draw_tier_badge(
    self,
    canvas: np.ndarray,
    text: str,
    cx: int,
    cy: int,
    badge_w: int,
    badge_h: int,
    color_bgr: tuple[int, int, int],
    alpha: float = 1.0,
) -> None:
    """Vẽ neon-glow tier badge — match reference image v2.

    Layers (back → front):
      1. Outer halo glow (multi-pass gaussian blur)
      2. Solid pill/rect FILL (tier color)
      3. Bright neon BORDER (lightened tier color, thick)
      4. Bold WHITE text (uppercase, no tilt)
    """
    if badge_w <= 0 or badge_h <= 0 or alpha <= 0:
        return

    # Geometry
    x = cx - badge_w // 2
    y = cy - badge_h // 2
    rr = max(12, int(badge_h * 0.18))   # rounded rect (KHÔNG pill)
    border_th = max(4, int(badge_h * 0.08))

    # Colors
    fill_color = color_bgr
    border_color = self._lighten_bgr(color_bgr, 0.35)
    text_color = (255, 255, 255)

    # Working canvas (RGBA-style via separate alpha tracking)
    glow_intensity = (self.combo_glow_strength / 100.0) * alpha

    # ── Step 1: Outer halo glow ──────────────────────────────
    self._draw_neon_halo(
        canvas,
        badge_rect=(x, y, badge_w, badge_h),
        color_bgr=color_bgr,
        intensity=glow_intensity,
    )

    # ── Step 2: Solid fill rounded rect ──────────────────────
    self._draw_rounded_rect_filled(
        canvas, (x, y, badge_w, badge_h), rr, fill_color, alpha=alpha,
    )

    # ── Step 3: Neon border (thick, lightened) ───────────────
    self._draw_rounded_rect_outline(
        canvas, (x, y, badge_w, badge_h), rr,
        border_color, thickness=border_th, alpha=alpha,
    )

    # ── Step 4: White bold text — no tilt ────────────────────
    if self.combo_tier_font_scale > 0:
        font_scale = self.combo_tier_font_scale
    else:
        # Auto-fit: text ~50% chiều cao badge
        font_scale = self._auto_text_scale(text, badge_w, badge_h, ratio=0.5)

    font = cv2.FONT_HERSHEY_TRIPLEX   # bold heavy
    th = max(2, int(badge_h * 0.05))   # text stroke thickness

    (tw, th_text), baseline = cv2.getTextSize(text, font, font_scale, th)
    text_x = cx - tw // 2
    text_y = cy + th_text // 2

    # Subtle text shadow (1px offset, dark)
    cv2.putText(
        canvas, text, (text_x + 1, text_y + 1), font, font_scale,
        (0, 0, 0), th + 1, cv2.LINE_AA,
    )
    # Main white text
    cv2.putText(
        canvas, text, (text_x, text_y), font, font_scale,
        text_color, th, cv2.LINE_AA,
    )
```

### Helper methods cần thêm

```python
@staticmethod
def _lighten_bgr(bgr: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Trộn trắng vào BGR color để tạo neon highlight."""
    b, g, r = bgr
    return (
        int(min(255, b + (255 - b) * factor)),
        int(min(255, g + (255 - g) * factor)),
        int(min(255, r + (255 - r) * factor)),
    )

def _draw_rounded_rect_filled(
    self, canvas, rect, radius, color_bgr, alpha=1.0
):
    """Vẽ rounded rect đặc."""
    x, y, w, h = rect
    overlay = canvas.copy()
    # 4 corner circles + 2 rectangles cross-pattern
    cv2.rectangle(overlay, (x + radius, y), (x + w - radius, y + h), color_bgr, -1)
    cv2.rectangle(overlay, (x, y + radius), (x + w, y + h - radius), color_bgr, -1)
    cv2.circle(overlay, (x + radius, y + radius), radius, color_bgr, -1)
    cv2.circle(overlay, (x + w - radius, y + radius), radius, color_bgr, -1)
    cv2.circle(overlay, (x + radius, y + h - radius), radius, color_bgr, -1)
    cv2.circle(overlay, (x + w - radius, y + h - radius), radius, color_bgr, -1)
    cv2.addWeighted(overlay, alpha, canvas, 1 - alpha, 0, canvas)

def _draw_rounded_rect_outline(
    self, canvas, rect, radius, color_bgr, thickness, alpha=1.0
):
    """Vẽ rounded rect outline (4 cạnh + 4 cung)."""
    x, y, w, h = rect
    overlay = canvas.copy()
    # 4 thẳng
    cv2.line(overlay, (x + radius, y), (x + w - radius, y), color_bgr, thickness, cv2.LINE_AA)
    cv2.line(overlay, (x + radius, y + h), (x + w - radius, y + h), color_bgr, thickness, cv2.LINE_AA)
    cv2.line(overlay, (x, y + radius), (x, y + h - radius), color_bgr, thickness, cv2.LINE_AA)
    cv2.line(overlay, (x + w, y + radius), (x + w, y + h - radius), color_bgr, thickness, cv2.LINE_AA)
    # 4 cung
    cv2.ellipse(overlay, (x + radius, y + radius), (radius, radius), 180, 0, 90, color_bgr, thickness, cv2.LINE_AA)
    cv2.ellipse(overlay, (x + w - radius, y + radius), (radius, radius), 270, 0, 90, color_bgr, thickness, cv2.LINE_AA)
    cv2.ellipse(overlay, (x + radius, y + h - radius), (radius, radius), 90, 0, 90, color_bgr, thickness, cv2.LINE_AA)
    cv2.ellipse(overlay, (x + w - radius, y + h - radius), (radius, radius), 0, 0, 90, color_bgr, thickness, cv2.LINE_AA)
    cv2.addWeighted(overlay, alpha, canvas, 1 - alpha, 0, canvas)

def _auto_text_scale(self, text: str, max_w: int, max_h: int, ratio: float = 0.5) -> float:
    """Tìm font_scale lớn nhất để text fit trong (max_w, max_h) với chiều cao ~ratio×max_h."""
    target_h = max_h * ratio
    # Binary search font scale
    lo, hi = 0.3, 5.0
    for _ in range(20):
        mid = (lo + hi) / 2
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_TRIPLEX, mid, max(2, int(max_h * 0.05)))
        if th < target_h and tw < max_w * 0.85:
            lo = mid
        else:
            hi = mid
    return lo
```

### Số combo — refactor render

```python
def _draw_number(self, canvas, number_str, cx, cy, max_w, max_h, color_bgr, alpha):
    """Render số combo CỰC TO bold trắng."""
    if self.combo_number_font_scale > 0:
        scale = self.combo_number_font_scale
    else:
        # Auto: chiếm ~70% chiều cao layer bbox
        scale = self._auto_text_scale(number_str, max_w, max_h, ratio=0.70) * 1.0

    font = cv2.FONT_HERSHEY_DUPLEX   # bold clean
    th = max(3, int(max_h * 0.06))

    (tw, th_text), _ = cv2.getTextSize(number_str, font, scale, th)
    x = cx - tw // 2
    y = cy + th_text // 2

    # Subtle glow on number (less than badge)
    if self.combo_glow_strength > 0:
        glow_intensity = self.combo_glow_strength / 100.0 * 0.3 * alpha
        # Single soft blur pass
        ... (similar to _draw_neon_halo nhưng intensity nhẹ hơn)

    # Shadow + main text
    cv2.putText(canvas, number_str, (x + 2, y + 2), font, scale, (0, 0, 0), th + 1, cv2.LINE_AA)
    cv2.putText(canvas, number_str, (x, y), font, scale, color_bgr, th, cv2.LINE_AA)
```

### Label "COMBO" — letter-spacing wide

```python
def _draw_label(self, canvas, label, cx, cy, max_w, max_h, color_bgr, alpha):
    """Render label 'COMBO' với letter-spacing rộng."""
    text = label.upper()

    if self.combo_label_font_scale > 0:
        scale = self.combo_label_font_scale
    else:
        scale = self._auto_text_scale(text, max_w, max_h, ratio=0.4)

    font = cv2.FONT_HERSHEY_DUPLEX
    th = max(1, int(max_h * 0.04))

    # Letter-spacing wide: render từng ký tự với khoảng cách rộng
    spacing_factor = 1.4   # 40% gap giữa chars

    char_widths = []
    for ch in text:
        (cw, _), _ = cv2.getTextSize(ch, font, scale, th)
        char_widths.append(cw)

    total_w = sum(char_widths) * spacing_factor
    start_x = cx - int(total_w // 2)

    cur_x = start_x
    for ch, cw in zip(text, char_widths):
        (_, th_text), _ = cv2.getTextSize(ch, font, scale, th)
        y = cy + th_text // 2
        cv2.putText(canvas, ch, (cur_x, y), font, scale, color_bgr, th, cv2.LINE_AA)
        cur_x += int(cw * spacing_factor)
```

### Layout vertical — 3 phần stack

```
┌─────────────────────────┐
│                         │
│         4   0           │   ← Số combo (60-70% bbox height)
│                         │
│      C O M B O          │   ← Label letter-spaced (12-15% bbox height)
│   ┌───────────────┐     │
│   │  ★ GREAT ★    │     │   ← Tier badge với neon glow (20-25%)
│   └───────────────┘     │
└─────────────────────────┘
```

Vertical split (% of total layer bbox height):
- Top padding: 5%
- Number block: 55%
- Label block: 12%
- Spacing: 3%
- Badge block: 20%
- Bottom padding: 5%

### Default field values cập nhật

```python
# Update defaults trong BaseRenderSettings
combo_glow_strength: int = Field(default=80, ge=0, le=100)   # was 30 → 80 (neon mạnh)
combo_border_thickness: int = Field(default=8, ge=0, le=30)  # was 2 → 8 (border dày)
combo_number_font_scale: float = Field(default=0.0, ge=0.0)   # 0 = auto-boost
combo_label_font_scale: float = Field(default=0.0, ge=0.0)
combo_tier_font_scale: float = Field(default=0.0, ge=0.0)

combo_tier1_color: str = "#22c55e"
combo_tier2_color: str = "#3b82f6"
combo_tier3_color: str = "#a855f7"
combo_tier4_color: str = "#f59e0b"
```

### Performance note

Multi-pass gaussian blur 4 lần × full canvas mỗi frame = expensive. Optimize:
1. **Cache blur result** khi badge_rect không đổi (chỉ re-blur khi tier_idx đổi hoặc bbox đổi).
2. **Dùng ROI** thay vì full canvas — blur chỉ trong vùng `[x-pad×2, y-pad×2, w+pad×4, h+pad×4]`.
3. **Downscale** trước khi blur, upscale lại — giảm pixel count đáng kể.

```python
def _draw_neon_halo_optimized(self, canvas, badge_rect, color_bgr, intensity):
    x, y, w, h = badge_rect
    H, W = canvas.shape[:2]
    pad = int(min(w, h) * 1.2)   # halo extends 1.2× badge size

    rx0 = max(0, x - pad)
    ry0 = max(0, y - pad)
    rx1 = min(W, x + w + pad)
    ry1 = min(H, y + h + pad)

    # ROI work
    roi = canvas[ry0:ry1, rx0:rx1].copy()
    rh, rw = roi.shape[:2]

    # Mask cho badge trong ROI coords
    bx, by = x - rx0, y - ry0
    mask = np.zeros((rh, rw), dtype=np.uint8)
    cv2.rectangle(mask, (bx, by), (bx + w, by + h), 255, -1)

    # Downscale 4×
    small_mask = cv2.resize(mask, (rw // 4, rh // 4))
    glow_small = np.zeros((rh // 4, rw // 4, 3), dtype=np.float32)

    for blur_r, alpha_m in [(7, 0.55), (15, 0.4), (29, 0.25), (51, 0.15)]:
        blurred = cv2.GaussianBlur(small_mask, (blur_r, blur_r), 0)
        a = (blurred.astype(np.float32) / 255.0) * alpha_m * intensity
        for c in range(3):
            glow_small[:, :, c] = np.clip(glow_small[:, :, c] + a * color_bgr[c], 0, 255)

    # Upscale back
    glow = cv2.resize(glow_small, (rw, rh)).astype(np.uint8)

    # Additive blend onto canvas ROI
    canvas[ry0:ry1, rx0:rx1] = cv2.add(canvas[ry0:ry1, rx0:rx1], glow)
```

Ước lượng ~3-5ms/frame với optimize, vs 15-25ms/frame với full canvas blur — chấp nhận được cho 60fps target.

### Test cases bổ sung — visual match

```
✓ Test 29: Render combo=40 tier=GREAT → screenshot match hình mẫu pixel-by-pixel ±10%
✓ Test 30: Glow strength=0 → không có halo, badge flat (legacy mode)
✓ Test 31: Glow strength=100 → halo cực mạnh, sáng cả vùng xung quanh
✓ Test 32: Border thickness=0 → no border, chỉ solid fill
✓ Test 33: Border thickness=20 → border cực dày, fill area bị thu nhỏ
✓ Test 34: Tier label dài "ULTRA GODLIKE" → auto font_scale shrink để fit
✓ Test 35: Performance — render 60fps trên 1080p với glow_strength=100, KHÔNG dropped frames
```

### Migration từ implementation hiện tại

Bước 1: Add 7 helper methods mới (`_lighten_bgr`, `_draw_rounded_rect_filled`, `_draw_rounded_rect_outline`, `_auto_text_scale`, `_draw_neon_halo`, `_draw_neon_halo_optimized`, `_draw_label` với letter-spacing).

Bước 2: Replace toàn bộ `_draw_tier_badge` bằng version mới (no tilt, rounded rect, multi-pass glow).

Bước 3: Replace `_draw_number` để dùng NUMBER_SIZE_BOOST=1.6.

Bước 4: Replace `_draw_label` để dùng letter-spacing wide.

Bước 5: Update default values trong Pydantic schema (`combo_glow_strength=80`, `combo_border_thickness=8`).

Bước 6: Test với combo=40 tier=GREAT, so sánh với hình mẫu, tinh chỉnh các magic number cho đến khi pixel-match.

### Acceptance — final visual lock

```
✓ Badge KHÔNG còn pill → rounded rect với corner radius ~18% chiều cao
✓ Badge có border NEON dày 6-10px màu sáng hơn fill ~35%
✓ Badge có HALO glow sáng rực ra ngoài 20-40px (multi-pass blur)
✓ Số combo TO HƠN 1.6× so với current implementation
✓ Label "COMBO" có letter-spacing wide (1.4× normal)
✓ Text "GREAT" trắng đậm bold heavy (FONT_HERSHEY_TRIPLEX), no tilt
✓ Render 60fps không drop frame trên 1080p
✓ Mỗi tier 1 màu riêng từ field, default Tailwind palette
✓ Visual A/B compare với hình mẫu — match ≥90% perceptual similarity
```

> ⚠️ **Section trên (v2) SUPERSEDED bởi v3 bên dưới.** Giữ lại làm history reference. Implementation thực tế phải theo v3.

---

## NEON GLOW STYLE v3 — FINAL SPEC (skew + PIL TTF + auto tier text color)

> **Confirmed by user**:
> 1. Italic angle = **7° fixed**
> 2. Tier text color = **darker variant của tier color (auto, không expose field)**
> 3. Load font ngoài qua **PIL/Pillow TTF** — thêm field `combo_font_path`
> 4. Transform = **SKEW (parallelogram)**, không phải rotate

### Dependencies bổ sung

```
Pillow >= 9.0   # PIL.Image, PIL.ImageDraw, PIL.ImageFont
```

Đã có trong project. Không cần thêm.

### Fields cập nhật (tổng hợp)

| Field | Type | Default | Mô tả |
|---|---|---|---|
| `combo_font_path` | `str` | `""` | Path tới TTF/OTF file. Empty → fallback sang built-in cv2 font. |
| `combo_italic_skew_deg` | `float` | `7.0` | Góc skew (KHÔNG phải rotate). Range 0-30°. |
| `combo_number_letter_spacing` | `float` | `0.0` | Extra spacing giữa các chữ số (px). Mặc định 0. |
| `combo_label_letter_spacing` | `float` | `0.5` | Letter-spacing cho COMBO (×em). 0.5 = 50% width 1 char. |
| `combo_tier_letter_spacing` | `float` | `0.0` | Letter-spacing cho tier text. |
| `combo_glow_strength` | `int` | `90` | Halo intensity 0-100. Mặc định cao (matching ref). |
| `combo_border_thickness` | `int` | `7` | Border badge (px). |
| `combo_badge_corner_radius` | `int` | `12` | Corner radius badge (px). 0-50. |
| `combo_badge_padding_x` | `float` | `0.20` | Padding ngang text trong badge (×text_w). |
| `combo_badge_padding_y` | `float` | `0.18` | Padding dọc (×text_h). |
| `combo_tier_text_darken` | `float` | `0.55` | Mức darken tier text vs fill (0-1). 0.55 = 45% as bright. |

`combo_tier_text_color` **KHÔNG expose** — auto compute từ tier color × darken factor.

### Constants chốt visual

```python
# Tỉ lệ layout vertical (% bbox height)
LAYOUT_TOP_PAD       = 0.05
LAYOUT_NUMBER_RATIO  = 0.55   # Số "40"
LAYOUT_LABEL_GAP     = 0.02
LAYOUT_LABEL_RATIO   = 0.10   # "COMBO"
LAYOUT_BADGE_GAP     = 0.04
LAYOUT_BADGE_RATIO   = 0.20   # "GREAT"
LAYOUT_BOTTOM_PAD    = 0.04

# Italic skew
ITALIC_SKEW_RAD = math.radians(7.0)   # = ~0.122 rad
SKEW_MATRIX_2x3 = np.array([[1, math.tan(ITALIC_SKEW_RAD), 0],
                            [0, 1, 0]], dtype=np.float32)

# Color computation
NEON_BORDER_LIGHTEN = 0.40   # border = lighten(tier_color, 0.40)
NEON_FILL_DARKEN    = 0.10   # fill   = darken(tier_color, 0.10) — slightly darker than border
TIER_TEXT_DARKEN    = 0.55   # text   = darken(tier_color, 0.55)

# Glow halo
HALO_PASSES = [
    # (blur_radius_ratio, alpha_multiplier)
    (0.20, 0.65),
    (0.45, 0.50),
    (0.80, 0.32),
    (1.30, 0.18),
    (2.00, 0.10),   # extra outer aura
]

# Number style
NUMBER_HALO_BLUR_R   = 9     # px
NUMBER_HALO_ALPHA    = 0.30
NUMBER_OUTLINE_PX    = 2

# Letter spacing
LABEL_LETTER_SPACING_EM = 0.50   # 50% extra gap between chars
```

### PIL font rendering helper

```python
from PIL import Image, ImageDraw, ImageFont

class PILFontRenderer:
    """Render text với TTF font qua PIL, return BGR numpy array với alpha mask."""

    _font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    @classmethod
    def _get_font(cls, path: str, size_px: int) -> ImageFont.FreeTypeFont | None:
        if not path:
            return None
        key = (path, size_px)
        if key in cls._font_cache:
            return cls._font_cache[key]
        try:
            font = ImageFont.truetype(path, size_px)
        except (OSError, IOError):
            return None
        cls._font_cache[key] = font
        return font

    @classmethod
    def render_text(
        cls,
        text: str,
        font_path: str,
        font_size_px: int,
        color_bgr: tuple[int, int, int],
        letter_spacing_px: int = 0,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """Render text → (BGR image, alpha mask). Return None nếu font load fail.

        Returned image là tight-cropped quanh text. Caller responsible for placement.
        """
        font = cls._get_font(font_path, font_size_px)
        if font is None:
            return None

        # Measure
        if letter_spacing_px == 0:
            bbox = font.getbbox(text)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            ascent = -bbox[1]
        else:
            char_widths = []
            max_top = 0
            max_bottom = 0
            for ch in text:
                cb = font.getbbox(ch)
                char_widths.append((cb[2] - cb[0], cb[0]))
                max_top = max(max_top, -cb[1])
                max_bottom = max(max_bottom, cb[3])
            tw = sum(cw for cw, _ in char_widths) + letter_spacing_px * (len(text) - 1)
            th = max_top + max_bottom
            ascent = max_top

        pad = 4
        canvas_w = tw + pad * 2
        canvas_h = th + pad * 2

        img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        rgb = (color_bgr[2], color_bgr[1], color_bgr[0])

        if letter_spacing_px == 0:
            draw.text((pad, pad), text, font=font, fill=(*rgb, 255))
        else:
            cur_x = pad
            for ch, (cw, lsb) in zip(text, char_widths):
                draw.text((cur_x, pad), ch, font=font, fill=(*rgb, 255))
                cur_x += cw + letter_spacing_px

        arr = np.array(img)   # RGBA H×W×4
        bgr = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2BGR)
        alpha = arr[:, :, 3]
        return bgr, alpha
```

### Skew transformation helper

```python
def apply_skew_to_image(
    img_bgr: np.ndarray,
    alpha: np.ndarray,
    skew_deg: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply horizontal skew (italic effect) to image + alpha.

    Skew formula: x' = x + tan(angle) * (y - y_center)
                  y' = y
    Top dịch trái, bottom dịch phải (italic standard).
    """
    if abs(skew_deg) < 0.01:
        return img_bgr, alpha

    h, w = img_bgr.shape[:2]
    skew_rad = math.radians(skew_deg)
    shift = math.tan(skew_rad) * h

    # New canvas wider để chứa skew
    new_w = int(w + abs(shift)) + 2

    # Affine matrix: x' = x + tan(angle) * (h - y)
    # → top (y=0) dịch +shift, bottom (y=h) dịch 0
    M = np.array([
        [1, math.tan(skew_rad), 0],
        [0, 1, 0],
    ], dtype=np.float32)

    # Adjust translation để fit new canvas
    if skew_rad > 0:
        # Top dịch trái nếu tan > 0 và y nghĩa ngược → cần offset
        M[0, 2] = 0
    else:
        M[0, 2] = abs(shift)

    skewed_bgr = cv2.warpAffine(
        img_bgr, M, (new_w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    skewed_alpha = cv2.warpAffine(
        alpha, M, (new_w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return skewed_bgr, skewed_alpha


def alpha_blit(
    canvas: np.ndarray,
    img_bgr: np.ndarray,
    alpha: np.ndarray,
    x: int,
    y: int,
    global_alpha: float = 1.0,
) -> None:
    """Blit BGR + alpha mask onto canvas tại (x, y) — left-top corner."""
    H, W = canvas.shape[:2]
    h, w = img_bgr.shape[:2]

    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(W, x + w)
    y1 = min(H, y + h)

    if x0 >= x1 or y0 >= y1:
        return

    sx0 = x0 - x
    sy0 = y0 - y
    sx1 = sx0 + (x1 - x0)
    sy1 = sy0 + (y1 - y0)

    src = img_bgr[sy0:sy1, sx0:sx1].astype(np.float32)
    a = (alpha[sy0:sy1, sx0:sx1].astype(np.float32) / 255.0) * global_alpha
    a3 = np.dstack([a, a, a])

    canvas[y0:y1, x0:x1] = (
        canvas[y0:y1, x0:x1].astype(np.float32) * (1 - a3) + src * a3
    ).astype(np.uint8)
```

### Color helpers

```python
@staticmethod
def _lighten_bgr(bgr: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Mix BGR with white. factor 0..1."""
    b, g, r = bgr
    return (
        int(min(255, b + (255 - b) * factor)),
        int(min(255, g + (255 - g) * factor)),
        int(min(255, r + (255 - r) * factor)),
    )

@staticmethod
def _darken_bgr(bgr: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Multiply BGR by (1 - factor). factor 0..1."""
    b, g, r = bgr
    k = 1.0 - factor
    return (int(b * k), int(g * k), int(r * k))

@staticmethod
def _hex_to_bgr(hex_str: str) -> tuple[int, int, int]:
    s = hex_str.lstrip("#")
    r = int(s[0:2], 16)
    g = int(s[2:4], 16)
    b = int(s[4:6], 16)
    return (b, g, r)

def _resolve_tier_color(self, tier_idx: int) -> tuple[int, int, int]:
    """Get tier color BGR by index 0-3."""
    hex_colors = [
        self.combo_tier1_color,
        self.combo_tier2_color,
        self.combo_tier3_color,
        self.combo_tier4_color,
    ]
    return self._hex_to_bgr(hex_colors[tier_idx])
```

### Number render — italic skew + soft white halo

```python
def _draw_number(
    self,
    canvas: np.ndarray,
    number_str: str,
    bbox: tuple[int, int, int, int],   # (cx, cy, max_w, max_h)
    alpha: float,
) -> None:
    """Render số combo italic skew với soft white halo."""
    cx, cy, max_w, max_h = bbox

    # Determine font size — auto fit với boost
    if self.combo_number_font_scale > 0:
        font_size_px = int(max_h * self.combo_number_font_scale)
    else:
        font_size_px = int(max_h * 0.85)   # 85% bbox height

    color_bgr = (245, 245, 240)   # off-white kem

    # Try PIL TTF render
    pil_result = None
    if self.combo_font_path:
        pil_result = PILFontRenderer.render_text(
            number_str, self.combo_font_path, font_size_px,
            color_bgr=color_bgr,
            letter_spacing_px=int(self.combo_number_letter_spacing),
        )

    if pil_result is None:
        # Fallback cv2 — không có italic native, dùng FONT_HERSHEY_DUPLEX
        bgr, mask = self._cv2_text_to_bgr_alpha(
            number_str, cv2.FONT_HERSHEY_DUPLEX,
            scale=font_size_px / 30.0,
            color=color_bgr,
            thickness=max(2, font_size_px // 25),
        )
    else:
        bgr, mask = pil_result

    # Apply 7° italic skew
    bgr, mask = apply_skew_to_image(bgr, mask, self.combo_italic_skew_deg)

    # Soft white halo
    if self.combo_glow_strength > 0:
        halo_intensity = (self.combo_glow_strength / 100.0) * 0.35 * alpha
        self._add_text_halo(canvas, bgr, mask, cx, cy, color_bgr, halo_intensity)

    # Outline đen mỏng
    outline_mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
    outline_only = cv2.subtract(outline_mask, mask)
    outline_bgr = np.zeros_like(bgr)
    self._blit_centered(canvas, outline_bgr, outline_only, cx, cy, alpha * 0.7)

    # Main text
    self._blit_centered(canvas, bgr, mask, cx, cy, alpha)


def _add_text_halo(
    self, canvas, bgr, mask, cx, cy, color_bgr, intensity,
):
    """Soft glow xung quanh text bằng blur mask."""
    glow_mask = cv2.GaussianBlur(mask, (25, 25), 0)
    glow_bgr = np.full_like(bgr, color_bgr, dtype=np.uint8)
    self._blit_centered(canvas, glow_bgr, (glow_mask * intensity).astype(np.uint8), cx, cy, 1.0)


def _blit_centered(self, canvas, img_bgr, alpha, cx, cy, global_alpha):
    h, w = img_bgr.shape[:2]
    x = cx - w // 2
    y = cy - h // 2
    alpha_blit(canvas, img_bgr, alpha, x, y, global_alpha)
```

### Label "COMBO" render — wide letter-spacing italic

```python
def _draw_label(self, canvas, label_text, bbox, alpha):
    cx, cy, max_w, max_h = bbox
    text = label_text.upper()

    if self.combo_label_font_scale > 0:
        font_size_px = int(max_h * self.combo_label_font_scale)
    else:
        font_size_px = int(max_h * 0.80)

    color_bgr = (208, 208, 208)   # gray-white

    # Letter-spacing px
    ls_px = int(font_size_px * self.combo_label_letter_spacing)

    pil_result = None
    if self.combo_font_path:
        pil_result = PILFontRenderer.render_text(
            text, self.combo_font_path, font_size_px,
            color_bgr=color_bgr, letter_spacing_px=ls_px,
        )

    if pil_result is None:
        # Fallback: render từng char với cv2 + manual spacing
        bgr, mask = self._cv2_text_with_spacing(
            text, cv2.FONT_HERSHEY_DUPLEX,
            scale=font_size_px / 30.0,
            color=color_bgr,
            thickness=max(1, font_size_px // 30),
            spacing_px=ls_px,
        )
    else:
        bgr, mask = pil_result

    bgr, mask = apply_skew_to_image(bgr, mask, self.combo_italic_skew_deg)

    self._blit_centered(canvas, bgr, mask, cx, cy, alpha)
```

### Tier badge render — neon glow + skew + auto-darker text

```python
def _draw_tier_badge(
    self,
    canvas: np.ndarray,
    text: str,
    bbox: tuple[int, int, int, int],   # (cx, cy, max_w, max_h)
    tier_color_bgr: tuple[int, int, int],
    alpha: float,
) -> None:
    """Vẽ neon green badge với skew + glow + darker text — match ref v3."""
    cx, cy, max_w, max_h = bbox
    if max_w <= 0 or max_h <= 0 or alpha <= 0:
        return

    # ── Color computation ─────────────────────────────────
    fill_color   = self._darken_bgr(tier_color_bgr, NEON_FILL_DARKEN)    # ~10% darker
    border_color = self._lighten_bgr(tier_color_bgr, NEON_BORDER_LIGHTEN)  # ~40% lighter neon
    text_color   = self._darken_bgr(tier_color_bgr, self.combo_tier_text_darken)  # ~55% darker

    # ── Sizing badge dựa vào text ──────────────────────────
    text_target_h = int(max_h * 0.65)
    if self.combo_tier_font_scale > 0:
        text_target_h = int(max_h * self.combo_tier_font_scale)

    # PIL render text trước (để biết kích thước)
    text_render = None
    if self.combo_font_path:
        text_render = PILFontRenderer.render_text(
            text.upper(), self.combo_font_path, text_target_h,
            color_bgr=text_color,
            letter_spacing_px=int(self.combo_tier_letter_spacing),
        )

    if text_render is None:
        text_bgr, text_mask = self._cv2_text_to_bgr_alpha(
            text.upper(), cv2.FONT_HERSHEY_TRIPLEX,
            scale=text_target_h / 25.0,
            color=text_color,
            thickness=max(2, text_target_h // 12),
        )
    else:
        text_bgr, text_mask = text_render

    text_h, text_w = text_bgr.shape[:2]

    # Badge size = text + padding
    badge_w = int(text_w * (1 + 2 * self.combo_badge_padding_x))
    badge_h = int(text_h * (1 + 2 * self.combo_badge_padding_y))

    # Position badge centered tại (cx, cy)
    badge_x = cx - badge_w // 2
    badge_y = cy - badge_h // 2

    # ── Render badge VÀO MỘT layer riêng để skew toàn bộ ────
    pad_for_glow = int(max(badge_w, badge_h) * 0.4)
    layer_w = badge_w + pad_for_glow * 2
    layer_h = badge_h + pad_for_glow * 2

    layer_bgr = np.zeros((layer_h, layer_w, 3), dtype=np.uint8)
    layer_alpha = np.zeros((layer_h, layer_w), dtype=np.uint8)

    # Coordinates trong layer
    bx = pad_for_glow
    by = pad_for_glow

    # 1) Glow halo (multi-pass) — vẽ vào layer trước
    self._draw_neon_halo_into_layer(
        layer_bgr, layer_alpha,
        rect=(bx, by, badge_w, badge_h),
        color_bgr=tier_color_bgr,
        radius=self.combo_badge_corner_radius,
        intensity=(self.combo_glow_strength / 100.0),
    )

    # 2) Solid fill rounded rect
    self._draw_rounded_rect_filled_layer(
        layer_bgr, layer_alpha,
        rect=(bx, by, badge_w, badge_h),
        radius=self.combo_badge_corner_radius,
        color_bgr=fill_color,
    )

    # 3) Neon border thick
    self._draw_rounded_rect_outline_layer(
        layer_bgr, layer_alpha,
        rect=(bx, by, badge_w, badge_h),
        radius=self.combo_badge_corner_radius,
        color_bgr=border_color,
        thickness=self.combo_border_thickness,
    )

    # 4) Text vào giữa badge
    text_x = bx + (badge_w - text_w) // 2
    text_y = by + (badge_h - text_h) // 2
    alpha_blit(layer_bgr, text_bgr, text_mask, text_x, text_y, 1.0)
    # Update layer_alpha với text alpha
    layer_alpha[text_y:text_y+text_h, text_x:text_x+text_w] = np.maximum(
        layer_alpha[text_y:text_y+text_h, text_x:text_x+text_w],
        text_mask,
    )

    # ── Apply skew TOÀN BỘ layer ────────────────────────────
    layer_bgr, layer_alpha = apply_skew_to_image(
        layer_bgr, layer_alpha, self.combo_italic_skew_deg,
    )

    # ── Blit layer lên canvas tại tâm (cx, cy) ─────────────
    skewed_h, skewed_w = layer_bgr.shape[:2]
    final_x = cx - skewed_w // 2
    final_y = cy - skewed_h // 2
    alpha_blit(canvas, layer_bgr, layer_alpha, final_x, final_y, alpha)
```

### Multi-pass halo glow vào layer

```python
def _draw_neon_halo_into_layer(
    self, layer_bgr, layer_alpha, rect, color_bgr, radius, intensity,
):
    """Vẽ multi-pass blur glow vào layer (additive)."""
    x, y, w, h = rect
    H, W = layer_bgr.shape[:2]

    # Tạo solid mask cho rounded rect
    base_mask = np.zeros((H, W), dtype=np.uint8)
    self._fill_rounded_rect_into_mask(base_mask, rect, radius, 255)

    glow_color = np.array(color_bgr, dtype=np.float32)
    accum = np.zeros((H, W, 3), dtype=np.float32)

    min_dim = min(w, h)
    for blur_ratio, alpha_m in HALO_PASSES:
        kr = max(3, int(min_dim * blur_ratio))
        if kr % 2 == 0:
            kr += 1
        blurred = cv2.GaussianBlur(base_mask, (kr, kr), 0)
        a = (blurred.astype(np.float32) / 255.0) * alpha_m * intensity
        for c in range(3):
            accum[:, :, c] += a * glow_color[c]

    accum = np.clip(accum, 0, 255).astype(np.uint8)
    # Additive blend onto layer
    layer_bgr[:] = cv2.add(layer_bgr, accum)

    # Update alpha — glow areas có alpha
    glow_alpha = (cv2.GaussianBlur(base_mask, (51, 51), 0).astype(np.float32) * intensity * 0.8).clip(0, 255).astype(np.uint8)
    layer_alpha[:] = np.maximum(layer_alpha, glow_alpha)


def _fill_rounded_rect_into_mask(self, mask, rect, radius, value):
    x, y, w, h = rect
    r = max(0, min(radius, min(w, h) // 2))
    cv2.rectangle(mask, (x + r, y), (x + w - r, y + h), value, -1)
    cv2.rectangle(mask, (x, y + r), (x + w, y + h - r), value, -1)
    if r > 0:
        cv2.circle(mask, (x + r, y + r), r, value, -1)
        cv2.circle(mask, (x + w - r, y + r), r, value, -1)
        cv2.circle(mask, (x + r, y + h - r), r, value, -1)
        cv2.circle(mask, (x + w - r, y + h - r), r, value, -1)


def _draw_rounded_rect_filled_layer(self, layer_bgr, layer_alpha, rect, radius, color_bgr):
    H, W = layer_bgr.shape[:2]
    fill_mask = np.zeros((H, W), dtype=np.uint8)
    self._fill_rounded_rect_into_mask(fill_mask, rect, radius, 255)
    color_layer = np.full_like(layer_bgr, color_bgr)
    a = (fill_mask.astype(np.float32) / 255.0)
    a3 = np.dstack([a, a, a])
    layer_bgr[:] = (layer_bgr.astype(np.float32) * (1 - a3) + color_layer.astype(np.float32) * a3).astype(np.uint8)
    layer_alpha[:] = np.maximum(layer_alpha, fill_mask)


def _draw_rounded_rect_outline_layer(self, layer_bgr, layer_alpha, rect, radius, color_bgr, thickness):
    H, W = layer_bgr.shape[:2]
    outer_mask = np.zeros((H, W), dtype=np.uint8)
    inner_mask = np.zeros((H, W), dtype=np.uint8)
    self._fill_rounded_rect_into_mask(outer_mask, rect, radius, 255)
    inner_rect = (rect[0] + thickness, rect[1] + thickness, rect[2] - 2 * thickness, rect[3] - 2 * thickness)
    inner_r = max(0, radius - thickness)
    if inner_rect[2] > 0 and inner_rect[3] > 0:
        self._fill_rounded_rect_into_mask(inner_mask, inner_rect, inner_r, 255)
    border_mask = cv2.subtract(outer_mask, inner_mask)
    color_layer = np.full_like(layer_bgr, color_bgr)
    a = (border_mask.astype(np.float32) / 255.0)
    a3 = np.dstack([a, a, a])
    layer_bgr[:] = (layer_bgr.astype(np.float32) * (1 - a3) + color_layer.astype(np.float32) * a3).astype(np.uint8)
    layer_alpha[:] = np.maximum(layer_alpha, border_mask)
```

### Render method tổng — `_draw_combo_layer`

```python
def _draw_combo_layer(self, canvas, layer_state, alpha=1.0):
    """Entry point — vẽ toàn bộ combo layer.

    layer_state cung cấp:
      - combo_count: int
      - tier_idx: int (0-3 hoặc -1 nếu chưa đạt tier)
      - tier_label: str
      - bbox: (x, y, w, h) trong canvas coords
    """
    bx, by, bw, bh = layer_state.bbox

    # Tier color
    if layer_state.tier_idx < 0:
        tier_color = self._hex_to_bgr(self.combo_tier1_color)   # default
    else:
        tier_color = self._resolve_tier_color(layer_state.tier_idx)

    # Vertical layout
    cur_y = by + int(bh * LAYOUT_TOP_PAD)

    # 1) Number
    num_h = int(bh * LAYOUT_NUMBER_RATIO)
    num_cy = cur_y + num_h // 2
    num_cx = bx + bw // 2
    self._draw_number(
        canvas, str(layer_state.combo_count),
        bbox=(num_cx, num_cy, bw, num_h),
        alpha=alpha,
    )
    cur_y += num_h + int(bh * LAYOUT_LABEL_GAP)

    # 2) Label "COMBO"
    label_h = int(bh * LAYOUT_LABEL_RATIO)
    label_cy = cur_y + label_h // 2
    label_cx = bx + bw // 2
    self._draw_label(
        canvas, self.combo_label,
        bbox=(label_cx, label_cy, bw, label_h),
        alpha=alpha,
    )
    cur_y += label_h + int(bh * LAYOUT_BADGE_GAP)

    # 3) Tier badge (chỉ vẽ nếu đạt tier)
    if layer_state.tier_idx >= 0 and layer_state.tier_label:
        badge_h = int(bh * LAYOUT_BADGE_RATIO)
        badge_cy = cur_y + badge_h // 2
        badge_cx = bx + bw // 2
        self._draw_tier_badge(
            canvas, layer_state.tier_label,
            bbox=(badge_cx, badge_cy, bw, badge_h),
            tier_color_bgr=tier_color,
            alpha=alpha,
        )
```

### Pydantic schema additions (consolidated)

```python
# Trong BaseRenderSettings (studio/models/render_settings.py)

# Existing 26 fields giữ nguyên, REPLACE/ADD những field sau:
combo_glow_strength: int = Field(default=90, ge=0, le=100)
combo_border_thickness: int = Field(default=7, ge=0, le=30)
combo_badge_corner_radius: int = Field(default=12, ge=0, le=50)
combo_badge_padding_x: float = Field(default=0.20, ge=0.0, le=1.0)
combo_badge_padding_y: float = Field(default=0.18, ge=0.0, le=1.0)

# Italic + font external
combo_font_path: str = Field(default="")
combo_italic_skew_deg: float = Field(default=7.0, ge=0.0, le=30.0)

# Letter spacing
combo_number_letter_spacing: float = Field(default=0.0, ge=-10.0, le=20.0)
combo_label_letter_spacing: float = Field(default=0.5, ge=0.0, le=2.0)
combo_tier_letter_spacing: float = Field(default=0.0, ge=-10.0, le=20.0)

# Auto-darker tier text
combo_tier_text_darken: float = Field(default=0.55, ge=0.0, le=1.0)
```

**TỔNG = 26 + 7 (vẫn 4 tier color + 3 font_scale từ v2) + 4 mới (font_path, italic_skew, padding_x, padding_y, corner_radius) — ~37 fields.**

### UI Inspector — sections cập nhật

```
┌─ Combo Layer ─────────────────────────────┐
│ ▼ Position                                │
│   X: [____]  Y: [____]                    │
│   W: [____]  H: [____]                    │
│                                           │
│ ▼ Style                                   │
│   Color:        [████]                    │
│   Border thick: [ 7  ]                    │
│   Glow:         [ 90 ]                    │
│   Italic skew:  [ 7° ]                    │ ← NEW
│                                           │
│ ▼ Font                                    │
│   ⓘ Empty = built-in cv2 font             │
│   Font file:    [/path/to/font.ttf] [📁]  │ ← NEW
│   Number scale: [auto / 0.0 ]             │
│   Label scale:  [auto / 0.0 ]             │
│   Tier scale:   [auto / 0.0 ]             │
│   Number ls:    [ 0.0 ]                   │ ← NEW
│   Label ls:     [ 0.5 ]                   │ ← NEW
│   Tier ls:      [ 0.0 ]                   │ ← NEW
│                                           │
│ ▼ Badge                                   │
│   Corner radius: [ 12 ]                   │ ← NEW
│   Padding X:     [0.20]                   │ ← NEW
│   Padding Y:     [0.18]                   │ ← NEW
│   Text darken:   [0.55]                   │ ← NEW (auto darker)
│                                           │
│ ▼ Tier System                             │
│   Label: [____________]   (combo_label)   │
│   Tier 1: ≥ [ 30] [_Great__]  [████ ]     │
│   Tier 2: ≥ [ 60] [_Superb_]  [████ ]     │
│   Tier 3: ≥ [ 90] [_Perfect] [████ ]      │
│   Tier 4: ≥ [120] [_Godlike] [████ ]      │
│                                           │
│ ▼ Animation / Audio                       │
│   ... (giữ nguyên)                        │
└───────────────────────────────────────────┘
```

### Recommended TTF fonts để bundle

User upload font hoặc tool ship sẵn 1-2 font default trong `assets/fonts/`:

| Font | Use case | License | URL |
|---|---|---|---|
| **Saira Condensed** | Condensed bold italic — match ref tốt nhất | OFL | Google Fonts |
| **Bebas Neue** | All caps display — alt cho number | OFL | Google Fonts |
| **Anton** | Heavy condensed — alt cho tier label | OFL | Google Fonts |
| **Russo One** | Bold geometric — alt | OFL | Google Fonts |

Default `combo_font_path` = `""` → fallback cv2. User có thể browse pick file qua Inspector button `[📁]`.

### Visual reference checklist

So sánh output với hình ref:
```
✓ Số "40":
  - Italic skew 7° ✓
  - Font condensed bold (Saira/Bebas) qua TTF ✓
  - Color off-white (#F5F5F0) ✓
  - Soft white halo blur ✓
  - Outline đen 2px ✓

✓ "COMBO" label:
  - Italic skew 7° (đồng bộ số) ✓
  - Letter-spacing wide (0.5em) ✓
  - Color gray (#D0D0D0) ✓
  - KHÔNG có glow ✓
  - Size ~10% bbox h ✓

✓ "GREAT" badge:
  - Skew 7° toàn bộ badge (parallelogram) ✓
  - Rounded rect corner_radius=12px (KHÔNG pill) ✓
  - Border neon thick=7px, color=lighten(tier, 0.40) ✓
  - Fill = darken(tier, 0.10) ✓
  - Text color = darken(tier, 0.55) → green đậm hơn ✓
  - Multi-pass halo glow 5 layers, intensity 0.90 ✓
  - Text font condensed bold qua TTF ✓
  - Padding 20% ngang × 18% dọc quanh text ✓
```

### Test cases v3

```
✓ Test 36: Render combo=40 tier=GREAT với font Saira Condensed → so sánh hình ref pixel diff <8%
✓ Test 37: Skew 0° → render thẳng đứng (legacy mode)
✓ Test 38: Skew 7° → toàn bộ 3 element nghiêng đồng bộ (số + label + badge)
✓ Test 39: Font path invalid → fallback cv2 font, không crash
✓ Test 40: Font path valid OTF → render đúng OTF
✓ Test 41: Tier color đỏ #DC2626 → text auto = darken(0.55) = #631111 (dark red), readable
✓ Test 42: Tier color vàng #F59E0B → text = #6E4705 (dark amber)
✓ Test 43: Performance — full render với glow=100, font TTF, skew, multi-pass blur — vẫn 60fps
✓ Test 44: Letter-spacing label = 0 → các chữ COMBO sát nhau (tight)
✓ Test 45: Letter-spacing label = 1.5 → các chữ COMBO cực rộng
```

### Migration plan implementation

**Phase 1 — Helpers (low risk, isolated):**
1. Add `PILFontRenderer` class (font cache + render_text)
2. Add `apply_skew_to_image()` standalone function
3. Add `alpha_blit()` standalone function
4. Add color helpers `_lighten_bgr`, `_darken_bgr`, `_hex_to_bgr`

**Phase 2 — ComboHUD refactor:**
5. Replace `_draw_number` với new version (PIL + skew + halo)
6. Replace `_draw_label` với new version (letter-spacing wide)
7. Replace `_draw_tier_badge` với new version (layer + skew + multi-pass glow)
8. Add `_draw_neon_halo_into_layer`, `_fill_rounded_rect_into_mask`, `_draw_rounded_rect_filled_layer`, `_draw_rounded_rect_outline_layer`
9. Replace top-level `_draw_combo_layer` với new orchestration

**Phase 3 — Schema + UI:**
10. Update Pydantic BaseRenderSettings (~11 fields mới/changed)
11. Update Inspector panel UI (4 section: Style/Font/Badge/Tier)
12. Add font file picker button [📁] gọi QFileDialog filter `*.ttf;;*.otf`

**Phase 4 — Bundle assets (optional):**
13. Tạo `assets/fonts/` directory
14. Ship 2 default font: `SairaCondensed-BoldItalic.ttf`, `BebasNeue-Regular.ttf`
15. Default `combo_font_path` có thể trỏ tới bundled font qua `bundle_paths.py`

**Phase 5 — Testing:**
16. Unit tests cho PIL renderer, skew, color helpers
17. Visual regression test — render reference scenarios, compare với golden images
18. Performance test 60fps với worst-case (max glow + TTF + skew)

### Acceptance criteria v3

```
✓ Render output match hình ref user (40 COMBO GREAT) ≥92% perceptual similarity (SSIM)
✓ 3 element (số + label + badge) ĐỀU SKEW 7° đồng bộ — không có element nào thẳng
✓ Tier text color tự động darker hơn fill — KHÔNG cần user pick
✓ Font TTF render qua PIL hoạt động với .ttf và .otf
✓ Fallback cv2 font khi font_path empty hoặc invalid
✓ 60fps trên 1080p với full feature (glow=100, TTF, skew, multi-pass)
✓ Inspector có 4 field mới: font_path picker, italic_skew, badge corner_radius, badge padding x/y
✓ KHÔNG break existing 26 fields (backward compat) — defaults match hình ref
```
