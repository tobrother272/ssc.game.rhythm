"""Export Dialog — detailed render-and-concat export panel.

Architecture:
  • N RenderService instances are created on the MAIN thread with their
    progress/finished/failed signals properly connected.
  • Each service owns one background worker thread and processes one job
    at a time.  With N services, N segments render in parallel.
  • No Qt objects are created or connected inside background threads.
  • Dialog stays open; user closes it manually.
  • Emits ``segment_rendered(segment_id, video_path)`` so MainWindow
    updates segment.video_path and persists the project.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from studio.models import Project, Segment


# ---------------------------------------------------------------------------
# Resolution presets
# ---------------------------------------------------------------------------
_RES_PRESETS: list[tuple[str, int, int]] = [
    ("1920 × 1080  (Full HD)",  1920, 1080),
    ("1280 × 720   (HD)",       1280,  720),
    ("3840 × 2160  (4K UHD)",   3840, 2160),
    ("2560 × 1440  (2K QHD)",   2560, 1440),
    ("Custom…",                    0,    0),
]

# Segment-row states
_ST_READY     = "ready"
_ST_QUEUED    = "queued"
_ST_RENDERING = "rendering"
_ST_DONE      = "done"
_ST_ERROR     = "error"

_STATE_META = {
    _ST_READY:     ("✅ Ready",      "#22c55e"),
    _ST_QUEUED:    ("⏳ Queued",     "#888888"),
    _ST_RENDERING: ("⚙ Rendering…", "#3bb6ff"),
    _ST_DONE:      ("✅ Rendered",   "#22c55e"),
    _ST_ERROR:     ("❌ Error",      "#ef4444"),
}

_ROW_BG = {
    _ST_READY:     "#172217",
    _ST_QUEUED:    "#1e1e1e",
    _ST_RENDERING: "#171d27",
    _ST_DONE:      "#172217",
    _ST_ERROR:     "#27171a",
}


# ---------------------------------------------------------------------------
# _SegmentRow
# ---------------------------------------------------------------------------
class _SegmentRow(QWidget):
    def __init__(self, index: int, segment: Segment, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.segment = segment
        has_video = bool(segment.video_path and Path(segment.video_path).exists())
        self.state = _ST_READY if has_video else _ST_QUEUED
        self._error_text = ""

        self.setFixedHeight(56)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5)
        lay.setSpacing(8)

        # Index
        idx = QLabel(f"{index + 1:02d}")
        idx.setFixedWidth(22)
        idx.setStyleSheet("color: #555; font-size: 11px;")
        lay.addWidget(idx)

        # Name + duration column
        name_col = QVBoxLayout()
        name_col.setSpacing(1)
        self._name_lbl = QLabel(segment.name or f"Segment {index + 1}")
        self._name_lbl.setStyleSheet("font-weight: bold; font-size: 12px; color: #e0e0e0;")
        dur = segment.duration_sec or 0.0
        mm, ss = divmod(int(dur), 60)
        self._dur_lbl = QLabel(f"{mm:02d}:{ss:02d}")
        self._dur_lbl.setStyleSheet("color: #666; font-size: 10px;")
        name_col.addWidget(self._name_lbl)
        name_col.addWidget(self._dur_lbl)
        lay.addLayout(name_col, 2)

        # Status label
        self._status_lbl = QLabel()
        self._status_lbl.setFixedWidth(108)
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setStyleSheet("font-size: 11px;")
        lay.addWidget(self._status_lbl)

        # Progress bar
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(100 if has_video else 0)
        self._bar.setTextVisible(True)
        self._bar.setFixedHeight(14)
        lay.addWidget(self._bar, 3)

        self._apply_state()

    # ------------------------------------------------------------------
    def set_state(self, state: str, *, progress: int = 0, error: str = "") -> None:
        self.state = state
        self._error_text = error
        if state == _ST_RENDERING:
            self._bar.setValue(max(1, progress))
        elif state in (_ST_DONE, _ST_READY):
            self._bar.setValue(100)
        elif state == _ST_QUEUED:
            self._bar.setValue(0)
        elif state == _ST_ERROR:
            self._bar.setValue(0)
        self._apply_state()

    def set_progress(self, pct: int) -> None:
        self._bar.setValue(max(1, pct))

    def _apply_state(self) -> None:
        label, color = _STATE_META.get(self.state, ("—", "#888"))
        self._status_lbl.setText(label)
        self._status_lbl.setStyleSheet(f"font-size: 11px; color: {color};")

        bar_chunk = "#22c55e" if self.state in (_ST_READY, _ST_DONE) else "#3bb6ff"
        if self.state == _ST_ERROR:
            bar_chunk = "#ef4444"
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                background: #252525; border: 1px solid #303030; border-radius: 3px;
                color: #ddd; font-size: 10px;
            }}
            QProgressBar::chunk {{ background: {bar_chunk}; border-radius: 2px; }}
        """)

        bg = _ROW_BG.get(self.state, "#1e1e1e")
        self.setStyleSheet(
            f"_SegmentRow {{ background: {bg}; border-bottom: 1px solid #282828; }}"
        )


