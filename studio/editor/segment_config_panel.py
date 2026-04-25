"""Inspector panel for selected timeline segment."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional



from PySide6.QtCore import QSignalBlocker, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from studio.models import Project, Segment, build_settings


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
            short = err[-1] if err else "(no message)"
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
        self.audio_combo.currentIndexChanged.connect(self._commit_general)
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
        self.form_layout.addRow("Start (s)", self.start_spin)

        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(0.0, 36000.0)
        self.end_spin.setDecimals(2)
        self.end_spin.valueChanged.connect(self._commit_general)
        self.form_layout.addRow("End (s)", self.end_spin)

        self.mode_combo = QComboBox()
        for mode in ["punch", "dance", "line", "relax", "combo"]:
            self.mode_combo.addItem(mode.capitalize(), mode)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
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
        self.preview_button.setToolTip(
            "Fast preview render: 960×540 @ 24 fps, bloom off.\n"
            "Same gameplay/beats/mode as the full render — only pixel\n"
            "cost is reduced so you can iterate on settings quickly.\n"
            "Result auto-plays in the player and is NOT saved to the project."
        )
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
        self.preview_button.setEnabled(has_audio)
        self.render_button.setEnabled(has_audio)
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
            # Show the first line of the error inline + full message in tooltip.
            err = segment.last_render_error.strip().splitlines()
            short = err[-1] if err else "(no message)"
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

    # Fields to expose in the Properties panel, in display order.
    # All other render_settings keys are kept internally (saved/restored
    # from project files) but never shown in the UI — simplifies the form
    # so users only see the parameters that have a direct, audible impact.
    _VISIBLE_FIELDS = (
        "beat_source",
        "beat_sens",
        "density",
        "speed",
        "floor_panels",
        "stickman",
    )

    _FIELD_LABELS = {
        "beat_source": "Beat source",
        "beat_sens":   "Beat sens",
        "density":     "Density",
        "speed":       "Speed",
        "floor_panels":"Floor panels",
        "stickman":    "Stickman",
    }

    def _rebuild_dynamic_settings(self) -> None:
        segment = self._segment
        if segment is None:
            return
        self._clear_dynamic()
        model = build_settings(segment.mode, segment.render_settings)
        defaults = model.model_dump(mode="json", exclude_none=True)
        # Persist all values (so hidden params survive save/reload) but
        # only build widgets for the whitelisted visible fields.
        segment.render_settings = defaults
        for key in self._VISIBLE_FIELDS:
            value = defaults.get(key)
            if value is None:
                continue
            widget = self._build_widget_for_value(key, value)
            if widget is None:
                continue
            self._setting_widgets[key] = widget
            label = self._FIELD_LABELS.get(key, key.replace("_", " ").capitalize())
            self.dynamic_layout.addRow(label, widget)

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
    }

    def _build_widget_for_value(self, key: str, value):
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
            return widget

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
            return widget

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
                return combo
            line = QLineEdit(value)
            line.editingFinished.connect(self._commit_settings)
            return line

        if isinstance(value, list):
            line = QLineEdit(",".join(str(item) for item in value))
            line.setPlaceholderText("Comma-separated values")
            line.editingFinished.connect(self._commit_settings)
            return line
        return None

    def _collect_setting_widget_value(self, key: str, widget: QWidget):
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
        segment.name = self.name_input.text().strip() or "Segment"
        segment.audio_path = str(self.audio_combo.currentData() or "")
        start = float(self.start_spin.value())
        end = float(self.end_spin.value())
        segment.start_time_sec = min(start, end)
        segment.end_time_sec = max(start, end)
        self._load_segment_fields(segment)
        # Audio source may have just changed → re-evaluate preview/render gating.
        self._set_empty_state(False)
        self.segment_changed.emit(segment.id)

    def _on_mode_changed(self) -> None:
        segment = self._segment
        if segment is None:
            return
        segment.mode = str(self.mode_combo.currentData())
        self._rebuild_dynamic_settings()
        self.segment_changed.emit(segment.id)

    def _commit_settings(self) -> None:
        segment = self._segment
        if segment is None:
            return
        for key, widget in self._setting_widgets.items():
            segment.render_settings[key] = self._collect_setting_widget_value(key, widget)
        validated = build_settings(segment.mode, segment.render_settings)
        segment.render_settings = validated.model_dump(mode="json", exclude_none=True)
        self.segment_changed.emit(segment.id)

    def _on_preview_clicked(self) -> None:
        if self._segment is None:
            return
        self.preview_requested.emit(self._segment.id)

    def _on_render_clicked(self) -> None:
        if self._segment is None:
            return
        self.render_requested.emit(self._segment.id)

    def _on_reset_clicked(self) -> None:
        segment = self._segment
        if segment is None:
            return
        segment.render_settings = build_settings(segment.mode, {}).model_dump(
            mode="json", exclude_none=True
        )
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

