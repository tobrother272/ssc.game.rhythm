"""Layer edit sections for timeline layer blocks.

Each layer kind has a dedicated section widget (QGroupBox) with:
  - __init__(config: dict)   — populate widgets from dict
  - changed = Signal()       — emitted on any user edit
  - get_config() -> dict     — export current state

"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)
from studio.editor.inspector_drop_helper import (
    get_media_from_drop,
    set_drop_highlight,
    set_drop_reject_flash,
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
    media_dropped = Signal(str, bool)  # message, is_error

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

    def __init__(
        self,
        config: dict,
        project=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Background", parent)
        self._project = project
        self.setAcceptDrops(True)
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

    def _notify_drop(self, msg: str, is_error: bool = False) -> None:
        self.media_dropped.emit(msg, is_error)
        if is_error:
            set_drop_reject_flash(self)
            QTimer.singleShot(800, lambda: set_drop_highlight(self, False))

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData() and event.mimeData().hasFormat("application/x-htstudio-media-id"):
            event.acceptProposedAction()
            set_drop_highlight(self, True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData() and event.mimeData().hasFormat("application/x-htstudio-media-id"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        set_drop_highlight(self, False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        set_drop_highlight(self, False)
        result = get_media_from_drop(event, self._project)
        if result is None:
            event.ignore()
            return
        media, kind = result
        if kind == "image":
            idx = next((i for i, (_label, val) in enumerate(self._BG_TYPES) if val == "image"), 1)
            self._type_cb.setCurrentIndex(idx)
            self._img_edit.set_value(str(media.source_path), emit_changed=False)
            self._vid_edit.set_value("", emit_changed=False)
            self.changed.emit()
            self._notify_drop(f"Background -> image: {media.display_name}")
            event.acceptProposedAction()
            return
        if kind == "video":
            idx = next((i for i, (_label, val) in enumerate(self._BG_TYPES) if val == "video"), 2)
            self._type_cb.setCurrentIndex(idx)
            self._vid_edit.set_value(str(media.source_path), emit_changed=False)
            self._img_edit.set_value("", emit_changed=False)
            self.changed.emit()
            self._notify_drop(f"Background -> video: {media.display_name}")
            event.acceptProposedAction()
            return
        self._notify_drop("Background accepts image/video only.", True)
        event.ignore()

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
    media_dropped = Signal(str, bool)  # message, is_error

    def __init__(
        self,
        config: dict,
        project=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Stickman", parent)
        self._project = project
        self.setAcceptDrops(True)
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

    def _notify_drop(self, msg: str, is_error: bool = False) -> None:
        self.media_dropped.emit(msg, is_error)
        if is_error:
            set_drop_reject_flash(self)
            QTimer.singleShot(800, lambda: set_drop_highlight(self, False))

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData() and event.mimeData().hasFormat("application/x-htstudio-media-id"):
            event.acceptProposedAction()
            set_drop_highlight(self, True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData() and event.mimeData().hasFormat("application/x-htstudio-media-id"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        set_drop_highlight(self, False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        set_drop_highlight(self, False)
        result = get_media_from_drop(event, self._project)
        if result is None:
            event.ignore()
            return
        self._notify_drop(
            "Stickman doesn't accept media — edit position via spinboxes.",
            True,
        )
        event.ignore()

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
    media_dropped = Signal(str, bool)  # message, is_error
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

    def __init__(
        self,
        config: dict,
        project=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Countdown", parent)
        self._project = project
        self.setAcceptDrops(True)
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

        # Countdown box (x/y/w/h) is edited via drag handles in preview.
        # Preserve these values on Inspector commits to avoid data loss.
        self._countdown_x = float(config.get("relax_countdown_x", 0.88) or 0.88)
        self._countdown_y = float(config.get("relax_countdown_y", 0.04) or 0.04)
        self._countdown_w = float(config.get("relax_countdown_w", 0.10) or 0.10)
        self._countdown_h = float(config.get("relax_countdown_h", 0.16) or 0.16)

        self._style_group = QGroupBox("Style", self)
        style_form = QFormLayout(self._style_group)
        style_form.setContentsMargins(8, 8, 8, 8)
        style_form.setSpacing(6)

        self._border_thick_sp = QDoubleSpinBox()
        self._border_thick_sp.setRange(0.0, 10.0)
        self._border_thick_sp.setSingleStep(0.5)
        self._border_thick_sp.setDecimals(1)
        self._border_thick_sp.setValue(
            float(config.get("relax_countdown_border_thickness", 2.0) or 2.0)
        )
        self._border_thick_sp.setToolTip("Độ dày viền (0 = không viền)")
        self._border_thick_sp.valueChanged.connect(self.changed)
        style_form.addRow("Border thickness", self._border_thick_sp)

        self._glow_strength_sp = QSpinBox()
        self._glow_strength_sp.setRange(0, 100)
        self._glow_strength_sp.setSingleStep(5)
        self._glow_strength_sp.setSuffix(" %")
        self._glow_strength_sp.setValue(
            int(round(float(config.get("relax_countdown_glow_strength", 60.0) or 60.0)))
        )
        self._glow_strength_sp.setToolTip("Cường độ vầng quang (0 = tắt)")
        self._glow_strength_sp.valueChanged.connect(self.changed)
        style_form.addRow("Glow strength", self._glow_strength_sp)

        form.addRow(self._style_group)

        self._update_audio_visibility()

    def _notify_drop(self, msg: str, is_error: bool = False) -> None:
        self.media_dropped.emit(msg, is_error)
        if is_error:
            set_drop_reject_flash(self)
            QTimer.singleShot(800, lambda: set_drop_highlight(self, False))

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData() and event.mimeData().hasFormat("application/x-htstudio-media-id"):
            event.acceptProposedAction()
            set_drop_highlight(self, True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData() and event.mimeData().hasFormat("application/x-htstudio-media-id"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        set_drop_highlight(self, False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        set_drop_highlight(self, False)
        result = get_media_from_drop(event, self._project)
        if result is None:
            event.ignore()
            return
        media, kind = result
        if kind != "audio":
            self._notify_drop("Countdown accepts audio only.", True)
            event.ignore()
            return
        self._audio_enabled_cb.setChecked(True)
        idx = next(
            (i for i, (_label, val) in enumerate(self._AUDIO_MODE_OPTIONS) if val == "file"),
            1,
        )
        self._audio_mode_cb.setCurrentIndex(idx)
        self._audio_file_edit.set_value(str(media.source_path), emit_changed=False)
        self._update_audio_visibility()
        self.changed.emit()
        self._notify_drop(f"Countdown audio set: {media.display_name}")
        event.acceptProposedAction()

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
            "relax_countdown_x": self._countdown_x,
            "relax_countdown_y": self._countdown_y,
            "relax_countdown_w": self._countdown_w,
            "relax_countdown_h": self._countdown_h,
            "relax_countdown_border_thickness": self._border_thick_sp.value(),
            "relax_countdown_glow_strength": float(self._glow_strength_sp.value()),
        }


class _StartGateSection(QGroupBox):
    """Config section for a Start Gate layer block."""

    changed = Signal()
    media_dropped = Signal(str, bool)  # message, is_error
    _TYPE_OPTIONS = [
        ("Solid color", "color"),
        ("Image", "image"),
        ("Video", "video"),
    ]

    @staticmethod
    def _normalize_type(value: object) -> str:
        raw = str(value or "color").strip().lower()
        return raw if raw in {"color", "image", "video"} else "color"

    def __init__(
        self,
        config: dict,
        project=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Start Gate", parent)
        self._project = project
        self.setAcceptDrops(True)
        form = QFormLayout(self)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(8)

        self._enabled_cb = QCheckBox("Enabled")
        self._enabled_cb.setChecked(bool(config.get("start_gate_enabled", True)))
        self._enabled_cb.stateChanged.connect(self.changed)
        form.addRow("Start gate", self._enabled_cb)

        from PySide6.QtWidgets import QComboBox

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

        self._color: str = str(config.get("start_gate_color") or "#1a1a1a")
        self._color_btn = QPushButton()
        self._color_btn.setMinimumWidth(90)
        self._refresh_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        self._color_label = QLabel("Color")
        form.addRow(self._color_label, self._color_btn)

        self._border_color: str = str(
            config.get("start_gate_border_color") or "#ffffff"
        )
        self._border_color_btn = QPushButton()
        self._border_color_btn.setMinimumWidth(90)
        self._refresh_border_color_btn()
        self._border_color_btn.clicked.connect(self._pick_border_color)
        form.addRow("Border color", self._border_color_btn)

        self._border_thickness_sp = QDoubleSpinBox()
        self._border_thickness_sp.setRange(0.0, 10.0)
        self._border_thickness_sp.setSingleStep(0.5)
        self._border_thickness_sp.setDecimals(1)
        self._border_thickness_sp.setValue(
            float(config.get("start_gate_border_thickness", 0.0) or 0.0)
        )
        self._border_thickness_sp.valueChanged.connect(self.changed)
        form.addRow("Border thickness", self._border_thickness_sp)

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
        # Legacy bbox fields are preserved silently for old projects.
        self._start_gate_x = float(config.get("start_gate_x", 0.30) or 0.30)
        self._start_gate_y = float(config.get("start_gate_y", 0.18) or 0.18)
        self._start_gate_w = float(config.get("start_gate_w", 0.40) or 0.40)
        self._start_gate_h = float(config.get("start_gate_h", 0.14) or 0.14)
        self._update_visibility()

    def _notify_drop(self, msg: str, is_error: bool = False) -> None:
        self.media_dropped.emit(msg, is_error)
        if is_error:
            set_drop_reject_flash(self)
            QTimer.singleShot(800, lambda: set_drop_highlight(self, False))

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData() and event.mimeData().hasFormat("application/x-htstudio-media-id"):
            event.acceptProposedAction()
            set_drop_highlight(self, True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData() and event.mimeData().hasFormat("application/x-htstudio-media-id"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        set_drop_highlight(self, False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        set_drop_highlight(self, False)
        result = get_media_from_drop(event, self._project)
        if result is None:
            event.ignore()
            return
        media, kind = result
        if kind == "image":
            idx = next((i for i, (_label, v) in enumerate(self._TYPE_OPTIONS) if v == "image"), 1)
            self._type_cb.setCurrentIndex(idx)
            self._image_edit.set_value(str(media.source_path), emit_changed=False)
            self._video_edit.set_value("", emit_changed=False)
            self.changed.emit()
            self._notify_drop(f"Start gate -> image: {media.display_name}")
            event.acceptProposedAction()
            return
        if kind == "video":
            idx = next((i for i, (_label, v) in enumerate(self._TYPE_OPTIONS) if v == "video"), 2)
            self._type_cb.setCurrentIndex(idx)
            self._video_edit.set_value(str(media.source_path), emit_changed=False)
            self._image_edit.set_value("", emit_changed=False)
            self.changed.emit()
            self._notify_drop(f"Start gate -> video: {media.display_name}")
            event.acceptProposedAction()
            return
        self._notify_drop("Start gate accepts image/video only.", True)
        event.ignore()

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

    def _refresh_border_color_btn(self) -> None:
        c = QColor(self._border_color)
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        fg = "#000000" if lum > 128 else "#ffffff"
        self._border_color_btn.setStyleSheet(
            f"background-color:{self._border_color};color:{fg};border:1px solid #555;"
        )
        self._border_color_btn.setText(self._border_color.upper())

    def _pick_border_color(self) -> None:
        color = _pick_color(self._border_color, "Start gate border color", self)
        if color:
            self._border_color = color
            self._refresh_border_color_btn()
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
            "start_gate_border_color": self._border_color,
            "start_gate_border_thickness": self._border_thickness_sp.value(),
            "start_gate_image": self._image_edit.get_value(),
            "start_gate_video": self._video_edit.get_value(),
            "start_gate_x": self._start_gate_x,
            "start_gate_y": self._start_gate_y,
            "start_gate_w": self._start_gate_w,
            "start_gate_h": self._start_gate_h,
        }


# ---------------------------------------------------------------------------
# Combo Counter section
# ---------------------------------------------------------------------------


class _ComboSection(QGroupBox):
    """Config section for a Combo counter layer block."""

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
    _MILESTONE_MODE_OPTIONS = [
        ("Default milestone beep", "default"),
        ("Use another file", "file"),
        ("Same as regular", "same"),
    ]
    _FONT_OPTIONS = [
        ("Simplex", "simplex"),
        ("Plain", "plain"),
        ("Duplex", "duplex"),
        ("Complex", "complex"),
        ("Triplex", "triplex"),
        ("Complex Small", "complex_small"),
        ("Script Simplex", "script_simplex"),
        ("Script Complex", "script_complex"),
    ]

    @staticmethod
    def _normalize_anim(value: object) -> str:
        raw = str(value or "").strip().lower().replace("-", "_")
        if raw == "flash":
            return "flash"
        if raw in {"fade", "fade_cross", "crossfade", "cross_fade"}:
            return "fade_cross"
        if raw in {"shake", "jitter"}:
            return "shake"
        return "pop"

    @staticmethod
    def _normalize_audio_mode(value: object) -> str:
        raw = str(value or "").strip().lower()
        return "file" if raw == "file" else "default"

    @staticmethod
    def _normalize_milestone_mode(value: object) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"file", "same"}:
            return raw
        return "default"

    def __init__(self, config: dict, parent: QWidget | None = None) -> None:
        super().__init__("Combo Counter", parent)
        form = QFormLayout(self)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(8)

        # ── Enabled ──────────────────────────────────────────────────────────
        self._enabled_cb = QCheckBox("Enabled")
        self._enabled_cb.setChecked(bool(config.get("combo_enabled", True)))
        self._enabled_cb.stateChanged.connect(self.changed)
        form.addRow("Combo", self._enabled_cb)

        # ── Color ─────────────────────────────────────────────────────────────
        self._color: str = str(config.get("combo_color") or "#FFFFFF")
        self._color_btn = QPushButton()
        self._color_btn.setMinimumWidth(90)
        self._refresh_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        form.addRow("Color", self._color_btn)

        # ── Font family ───────────────────────────────────────────────────────
        self._font_cb = QComboBox()
        for label, val in self._FONT_OPTIONS:
            self._font_cb.addItem(label, val)
        cur_font = str(config.get("combo_font_family", "duplex") or "duplex")
        idx = next(
            (i for i, (_l, v) in enumerate(self._FONT_OPTIONS) if v == cur_font), 2
        )
        self._font_cb.setCurrentIndex(idx)
        self._font_cb.currentIndexChanged.connect(self.changed)
        form.addRow("Font family", self._font_cb)

        # ── Label text ────────────────────────────────────────────────────────
        self._label_edit = QLineEdit(str(config.get("combo_label", "COMBO") or "COMBO"))
        self._label_edit.setMaxLength(20)
        self._label_edit.setPlaceholderText("e.g. COMBO, HITS, STREAK")
        self._label_edit.textChanged.connect(self.changed)
        form.addRow("Label text", self._label_edit)

        # ── Fade after break ──────────────────────────────────────────────────
        self._fade_sp = QDoubleSpinBox()
        self._fade_sp.setRange(0.0, 10.0)
        self._fade_sp.setSingleStep(0.5)
        self._fade_sp.setDecimals(1)
        self._fade_sp.setValue(float(config.get("combo_fade_after_break_sec", 0.5) or 0.5))
        self._fade_sp.setSuffix(" s")
        self._fade_sp.setToolTip("Fade duration after combo breaks (0 = hide instantly)")
        self._fade_sp.valueChanged.connect(self.changed)
        form.addRow("Fade after break", self._fade_sp)

        # ── Animation ─────────────────────────────────────────────────────────
        self._anim_cb = QComboBox()
        for label, val in self._ANIM_OPTIONS:
            self._anim_cb.addItem(label, val)
        cur_anim = self._normalize_anim(config.get("combo_anim", "pop"))
        idx = next(
            (i for i, (_l, v) in enumerate(self._ANIM_OPTIONS) if v == cur_anim), 0
        )
        self._anim_cb.setCurrentIndex(idx)
        self._anim_cb.currentIndexChanged.connect(self.changed)
        form.addRow("Number effect", self._anim_cb)

        # ── Audio ─────────────────────────────────────────────────────────────
        self._audio_enabled_cb = QCheckBox("Enable combo hit sound")
        self._audio_enabled_cb.setChecked(bool(config.get("combo_audio_enabled", False)))
        self._audio_enabled_cb.stateChanged.connect(self._on_audio_toggle)
        form.addRow("Audio", self._audio_enabled_cb)

        self._audio_mode_cb = QComboBox()
        for label, val in self._AUDIO_MODE_OPTIONS:
            self._audio_mode_cb.addItem(label, val)
        am = self._normalize_audio_mode(config.get("combo_audio_mode", "default"))
        self._audio_mode_cb.setCurrentIndex(
            next((i for i, (_l, v) in enumerate(self._AUDIO_MODE_OPTIONS) if v == am), 0)
        )
        self._audio_mode_cb.currentIndexChanged.connect(self._on_audio_mode_changed)
        self._audio_mode_label = QLabel("Sound source")
        form.addRow(self._audio_mode_label, self._audio_mode_cb)

        from .segment_config_panel import _PathBrowseWidget
        self._audio_file_edit = _PathBrowseWidget(
            str(config.get("combo_audio_file", "") or ""),
            title="Select combo hit sound",
            file_filter="Audio (*.wav *.mp3 *.ogg *.m4a *.aac *.flac);;All files (*.*)",
            placeholder="Optional hit sound file",
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
            float(config.get("combo_audio_volume", 0.65) or 0.65)
        )
        self._audio_volume_sp.valueChanged.connect(self.changed)
        self._audio_volume_label = QLabel("Sound volume")
        form.addRow(self._audio_volume_label, self._audio_volume_sp)

        self._milestone_mode_cb = QComboBox()
        for label, val in self._MILESTONE_MODE_OPTIONS:
            self._milestone_mode_cb.addItem(label, val)
        mm = self._normalize_milestone_mode(
            config.get("combo_audio_milestone_mode", "default")
        )
        self._milestone_mode_cb.setCurrentIndex(
            next((i for i, (_l, v) in enumerate(self._MILESTONE_MODE_OPTIONS) if v == mm), 0)
        )
        self._milestone_mode_cb.currentIndexChanged.connect(self._on_audio_mode_changed)
        self._milestone_mode_label = QLabel("Milestone sound")
        form.addRow(self._milestone_mode_label, self._milestone_mode_cb)

        self._milestone_file_edit = _PathBrowseWidget(
            str(config.get("combo_audio_milestone_file", "") or ""),
            title="Select milestone sound",
            file_filter="Audio (*.wav *.mp3 *.ogg *.m4a *.aac *.flac);;All files (*.*)",
            placeholder="Optional milestone sound file",
            parent=self,
        )
        self._milestone_file_edit.changed.connect(self.changed)
        self._milestone_file_label = QLabel("Milestone file")
        form.addRow(self._milestone_file_label, self._milestone_file_edit)

        # ── Position & size (stored internally; dragged via overlay) ──────────
        self._combo_x = float(config.get("combo_x", 0.85) or 0.85)
        self._combo_y = float(config.get("combo_y", 0.08) or 0.08)
        self._combo_w = float(config.get("combo_w", 0.13) or 0.13)
        self._combo_h = float(config.get("combo_h", 0.18) or 0.18)

        # ── Style groupbox ────────────────────────────────────────────────────
        self._style_group = QGroupBox("Style", self)
        style_form = QFormLayout(self._style_group)
        style_form.setContentsMargins(8, 8, 8, 8)
        style_form.setSpacing(6)

        self._border_sp = QDoubleSpinBox()
        self._border_sp.setRange(0.0, 30.0)
        self._border_sp.setSingleStep(1.0)
        self._border_sp.setDecimals(1)
        self._border_sp.setValue(float(config.get("combo_border_thickness", 8.0) or 8.0))
        self._border_sp.setToolTip("Neon border thickness (0 = none)")
        self._border_sp.valueChanged.connect(self.changed)
        style_form.addRow("Border thickness", self._border_sp)

        self._glow_sp = QSpinBox()
        self._glow_sp.setRange(0, 100)
        self._glow_sp.setSingleStep(5)
        self._glow_sp.setSuffix(" %")
        self._glow_sp.setValue(
            int(round(float(config.get("combo_glow_strength", 85.0) or 85.0)))
        )
        self._glow_sp.setToolTip("Glow intensity (0 = off)")
        self._glow_sp.valueChanged.connect(self.changed)
        style_form.addRow("Glow strength", self._glow_sp)

        form.addRow(self._style_group)

        # ── Tier milestones groupbox ──────────────────────────────────────────
        self._tier_group = QGroupBox("Tier milestones", self)
        tier_form = QFormLayout(self._tier_group)
        tier_form.setContentsMargins(8, 10, 8, 8)
        tier_form.setSpacing(6)

        _tier_defaults = [
            (30, "Great",   "#22c55e"),
            (60, "Superb",  "#3b82f6"),
            (90, "Perfect", "#a855f7"),
            (120, "Godlike","#f59e0b"),
        ]
        self._tier_colors: list[str] = []
        self._tier_color_btns: list[QPushButton] = []
        self._tier_widgets: list[tuple[QSpinBox, QLineEdit]] = []
        for i, (def_thresh, def_label, def_color) in enumerate(_tier_defaults, 1):
            thresh_sp = QSpinBox()
            thresh_sp.setRange(0, 9999)
            thresh_sp.setValue(int(config.get(f"combo_tier{i}_threshold", def_thresh) or def_thresh))
            thresh_sp.setToolTip("Combo count to activate this tier (0 = disabled)")
            thresh_sp.valueChanged.connect(self.changed)

            label_edit = QLineEdit(str(config.get(f"combo_tier{i}_label", def_label) or def_label))
            label_edit.setMaxLength(20)
            label_edit.setPlaceholderText(f"e.g. {def_label}")
            label_edit.textChanged.connect(self.changed)

            tier_col = str(config.get(f"combo_tier{i}_color", def_color) or def_color)
            self._tier_colors.append(tier_col)
            color_btn = QPushButton(tier_col.upper())
            color_btn.setFixedWidth(74)
            _idx = i - 1
            color_btn.clicked.connect(lambda _chk, idx=_idx: self._pick_tier_color(idx))
            self._tier_color_btns.append(color_btn)
            self._refresh_tier_color_btn(_idx)

            row_w = QWidget()
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.setSpacing(4)
            row_l.addWidget(thresh_sp, 1)
            row_l.addWidget(label_edit, 2)
            row_l.addWidget(color_btn, 0)
            tier_form.addRow(f"Tier {i} ≥", row_w)
            self._tier_widgets.append((thresh_sp, label_edit))

        hint = QLabel("<i>Set threshold = 0 to disable a tier.</i>")
        hint.setStyleSheet("color: #888;")
        tier_form.addRow(hint)
        form.addRow(self._tier_group)

        self._update_audio_visibility()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _refresh_color_btn(self) -> None:
        c = QColor(self._color)
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        fg = "#000000" if lum > 128 else "#ffffff"
        self._color_btn.setStyleSheet(
            f"background-color:{self._color};color:{fg};border:1px solid #555;"
        )
        self._color_btn.setText(self._color.upper())

    def _pick_color(self) -> None:
        color = _pick_color(self._color, "Combo color", self)
        if color:
            self._color = color
            self._refresh_color_btn()
            self.changed.emit()

    def _refresh_tier_color_btn(self, idx: int) -> None:
        col = self._tier_colors[idx]
        c = QColor(col)
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        fg = "#000000" if lum > 128 else "#ffffff"
        btn = self._tier_color_btns[idx]
        btn.setStyleSheet(
            f"background-color:{col};color:{fg};border:1px solid #555;"
            "padding:1px 4px;"
        )
        btn.setText(col.upper())

    def _pick_tier_color(self, idx: int) -> None:
        color = _pick_color(self._tier_colors[idx], f"Tier {idx + 1} color", self)
        if color:
            self._tier_colors[idx] = color
            self._refresh_tier_color_btn(idx)
            self.changed.emit()

    def _on_audio_toggle(self) -> None:
        self._update_audio_visibility()
        self.changed.emit()

    def _on_audio_mode_changed(self) -> None:
        self._update_audio_visibility()
        self.changed.emit()

    def _update_audio_visibility(self) -> None:
        enabled = self._audio_enabled_cb.isChecked()
        mode = self._normalize_audio_mode(self._audio_mode_cb.currentData())
        ms_mode = self._normalize_milestone_mode(self._milestone_mode_cb.currentData())

        self._audio_mode_label.setVisible(enabled)
        self._audio_mode_cb.setVisible(enabled)
        self._audio_volume_label.setVisible(enabled)
        self._audio_volume_sp.setVisible(enabled)
        self._milestone_mode_label.setVisible(enabled)
        self._milestone_mode_cb.setVisible(enabled)

        self._audio_file_label.setVisible(enabled and mode == "file")
        self._audio_file_edit.setVisible(enabled and mode == "file")
        self._milestone_file_label.setVisible(enabled and ms_mode == "file")
        self._milestone_file_edit.setVisible(enabled and ms_mode == "file")

    def set_bbox(self, x: float, y: float, w: float, h: float) -> None:
        """Called by overlay drag to update internal bbox without UI rebuild."""
        self._combo_x = float(x)
        self._combo_y = float(y)
        self._combo_w = float(w)
        self._combo_h = float(h)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_config(self) -> dict:
        cfg: dict = {
            "combo_enabled": self._enabled_cb.isChecked(),
            "combo_color": self._color,
            "combo_font_family": self._font_cb.currentData() or "duplex",
            "combo_label": self._label_edit.text().strip() or "COMBO",
            "combo_fade_after_break_sec": self._fade_sp.value(),
            "combo_anim": self._normalize_anim(self._anim_cb.currentData()),
            "combo_audio_enabled": self._audio_enabled_cb.isChecked(),
            "combo_audio_mode": self._normalize_audio_mode(self._audio_mode_cb.currentData()),
            "combo_audio_file": self._audio_file_edit.get_value(),
            "combo_audio_volume": self._audio_volume_sp.value(),
            "combo_audio_milestone_mode": self._normalize_milestone_mode(
                self._milestone_mode_cb.currentData()
            ),
            "combo_audio_milestone_file": self._milestone_file_edit.get_value(),
            "combo_x": self._combo_x,
            "combo_y": self._combo_y,
            "combo_w": self._combo_w,
            "combo_h": self._combo_h,
            "combo_border_thickness": self._border_sp.value(),
            "combo_glow_strength": float(self._glow_sp.value()),
            "combo_number_font_scale": 0.0,
            "combo_label_font_scale": 0.0,
            "combo_tier_font_scale": 0.0,
        }
        for i, (thresh_sp, label_edit) in enumerate(self._tier_widgets, 1):
            cfg[f"combo_tier{i}_threshold"] = thresh_sp.value()
            cfg[f"combo_tier{i}_label"] = label_edit.text().strip() or f"Tier{i}"
            cfg[f"combo_tier{i}_color"] = self._tier_colors[i - 1]
        return cfg