# ---------------------------------------------------------------------------
# ExportDialog
# ---------------------------------------------------------------------------
class ExportDialog(QDialog):
    """Stay-open export dialog with parallel render + concat."""

    segment_rendered = Signal(str, str)   # segment_id, video_path

    # Internal signals used to safely marshal concat results from
    # a background threading.Thread back onto the Qt main thread.
    _sig_concat_done  = Signal(str)   # output_path
    _sig_concat_error = Signal(str)   # error message

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        project: Project,
        app_root: Path,
        temps_dir: Path,
        token_provider: Optional[Callable[[], Optional[str]]] = None,
        url_provider: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._app_root = app_root
        self._temps_dir = temps_dir
        self._token_provider = token_provider
        self._url_provider = url_provider

        self._rows: dict[str, _SegmentRow] = {}
        self._pending_ids: set[str] = set()
        self._error_ids: set[str] = set()
        self._is_running = False
        self._output_path = ""
        self._services: list = []   # RenderService instances kept alive

        self.setWindowTitle("Export")
        self.setMinimumSize(680, 580)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        # Wire internal signals so concat thread can safely update the UI.
        self._sig_concat_done.connect(self._on_concat_done)
        self._sig_concat_error.connect(self._on_concat_error)

        self._build_ui()
        self._populate_segments()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(14, 14, 14, 14)

        # ── Settings ──────────────────────────────────────────────────
        grp = QGroupBox("Export Settings")
        form = QFormLayout(grp)
        form.setSpacing(6)
        form.setContentsMargins(10, 10, 10, 10)

        self._res_combo = QComboBox()
        for label, _w, _h in _RES_PRESETS:
            self._res_combo.addItem(label)
        pw, ph = self._project.output_width, self._project.output_height
        matched = any(
            self._res_combo.setCurrentIndex(i) is None and True
            for i, (_l, w, h) in enumerate(_RES_PRESETS)
            if w == pw and h == ph
        )
        # simpler preset match
        self._res_combo.setCurrentIndex(0)
        for i, (_l, w, h) in enumerate(_RES_PRESETS):
            if w == pw and h == ph:
                self._res_combo.setCurrentIndex(i)
                matched = True
                break
        else:
            matched = False
        self._res_combo.currentIndexChanged.connect(self._on_res_changed)
        form.addRow("Resolution:", self._res_combo)

        self._custom_row = QWidget()
        cr = QHBoxLayout(self._custom_row)
        cr.setContentsMargins(0, 0, 0, 0); cr.setSpacing(6)
        self._width_spin = QSpinBox(); self._width_spin.setRange(320, 7680); self._width_spin.setSingleStep(2); self._width_spin.setValue(pw)
        self._height_spin = QSpinBox(); self._height_spin.setRange(240, 4320); self._height_spin.setSingleStep(2); self._height_spin.setValue(ph)
        cr.addWidget(QLabel("W:")); cr.addWidget(self._width_spin)
        cr.addWidget(QLabel("H:")); cr.addWidget(self._height_spin)
        cr.addStretch()
        form.addRow("", self._custom_row)
        self._custom_row.setVisible(not matched)

        self._fps_combo = QComboBox()
        for fps in (24, 30, 60):
            self._fps_combo.addItem(str(fps), fps)
        for i in range(self._fps_combo.count()):
            if self._fps_combo.itemData(i) == self._project.output_fps:
                self._fps_combo.setCurrentIndex(i); break
        form.addRow("Frame rate:", self._fps_combo)

        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(1, 4); self._workers_spin.setValue(2)
        self._workers_spin.setSuffix(" worker(s)")
        self._workers_spin.setToolTip("Number of segments to render simultaneously")
        form.addRow("Parallel:", self._workers_spin)

        path_row = QWidget()
        pr = QHBoxLayout(path_row); pr.setContentsMargins(0,0,0,0); pr.setSpacing(6)
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Choose output file…")
        browse_btn = QPushButton("Browse…"); browse_btn.setFixedWidth(76)
        browse_btn.clicked.connect(self._browse_output)
        pr.addWidget(self._path_edit, 1); pr.addWidget(browse_btn)
        form.addRow("Save to:", path_row)
        outer.addWidget(grp)

        # ── Segments list ─────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Segments").setObjectName("") or QLabel("Segments"))
        hdr.itemAt(0).widget().setStyleSheet("font-weight:bold; font-size:13px;")
        hdr.addStretch()
        self._seg_summary = QLabel("")
        self._seg_summary.setStyleSheet("color:#888; font-size:11px;")
        hdr.addWidget(self._seg_summary)
        outer.addLayout(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0,0,0,0); self._rows_layout.setSpacing(1)
        self._rows_layout.addStretch()
        scroll.setWidget(self._rows_container)
        scroll.setMinimumHeight(200)
        outer.addWidget(scroll, 1)

        # ── Error log (hidden until errors appear) ────────────────────
        self._error_box = QTextEdit()
        self._error_box.setReadOnly(True)
        self._error_box.setFixedHeight(80)
        self._error_box.setVisible(False)
        self._error_box.setStyleSheet(
            "background:#1e0f0f; color:#ef4444; font-size:11px; border:1px solid #5a1e1e;"
        )
        self._error_box.setPlaceholderText("Render errors will appear here…")
        outer.addWidget(self._error_box)

        # ── Status + overall bar ──────────────────────────────────────
        self._status_lbl = QLabel("Configure settings above, then click Run Export.")
        self._status_lbl.setStyleSheet("color:#888; font-size:11px; padding:2px 0;")
        self._status_lbl.setWordWrap(True)
        outer.addWidget(self._status_lbl)

        self._overall_bar = QProgressBar()
        self._overall_bar.setRange(0, 100); self._overall_bar.setValue(0)
        self._overall_bar.setTextVisible(True); self._overall_bar.setFixedHeight(16)
        self._overall_bar.setVisible(False)
        self._overall_bar.setStyleSheet("""
            QProgressBar { background:#252525; border:1px solid #333; border-radius:4px; }
            QProgressBar::chunk { background:#22c55e; border-radius:3px; }
        """)
        outer.addWidget(self._overall_bar)

        # ── Buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        self._run_btn = QPushButton("▶  Run Export")
        self._run_btn.setObjectName("accentButton")
        self._run_btn.setFixedHeight(32)
        self._run_btn.clicked.connect(self._on_run_clicked)
        btn_row.addWidget(self._run_btn, 1)
        close_btn = QPushButton("Close"); close_btn.setFixedHeight(32); close_btn.setFixedWidth(80)
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

    def _populate_segments(self) -> None:
        for row in self._rows.values():
            self._rows_layout.removeWidget(row); row.deleteLater()
        self._rows.clear()
        item = self._rows_layout.takeAt(self._rows_layout.count() - 1)
        if item: del item

        for i, seg in enumerate(self._project.sorted_segments()):
            row = _SegmentRow(i, seg)
            self._rows[seg.id] = row
            self._rows_layout.addWidget(row)
        self._rows_layout.addStretch()
        self._update_summary()

    # ------------------------------------------------------------------
    def _on_res_changed(self, index: int) -> None:
        _l, w, h = _RES_PRESETS[index]
        if w > 0:
            self._width_spin.setValue(w); self._height_spin.setValue(h)
        self._custom_row.setVisible(index == len(_RES_PRESETS) - 1)

    def _browse_output(self) -> None:
        start = str(Path(self._path_edit.text()).parent) if self._path_edit.text() else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save export as…",
            f"{start}/export.mp4" if start else "export.mp4",
            "Video (*.mp4);;All files (*)",
        )
        if path:
            if not path.lower().endswith(".mp4"):
                path += ".mp4"
            self._path_edit.setText(path)

    def _get_resolution(self) -> tuple[int, int]:
        idx = self._res_combo.currentIndex()
        _l, pw, ph = _RES_PRESETS[idx]
        return (pw, ph) if pw > 0 else (self._width_spin.value(), self._height_spin.value())

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def _on_run_clicked(self) -> None:
        output_path = self._path_edit.text().strip()
        if not output_path:
            self._path_edit.setFocus()
            self._path_edit.setStyleSheet("border:1px solid #e05252;")
            self._status_lbl.setText("⚠ Please specify an output file path.")
            return
        self._path_edit.setStyleSheet("")
        if not output_path.lower().endswith(".mp4"):
            output_path += ".mp4"
            self._path_edit.setText(output_path)

        if self._is_running:
            return

        self._output_path = output_path
        self._is_running = True
        self._run_btn.setEnabled(False)
        self._run_btn.setText("⏳  Running…")
        self._overall_bar.setValue(0)
        self._overall_bar.setVisible(True)
        self._error_box.clear()
        self._error_box.setVisible(False)
        self._error_ids.clear()

        width, height = self._get_resolution()
        fps = self._fps_combo.currentData() or 30
        n_workers = self._workers_spin.value()

        self._project.output_width = width
        self._project.output_height = height
        self._project.output_fps = fps

        segs = self._project.sorted_segments()
        unrendered = [
            s for s in segs
            if not s.video_path or not Path(s.video_path).exists()
        ]

        if not unrendered:
            self._status_lbl.setText("All segments already rendered. Starting concat…")
            self._start_concat()
            return

        # Mark all unrendered rows as queued
        for seg in unrendered:
            if seg.id in self._rows:
                self._rows[seg.id].set_state(_ST_QUEUED)
        self._pending_ids = {s.id for s in unrendered}
        self._update_summary()

        # ── Create N RenderService instances on the MAIN THREAD ───────
        from studio.core_bridge.render_service import RenderService

        # Stop any previous services
        self._services.clear()

        for _ in range(n_workers):
            svc = RenderService(
                self._app_root,
                token_provider=self._token_provider,
                url_provider=self._url_provider,
            )
            # All signal connections happen here on the main thread — safe.
            svc.progress.connect(self._on_svc_progress)
            svc.finished.connect(self._on_svc_finished)
            svc.failed.connect(self._on_svc_failed)
            self._services.append(svc)

        # Distribute jobs round-robin across services
        for i, seg in enumerate(unrendered):
            svc = self._services[i % n_workers]
            out = str(self._temps_dir / f"segment_{seg.id}.mp4")
            job = svc.build_job(
                seg, Path(out),
                output_width=width, output_height=height, output_fps=fps,
                project_temps_dir=str(self._temps_dir),
            )
            svc.enqueue(job)

        self._status_lbl.setText(
            f"Rendering {len(unrendered)} segment(s) with {n_workers} worker(s)…"
        )

    # ------------------------------------------------------------------
    # RenderService signal handlers (called on main thread)
    # ------------------------------------------------------------------
    def _on_svc_progress(self, segment_id: str, pct: int) -> None:
        row = self._rows.get(segment_id)
        if row:
            if row.state != _ST_RENDERING:
                row.set_state(_ST_RENDERING, progress=pct)
            else:
                row.set_progress(pct)
        self._update_overall_bar()

    def _on_svc_finished(self, segment_id: str, output_path: str) -> None:
        self._pending_ids.discard(segment_id)
        row = self._rows.get(segment_id)
        if row:
            row.set_state(_ST_DONE)

        seg = self._project.get_segment(segment_id)
        if seg:
            seg.video_path = str(Path(output_path).resolve())
        self.segment_rendered.emit(segment_id, output_path)

        self._update_summary()
        self._update_overall_bar()
        self._check_all_done()

    def _on_svc_failed(self, segment_id: str, error: str) -> None:
        self._pending_ids.discard(segment_id)
        self._error_ids.add(segment_id)
        row = self._rows.get(segment_id)
        if row:
            row.set_state(_ST_ERROR, error=error)

        # Show error in the log box
        self._error_box.setVisible(True)
        seg = self._project.get_segment(segment_id)
        seg_name = seg.name if seg else segment_id
        self._error_box.append(f"[{seg_name}] {error}")

        self._update_summary()
        self._update_overall_bar()
        self._check_all_done()

    def _check_all_done(self) -> None:
        if self._pending_ids:
            return  # still rendering

        if not self._error_ids:
            # All rendered successfully → concat
            self._status_lbl.setText("All renders complete. Starting concat…")
            self._start_concat()
        else:
            n_ok = sum(1 for s in self._project.segments
                       if s.video_path and Path(s.video_path).exists())
            n_err = len(self._error_ids)
            self._status_lbl.setText(
                f"⚠ {n_err} segment(s) failed to render. "
                f"{n_ok} rendered successfully. "
                "Fix errors and re-run, or proceed to concat with available videos."
            )
            # Offer to concat with whatever is available
            self._run_btn.setText("▶  Concat Available")
            self._run_btn.setEnabled(True)
            self._run_btn.clicked.disconnect()
            self._run_btn.clicked.connect(self._force_concat)
            self._is_running = False

    def _force_concat(self) -> None:
        self._run_btn.setEnabled(False)
        self._run_btn.setText("⏳  Concatenating…")
        self._start_concat()

    # ------------------------------------------------------------------
    # Overall progress bar
    # ------------------------------------------------------------------
    def _update_overall_bar(self) -> None:
        segs = self._project.sorted_segments()
        total = len(segs)
        if total == 0:
            return
        done = sum(
            1 for s in segs
            if s.video_path and Path(s.video_path).exists()
        )
        pct = int(done / total * 90)   # reserve 10% for concat
        self._overall_bar.setValue(pct)

    # ------------------------------------------------------------------
    # Summary label
    # ------------------------------------------------------------------
    def _update_summary(self) -> None:
        segs = self._project.sorted_segments()
        total = len(segs)
        ready = sum(1 for s in segs if s.video_path and Path(s.video_path).exists())
        rendering = len(self._pending_ids)
        errors = len(self._error_ids)
        parts = [f"{total} segment(s)", f"{ready} ready"]
        if rendering:
            parts.append(f"{rendering} rendering")
        if errors:
            parts.append(f"{errors} error(s)")
        self._seg_summary.setText(" · ".join(parts))

    # ------------------------------------------------------------------
    # Concat
    # ------------------------------------------------------------------
    def _start_concat(self) -> None:
        self._overall_bar.setValue(92)
        t = threading.Thread(target=self._concat_thread, daemon=True)
        t.start()

    def _concat_thread(self) -> None:
        import subprocess
        import tempfile
        from src.bundle_paths import find_ffmpeg

        segs = self._project.sorted_segments()
        video_paths = [
            s.video_path for s in segs
            if s.video_path and Path(s.video_path).exists()
        ]

        if not video_paths:
            self._sig_concat_error.emit("No rendered video files available.")
            return

        try:
            ffmpeg = find_ffmpeg()
        except FileNotFoundError:
            self._sig_concat_error.emit("ffmpeg not found.")
            return

        Path(self._output_path).parent.mkdir(parents=True, exist_ok=True)

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        for vp in video_paths:
            safe = vp.replace("\\", "/").replace("'", "\\'")
            tmp.write(f"file '{safe}'\n")
        tmp.flush(); tmp.close()
        concat_list = tmp.name

        cmd = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_list,
            "-c", "copy",
            self._output_path,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception as exc:
            self._sig_concat_error.emit(str(exc))
            return
        finally:
            try:
                Path(concat_list).unlink(missing_ok=True)
            except Exception:
                pass

        if result.returncode != 0:
            err = (result.stderr or result.stdout)[-800:]
            self._sig_concat_error.emit(f"ffmpeg rc={result.returncode}:\n{err}")
        else:
            self._sig_concat_done.emit(self._output_path)

    def _on_concat_done(self, output_path: str) -> None:
        self._overall_bar.setValue(100)
        self._status_lbl.setText(f"✅ Export complete → {output_path}")
        self._run_btn.setText("▶  Run Export")
        self._run_btn.setEnabled(True)
        # Restore normal click handler
        try:
            self._run_btn.clicked.disconnect()
        except Exception:
            pass
        self._run_btn.clicked.connect(self._on_run_clicked)
        self._is_running = False

    def _on_concat_error(self, error: str) -> None:
        self._status_lbl.setText(f"❌ Concat failed: {error}")
        self._error_box.setVisible(True)
        self._error_box.append(f"[Concat] {error}")
        self._run_btn.setText("▶  Run Export")
        self._run_btn.setEnabled(True)
        try:
            self._run_btn.clicked.disconnect()
        except Exception:
            pass
        self._run_btn.clicked.connect(self._on_run_clicked)
        self._is_running = False

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # type: ignore[override]
        super().closeEvent(event)
