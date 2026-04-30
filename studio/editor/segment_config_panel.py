"""Inspector panel for selected timeline segment."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional



from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from studio.models import Project, Segment, build_settings


class _NoScrollWheelFilter(QObject):
    """Event filter that drops wheel events unless the widget has keyboard focus.

    Install this on any QSpinBox / QDoubleSpinBox / QComboBox to prevent
    accidental value changes while the user is scrolling the config panel.
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.Wheel:
            # Only let the wheel through when the widget is actively focused
            # (i.e. the user clicked into it first).
            if not obj.hasFocus():
                event.ignore()
                return True   # consumed — do NOT propagate to the widget
        return super().eventFilter(obj, event)


def _no_scroll(widget: QWidget) -> QWidget:
    """Attach the wheel-lock filter and set StrongFocus on *widget*, then return it."""
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.installEventFilter(_NO_SCROLL_FILTER)
    return widget


# Single shared instance — stateless, safe to reuse across all widgets.
_NO_SCROLL_FILTER = _NoScrollWheelFilter()


class _ModeListWidget(QWidget):
    """Row of checkboxes for selecting the sub-modes in combo mode.

    Emits ``changed`` whenever any checkbox is toggled so the parent
    form can call ``_commit_settings()`` immediately.
    """

    changed = Signal()
    _ALL_MODES = ("punch", "dance", "line", "relax")

    def __init__(self, value: list, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        current: set[str] = set(value) if isinstance(value, list) else {"punch"}
        self._boxes: dict[str, QCheckBox] = {}
        for mode in self._ALL_MODES:
            cb = QCheckBox(mode.capitalize())
            cb.setChecked(mode in current)
            cb.stateChanged.connect(self.changed)
            row.addWidget(cb)
            self._boxes[mode] = cb
        row.addStretch()
        self.setToolTip(
            "Select the sub-modes that alternate each beat in combo mode.\n"
            "At least one must be checked (defaults to Punch if all unchecked)."
        )

    def get_value(self) -> list:
        selected = [m for m in self._ALL_MODES if self._boxes[m].isChecked()]
        return selected or ["punch"]


class _PathBrowseWidget(QWidget):
    """Line-edit file path with a browse button."""

    changed = Signal()

    def __init__(
        self,
        value: Optional[str],
        *,
        title: str,
        file_filter: str,
        placeholder: str = "Optional file path",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._file_filter = file_filter

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._edit = QLineEdit("" if value is None else str(value))
        self._edit.setPlaceholderText(placeholder)
        self._edit.editingFinished.connect(self.changed)
        browse = QPushButton("…")
        browse.setFixedWidth(28)
        browse.setToolTip("Browse for a file")
        browse.clicked.connect(self._browse)
        row.addWidget(self._edit)
        row.addWidget(browse)

    def _browse(self) -> None:
        current = self._edit.text().strip()
        start_dir = ""
        if current:
            p = Path(current)
            start_dir = str(p.parent if p.parent.exists() else p)
        path, _ = QFileDialog.getOpenFileName(
            self,
            self._title,
            start_dir,
            self._file_filter,
        )
        if path:
            self._edit.setText(path)
            self.changed.emit()

    def get_value(self) -> Optional[str]:
        text = self._edit.text().strip()
        return text or None


class _ColorPickerWidget(QWidget):
    """Simple color picker button that stores a #RRGGBB value."""

    changed = Signal()

    def __init__(
        self,
        value: Optional[str],
        *,
        title: str,
        default_color: str = "#FFFFFF",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._default_color = default_color
        self._color = str(value or default_color)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._btn = QPushButton()
        self._btn.setMinimumWidth(100)
        self._btn.setMaximumWidth(170)
        self._btn.clicked.connect(self._pick)
        row.addWidget(self._btn)
        row.addStretch()
        self._refresh()

    def _refresh(self) -> None:
        c = QColor(self._color)
        if not c.isValid():
            self._color = self._default_color
            c = QColor(self._color)
        self._btn.setText(self._color.upper())
        text_col = "#000000" if c.lightness() > 140 else "#FFFFFF"
        self._btn.setStyleSheet(
            f"background-color:{self._color}; color:{text_col};"
            " border:1px solid #888; border-radius:3px;"
        )

    def _pick(self) -> None:
        initial = QColor(self._color)
        dlg = QColorDialog(initial, self)
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        dlg.setWindowTitle(self._title)
        if dlg.exec():
            color = dlg.selectedColor()
            if color.isValid():
                self._color = color.name(QColor.NameFormat.HexRgb)
                self._refresh()
                self.changed.emit()

    def get_value(self) -> str:
        return str(self._color or self._default_color)


class _FloorPanelSection(QGroupBox):
    """Collapsible config sub-section for floor panel customisation.

    Shown/hidden by the parent form based on the "Floor panels" toggle.
    Emits ``changed`` when any of its controls changes so the parent can
    persist the values immediately.

    Controls (top → bottom):
        Layout combobox  (auto / chevron_strip)
        BG color picker + Clear button
        Tile color picker
        Blink checkbox
        Tile image chooser
        ── Chevron sub-group (enabled only when layout='chevron_strip') ──
        Chevron color picker
        Chevron blink checkbox
        Chevron scroll checkbox
        Chevron width fraction spinbox
        Chevron count spinbox
    """

    changed = Signal()

    _LAYOUTS = [
        ("Auto (mode default)", "auto"),
        ("Chevron strip (>>>)", "chevron_strip"),
    ]

    def __init__(
        self,
        color: str | None,
        blink: bool,
        image: str | None,
        floor_panel_opacity: float = 1.0,
        floor_layout: str = "auto",
        floor_bg_color: str | None = None,
        floor_bg_opacity: float = 1.0,
        chevron_color: str = "#FFD700",
        chevron_scroll: bool = True,
        chevron_blink: bool = False,
        chevron_width_frac: float = 0.45,
        chevron_count: int = 6,
        full_static_image: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__("Floor Panel Options", parent)
        form = QFormLayout(self)
        self._form = form
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # ---- Layout combobox ----
        self._layout_cb = QComboBox()
        for label, val in self._LAYOUTS:
            self._layout_cb.addItem(label, val)
        lidx = next((i for i, (_, v) in enumerate(self._LAYOUTS)
                     if v == floor_layout), 0)
        self._layout_cb.setCurrentIndex(lidx)
        _no_scroll(self._layout_cb)
        self._layout_cb.currentIndexChanged.connect(self._on_layout_changed)
        form.addRow("Layout", self._layout_cb)

        # ---- BG color picker + Clear ----
        bg_row = QWidget()
        bg_layout = QHBoxLayout(bg_row)
        bg_layout.setContentsMargins(0, 0, 0, 0)
        bg_layout.setSpacing(4)
        self._bg_color: str | None = floor_bg_color or None
        self._bg_btn = QPushButton()
        self._bg_btn.setMinimumWidth(90)
        self._bg_btn.setMaximumWidth(160)
        self._bg_btn.setToolTip("Background color for the whole runway trapezoid")
        self._refresh_bg_btn()
        self._bg_btn.clicked.connect(self._pick_bg_color)
        self._bg_clear_btn = QPushButton("✕")
        self._bg_clear_btn.setFixedWidth(26)
        self._bg_clear_btn.setToolTip("Remove background color (transparent)")
        self._bg_clear_btn.clicked.connect(self._clear_bg_color)
        bg_layout.addWidget(self._bg_btn)
        bg_layout.addWidget(self._bg_clear_btn)
        bg_layout.addStretch()
        form.addRow("BG color", bg_row)

        # ---- BG opacity (numeric input, no slider) ----
        self._bg_opacity_sp = QDoubleSpinBox()
        self._bg_opacity_sp.setRange(0.0, 1.0)
        self._bg_opacity_sp.setSingleStep(0.05)
        self._bg_opacity_sp.setDecimals(2)
        self._bg_opacity_sp.setValue(float(floor_bg_opacity))
        self._bg_opacity_sp.setToolTip("Background opacity: 0 = invisible, 1 = fully opaque")
        _no_scroll(self._bg_opacity_sp)
        self._bg_opacity_sp.valueChanged.connect(self.changed)
        form.addRow("BG opacity", self._bg_opacity_sp)

        # ---- Tile color picker + opacity on same row ----
        color_row = QWidget()
        color_layout = QHBoxLayout(color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(6)
        self._color_btn = QPushButton()
        self._color_btn.setMinimumWidth(90)
        self._color_btn.setMaximumWidth(160)
        self._color_btn.setToolTip("Click to choose the neon color for floor tiles")
        self._color: str | None = color
        self._refresh_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        color_layout.addWidget(self._color_btn)
        self._tile_opacity_lbl = QLabel("Opacity")
        self._tile_opacity_sp = QDoubleSpinBox()
        self._tile_opacity_sp.setRange(0.0, 1.0)
        self._tile_opacity_sp.setSingleStep(0.05)
        self._tile_opacity_sp.setDecimals(2)
        self._tile_opacity_sp.setValue(float(floor_panel_opacity))
        self._tile_opacity_sp.setToolTip("Tile opacity: 0 = invisible, 1 = fully opaque")
        self._tile_opacity_sp.setFixedWidth(72)
        _no_scroll(self._tile_opacity_sp)
        self._tile_opacity_sp.valueChanged.connect(self.changed)
        color_layout.addWidget(self._tile_opacity_lbl)
        color_layout.addWidget(self._tile_opacity_sp)
        form.addRow("Tile color", color_row)
        self._tile_color_row = color_row

        # ---- Blink checkbox ----
        blink_row = QWidget()
        blink_layout = QHBoxLayout(blink_row)
        blink_layout.setContentsMargins(0, 0, 0, 0)
        self._blink_cb = QCheckBox()
        self._blink_cb.setChecked(bool(blink))
        self._blink_cb.setToolTip("Flash tiles on/off every half-second")
        self._blink_cb.stateChanged.connect(self.changed)
        blink_layout.addWidget(self._blink_cb)
        blink_layout.addStretch()
        form.addRow("Blink to beat", blink_row)

        # ---- Image file chooser ----
        self._img_row = QWidget()
        img_layout = QHBoxLayout(self._img_row)
        img_layout.setContentsMargins(0, 0, 0, 0)
        img_layout.setSpacing(4)
        self._img_edit = QLineEdit(image or "")
        self._img_edit.setPlaceholderText("Image file (optional)…")
        self._img_edit.setToolTip(
            "Image to warp onto floor tiles instead of flat fill.\n"
            "Leave blank to use the default shape fill."
        )
        self._img_edit.editingFinished.connect(self._on_img_edit_changed)
        img_browse = QPushButton("…")
        img_browse.setFixedWidth(28)
        img_browse.setToolTip("Browse for an image file")
        img_browse.clicked.connect(self._browse_image)
        img_layout.addWidget(self._img_edit)
        img_layout.addWidget(img_browse)
        form.addRow("Tile image", self._img_row)

        # ── Full static image checkbox (visible only with image set) ───
        full_row = QWidget()
        full_layout = QHBoxLayout(full_row)
        full_layout.setContentsMargins(0, 0, 0, 0)
        self._full_static_cb = QCheckBox()
        self._full_static_cb.setChecked(bool(full_static_image))
        self._full_static_cb.setToolTip(
            "Stretch the tile image across the WHOLE floor as a single static\n"
            "graphic. All other floor effects (chevron, tiles, BG color, blink,\n"
            "opacity) are bypassed. Visible only when a tile image is selected."
        )
        self._full_static_cb.stateChanged.connect(self._on_full_static_changed)
        full_layout.addWidget(self._full_static_cb)
        full_layout.addStretch()
        form.addRow("Full static image", full_row)
        self._full_static_row = full_row

        # ── Chevron sub-group separator ────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        form.addRow(sep)
        self._chevron_header = QLabel("<b>Chevron options</b>")
        form.addRow(self._chevron_header)

        # ---- Chevron color picker ----
        chev_color_row = QWidget()
        chcl = QHBoxLayout(chev_color_row)
        chcl.setContentsMargins(0, 0, 0, 0)
        self._chev_color: str = chevron_color or "#FFD700"
        self._chev_color_btn = QPushButton()
        self._chev_color_btn.setMinimumWidth(90)
        self._chev_color_btn.setMaximumWidth(160)
        self._chev_color_btn.setToolTip("Arrow fill color")
        self._refresh_chev_color_btn()
        self._chev_color_btn.clicked.connect(self._pick_chev_color)
        chcl.addWidget(self._chev_color_btn)
        chcl.addStretch()
        self._chev_color_label = form.addRow("Color", chev_color_row)

        # ---- Chevron scroll checkbox ----
        cs_row = QWidget()
        csl = QHBoxLayout(cs_row)
        csl.setContentsMargins(0, 0, 0, 0)
        self._chev_scroll_cb = QCheckBox()
        self._chev_scroll_cb.setChecked(bool(chevron_scroll))
        self._chev_scroll_cb.setToolTip("Scroll arrows toward the camera continuously")
        self._chev_scroll_cb.stateChanged.connect(self.changed)
        csl.addWidget(self._chev_scroll_cb)
        csl.addStretch()
        form.addRow("Scroll", cs_row)

        # ---- Chevron blink checkbox ----
        cb_row = QWidget()
        cbl = QHBoxLayout(cb_row)
        cbl.setContentsMargins(0, 0, 0, 0)
        self._chev_blink_cb = QCheckBox()
        self._chev_blink_cb.setChecked(bool(chevron_blink))
        self._chev_blink_cb.setToolTip("Blink chevrons on/off every ~0.5 s")
        self._chev_blink_cb.stateChanged.connect(self.changed)
        cbl.addWidget(self._chev_blink_cb)
        cbl.addStretch()
        form.addRow("Blink", cb_row)

        # ---- Chevron width fraction ----
        self._chev_width_sp = QDoubleSpinBox()
        self._chev_width_sp.setRange(0.10, 2.0)
        self._chev_width_sp.setSingleStep(0.05)
        self._chev_width_sp.setDecimals(2)
        self._chev_width_sp.setValue(float(chevron_width_frac))
        self._chev_width_sp.setToolTip("Strip width as fraction of lane spread (0.1 – 2.0)")
        _no_scroll(self._chev_width_sp)
        self._chev_width_sp.valueChanged.connect(self.changed)
        form.addRow("Width frac", self._chev_width_sp)

        # ---- Chevron count ----
        self._chev_count_sp = QSpinBox()
        self._chev_count_sp.setRange(3, 12)
        self._chev_count_sp.setValue(int(chevron_count))
        self._chev_count_sp.setToolTip("Number of arrows visible simultaneously (3 – 12)")
        _no_scroll(self._chev_count_sp)
        self._chev_count_sp.valueChanged.connect(self.changed)
        form.addRow("Count", self._chev_count_sp)

        # Keep references to all chevron widgets for enable/disable
        self._chevron_widgets = [
            chev_color_row, self._chev_color_btn,
            cs_row, self._chev_scroll_cb,
            cb_row, self._chev_blink_cb,
            self._chev_width_sp, self._chev_count_sp,
            sep, self._chevron_header,
        ]
        # Rows that disappear when "Full static image" is enabled.
        # (Layout / BG / tiles / blink / chevron all become irrelevant — the
        # whole floor becomes a single stretched image.)
        self._effect_rows = [
            self._layout_cb, bg_row, self._bg_opacity_sp,
            color_row, blink_row, sep, self._chevron_header,
            chev_color_row, cs_row, cb_row,
            self._chev_width_sp, self._chev_count_sp,
        ]
        self._update_chevron_visibility()
        self._update_full_static_visibility()

    # ------------------------------------------------------------------
    def _on_layout_changed(self) -> None:
        self._update_chevron_visibility()
        self.changed.emit()

    def _update_chevron_visibility(self) -> None:
        enabled = (self._layout_cb.currentData() == "chevron_strip")
        for w in self._chevron_widgets:
            w.setEnabled(enabled)
        # In auto layout, chevron controls are not needed -> hide the whole block.
        for w in self._chevron_widgets:
            self._set_floor_row_visible(w, enabled)
        # In chevron layout, tile color/opacity are irrelevant (legacy tiles only).
        self._set_floor_row_visible(self._tile_color_row, not enabled)

    # ------------------------------------------------------------------
    def _refresh_bg_btn(self) -> None:
        if self._bg_color:
            self._bg_btn.setText(self._bg_color)
            self._bg_btn.setStyleSheet(
                f"background-color:{self._bg_color}; color: #fff;"
                f" border:1px solid #888; border-radius:3px;"
            )
        else:
            self._bg_btn.setText("None")
            self._bg_btn.setStyleSheet("")

    def _pick_bg_color(self) -> None:
        initial = QColor(self._bg_color) if self._bg_color else QColor(90, 26, 140)
        dlg = QColorDialog(initial, self)
        # Native dialog on some Windows setups occasionally reports stale/invalid
        # selected color; force Qt dialog for deterministic behavior.
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        dlg.setWindowTitle("Runway background color")
        if dlg.exec():
            color = dlg.selectedColor()
            if color.isValid():
                self._bg_color = color.name(QColor.NameFormat.HexRgb)
                self._refresh_bg_btn()
                self.changed.emit()

    def _clear_bg_color(self) -> None:
        self._bg_color = None
        self._refresh_bg_btn()
        self.changed.emit()

    # ------------------------------------------------------------------
    def _refresh_color_btn(self) -> None:
        if self._color:
            self._color_btn.setText(self._color)
            self._color_btn.setStyleSheet(
                f"background-color:{self._color}; color: #fff;"
                f" border:1px solid #888; border-radius:3px;"
            )
        else:
            self._color_btn.setText("Default")
            self._color_btn.setStyleSheet("")

    def _pick_color(self) -> None:
        initial = QColor(self._color) if self._color else QColor(170, 175, 180)
        color = QColorDialog.getColor(initial, self, "Floor tile color")
        if color.isValid():
            self._color = color.name()
            self._refresh_color_btn()
            self.changed.emit()

    def _browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select tile image",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tga *.webp);;All files (*.*)",
        )
        if path:
            self._img_edit.setText(path)
            self._update_full_static_visibility()
            self.changed.emit()

    def _on_img_edit_changed(self) -> None:
        self._update_full_static_visibility()
        self.changed.emit()

    def _on_full_static_changed(self) -> None:
        self._apply_full_static_state()
        self.changed.emit()

    def _has_image(self) -> bool:
        return bool(self._img_edit.text().strip())

    def _update_full_static_visibility(self) -> None:
        """Show the 'Full static image' row only when a tile image is set."""
        has_img = self._has_image()
        self._set_floor_row_visible(self._full_static_row, has_img)
        # If the image was just cleared, also clear the checkbox state so the
        # value persisted to render_settings reflects what's currently usable.
        if not has_img and self._full_static_cb.isChecked():
            self._full_static_cb.blockSignals(True)
            self._full_static_cb.setChecked(False)
            self._full_static_cb.blockSignals(False)
        self._apply_full_static_state()

    def _apply_full_static_state(self) -> None:
        """Hide the rest of the floor effect rows when full-static is on."""
        full = (self._full_static_cb.isChecked() and self._has_image())
        for w in self._effect_rows:
            self._set_floor_row_visible(w, not full)
        # Image row stays visible regardless (user must see/edit the path).
        # When full-static is OFF, restore the layout-driven chevron visibility.
        if not full:
            self._update_chevron_visibility()

    def _set_floor_row_visible(self, widget: QWidget, visible: bool) -> None:
        """Safely toggle a floor row without Qt invalid-widget warnings."""
        try:
            if self._form.indexOf(widget) >= 0:
                self._form.setRowVisible(widget, visible)
            else:
                widget.setVisible(visible)
        except Exception:
            widget.setVisible(visible)

    # ------------------------------------------------------------------
    def _refresh_chev_color_btn(self) -> None:
        self._chev_color_btn.setText(self._chev_color)
        self._chev_color_btn.setStyleSheet(
            f"background-color:{self._chev_color}; color: #000;"
            f" border:1px solid #888; border-radius:3px;"
        )

    def _pick_chev_color(self) -> None:
        initial = QColor(self._chev_color)
        color = QColorDialog.getColor(initial, self, "Chevron arrow color")
        if color.isValid():
            self._chev_color = color.name()
            self._refresh_chev_color_btn()
            self.changed.emit()

    # ------------------------------------------------------------------
    def get_color(self) -> str | None:
        return self._color or None

    def get_blink(self) -> bool:
        return self._blink_cb.isChecked()

    def get_image(self) -> str | None:
        t = self._img_edit.text().strip()
        return t or None

    def get_floor_layout(self) -> str:
        return self._layout_cb.currentData() or "auto"

    def get_floor_bg_color(self) -> str | None:
        return self._bg_color or None

    def get_floor_bg_opacity(self) -> float:
        return self._bg_opacity_sp.value()

    def get_floor_panel_opacity(self) -> float:
        return self._tile_opacity_sp.value()

    def get_chevron_color(self) -> str:
        return self._chev_color

    def get_chevron_scroll(self) -> bool:
        return self._chev_scroll_cb.isChecked()

    def get_chevron_blink(self) -> bool:
        return self._chev_blink_cb.isChecked()

    def get_chevron_width_frac(self) -> float:
        return self._chev_width_sp.value()

    def get_chevron_count(self) -> int:
        return self._chev_count_sp.value()

    def get_full_static_image(self) -> bool:
        return bool(self._full_static_cb.isChecked()) and self._has_image()


class _SideRailSection(QGroupBox):
    """Config sub-section for side-rail settings.

    Shown/hidden by the parent form based on the "Side rails" toggle.
    """

    changed = Signal()

    _SHAPES = [("Chunky (fence blocks)", "chunky"),
               ("Tube (strip)", "tube"),
               ("Chevron (arrows)", "chevron"),
               ("Pillar (LED chase)", "pillar"),
               ("Dot (glowing dots)", "dot")]
    _PULSES = [("None (static)", "none"),
               ("Beat (flash on hit)", "beat"),
               ("RMS (breathe with bass)", "rms")]

    def __init__(
        self,
        color: str,
        shape: str,
        height: float,
        offset_x: float,
        image: str | None,
        pulse: str,
        pulse_intensity: float,
        texture_non_loop: bool = False,
        chevron_depth: float = 1.0,
        chevron_density: int = 6,
        pillar_count: int = 16,
        pillar_radius: float = 1.0,
        chase_mode: str = "time",
        chase_speed_frames: int = 4,
        dot_count: int = 24,
        dot_lines: int = 1,
        dot_size_px: int = 6,
        dot_anim_mode: str = "audio",
        dot_color_near: str = "#FF60FF",
        dot_color_far: str = "#00FFFF",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__("Side Rail Options", parent)
        form = QFormLayout(self)
        self._form = form
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # ---- Color ----
        color_row = QWidget()
        cl = QHBoxLayout(color_row)
        cl.setContentsMargins(0, 0, 0, 0)
        self._color: str = color or "#FF60FF"
        self._color_btn = QPushButton()
        self._color_btn.setMinimumWidth(90)
        self._color_btn.setMaximumWidth(160)
        self._color_btn.setToolTip("Neon color for side rails")
        self._refresh_color_btn()
        self._color_btn.clicked.connect(self._pick_color)
        cl.addWidget(self._color_btn)
        cl.addStretch()
        form.addRow("Color", color_row)

        # ---- Shape ----
        self._shape_cb = QComboBox()
        for label, val in self._SHAPES:
            self._shape_cb.addItem(label, val)
        idx = next((i for i, (_, v) in enumerate(self._SHAPES) if v == shape), 0)
        self._shape_cb.setCurrentIndex(idx)
        _no_scroll(self._shape_cb)
        self._shape_cb.currentIndexChanged.connect(self.changed)
        form.addRow("Shape", self._shape_cb)

        # ---- Height ----
        self._height_sp = QDoubleSpinBox()
        self._height_sp.setRange(0.01, 1.0)
        self._height_sp.setSingleStep(0.01)
        self._height_sp.setDecimals(2)
        self._height_sp.setValue(float(height))
        self._height_sp.setToolTip("Fence height above floor (world units, e.g. 0.12)")
        _no_scroll(self._height_sp)
        self._height_sp.valueChanged.connect(self.changed)
        form.addRow("Height", self._height_sp)

        # ---- Offset X ----
        self._offset_sp = QDoubleSpinBox()
        self._offset_sp.setRange(0.0, 1.0)
        self._offset_sp.setSingleStep(0.01)
        self._offset_sp.setDecimals(2)
        self._offset_sp.setValue(float(offset_x))
        self._offset_sp.setToolTip("Gap from outer lane tile to fence face (0 = flush with lane edge)")
        _no_scroll(self._offset_sp)
        self._offset_sp.valueChanged.connect(self.changed)
        form.addRow("Offset X", self._offset_sp)

        # ---- Image file ----
        self._img_edit = QLineEdit(image or "")
        self._img_edit.setPlaceholderText("Texture image (optional)…")
        self._img_edit.setToolTip(
            "Optional PNG/JPG to texture the rail blocks.\n"
            "Leave blank to use the solid neon color."
        )
        self._img_edit.editingFinished.connect(self._on_texture_edited)
        img_browse = QPushButton("…")
        img_browse.setFixedWidth(28)
        img_browse.clicked.connect(self._browse_image)
        img_row = QWidget()
        ir = QHBoxLayout(img_row)
        ir.setContentsMargins(0, 0, 0, 0)
        ir.setSpacing(4)
        ir.addWidget(self._img_edit)
        ir.addWidget(img_browse)
        form.addRow("Texture", img_row)
        self._texture_row = img_row

        # Tube-only texture option
        non_loop_row = QWidget()
        nl = QHBoxLayout(non_loop_row)
        nl.setContentsMargins(0, 0, 0, 0)
        self._texture_non_loop_cb = QCheckBox()
        self._texture_non_loop_cb.setChecked(bool(texture_non_loop))
        self._texture_non_loop_cb.setToolTip(
            "Tube mode only. ON = map texture once over full rail length;\n"
            "OFF = tile/loop texture per segment."
        )
        self._texture_non_loop_cb.stateChanged.connect(self.changed)
        nl.addWidget(self._texture_non_loop_cb)
        nl.addStretch()
        form.addRow("Non-loop", non_loop_row)
        self._texture_non_loop_row = non_loop_row

        # ---- Pulse mode ----
        self._pulse_cb = QComboBox()
        for label, val in self._PULSES:
            self._pulse_cb.addItem(label, val)
        pidx = next((i for i, (_, v) in enumerate(self._PULSES) if v == pulse), 1)
        self._pulse_cb.setCurrentIndex(pidx)
        _no_scroll(self._pulse_cb)
        self._pulse_cb.currentIndexChanged.connect(self.changed)
        form.addRow("Pulse", self._pulse_cb)

        # ---- Pulse intensity ----
        self._intensity_sp = QDoubleSpinBox()
        self._intensity_sp.setRange(0.0, 1.0)
        self._intensity_sp.setSingleStep(0.05)
        self._intensity_sp.setDecimals(2)
        self._intensity_sp.setValue(float(pulse_intensity))
        self._intensity_sp.setToolTip("Pulse intensity 0 = static, 1 = full flash")
        _no_scroll(self._intensity_sp)
        self._intensity_sp.valueChanged.connect(self.changed)
        form.addRow("Intensity", self._intensity_sp)

        # ── Chevron-only sub-group ─────────────────────────────────────
        self._chev_sep = QFrame()
        self._chev_sep.setFrameShape(QFrame.Shape.HLine)
        self._chev_sep.setFrameShadow(QFrame.Shadow.Sunken)
        form.addRow(self._chev_sep)
        self._chev_header = QLabel("<b>Chevron options</b>")
        form.addRow(self._chev_header)

        # ---- Depth (pointedness) — slider 0.2 … 5.0 step 0.1 ----
        depth_row = QWidget()
        dl = QHBoxLayout(depth_row)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(6)
        self._chev_depth_sl = QSlider(Qt.Orientation.Horizontal)
        self._chev_depth_sl.setRange(2, 50)   # ×10 of real value
        self._chev_depth_sl.setSingleStep(1)
        self._chev_depth_sl.setPageStep(5)
        self._chev_depth_sl.setValue(max(2, min(50, round(float(chevron_depth) * 10))))
        self._chev_depth_sl.setToolTip(
            "Chevron pointedness multiplier.\n"
            "1.0 = 120° opening angle.  >1 = sharper.  <1 = flatter."
        )
        self._chev_depth_lbl = QLabel(f"{float(chevron_depth):.1f}×")
        self._chev_depth_lbl.setMinimumWidth(36)
        self._chev_depth_sl.valueChanged.connect(self._on_chev_depth_changed)
        dl.addWidget(self._chev_depth_sl, stretch=1)
        dl.addWidget(self._chev_depth_lbl)
        form.addRow("Depth", depth_row)

        # ---- Density (count) — slider 6 … 20 ----
        density_row = QWidget()
        dsl = QHBoxLayout(density_row)
        dsl.setContentsMargins(0, 0, 0, 0)
        dsl.setSpacing(6)
        self._chev_density_sl = QSlider(Qt.Orientation.Horizontal)
        self._chev_density_sl.setRange(6, 20)
        self._chev_density_sl.setSingleStep(1)
        self._chev_density_sl.setValue(max(6, min(20, int(chevron_density))))
        self._chev_density_sl.setToolTip("Number of chevrons visible on each wall (6 – 20)")
        self._chev_density_lbl = QLabel(str(self._chev_density_sl.value()))
        self._chev_density_lbl.setMinimumWidth(26)
        self._chev_density_sl.valueChanged.connect(self._on_chev_density_changed)
        dsl.addWidget(self._chev_density_sl, stretch=1)
        dsl.addWidget(self._chev_density_lbl)
        form.addRow("Density", density_row)

        self._chev_widgets = [
            self._chev_sep, self._chev_header,
            depth_row, density_row,
        ]

        # ── Pillar-only sub-group ──────────────────────────────────────
        self._pillar_sep = QFrame()
        self._pillar_sep.setFrameShape(QFrame.Shape.HLine)
        self._pillar_sep.setFrameShadow(QFrame.Shadow.Sunken)
        form.addRow(self._pillar_sep)
        self._pillar_header = QLabel("<b>Pillar options</b>")
        form.addRow(self._pillar_header)

        self._pillar_count_sp = QSpinBox()
        self._pillar_count_sp.setRange(4, 32)
        self._pillar_count_sp.setValue(int(pillar_count))
        self._pillar_count_sp.setToolTip("Number of pillars (4..32). More = denser LED row.")
        _no_scroll(self._pillar_count_sp)
        self._pillar_count_sp.valueChanged.connect(self.changed)
        form.addRow("Count", self._pillar_count_sp)

        self._pillar_radius_sp = QDoubleSpinBox()
        self._pillar_radius_sp.setRange(0.2, 2.0)
        self._pillar_radius_sp.setSingleStep(0.05)
        self._pillar_radius_sp.setDecimals(2)
        self._pillar_radius_sp.setValue(float(pillar_radius))
        self._pillar_radius_sp.setToolTip(
            "Pillar circumference scale. <1 = thinner columns, >1 = thicker."
        )
        _no_scroll(self._pillar_radius_sp)
        self._pillar_radius_sp.valueChanged.connect(self.changed)
        form.addRow("Radius", self._pillar_radius_sp)

        self._chase_mode_cb = QComboBox()
        self._chase_mode_cb.addItems(["time", "beat"])
        self._chase_mode_cb.setCurrentText(str(chase_mode))
        self._chase_mode_cb.setToolTip(
            "time = constant frame interval; beat = advance on each beat hit"
        )
        _no_scroll(self._chase_mode_cb)
        self._chase_mode_cb.currentTextChanged.connect(self.changed)
        form.addRow("Chase mode", self._chase_mode_cb)

        self._chase_speed_sp = QSpinBox()
        self._chase_speed_sp.setRange(1, 60)
        self._chase_speed_sp.setValue(int(chase_speed_frames))
        self._chase_speed_sp.setToolTip(
            "Frames per chase advance (only for time mode). 4 ≈ 133ms @30fps."
        )
        _no_scroll(self._chase_speed_sp)
        self._chase_speed_sp.valueChanged.connect(self.changed)
        form.addRow("Chase speed", self._chase_speed_sp)

        self._pillar_widgets = [
            self._pillar_sep, self._pillar_header,
            self._pillar_count_sp, self._pillar_radius_sp,
            self._chase_mode_cb, self._chase_speed_sp,
        ]

        # ── Dot-only sub-group ─────────────────────────────────────────
        self._dot_sep = QFrame()
        self._dot_sep.setFrameShape(QFrame.Shape.HLine)
        self._dot_sep.setFrameShadow(QFrame.Shadow.Sunken)
        form.addRow(self._dot_sep)
        self._dot_header = QLabel("<b>Dot options</b>")
        form.addRow(self._dot_header)

        self._dot_count_sp = QSpinBox()
        self._dot_count_sp.setRange(8, 64)
        self._dot_count_sp.setValue(int(dot_count))
        self._dot_count_sp.setToolTip("Number of dots per rail (8..64)")
        _no_scroll(self._dot_count_sp)
        self._dot_count_sp.valueChanged.connect(self.changed)
        form.addRow("Count", self._dot_count_sp)

        self._dot_lines_sp = QSpinBox()
        self._dot_lines_sp.setRange(1, 8)
        self._dot_lines_sp.setValue(int(dot_lines))
        self._dot_lines_sp.setToolTip("Number of vertical dot lines on wall (top -> bottom) (1..8)")
        _no_scroll(self._dot_lines_sp)
        self._dot_lines_sp.valueChanged.connect(self.changed)
        form.addRow("Lines (vertical)", self._dot_lines_sp)

        self._dot_size_sp = QSpinBox()
        self._dot_size_sp.setRange(2, 20)
        self._dot_size_sp.setValue(int(dot_size_px))
        self._dot_size_sp.setToolTip("Base dot radius in pixels at Z_NEAR")
        _no_scroll(self._dot_size_sp)
        self._dot_size_sp.valueChanged.connect(self.changed)
        form.addRow("Size (px)", self._dot_size_sp)

        self._dot_anim_cb = QComboBox()
        self._dot_anim_cb.addItems(["audio", "twinkle", "wave"])
        self._dot_anim_cb.setCurrentText(str(dot_anim_mode))
        self._dot_anim_cb.setToolTip(
            "audio = brightness from bass; twinkle = random fade; wave = sin wave"
        )
        _no_scroll(self._dot_anim_cb)
        self._dot_anim_cb.currentTextChanged.connect(self.changed)
        form.addRow("Anim mode", self._dot_anim_cb)

        self._dot_color_near = str(dot_color_near or "#FF60FF")
        self._dot_near_btn = QPushButton(self._dot_color_near)
        self._refresh_dot_color_btn(self._dot_near_btn, self._dot_color_near)
        self._dot_near_btn.clicked.connect(self._pick_dot_color_near)
        form.addRow("Near color", self._dot_near_btn)

        self._dot_color_far = str(dot_color_far or "#00FFFF")
        self._dot_far_btn = QPushButton(self._dot_color_far)
        self._refresh_dot_color_btn(self._dot_far_btn, self._dot_color_far)
        self._dot_far_btn.clicked.connect(self._pick_dot_color_far)
        form.addRow("Far color", self._dot_far_btn)

        self._dot_widgets = [
            self._dot_sep, self._dot_header,
            self._dot_count_sp, self._dot_lines_sp, self._dot_size_sp, self._dot_anim_cb,
            self._dot_near_btn, self._dot_far_btn,
        ]
        # Wire shape change to show/hide chevron sub-group
        self._shape_cb.currentIndexChanged.connect(self._on_shape_changed)
        self._update_chev_visibility()
        self._update_pillar_visibility()
        self._update_dot_visibility()
        self._update_texture_visibility()
        self._update_texture_tube_options_visibility()

        # 2-second debounce for continuous slider input — label updates
        # instantly but `changed` is emitted only after the user stops
        # dragging for ≥2 s to avoid rebuilding the scene on every tick.
        self._slider_debounce = QTimer(self)
        self._slider_debounce.setSingleShot(True)
        self._slider_debounce.setInterval(2000)
        self._slider_debounce.timeout.connect(self.changed)

    # ------------------------------------------------------------------
    def _on_shape_changed(self) -> None:
        self._update_chev_visibility()
        self._update_pillar_visibility()
        self._update_dot_visibility()
        self._update_texture_visibility()
        self._update_texture_tube_options_visibility()
        self.changed.emit()

    def _on_chev_depth_changed(self, raw: int) -> None:
        self._chev_depth_lbl.setText(f"{raw / 10:.1f}×")
        self._slider_debounce.start()   # restart 2-s window

    def _on_chev_density_changed(self, val: int) -> None:
        self._chev_density_lbl.setText(str(val))
        self._slider_debounce.start()   # restart 2-s window

    def _update_chev_visibility(self) -> None:
        is_chev = (self._shape_cb.currentData() == "chevron")
        for w in self._chev_widgets:
            self._set_side_row_visible(w, is_chev)

    def _update_pillar_visibility(self) -> None:
        is_pillar = (self._shape_cb.currentData() == "pillar")
        for w in self._pillar_widgets:
            self._set_side_row_visible(w, is_pillar)

    def _update_dot_visibility(self) -> None:
        is_dot = (self._shape_cb.currentData() == "dot")
        for w in self._dot_widgets:
            self._set_side_row_visible(w, is_dot)

    def _set_side_row_visible(self, widget: QWidget, visible: bool) -> None:
        """Hide/show a full form row (label + field) when possible."""
        # During panel rebuilds, stale signals can hit widgets that are no
        # longer attached to this form. Guard to avoid Qt warning:
        # "QFormLayout::setRowVisible: Invalid widget".
        try:
            if self._form.indexOf(widget) >= 0:
                self._form.setRowVisible(widget, visible)
            else:
                widget.setVisible(visible)
        except Exception:
            widget.setVisible(visible)

    def _update_texture_visibility(self) -> None:
        shape = self._shape_cb.currentData()
        show_texture = shape not in ("chevron", "pillar", "dot")
        self._set_side_row_visible(self._texture_row, show_texture)

    def _update_texture_tube_options_visibility(self) -> None:
        is_tube = (self._shape_cb.currentData() == "tube")
        has_tex = bool(self._img_edit.text().strip())
        self._set_side_row_visible(self._texture_non_loop_row, is_tube and has_tex)

    @staticmethod
    def _refresh_dot_color_btn(btn: QPushButton, hex_color: str) -> None:
        btn.setText(hex_color)
        btn.setStyleSheet(
            f"background-color:{hex_color}; color:#fff;"
            " border:1px solid #888; border-radius:3px;"
        )

    # ------------------------------------------------------------------
    def _refresh_color_btn(self) -> None:
        self._color_btn.setText(self._color)
        self._color_btn.setStyleSheet(
            f"background-color:{self._color}; color: #fff;"
            f" border:1px solid #888; border-radius:3px;"
        )

    def _pick_color(self) -> None:
        initial = QColor(self._color)
        color = QColorDialog.getColor(initial, self, "Rail neon color")
        if color.isValid():
            self._color = color.name()
            self._refresh_color_btn()
            self.changed.emit()

    def _browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select rail texture", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tga *.webp);;All files (*.*)",
        )
        if path:
            self._img_edit.setText(path)
            self._update_texture_tube_options_visibility()
            self.changed.emit()

    def _on_texture_edited(self) -> None:
        self._update_texture_tube_options_visibility()
        self.changed.emit()

    def _pick_dot_color_near(self) -> None:
        self._pick_dot_color_for("near")

    def _pick_dot_color_far(self) -> None:
        self._pick_dot_color_for("far")

    def _pick_dot_color_for(self, side: str) -> None:
        current = self._dot_color_near if side == "near" else self._dot_color_far
        dlg = QColorDialog(QColor(current), self)
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        if dlg.exec():
            col = dlg.selectedColor()
            if col.isValid():
                hex_str = col.name(QColor.NameFormat.HexRgb).upper()
                if side == "near":
                    self._dot_color_near = hex_str
                    self._refresh_dot_color_btn(self._dot_near_btn, hex_str)
                else:
                    self._dot_color_far = hex_str
                    self._refresh_dot_color_btn(self._dot_far_btn, hex_str)
                self.changed.emit()

    # ------------------------------------------------------------------
    def get_color(self) -> str:
        return self._color

    def get_shape(self) -> str:
        return self._shape_cb.currentData() or "chunky"

    def get_height(self) -> float:
        return self._height_sp.value()

    def get_offset_x(self) -> float:
        return self._offset_sp.value()

    def get_image(self) -> str | None:
        t = self._img_edit.text().strip()
        return t or None

    def get_texture_non_loop(self) -> bool:
        return (
            bool(self._texture_non_loop_cb.isChecked())
            and (self.get_shape() == "tube")
            and bool(self.get_image())
        )

    def get_pulse(self) -> str:
        return self._pulse_cb.currentData() or "beat"

    def get_pulse_intensity(self) -> float:
        return self._intensity_sp.value()

    def get_chevron_depth(self) -> float:
        return self._chev_depth_sl.value() / 10.0

    def get_chevron_density(self) -> int:
        return self._chev_density_sl.value()

    def get_pillar_count(self) -> int:
        return self._pillar_count_sp.value()

    def get_pillar_radius(self) -> float:
        return self._pillar_radius_sp.value()

    def get_chase_mode(self) -> str:
        return self._chase_mode_cb.currentText()

    def get_chase_speed_frames(self) -> int:
        return self._chase_speed_sp.value()

    def get_dot_count(self) -> int:
        return self._dot_count_sp.value()

    def get_dot_lines(self) -> int:
        return self._dot_lines_sp.value()

    def get_dot_size_px(self) -> int:
        return self._dot_size_sp.value()

    def get_dot_anim_mode(self) -> str:
        return self._dot_anim_cb.currentText()

    def get_dot_color_near(self) -> str:
        return self._dot_color_near

    def get_dot_color_far(self) -> str:
        return self._dot_color_far


def format_sec(value: float) -> str:
    total = max(0, int(value))
    mm, ss = divmod(total, 60)
    return f"{mm:02d}:{ss:02d}"


class SegmentConfigPanel(QWidget):
    """Two-way editable form for selected segment configuration."""

    segment_changed = Signal(str)  # segment_id
    render_requested = Signal(str)   # segment_id
    preview_requested = Signal(str)  # segment_id

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._segment: Optional[Segment] = None
        self._setting_widgets: dict[str, QWidget] = {}
        self._floor_panel_section: Optional[_FloorPanelSection] = None
        self._side_rail_section: Optional[_SideRailSection] = None
        self._build_ui()
        self._set_empty_state(True)

    def set_project(self, project: Project) -> None:
        """Attach project state reference."""
        self._project = project
        self._refresh_audio_options()

    @property
    def current_segment(self) -> Segment | None:
        """Return currently bound segment."""
        return self._segment

    def set_segment(self, segment: Segment | None) -> None:
        """Load currently selected segment into form."""
        self._segment = segment
        self._set_empty_state(segment is None)
        if segment is None:
            return
        self._refresh_audio_options()
        self._load_segment_fields(segment)
        self._rebuild_dynamic_settings()
        self._apply_video_segment_lock_state(segment)

    @staticmethod
    def _is_video_segment_locked(segment: Segment | None) -> bool:
        return bool(segment is not None and getattr(segment, "is_video_segment", False))

    def _apply_video_segment_lock_state(self, segment: Segment) -> None:
        """Lock inspector controls for source-video playback segments."""
        locked = self._is_video_segment_locked(segment)
        self.start_spin.setEnabled(not locked)
        self.end_spin.setEnabled(not locked)
        self.min_spacing_spin.setEnabled(not locked)
        self.mode_combo.setEnabled(not locked)
        self.dynamic_root.setEnabled(not locked)
        self.reset_button.setEnabled(not locked)
        self.preview_button.setChecked(False)
        self.preview_button.setEnabled(False if locked else bool(segment.audio_path))
        self.render_button.setEnabled(False if locked else bool(segment.audio_path))
        if locked:
            msg = (
                "Source-video segment: duration/video are fixed from dropped media "
                "and effect settings are locked."
            )
            self.status_label.setText("Status: source-video segment (locked)")
            self.status_label.setStyleSheet("color:#a78bfa;")
            self.status_label.setToolTip(msg)
            self.preview_button.setToolTip(
                "This segment previews directly from its source video."
            )
            self.render_button.setToolTip(
                "Source-video segments are playback-only and do not render."
            )

    def refresh_status_only(self, segment: Segment) -> None:
        """Update only the status label/header without rebuilding the form.

        Use this during live render progress updates so spinbox widgets the
        user might be editing aren't destroyed and recreated every tick.
        """
        if self._segment is None or self._segment.id != segment.id:
            return
        self.header_label.setText(
            f"{segment.name}  {format_sec(segment.start_time_sec)} -> {format_sec(segment.end_time_sec)}"
        )
        status_value = segment.render_status.value
        if status_value == "rendering":
            pct = max(0, min(100, int(getattr(segment, "last_render_progress", 0))))
            self.status_label.setText(f"Status: rendering {pct}%")
            self.status_label.setStyleSheet("color:#5cc8ff;")
        elif status_value == "queued":
            self.status_label.setText("Status: queued")
            self.status_label.setStyleSheet("color:#5cc8ff;")
        elif status_value == "done":
            self.status_label.setText("Status: done")
            self.status_label.setStyleSheet("color:#7bd88f;")
        elif status_value == "error" and segment.last_render_error:
            err = segment.last_render_error.strip().splitlines()
            # Try to show the actual exception line rather than the last
            # debug-print.  Scan for "Error:", "Exception:", "Traceback"
            # from the end; fall back to the last line if none found.
            short = err[-1] if err else "(no message)"
            for line in reversed(err):
                l = line.strip()
                if l and any(l.startswith(k) for k in ("Error", "Exception", "Traceback")):
                    short = l
                    break
            self.status_label.setText(f"Status: error — {short}")
            self.status_label.setStyleSheet("color:#ff6b6b;")
            self.status_label.setToolTip(segment.last_render_error)
        else:
            self.status_label.setText(f"Status: {status_value}")
            self.status_label.setStyleSheet("color:#8a8a8a;")

    def _build_ui(self) -> None:
        self.setObjectName("PanelRoot")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header strip
        header = QWidget()
        header.setObjectName("panelHeader")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(10, 6, 10, 6)
        title = QLabel("Properties")
        title.setObjectName("panelTitle")
        header_row.addWidget(title)
        header_row.addStretch()

        # Open-folder reveals the rendered MP4 in the OS file explorer
        # with the file pre-selected. Only enabled when the segment has
        # a rendered video on disk.
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.setObjectName("headerButton")
        self.open_folder_button.setToolTip(
            "Open the folder containing the rendered video and select the file"
        )
        self.open_folder_button.clicked.connect(self._on_open_folder_clicked)
        header_row.addWidget(self.open_folder_button)

        # Reset-to-defaults sits in the header so it's always visible without
        # scrolling — handy when tweaking dynamic mode-specific settings.
        self.reset_button = QPushButton("Reset defaults")
        self.reset_button.setObjectName("headerButton")
        self.reset_button.setToolTip(
            "Reset all per-mode settings of the selected segment to defaults"
        )
        self.reset_button.clicked.connect(self._on_reset_clicked)
        header_row.addWidget(self.reset_button)
        outer.addWidget(header)

        # Body
        body = QWidget()
        body.setObjectName("PanelRoot")
        root = QVBoxLayout(body)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        outer.addWidget(body, 1)

        self.header_label = QLabel("No segment selected")
        self.header_label.setObjectName("segmentHeader")
        root.addWidget(self.header_label)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#8a8a8a;")
        root.addWidget(self.status_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        root.addWidget(self.scroll, 1)

        container = QWidget()
        self.scroll.setWidget(container)
        self.form_layout = QFormLayout(container)
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_layout.setVerticalSpacing(8)

        self.name_input = QLineEdit()
        self.name_input.editingFinished.connect(self._commit_general)
        self.form_layout.addRow("Name", self.name_input)

        self.audio_combo = QComboBox()
        # Audio source is bound to the segment at creation/split/join/duplicate
        # time only.  Config edits MUST NEVER mutate ``segment.audio_path`` —
        # otherwise toggling unrelated render settings during preview races
        # with the trim service and corrupts the per-segment trim file.
        # The combo is therefore display-only here.
        self.audio_combo.setEnabled(False)
        self.audio_combo.setToolTip(
            "Audio source is set when the segment is created, split, joined, "
            "or duplicated. To change the source, recreate the segment."
        )
        _no_scroll(self.audio_combo)
        self.form_layout.addRow("Audio source", self.audio_combo)

        # Trimmed audio path — read-only display + "Open" button to reveal
        # the file in the OS file explorer so the user can play/verify it.
        trimmed_row = QHBoxLayout()
        trimmed_row.setSpacing(4)
        self.trimmed_audio_label = QLabel("—")
        self.trimmed_audio_label.setObjectName("trimmedAudioLabel")
        self.trimmed_audio_label.setToolTip(
            "Pre-trimmed WAV for this segment (auto-generated).\n"
            "Stored next to the project so you can open it in any audio\n"
            "editor to verify the exact clip used for rendering."
        )
        self.trimmed_audio_label.setWordWrap(False)
        self.trimmed_audio_label.setMinimumWidth(0)
        trimmed_row.addWidget(self.trimmed_audio_label, 1)
        self.trimmed_audio_open_btn = QPushButton("Open")
        self.trimmed_audio_open_btn.setObjectName("zoomButton")
        self.trimmed_audio_open_btn.setFixedWidth(44)
        self.trimmed_audio_open_btn.setEnabled(False)
        self.trimmed_audio_open_btn.setToolTip("Reveal trimmed WAV in file explorer")
        self.trimmed_audio_open_btn.clicked.connect(self._on_open_trimmed_audio)
        trimmed_row.addWidget(self.trimmed_audio_open_btn)
        self.form_layout.addRow("Trimmed audio", trimmed_row)

        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0.0, 36000.0)
        self.start_spin.setDecimals(2)
        self.start_spin.valueChanged.connect(self._commit_general)
        _no_scroll(self.start_spin)
        self.form_layout.addRow("Start (s)", self.start_spin)

        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(0.0, 36000.0)
        self.end_spin.setDecimals(2)
        self.end_spin.valueChanged.connect(self._commit_general)
        _no_scroll(self.end_spin)
        self.form_layout.addRow("End (s)", self.end_spin)

        # Min beat spacing — anti-cluster filter for *Gen by Chart*.
        # When > 0 the panel's peak-detector collapses any cluster of
        # peaks closer than this gap into a single (highest-amplitude)
        # stick, which removes the "5-ticks-per-snare-hit" cluster the
        # user reported.  Auto Gen Block has its own lane-spacing
        # (rhythm.py ``--beat_min_gap``) so it is unaffected.  Stored on
        # ``Segment.min_beat_spacing_sec`` and persisted to the project.
        self.min_spacing_spin = QDoubleSpinBox()
        self.min_spacing_spin.setRange(0.0, 5.0)
        self.min_spacing_spin.setDecimals(2)
        self.min_spacing_spin.setSingleStep(0.01)
        self.min_spacing_spin.setSuffix(" s")
        self.min_spacing_spin.setToolTip(
            "Minimum spacing (seconds) between two beat sticks generated\n"
            "by the *Gen by Chart* button.  Peaks closer than this gap\n"
            "are collapsed into a single stick at the highest-amplitude\n"
            "peak of the cluster, so a single drum hit no longer emits\n"
            "5–6 ticks at the same spot.\n"
            "\n"
            "0.00 s          = off; every detected peak emits its own stick.\n"
            "0.05–0.10 s    = mild merge (preserves tight drum rolls).\n"
            "0.15 s (default) = balanced — one stick per audible beat for\n"
            "                  most music while keeping fast double-hits.\n"
            "0.20–0.30 s    = aggressive merge for very dense audio."
        )
        self.min_spacing_spin.valueChanged.connect(self._commit_general)
        _no_scroll(self.min_spacing_spin)
        self.form_layout.addRow("Min beat spacing", self.min_spacing_spin)

        self.mode_combo = QComboBox()
        for mode in ["punch", "dance", "line", "relax", "combo"]:
            self.mode_combo.addItem(mode.capitalize(), mode)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        _no_scroll(self.mode_combo)
        self.form_layout.addRow("Mode", self.mode_combo)

        self.dynamic_root = QWidget()
        self.dynamic_layout = QFormLayout(self.dynamic_root)
        self.dynamic_layout.setContentsMargins(0, 12, 0, 0)
        self.form_layout.addRow(self.dynamic_root)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 6, 0, 0)
        footer.setSpacing(6)

        self.preview_button = QPushButton("▶  Preview")
        self.preview_button.setObjectName("previewButton")
        self.preview_button.setCheckable(True)
        self._preview_default_tooltip = (
            "Toggle live preview mode.\n"
            "When ON, every edit (beat ticks, mode, density…) clears "
            "the buffered render and restarts from the current "
            "playhead so you watch the latest version without "
            "regenerating the whole segment.\n"
            "Click again to stop and free the renderer."
        )
        self.preview_button.setToolTip(self._preview_default_tooltip)
        self.preview_button.setEnabled(False)
        self.preview_button.clicked.connect(self._on_preview_clicked)
        footer.addWidget(self.preview_button, 1)

        self.render_button = QPushButton("Render")
        self.render_button.setObjectName("accentButton")
        self.render_button.setToolTip(
            "Full-quality render: 1920×1080 @ 30 fps with all effects.\n"
            "Result is saved as the segment's official rendered video\n"
            "and used when exporting the project."
        )
        self.render_button.setEnabled(False)
        self.render_button.clicked.connect(self._on_render_clicked)
        footer.addWidget(self.render_button, 1)

        root.addLayout(footer)

    def _set_empty_state(self, empty: bool) -> None:
        self.scroll.setEnabled(not empty)
        self.reset_button.setEnabled(not empty)
        # Preview/Render are stricter: they need a selected segment AND an
        # audio source (preview renders the segment's audio with the current
        # configs, so without audio there's nothing to preview/render).
        has_audio = (
            not empty
            and self._segment is not None
            and bool(self._segment.audio_path)
        )
        is_video_locked = self._is_video_segment_locked(self._segment)
        self.preview_button.setEnabled(has_audio and not is_video_locked)
        self.render_button.setEnabled(has_audio and not is_video_locked)
        self._refresh_open_folder_state()
        if empty:
            self.preview_button.setToolTip(
                "Select a segment in the timeline to enable preview"
            )
            self.render_button.setToolTip(
                "Select a segment in the timeline to enable render"
            )
            self.header_label.setText("Select a segment in the timeline to configure")
            self.status_label.setText("")
        elif not has_audio:
            if is_video_locked:
                self.preview_button.setToolTip(
                    "This segment previews directly from its source video."
                )
                self.render_button.setToolTip(
                    "Source-video segments are playback-only and do not render."
                )
            else:
                self.preview_button.setToolTip(
                    "Assign an audio source to this segment to enable preview"
                )
                self.render_button.setToolTip(
                    "Assign an audio source to this segment to enable render"
                )
        else:
            self.preview_button.setToolTip(
                "Render and play this segment with current settings"
            )
            self.render_button.setToolTip(
                "Enqueue this segment for game-data rendering"
            )

    def _rendered_video_path(self) -> Optional[Path]:
        """Absolute path of the segment's rendered MP4 if it exists on disk."""
        seg = self._segment
        if seg is None or not seg.video_path:
            return None
        path = Path(seg.video_path)
        return path if path.exists() else None

    def _refresh_open_folder_state(self) -> None:
        """Enable/disable the Open-folder button based on render presence."""
        path = self._rendered_video_path()
        if path is None:
            self.open_folder_button.setEnabled(False)
            if self._segment is None:
                self.open_folder_button.setToolTip(
                    "Select a rendered segment to open its folder"
                )
            elif not self._segment.video_path:
                self.open_folder_button.setToolTip(
                    "No render yet — render this segment to enable Open folder"
                )
            else:
                # video_path set but file missing on disk (moved/deleted).
                self.open_folder_button.setToolTip(
                    f"Rendered file is missing on disk:\n{self._segment.video_path}"
                )
        else:
            self.open_folder_button.setEnabled(True)
            self.open_folder_button.setToolTip(
                f"Reveal in file explorer:\n{path}"
            )

    def _refresh_audio_options(self) -> None:
        if self._project is None:
            return
        current = self.audio_combo.currentData()
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItem("(None)", "")
        for media in self._project.media_items:
            if media.kind.value == "audio":
                self.audio_combo.addItem(media.display_name, media.source_path)
        if current:
            idx = self.audio_combo.findData(current)
            if idx >= 0:
                self.audio_combo.setCurrentIndex(idx)
        self.audio_combo.blockSignals(False)

    def _load_segment_fields(self, segment: Segment) -> None:
        with QSignalBlocker(self.name_input):
            self.name_input.setText(segment.name)
        with QSignalBlocker(self.start_spin):
            self.start_spin.setValue(segment.start_time_sec)
        with QSignalBlocker(self.end_spin):
            self.end_spin.setValue(segment.end_time_sec)
        with QSignalBlocker(self.min_spacing_spin):
            self.min_spacing_spin.setValue(
                float(getattr(segment, "min_beat_spacing_sec", 0.0) or 0.0)
            )
        with QSignalBlocker(self.mode_combo):
            idx = self.mode_combo.findData(segment.mode)
            if idx >= 0:
                self.mode_combo.setCurrentIndex(idx)
        with QSignalBlocker(self.audio_combo):
            idx = self.audio_combo.findData(segment.audio_path)
            self.audio_combo.setCurrentIndex(idx if idx >= 0 else 0)

        self.header_label.setText(
            f"{segment.name}  {format_sec(segment.start_time_sec)} -> {format_sec(segment.end_time_sec)}"
        )

        # Trimmed audio label + open button.
        tap = segment.trimmed_audio_path
        tap_exists = bool(tap and Path(tap).exists())
        if tap_exists:
            self.trimmed_audio_label.setText(Path(tap).name)  # type: ignore[arg-type]
            self.trimmed_audio_label.setToolTip(str(tap))
            self.trimmed_audio_label.setStyleSheet("color:#7bd88f;")
            self.trimmed_audio_open_btn.setEnabled(True)
        elif tap:
            self.trimmed_audio_label.setText(f"⚠ missing: {Path(tap).name}")
            self.trimmed_audio_label.setToolTip(f"File not found: {tap}")
            self.trimmed_audio_label.setStyleSheet("color:#f59e0b;")
            self.trimmed_audio_open_btn.setEnabled(False)
        else:
            self.trimmed_audio_label.setText("— (not trimmed yet)")
            self.trimmed_audio_label.setToolTip(
                "Will be generated automatically when a segment is created\n"
                "or its start/end times are changed."
            )
            self.trimmed_audio_label.setStyleSheet("color:#6b6b6b;")
            self.trimmed_audio_open_btn.setEnabled(False)

        status_value = segment.render_status.value
        if status_value == "error" and segment.last_render_error:
            err = segment.last_render_error.strip().splitlines()
            short = err[-1] if err else "(no message)"
            for line in reversed(err):
                l = line.strip()
                if l and any(l.startswith(k) for k in ("Error", "Exception", "Traceback")):
                    short = l
                    break
            self.status_label.setText(f"Status: error — {short}")
            self.status_label.setStyleSheet("color:#ff6b6b;")
            self.status_label.setToolTip(segment.last_render_error)
            self.status_label.setWordWrap(True)
        elif status_value in ("rendering", "queued"):
            # Live render progress — pull from segment.last_render_progress
            # (updated by RenderService.progress signal).
            pct = max(0, min(100, int(getattr(segment, "last_render_progress", 0))))
            if status_value == "queued":
                self.status_label.setText("Status: queued")
            else:
                self.status_label.setText(f"Status: rendering {pct}%")
            self.status_label.setStyleSheet("color:#5cc8ff;")
            self.status_label.setToolTip("")
        elif status_value == "done":
            self.status_label.setText("Status: done")
            self.status_label.setStyleSheet("color:#7bd88f;")
            self.status_label.setToolTip("")
        else:
            self.status_label.setText(f"Status: {status_value}")
            self.status_label.setStyleSheet("color:#8a8a8a;")
            self.status_label.setToolTip("")

    def _clear_dynamic(self) -> None:
        while self.dynamic_layout.rowCount() > 0:
            self.dynamic_layout.removeRow(0)
        self._setting_widgets.clear()
        self._floor_panel_section = None
        self._side_rail_section = None

    # Fields always shown in the Properties panel, in display order.
    _VISIBLE_FIELDS = (
        "beat_source",
        "beat_sens",
        "density",
        "speed",
        "max_per_lane",
        "background_type",
        "background_color",
        "background_image",
        "background_video",
        "floor_panels",
        "side_rails",
        "stickman",
    )

    # Extra fields shown only for specific modes.  Keys must exist on the
    # corresponding Settings model (see studio/models/render_settings.py).
    # Values are ordered tuples of field names, inserted after the common
    # fields.
    _MODE_EXTRA_FIELDS: dict[str, tuple[str, ...]] = {
        "combo": ("mode_list",),
        "line":  ("line_zigzag",),
        "relax": (
            "relax_interval",
            "relax_travel_sec",
            "relax_wait_sec",
            "relax_texture_low",
            "relax_texture_high",
            "relax_texture_middle",
            "relax_hole_mask_path",
            "relax_kind_ratio_middle",
            "relax_show_low",
            "relax_show_high",
            "relax_show_middle",
            "relax_countdown_enabled",
            "relax_countdown_color",
            "relax_countdown_max_sec",
        ),
    }

    _FIELD_LABELS = {
        "beat_source": "Beat source",
        "beat_sens":   "Beat sens",
        "density":     "Density",
        "speed":       "Speed",
        "max_per_lane":"Max / lane",
        "background_type": "Background",
        "background_color": "Background color",
        "background_image": "Background image",
        "background_video": "Background video",
        "floor_panels":"Floor panels",
        "side_rails":  "Side rails",
        "stickman":    "Stickman",
        "mode_list":   "Sub-modes",
        "line_zigzag": "Zigzag",
        "relax_interval": "Relax interval",
        "relax_travel_sec": "Relax travel (sec)",
        "relax_wait_sec": "Relax wait (sec)",
        "relax_texture_low": "Relax texture low",
        "relax_texture_high": "Relax texture high",
        "relax_texture_middle": "Relax texture middle",
        "relax_hole_mask_path": "Relax hole mask",
        "relax_kind_ratio_middle": "Middle ratio",
        "relax_show_low": "Show low",
        "relax_show_high": "Show high",
        "relax_show_middle": "Show middle",
        "relax_countdown_enabled": "Countdown",
        "relax_countdown_color": "Countdown color",
        "relax_countdown_max_sec": "Countdown max sec",
    }

    def _rebuild_dynamic_settings(self) -> None:
        segment = self._segment
        if segment is None:
            return
        self._clear_dynamic()
        model = build_settings(segment.mode, segment.render_settings)
        # ``exclude_none=True`` keeps the persisted dict clean (no null
        # noise), but Optional fields like ``line_zigzag`` have a valid
        # ``None`` state we still want to display.  Keep a full dump for
        # widget-value lookup, and only the non-None dump for persistence.
        persist_defaults = model.model_dump(mode="json", exclude_none=True)
        all_defaults = model.model_dump(mode="json")
        segment.render_settings = persist_defaults

        mode_extras = self._MODE_EXTRA_FIELDS.get(segment.mode, ())
        if segment.mode == "combo":
            mode_list = model.model_dump(mode="json").get("mode_list") or []
            if "relax" in mode_list:
                mode_extras = (
                    mode_extras
                    + self._MODE_EXTRA_FIELDS.get("relax", ())
                )
        rs = segment.render_settings or {}
        for key in self._VISIBLE_FIELDS + mode_extras:
            # Prefer the full dump so Optional fields with value=None still
            # get a widget (their key may be absent from persist_defaults).
            if key in all_defaults:
                value = all_defaults[key]
            else:
                value = persist_defaults.get(key)
                if value is None:
                    continue
            widget = self._build_widget_for_value(key, value)
            if widget is None:
                continue
            self._setting_widgets[key] = widget
            label = self._FIELD_LABELS.get(key, key.replace("_", " ").capitalize())
            self.dynamic_layout.addRow(label, widget)

            # Inject the floor panel sub-section immediately after the
            # "floor_panels" toggle row so it appears visually grouped.
            if key == "floor_panels":
                section = _FloorPanelSection(
                    color=rs.get("floor_panel_color") or None,
                    blink=bool(rs.get("floor_panel_blink", False)),
                    image=rs.get("floor_panel_image") or None,
                    floor_panel_opacity=float(rs.get("floor_panel_opacity", 1.0) or 1.0),
                    floor_layout=str(rs.get("floor_layout", "auto")),
                    floor_bg_color=rs.get("floor_bg_color") or None,
                    floor_bg_opacity=float(rs.get("floor_bg_opacity", 1.0) or 1.0),
                    chevron_color=str(rs.get("chevron_color", "#FFD700")),
                    chevron_scroll=bool(rs.get("chevron_scroll", True)),
                    chevron_blink=bool(rs.get("chevron_blink", False)),
                    chevron_width_frac=float(rs.get("chevron_width_frac", 0.45) or 0.45),
                    chevron_count=int(rs.get("chevron_count", 6) or 6),
                    full_static_image=bool(rs.get("floor_full_static_image", False)),
                    parent=self,
                )
                section.setVisible(bool(value))
                section.changed.connect(self._commit_floor_panel_section)
                self.dynamic_layout.addRow(section)
                self._floor_panel_section = section
                # Connect checkbox to show/hide the section.
                widget.stateChanged.connect(
                    lambda state, s=section:
                        s.setVisible(state == Qt.CheckState.Checked.value
                                     or state == 2)  # Qt5/Qt6 compat
                )

            # Inject the side rail sub-section after the "side_rails" toggle.
            if key == "side_rails":
                sr_section = _SideRailSection(
                    color=rs.get("rail_color", "#FF60FF"),
                    shape=rs.get("rail_shape", "chunky"),
                    height=float(rs.get("rail_height", 0.14)),
                    offset_x=float(rs.get("rail_offset_x", 0.08)),
                    image=rs.get("rail_image") or None,
                    texture_non_loop=bool(rs.get("rail_texture_non_loop", False)),
                    pulse=rs.get("rail_pulse", "beat"),
                    pulse_intensity=float(rs.get("rail_pulse_intensity", 0.6)),
                    chevron_depth=float(rs.get("rail_chevron_depth", 1.0) or 1.0),
                    chevron_density=int(rs.get("rail_chevron_density", 6) or 6),
                    pillar_count=int(rs.get("rail_pillar_count", 16) or 16),
                    pillar_radius=float(rs.get("rail_pillar_radius", 1.0) or 1.0),
                    chase_mode=str(rs.get("rail_chase_mode", "time") or "time"),
                    chase_speed_frames=int(rs.get("rail_chase_speed_frames", 4) or 4),
                    dot_count=int(rs.get("rail_dot_count", 24) or 24),
                    dot_lines=int(rs.get("rail_dot_lines", 1) or 1),
                    dot_size_px=int(rs.get("rail_dot_size_px", 6) or 6),
                    dot_anim_mode=str(rs.get("rail_dot_anim_mode", "audio") or "audio"),
                    dot_color_near=str(rs.get("rail_dot_color_near", "#FF60FF") or "#FF60FF"),
                    dot_color_far=str(rs.get("rail_dot_color_far", "#00FFFF") or "#00FFFF"),
                    parent=self,
                )
                sr_section.setVisible(bool(value))
                sr_section.changed.connect(self._commit_side_rail_section)
                self.dynamic_layout.addRow(sr_section)
                self._side_rail_section = sr_section
                widget.stateChanged.connect(
                    lambda state, s=sr_section:
                        s.setVisible(state == Qt.CheckState.Checked.value
                                     or state == 2)
                )
        self._update_background_visibility()

    # Per-key UI hints (range, step, decimals, tooltip).
    # Keys not listed fall back to generic wide-range spinboxes.
    # Format: (min, max, step, decimals_or_None_for_int, tooltip)
    _NUMERIC_HINTS: dict[str, tuple[float, float, float, Optional[int], str]] = {
        # Beat detection
        "beat_sens":     (0.0, 1.0,   0.05, 2, "Beat sensitivity 0..1 (only used for beat/onset modes)"),
        "beat_min_gap":  (0,   60,    1,    None, "Minimum frames between consecutive targets (rhythm.py default: 4)"),
        "beat_subdiv":   (1,   8,     1,    None, "Blocks per beat (1, 2, 4, 8)"),
        "bpm":           (0.0, 400.0, 1.0,  1, "Force BPM (0 = auto-detect)"),
        # Flow
        "speed":         (0.1, 5.0,   0.1,  2, "Block movement speed multiplier"),
        "density":       (0.1, 5.0,   0.1,  2, "Overall block density multiplier"),
        "travel":        (-1,  500,   1,    None, "Travel frames (-1 = auto)"),
        "max_per_lane":  (1,   10,    1,    None, "Max blocks visible per lane"),
        # Cube
        "cube_radius":   (0.05, 1.0,  0.01, 3, "Cube half-size in world units"),
        # Pair cycles
        "punch_pair_cycle": (0, 16,   1,    None, "Punch pair cycle (0 = disabled, default 4)"),
        "dance_pair_cycle": (0, 16,   1,    None, "Dance pair cycle (0 = disabled, default 4)"),
        # Line mode
        "line_beats":    (1,   16,    1,    None, "Hold-note length in beats"),
        # Relax
        "relax_interval": (0.0, 60.0, 0.1,  2,
            "Relax obstacle cadence.\n"
            "0.0 (default) = music-driven: obstacles spawn on audio beats.\n"
            ">0.0 = EXTRA PAUSE (seconds) added AFTER each block fully\n"
            "      disappears before the next one spawns from the horizon.\n"
            "\n"
            "WARNING: relax mode already multiplies the travel time by 4×\n"
            "so blocks drift slowly.  A large value here (e.g. 2.0) adds\n"
            "2s on top of that ~15s cycle → only 1–2 blocks per minute.\n"
            "Recommended: 0.0 (beat-driven) or 0.3–0.5s for a slight gap."),
        "relax_travel_sec": (0.5, 10.0, 0.1, 2, "Relax block travel seconds (solo relax)."),
        "relax_wait_sec": (0.0, 10.0, 0.1, 2, "Hold time before relax block starts moving."),
        "relax_kind_ratio_middle": (0.0, 1.0, 0.01, 2, "Middle block spawn ratio (0..1)."),
        "relax_countdown_max_sec": (0.0, 20.0, 0.1, 2, "Countdown visible window (seconds)."),
    }

    def _build_widget_for_value(self, key: str, value):
        if key == "background_type":
            combo = QComboBox()
            combo.addItem("Solid color", "solid")
            combo.addItem("Image", "image")
            combo.addItem("Video", "video")
            combo.setCurrentIndex(max(0, combo.findData(str(value or "solid"))))
            combo.currentIndexChanged.connect(self._on_background_type_changed)
            return _no_scroll(combo)
        if key == "background_color":
            widget = _ColorPickerWidget(
                None if value is None else str(value),
                title="Background color",
                default_color="#000000",
                parent=self,
            )
            widget.changed.connect(self._commit_settings)
            return widget
        if key in {"background_image", "background_video"}:
            filt = ("Videos (*.mp4 *.mov *.mkv *.avi *.webm *.m4v);;All files (*.*)"
                    if key == "background_video"
                    else "Images (*.png *.jpg *.jpeg *.bmp *.tga *.webp);;All files (*.*)")
            title = "Select background video" if key == "background_video" else "Select background image"
            widget = _PathBrowseWidget(
                "" if value is None else str(value),
                title=title,
                file_filter=filt,
                placeholder="Optional file path",
                parent=self,
            )
            widget.changed.connect(self._commit_settings)
            return widget
        if key == "relax_countdown_color":
            widget = _ColorPickerWidget(
                None if value is None else str(value),
                title="Relax countdown color",
                default_color="#FFFFFF",
                parent=self,
            )
            widget.changed.connect(self._commit_settings)
            return widget
        if key in {
            "relax_texture_low",
            "relax_texture_high",
            "relax_texture_middle",
        }:
            widget = _PathBrowseWidget(
                "" if value is None else str(value),
                title="Select relax texture image",
                file_filter="Images (*.png *.jpg *.jpeg *.bmp *.tga *.webp);;All files (*.*)",
                placeholder="Optional file path",
                parent=self,
            )
            widget.changed.connect(self._commit_settings)
            return widget
        if key == "relax_hole_mask_path":
            line = QLineEdit("" if value is None else str(value))
            line.setPlaceholderText("Optional file path")
            line.editingFinished.connect(self._commit_settings)
            return line

        if isinstance(value, bool):
            widget = QCheckBox()
            widget.setChecked(value)
            widget.stateChanged.connect(self._commit_settings)
            return widget

        hint = self._NUMERIC_HINTS.get(key)

        if isinstance(value, int):
            widget = QSpinBox()
            if hint is not None:
                lo, hi, step, _, tip = hint
                widget.setRange(int(lo), int(hi))
                widget.setSingleStep(int(step) or 1)
                widget.setToolTip(tip)
            else:
                widget.setRange(-100000, 100000)
            widget.setValue(value)
            widget.valueChanged.connect(self._commit_settings)
            return _no_scroll(widget)

        if isinstance(value, float):
            widget = QDoubleSpinBox()
            if hint is not None:
                lo, hi, step, decimals, tip = hint
                widget.setRange(float(lo), float(hi))
                widget.setSingleStep(float(step))
                widget.setDecimals(decimals if decimals is not None else 2)
                widget.setToolTip(tip)
            else:
                widget.setRange(-100000.0, 100000.0)
                widget.setDecimals(3)
            widget.setValue(value)
            widget.valueChanged.connect(self._commit_settings)
            return _no_scroll(widget)

        if key == "mode_list":
            # Combo sub-mode selector: list of punch/dance/line/relax.
            modes = value if isinstance(value, list) else ["punch"]
            widget = _ModeListWidget(modes, self)
            widget.changed.connect(self._commit_settings)
            return widget

        # line_zigzag is Optional[Literal] — value can be None or a string.
        if key == "line_zigzag":
            combo = QComboBox()
            combo.addItem("Off", None)
            combo.addItem("Vertical", "vertical")
            combo.addItem("Horizontal", "horizontal")
            idx = combo.findData(value)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.setToolTip(
                "Zigzag pattern for line mode.\n"
                "Off      = straight chain of blocks\n"
                "Vertical = chain alternates up/down lanes\n"
                "Horizontal = chain alternates left/right lanes"
            )
            combo.currentIndexChanged.connect(self._commit_settings)
            return _no_scroll(combo)

        if isinstance(value, str):
            if key == "beat_source":
                combo = QComboBox()
                for item in ["tempo", "beat", "onset"]:
                    combo.addItem(item, item)
                combo.setCurrentIndex(max(0, combo.findData(value)))
                combo.setToolTip(
                    "tempo = uniform cadence from BPM\n"
                    "beat  = each librosa beat (may jitter)\n"
                    "onset = every transient"
                )
                combo.currentIndexChanged.connect(self._commit_settings)
                return _no_scroll(combo)
            line = QLineEdit(value)
            line.editingFinished.connect(self._commit_settings)
            return line

        if isinstance(value, list):
            line = QLineEdit(",".join(str(item) for item in value))
            line.setPlaceholderText("Comma-separated values")
            line.editingFinished.connect(self._commit_settings)
            return line
        return None

    def _set_dynamic_row_visible(self, widget: QWidget, visible: bool) -> None:
        try:
            lbl = self.dynamic_layout.labelForField(widget)
            if lbl is not None:
                lbl.setVisible(visible)
            widget.setVisible(visible)
        except Exception:
            widget.setVisible(visible)

    def _update_background_visibility(self) -> None:
        bg_type_widget = self._setting_widgets.get("background_type")
        if not isinstance(bg_type_widget, QComboBox):
            return
        bg_type = str(bg_type_widget.currentData() or "solid")
        w_color = self._setting_widgets.get("background_color")
        w_image = self._setting_widgets.get("background_image")
        w_video = self._setting_widgets.get("background_video")
        if isinstance(w_color, QWidget):
            self._set_dynamic_row_visible(w_color, bg_type == "solid")
        if isinstance(w_image, QWidget):
            self._set_dynamic_row_visible(w_image, bg_type == "image")
        if isinstance(w_video, QWidget):
            self._set_dynamic_row_visible(w_video, bg_type == "video")

    def _on_background_type_changed(self, *_args) -> None:
        self._update_background_visibility()
        self._commit_settings()

    def _collect_setting_widget_value(self, key: str, widget: QWidget):
        if isinstance(widget, _ModeListWidget):
            return widget.get_value()
        if isinstance(widget, _ColorPickerWidget):
            return widget.get_value()
        if isinstance(widget, _PathBrowseWidget):
            return widget.get_value()
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QSpinBox):
            return widget.value()
        if isinstance(widget, QDoubleSpinBox):
            return widget.value()
        if isinstance(widget, QComboBox):
            return widget.currentData()
        if isinstance(widget, QLineEdit):
            text = widget.text().strip()
            if "," in text:
                values = [part.strip() for part in text.split(",") if part.strip()]
                return [int(v) if v.isdigit() else v for v in values]
            return text or None
        return None

    def _commit_general(self) -> None:
        segment = self._segment
        if segment is None:
            return
        if self._is_video_segment_locked(segment):
            return
        # NOTE: ``segment.audio_path`` is intentionally NOT written here.
        # The audio source is bound to the segment by the create / split /
        # join / duplicate operations only; config edits must never change
        # which file the segment plays.
        segment.name = self.name_input.text().strip() or "Segment"
        start = float(self.start_spin.value())
        end = float(self.end_spin.value())
        segment.start_time_sec = min(start, end)
        segment.end_time_sec = max(start, end)
        segment.min_beat_spacing_sec = max(
            0.0, min(5.0, float(self.min_spacing_spin.value()))
        )
        self._load_segment_fields(segment)
        self._set_empty_state(False)
        self.segment_changed.emit(segment.id)

    def _on_mode_changed(self) -> None:
        segment = self._segment
        if segment is None:
            return
        if self._is_video_segment_locked(segment):
            return
        segment.mode = str(self.mode_combo.currentData())
        self._rebuild_dynamic_settings()
        self.segment_changed.emit(segment.id)

    def _commit_settings(self) -> None:
        segment = self._segment
        if segment is None:
            return
        if self._is_video_segment_locked(segment):
            return
        for key, widget in self._setting_widgets.items():
            segment.render_settings[key] = self._collect_setting_widget_value(key, widget)
        # Also persist floor panel sub-section values (if section exists).
        if self._floor_panel_section is not None:
            self._write_floor_panel_to_rs(segment, self._floor_panel_section)
        if self._side_rail_section is not None:
            self._write_side_rail_to_rs(segment, self._side_rail_section)
        validated = build_settings(segment.mode, segment.render_settings)
        segment.render_settings = validated.model_dump(mode="json", exclude_none=True)
        self.segment_changed.emit(segment.id)

    def _commit_floor_panel_section(self) -> None:
        """Called when any floor panel sub-section control changes."""
        segment = self._segment
        if segment is None or self._floor_panel_section is None:
            return
        if self._is_video_segment_locked(segment):
            return
        self._write_floor_panel_to_rs(segment, self._floor_panel_section)
        self.segment_changed.emit(segment.id)

    @staticmethod
    def _write_floor_panel_to_rs(
        segment: "Segment", sec: "_FloorPanelSection"
    ) -> None:
        segment.render_settings["floor_panel_color"] = sec.get_color()
        segment.render_settings["floor_panel_opacity"] = sec.get_floor_panel_opacity()
        segment.render_settings["floor_panel_blink"] = sec.get_blink()
        segment.render_settings["floor_panel_image"] = sec.get_image()
        segment.render_settings["floor_full_static_image"] = sec.get_full_static_image()
        segment.render_settings["floor_layout"]      = sec.get_floor_layout()
        segment.render_settings["floor_bg_color"]    = sec.get_floor_bg_color()
        segment.render_settings["floor_bg_opacity"]  = sec.get_floor_bg_opacity()
        segment.render_settings["chevron_color"]     = sec.get_chevron_color()
        segment.render_settings["chevron_scroll"]    = sec.get_chevron_scroll()
        segment.render_settings["chevron_blink"]     = sec.get_chevron_blink()
        segment.render_settings["chevron_width_frac"] = sec.get_chevron_width_frac()
        segment.render_settings["chevron_count"]     = sec.get_chevron_count()

    @staticmethod
    def _write_side_rail_to_rs(segment: "Segment", sec: "_SideRailSection") -> None:
        segment.render_settings["rail_color"]           = sec.get_color()
        segment.render_settings["rail_shape"]           = sec.get_shape()
        segment.render_settings["rail_height"]          = sec.get_height()
        segment.render_settings["rail_offset_x"]        = sec.get_offset_x()
        segment.render_settings["rail_image"]           = sec.get_image()
        segment.render_settings["rail_texture_non_loop"] = sec.get_texture_non_loop()
        segment.render_settings["rail_pulse"]            = sec.get_pulse()
        segment.render_settings["rail_pulse_intensity"]  = sec.get_pulse_intensity()
        segment.render_settings["rail_chevron_depth"]    = sec.get_chevron_depth()
        segment.render_settings["rail_chevron_density"]  = sec.get_chevron_density()
        segment.render_settings["rail_pillar_count"]     = sec.get_pillar_count()
        segment.render_settings["rail_pillar_radius"]    = sec.get_pillar_radius()
        segment.render_settings["rail_chase_mode"]       = sec.get_chase_mode()
        segment.render_settings["rail_chase_speed_frames"] = sec.get_chase_speed_frames()
        segment.render_settings["rail_dot_count"]        = sec.get_dot_count()
        segment.render_settings["rail_dot_lines"]        = sec.get_dot_lines()
        segment.render_settings["rail_dot_size_px"]      = sec.get_dot_size_px()
        segment.render_settings["rail_dot_anim_mode"]    = sec.get_dot_anim_mode()
        segment.render_settings["rail_dot_color_near"]   = sec.get_dot_color_near()
        segment.render_settings["rail_dot_color_far"]    = sec.get_dot_color_far()

    def _commit_side_rail_section(self) -> None:
        """Called when any side rail sub-section control changes."""
        segment = self._segment
        if segment is None or self._side_rail_section is None:
            return
        if self._is_video_segment_locked(segment):
            return
        self._write_side_rail_to_rs(segment, self._side_rail_section)
        self.segment_changed.emit(segment.id)

    def _on_preview_clicked(self) -> None:
        if self._segment is None:
            # The button is checkable; without a segment selected there's
            # nothing to toggle on, so make sure the visual stays unchecked.
            self.preview_button.setChecked(False)
            return
        self.preview_requested.emit(self._segment.id)

    def set_preview_active(self, active: bool) -> None:
        """Sync the Preview button's checked state + label with main window.

        Called from MainWindow whenever the live-preview mode flips on
        or off (either via this button OR programmatically — e.g. when
        the user switches segments while preview was running).  We
        avoid emitting ``clicked`` by using ``setChecked`` directly.
        """
        if not hasattr(self, "preview_button"):
            return
        # Stop any in-progress loading animation first.
        self.set_preview_loading(False)
        # ``blockSignals`` would also work, but Qt's QAbstractButton
        # only fires ``clicked`` from a real user click — programmatic
        # ``setChecked`` does not — so we don't need the guard.
        self.preview_button.setChecked(bool(active))
        if active:
            self.preview_button.setText("■  Stop Preview")
            self.preview_button.setToolTip(
                "Live preview is ON.\n"
                "Edits restart the render from the current playhead.\n"
                "Click to stop preview and free the renderer."
            )
        else:
            self.preview_button.setText("▶  Preview")
            self.preview_button.setToolTip(self._preview_default_tooltip)

    # Loading animation frames — cycled by _preview_loading_timer
    _LOADING_FRAMES = ["⠋  Loading…", "⠙  Loading…", "⠹  Loading…",
                       "⠸  Loading…", "⠼  Loading…", "⠴  Loading…",
                       "⠦  Loading…", "⠧  Loading…", "⠇  Loading…", "⠏  Loading…"]

    def set_preview_loading(self, loading: bool) -> None:
        """Show/hide a spinner animation on the Preview button while the
        renderer worker is building (~1-3 s)."""
        if not hasattr(self, "preview_button"):
            return

        # Lazily create the animation timer once.
        if not hasattr(self, "_preview_loading_timer"):
            self._preview_loading_timer = QTimer(self)
            self._preview_loading_timer.setInterval(80)
            self._preview_loading_frame = 0
            self._preview_loading_timer.timeout.connect(self._tick_preview_loading)

        if loading:
            if self._preview_loading_timer.isActive():
                return  # already loading
            self._preview_loading_frame = 0
            self.preview_button.setChecked(False)
            self.preview_button.setEnabled(False)
            self.preview_button.setToolTip(
                "Renderer is initialising (audio analysis + scene build).\n"
                "Click to cancel."
            )
            self._preview_loading_timer.start()
            self._tick_preview_loading()
        else:
            self._preview_loading_timer.stop()
            # Restore normal idle state (set_preview_active will set the
            # final label, so we only reset here if not already active).
            if not self.preview_button.isChecked():
                self.preview_button.setText("▶  Preview")
                self.preview_button.setEnabled(
                    self._segment is not None
                    and bool(getattr(self._segment, "audio_path", None))
                    and not self._is_video_segment_locked(self._segment)
                )
                self.preview_button.setToolTip(self._preview_default_tooltip)

    def _tick_preview_loading(self) -> None:
        frames = self._LOADING_FRAMES
        self.preview_button.setText(frames[self._preview_loading_frame % len(frames)])
        self._preview_loading_frame += 1

    def _on_render_clicked(self) -> None:
        if self._segment is None:
            return
        self.render_requested.emit(self._segment.id)

    def _on_reset_clicked(self) -> None:
        """Reset every *tunable* field of the current segment to its default.

        Two layers reset here:

        1. ``render_settings`` — the mode-specific dict driving the
           dynamic form rows (density / speed / beat_sens / …).
           Rebuilt via :func:`build_settings` so each Pydantic model's
           own defaults win.
        2. **Top-level Segment fields shown as static rows** — namely
           ``min_beat_spacing_sec`` (and any future tunable scalar in
           the same row group).  Without this, *Reset defaults* would
           silently leave ``Min beat spacing`` at whatever value the
           user last typed, which was the bug the user reported (the
           field stayed at ``0.00 s`` after Reset even though the
           dataclass default is ``0.15 s``).  Defaults are pulled from
           ``Segment.__dataclass_fields__`` so changing the dataclass
           default automatically updates Reset behaviour without
           hunting through this file.

        The static spinbox is then reloaded via
        :meth:`_load_segment_fields` so the UI reflects the reset
        immediately — ``_rebuild_dynamic_settings`` only repaints
        dynamic rows.
        """
        segment = self._segment
        if segment is None:
            return
        if self._is_video_segment_locked(segment):
            return
        segment.render_settings = build_settings(segment.mode, {}).model_dump(
            mode="json", exclude_none=True
        )
        defaults = Segment.__dataclass_fields__
        segment.min_beat_spacing_sec = float(
            defaults["min_beat_spacing_sec"].default
        )
        self._load_segment_fields(segment)
        self._rebuild_dynamic_settings()
        self.segment_changed.emit(segment.id)

    def _reveal_path(self, path: Path) -> None:
        """Reveal *path* in the OS file explorer with the file selected."""
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", f"/select,{path}"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        except Exception as exc:  # pragma: no cover
            try:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
            except Exception:
                print(f"[OpenFolder] failed: {exc}")

    def _on_open_folder_clicked(self) -> None:
        """Reveal the rendered MP4 in the OS file explorer."""
        path = self._rendered_video_path()
        if path is not None:
            self._reveal_path(path)

    def _on_open_trimmed_audio(self) -> None:
        """Reveal the pre-trimmed WAV in the OS file explorer."""
        seg = self._segment
        if seg is None or not seg.trimmed_audio_path:
            return
        path = Path(seg.trimmed_audio_path)
        if path.exists():
            self._reveal_path(path)

