"""Layer edit dialogs — per-kind configuration UI for timeline layer blocks.

Each layer kind has a dedicated section widget (QGroupBox) with:
  - __init__(config: dict)   — populate widgets from dict
  - changed = Signal()       — emitted on any user edit
  - get_config() -> dict     — export current state

``_LayerEditDialog`` wraps the appropriate section in a modal dialog.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from studio.models.layer import Layer

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
        self._max_sec_sp.setValue(float(config.get("relax_countdown_max_sec", 3.0)))
        self._max_sec_sp.setToolTip("Countdown visible window in seconds")
        self._max_sec_sp.valueChanged.connect(self.changed)
        form.addRow("Max seconds", self._max_sec_sp)

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

    def get_config(self) -> dict:
        return {
            "relax_countdown_enabled": self._enabled_cb.isChecked(),
            "relax_countdown_color": self._color,
            "relax_countdown_max_sec": self._max_sec_sp.value(),
        }


# ---------------------------------------------------------------------------
# _LayerEditDialog
# ---------------------------------------------------------------------------

class _LayerEditDialog(QDialog):
    """Modal dialog to edit a layer's config.

    Dispatches to the appropriate section widget based on layer.kind.
    Returns updated config via get_config() after Accepted.
    """

    def __init__(self, layer: "Layer", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.layer = layer
        self.setWindowTitle(f"Edit {layer.kind.replace('_', ' ').title()} Layer")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)

        kind = layer.kind
        cfg = dict(layer.config)

        if kind == "background":
            self._section: QWidget = _BackgroundSection(cfg, self)
        elif kind == "floor":
            from .segment_config_panel import _FloorPanelSection
            self._section = _FloorPanelSection(
                color=cfg.get("floor_panel_color"),
                blink=bool(cfg.get("floor_panel_blink", False)),
                image=cfg.get("floor_panel_image"),
                floor_panel_opacity=float(cfg.get("floor_panel_opacity", 1.0)),
                floor_layout=str(cfg.get("floor_layout", "auto")),
                floor_bg_color=cfg.get("floor_bg_color"),
                floor_bg_opacity=float(cfg.get("floor_bg_opacity", 1.0)),
                chevron_color=str(cfg.get("chevron_color", "#FFD700")),
                chevron_scroll=bool(cfg.get("chevron_scroll", True)),
                chevron_blink=bool(cfg.get("chevron_blink", False)),
                chevron_width_frac=float(cfg.get("chevron_width_frac", 0.45)),
                chevron_count=int(cfg.get("chevron_count", 6)),
                full_static_image=bool(cfg.get("floor_full_static_image", False)),
                parent=self,
            )
        elif kind == "side_rails":
            from .segment_config_panel import _SideRailSection
            self._section = _SideRailSection(
                color=str(cfg.get("rail_color", "#FF60FF")),
                shape=str(cfg.get("rail_shape", "chunky")),
                height=float(cfg.get("rail_height", 0.14)),
                offset_x=float(cfg.get("rail_offset_x", 0.08)),
                image=cfg.get("rail_image"),
                pulse=str(cfg.get("rail_pulse", "beat")),
                pulse_intensity=float(cfg.get("rail_pulse_intensity", 0.6)),
                texture_non_loop=bool(cfg.get("rail_texture_non_loop", False)),
                chevron_depth=float(cfg.get("rail_chevron_depth", 1.0)),
                chevron_density=int(cfg.get("rail_chevron_density", 6)),
                pillar_count=int(cfg.get("rail_pillar_count", 16)),
                pillar_radius=float(cfg.get("rail_pillar_radius", 1.0)),
                chase_mode=str(cfg.get("rail_chase_mode", "time")),
                chase_speed_frames=int(cfg.get("rail_chase_speed_frames", 4)),
                dot_count=int(cfg.get("rail_dot_count", 24)),
                dot_lines=int(cfg.get("rail_dot_lines", 1)),
                dot_size_px=int(cfg.get("rail_dot_size_px", 6)),
                dot_anim_mode=str(cfg.get("rail_dot_anim_mode", "audio")),
                dot_color_near=str(cfg.get("rail_dot_color_near", "#FF60FF")),
                dot_color_far=str(cfg.get("rail_dot_color_far", "#00FFFF")),
                parent=self,
            )
        elif kind == "stickman":
            self._section = _StickmanSection(cfg, self)
        elif kind == "countdown":
            self._section = _CountdownSection(cfg, self)
        else:
            raise ValueError(f"Unknown layer kind: {kind!r}")

        layout.addWidget(self._section)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_config(self) -> dict:
        """Return the section's current config dict."""
        return self._section.get_config()  # type: ignore[attr-defined]
