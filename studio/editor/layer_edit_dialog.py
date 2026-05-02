"""Layer edit sections for timeline layer blocks.

Each layer kind has a dedicated section widget (QGroupBox) with:
  - __init__(config: dict)   — populate widgets from dict
  - changed = Signal()       — emitted on any user edit
  - get_config() -> dict     — export current state

"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QWidget,
)

# ---------------------------------------------------------------------------
# Helpers imported from segment_config_panel — reuse existing section widgets
# ---------------------------------------------------------------------------


def _pick_color(current: str | None, title: str, parent: QWidget | None) -> str | None:
    """Open QColorDialog, return hex string or None on cancel."""
    from PySide6.QtWidgets import QColorDialog
    initial = QColor(current) if current else QColor("#000000")
    color = QColorDialog.getColor(initial, parent, title)
    if color.isValid():
        return color.name().upper()
    return None


# ---------------------------------------------------------------------------
# _BackgroundSection
# ---------------------------------------------------------------------------

class _BackgroundSection(QGroupBox):
    """Config section for a Background layer block."""

    changed = Signal()

    _BG_TYPES = [
        ("Solid color", "solid"),
        ("Image", "image"),
        ("Video", "video"),
    ]

    @staticmethod
    def _normalize_bg_type(value: object) -> str:
        """Map incoming/legacy values to one of: solid/image/video."""
        raw = str(value or "").strip().lower()
        if raw in {"image", "img"}:
            return "image"
        if raw in {"video", "vid", "movie"}:
            return "video"
        # Treat unknown / legacy labels as solid for backward compatibility.
        return "solid"

    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__("Background", parent)
        form = QFormLayout(self)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(8)

        from PySide6.QtWidgets import QComboBox
        self._type_cb = QComboBox()
        for label, val in self._BG_TYPES:
            self._type_cb.addItem(label, val)
        self._bg_type = self._normalize_bg_type(
            config.get("bg_type") or config.get("background_type") or "solid"
        )
        idx = next(
            (i for i, (_, v) in enumerate(self._BG_TYPES) if v == self._bg_type),
            0,
        )
        self._type_cb.setCurrentIndex(idx)
        self._type_cb.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Type", self._type_cb)

        # Color row
        self._color: str = config.get("bg_color") or config.get("background_color") or "#000000"
        self._color_btn = QPushButton()
        self._color_btn.setMinimumWidth(90)
        self._refresh_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        form.addRow("Color", self._color_btn)

        # Image row
        from .segment_config_panel import _PathBrowseWidget
        img_val = config.get("bg_image") or config.get("background_image") or ""
        self._img_edit = _PathBrowseWidget(
            img_val,
            title="Select background image",
            file_filter="Images (*.png *.jpg *.jpeg *.bmp *.tga *.webp);;All files (*.*)",
            placeholder="Optional image path",
            parent=self,
        )
        self._img_edit.changed.connect(self.changed)
        self._img_row_label = QLabel("Image")
        form.addRow(self._img_row_label, self._img_edit)

        # Video row
        vid_val = config.get("bg_video") or config.get("background_video") or ""
        self._vid_edit = _PathBrowseWidget(
            vid_val,
            title="Select background video",
            file_filter="Videos (*.mp4 *.mov *.mkv *.avi *.webm *.m4v);;All files (*.*)",
            placeholder="Optional video path",
            parent=self,
        )
        self._vid_edit.changed.connect(self.changed)
        self._vid_row_label = QLabel("Video")
        form.addRow(self._vid_row_label, self._vid_edit)

        self._update_visibility()

    def _refresh_color_btn(self) -> None:
        from PySide6.QtGui import QColor
        c = QColor(self._color)
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        fg = "#000000" if lum > 128 else "#ffffff"
        self._color_btn.setStyleSheet(
            f"background-color:{self._color};color:{fg};border:1px solid #555;"
        )
        self._color_btn.setText(self._color.upper())

    def _pick_color(self) -> None:
        color = _pick_color(self._color, "Background color", self)
        if color:
            self._color = color
            self._refresh_color_btn()
            self.changed.emit()

    def _on_type_changed(self) -> None:
        # Keep an explicit state variable instead of relying only on
        # currentData(); this avoids transient Qt combo data mismatches.
        self._bg_type = self._normalize_bg_type(
            self._type_cb.currentData() or self._type_cb.currentText()
        )
        self._update_visibility()
        self.changed.emit()

    def _update_visibility(self) -> None:
        self._color_btn.setVisible(self._bg_type == "solid")
        self._img_edit.setVisible(self._bg_type == "image")
        self._img_row_label.setVisible(self._bg_type == "image")
        self._vid_edit.setVisible(self._bg_type == "video")
        self._vid_row_label.setVisible(self._bg_type == "video")

    def get_config(self) -> dict:
        return {
            "bg_type": self._bg_type,
            "bg_color": self._color if self._bg_type == "solid" else None,
            "bg_image": self._img_edit.get_value() if self._bg_type == "image" else None,
            "bg_video": self._vid_edit.get_value() if self._bg_type == "video" else None,
        }


# ---------------------------------------------------------------------------
# _StickmanSection
# ---------------------------------------------------------------------------

class _StickmanSection(QGroupBox):
    """Config section for a Stickman layer block.

    Shows current stickman_location values as numeric spinboxes.
    The drag-in-preview flow auto-updates these values via main_window.
    """

    changed = Signal()

    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__("Stickman", parent)
        form = QFormLayout(self)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(8)

        self._enabled_cb = QCheckBox("Show stickman")
        self._enabled_cb.setChecked(bool(config.get("stickman", True)))
        self._enabled_cb.stateChanged.connect(self.changed)
        form.addRow(self._enabled_cb)

        loc = config.get("stickman_location") or {}

        def _sp(val: float, tip: str) -> QDoubleSpinBox:
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 1.0)
            sp.setSingleStep(0.01)
            sp.setDecimals(3)
            sp.setValue(float(val))
            sp.setToolTip(tip)
            sp.valueChanged.connect(self.changed)
            return sp

        self._x_sp = _sp(loc.get("x", 0.010), "X position (0..1 fraction of frame width)")
        self._y_sp = _sp(loc.get("y", 0.090), "Y position (0..1 fraction of frame height)")
        self._w_sp = _sp(loc.get("w", 0.135), "Width (fraction of frame width)")
        self._h_sp = _sp(loc.get("h", 0.540), "Height (fraction of frame height)")

        form.addRow("X", self._x_sp)
        form.addRow("Y", self._y_sp)
        form.addRow("Width", self._w_sp)
        form.addRow("Height", self._h_sp)

        note = QLabel("Tip: drag the stickman box directly on the video preview\nto position it visually.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#888;font-size:10px;")
        form.addRow(note)

    def get_config(self) -> dict:
        return {
            "stickman": self._enabled_cb.isChecked(),
            "stickman_location": {
                "x": self._x_sp.value(),
                "y": self._y_sp.value(),
                "w": self._w_sp.value(),
                "h": self._h_sp.value(),
            },
        }


# ---------------------------------------------------------------------------
# _CountdownSection
# ---------------------------------------------------------------------------

class _CountdownSection(QGroupBox):
    """Config section for a Countdown layer block."""

    changed = Signal()
    _ANIM_OPTIONS = [
        ("Pop", "pop"),
        ("Flash", "flash"),
        ("Fade Cross", "fade_cross"),
        ("Shake", "shake"),
    ]
    _AUDIO_MODE_OPTIONS = [
        ("Default beep", "default"),
        ("Audio file", "file"),
    ]
    _LAST_AUDIO_MODE_OPTIONS = [
        ("Default last beep", "default"),
        ("Use another file", "file"),
        ("Same as regular", "same"),
    ]

    @staticmethod
    def _normalize_anim(value: object) -> str:
        raw = str(value or "").strip().lower().replace("-", "_")
        if raw in {"flash"}:
            return "flash"
        if raw in {"fade", "fade_cross", "crossfade", "cross_fade"}:
            return "fade_cross"
        if raw in {"shake", "jitter"}:
            return "shake"
        return "pop"

    @staticmethod
    def _normalize_audio_mode(value: object) -> str:
        raw = str(value or "").strip().lower()
        if raw == "file":
            return "file"
        return "default"

    @staticmethod
    def _normalize_last_audio_mode(value: object) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"file", "same"}:
            return raw
        return "default"

    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__("Countdown", parent)
        form = QFormLayout(self)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(8)

        self._enabled_cb = QCheckBox("Enabled")
        self._enabled_cb.setChecked(bool(config.get("relax_countdown_enabled", True)))
        self._enabled_cb.stateChanged.connect(self.changed)
        form.addRow("Countdown", self._enabled_cb)

        self._color: str = config.get("relax_countdown_color") or "#FFFFFF"
        self._color_btn = QPushButton()
        self._color_btn.setMinimumWidth(90)
        self._refresh_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        form.addRow("Color", self._color_btn)

        self._max_sec_sp = QDoubleSpinBox()
        self._max_sec_sp.setRange(0.0, 20.0)
        self._max_sec_sp.setSingleStep(0.5)
        self._max_sec_sp.setDecimals(1)
        self._max_sec_sp.setValue(float(config.get("relax_countdown_max_sec", 5.0)))
        self._max_sec_sp.setToolTip("Countdown visible window in seconds")
        self._max_sec_sp.valueChanged.connect(self.changed)
        form.addRow("Max seconds", self._max_sec_sp)

        from PySide6.QtWidgets import QComboBox
        self._anim_cb = QComboBox()
        for label, val in self._ANIM_OPTIONS:
            self._anim_cb.addItem(label, val)
        current_anim = self._normalize_anim(config.get("relax_countdown_anim", "pop"))
        idx = next((i for i, (_label, val) in enumerate(self._ANIM_OPTIONS) if val == current_anim), 0)
        self._anim_cb.setCurrentIndex(idx)
        self._anim_cb.currentIndexChanged.connect(self.changed)
        form.addRow("Number effect", self._anim_cb)

        self._audio_enabled_cb = QCheckBox("Enable countdown sound")
        self._audio_enabled_cb.setChecked(
            bool(config.get("relax_countdown_audio_enabled", False))
        )
        self._audio_enabled_cb.stateChanged.connect(self._on_audio_toggle_changed)
        form.addRow("Audio", self._audio_enabled_cb)

        from PySide6.QtWidgets import QComboBox
        self._audio_mode_cb = QComboBox()
        for label, val in self._AUDIO_MODE_OPTIONS:
            self._audio_mode_cb.addItem(label, val)
        audio_mode = self._normalize_audio_mode(
            config.get("relax_countdown_audio_mode", "default")
        )
        idx = next(
            (i for i, (_label, val) in enumerate(self._AUDIO_MODE_OPTIONS) if val == audio_mode),
            0,
        )
        self._audio_mode_cb.setCurrentIndex(idx)
        self._audio_mode_cb.currentIndexChanged.connect(self._on_audio_mode_changed)
        self._audio_mode_label = QLabel("Sound source")
        form.addRow(self._audio_mode_label, self._audio_mode_cb)

        from .segment_config_panel import _PathBrowseWidget
        audio_file = str(config.get("relax_countdown_audio_file", "") or "")
        self._audio_file_edit = _PathBrowseWidget(
            audio_file,
            title="Select countdown audio",
            file_filter="Audio (*.wav *.mp3 *.ogg *.m4a *.aac *.flac);;All files (*.*)",
            placeholder="Optional countdown sound file",
            parent=self,
        )
        self._audio_file_edit.changed.connect(self.changed)
        self._audio_file_label = QLabel("Sound file")
        form.addRow(self._audio_file_label, self._audio_file_edit)

        self._audio_volume_sp = QDoubleSpinBox()
        self._audio_volume_sp.setRange(0.0, 1.0)
        self._audio_volume_sp.setSingleStep(0.05)
        self._audio_volume_sp.setDecimals(2)
        self._audio_volume_sp.setValue(
            float(config.get("relax_countdown_audio_volume", 0.65) or 0.65)
        )
        self._audio_volume_sp.valueChanged.connect(self.changed)
        self._audio_volume_label = QLabel("Sound volume")
        form.addRow(self._audio_volume_label, self._audio_volume_sp)

        self._audio_last_mode_cb = QComboBox()
        for label, val in self._LAST_AUDIO_MODE_OPTIONS:
            self._audio_last_mode_cb.addItem(label, val)
        last_mode = self._normalize_last_audio_mode(
            config.get("relax_countdown_audio_last_mode", "default")
        )
        idx = next(
            (i for i, (_label, val) in enumerate(self._LAST_AUDIO_MODE_OPTIONS) if val == last_mode),
            0,
        )
        self._audio_last_mode_cb.setCurrentIndex(idx)
        self._audio_last_mode_cb.currentIndexChanged.connect(self._on_audio_mode_changed)
        self._audio_last_mode_label = QLabel("Last count sound")
        form.addRow(self._audio_last_mode_label, self._audio_last_mode_cb)

        last_audio_file = str(config.get("relax_countdown_audio_last_file", "") or "")
        self._audio_last_file_edit = _PathBrowseWidget(
            last_audio_file,
            title="Select countdown last sound",
            file_filter="Audio (*.wav *.mp3 *.ogg *.m4a *.aac *.flac);;All files (*.*)",
            placeholder="Optional last-count sound file",
            parent=self,
        )
        self._audio_last_file_edit.changed.connect(self.changed)
        self._audio_last_file_label = QLabel("Last sound file")
        form.addRow(self._audio_last_file_label, self._audio_last_file_edit)

        self._bbox_group = QGroupBox("Position & size", self)
        self._bbox_group.setToolTip(
            "Vị trí và kích thước hộp số đếm ngược, tỉ lệ 0..1 theo khung hình. "
            "Có thể kéo trực tiếp trong preview để chỉnh nhanh."
        )
        bbox_form = QFormLayout(self._bbox_group)
        bbox_form.setContentsMargins(8, 8, 8, 8)
        bbox_form.setSpacing(6)

        self._x_sp = QDoubleSpinBox()
        self._x_sp.setRange(0.0, 1.0)
        self._x_sp.setSingleStep(0.01)
        self._x_sp.setDecimals(3)
        self._x_sp.setValue(float(config.get("relax_countdown_x", 0.88) or 0.88))
        self._x_sp.valueChanged.connect(self.changed)
        bbox_form.addRow("X (0..1)", self._x_sp)

        self._y_sp = QDoubleSpinBox()
        self._y_sp.setRange(0.0, 1.0)
        self._y_sp.setSingleStep(0.01)
        self._y_sp.setDecimals(3)
        self._y_sp.setValue(float(config.get("relax_countdown_y", 0.04) or 0.04))
        self._y_sp.valueChanged.connect(self.changed)
        bbox_form.addRow("Y (0..1)", self._y_sp)

        self._w_sp = QDoubleSpinBox()
        self._w_sp.setRange(0.02, 1.0)
        self._w_sp.setSingleStep(0.01)
        self._w_sp.setDecimals(3)
        self._w_sp.setValue(float(config.get("relax_countdown_w", 0.10) or 0.10))
        self._w_sp.valueChanged.connect(self.changed)
        bbox_form.addRow("Width (0..1)", self._w_sp)

        self._h_sp = QDoubleSpinBox()
        self._h_sp.setRange(0.02, 1.0)
        self._h_sp.setSingleStep(0.01)
        self._h_sp.setDecimals(3)
        self._h_sp.setValue(float(config.get("relax_countdown_h", 0.16) or 0.16))
        self._h_sp.valueChanged.connect(self.changed)
        bbox_form.addRow("Height (0..1)", self._h_sp)

        form.addRow(self._bbox_group)

        self._update_audio_visibility()

    def _refresh_color_btn(self) -> None:
        c = QColor(self._color)
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        fg = "#000000" if lum > 128 else "#ffffff"
        self._color_btn.setStyleSheet(
            f"background-color:{self._color};color:{fg};border:1px solid #555;"
        )
        self._color_btn.setText(self._color.upper())

    def _pick_color(self) -> None:
        color = _pick_color(self._color, "Countdown color", self)
        if color:
            self._color = color
            self._refresh_color_btn()
            self.changed.emit()

    def _on_audio_toggle_changed(self) -> None:
        self._update_audio_visibility()
        self.changed.emit()

    def _on_audio_mode_changed(self) -> None:
        self._update_audio_visibility()
        self.changed.emit()

    def _update_audio_visibility(self) -> None:
        enabled = self._audio_enabled_cb.isChecked()
        mode = self._normalize_audio_mode(self._audio_mode_cb.currentData())
        last_mode = self._normalize_last_audio_mode(self._audio_last_mode_cb.currentData())

        self._audio_mode_label.setVisible(enabled)
        self._audio_mode_cb.setVisible(enabled)
        self._audio_volume_label.setVisible(enabled)
        self._audio_volume_sp.setVisible(enabled)
        self._audio_last_mode_label.setVisible(enabled)
        self._audio_last_mode_cb.setVisible(enabled)

        show_main_file = enabled and mode == "file"
        self._audio_file_label.setVisible(show_main_file)
        self._audio_file_edit.setVisible(show_main_file)

        show_last_file = enabled and last_mode == "file"
        self._audio_last_file_label.setVisible(show_last_file)
        self._audio_last_file_edit.setVisible(show_last_file)

    def get_config(self) -> dict:
        return {
            "relax_countdown_enabled": self._enabled_cb.isChecked(),
            "relax_countdown_color": self._color,
            "relax_countdown_max_sec": self._max_sec_sp.value(),
            "relax_countdown_anim": self._normalize_anim(self._anim_cb.currentData()),
            "relax_countdown_audio_enabled": self._audio_enabled_cb.isChecked(),
            "relax_countdown_audio_mode": self._normalize_audio_mode(
                self._audio_mode_cb.currentData()
            ),
            "relax_countdown_audio_file": self._audio_file_edit.get_value(),
            "relax_countdown_audio_volume": self._audio_volume_sp.value(),
            "relax_countdown_audio_last_mode": self._normalize_last_audio_mode(
                self._audio_last_mode_cb.currentData()
            ),
            "relax_countdown_audio_last_file": self._audio_last_file_edit.get_value(),
            "relax_countdown_x": self._x_sp.value(),
            "relax_countdown_y": self._y_sp.value(),
            "relax_countdown_w": self._w_sp.value(),
            "relax_countdown_h": self._h_sp.value(),
        }
