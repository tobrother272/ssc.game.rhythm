"""Timeline panel built on QGraphicsView."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QPointF, QRect, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from studio.editor.media_library import MEDIA_ID_MIME
from studio.models import Project, Segment


def format_seconds(value: float) -> str:
    total = max(0, int(value))
    mm, ss = divmod(total, 60)
    return f"{mm:02d}:{ss:02d}"


def format_ruler_time(time_sec: float, major_step_sec: float) -> str:
    """Format a ruler tick label given the current major-tick interval.

    - Steps >= 1s         → ``MM:SS``  (whole-second precision is enough)
    - Steps in 0.1..1s    → ``MM:SS.s``
    - Steps < 0.1s        → ``MM:SS.ss`` (centisecond precision)
    """
    time_sec = max(0.0, time_sec)
    if major_step_sec >= 1.0:
        return format_seconds(time_sec)
    total_int = int(time_sec)
    mm, ss = divmod(total_int, 60)
    frac = time_sec - total_int
    if major_step_sec >= 0.1:
        return f"{mm:02d}:{ss:02d}.{int(round(frac * 10)) % 10:01d}"
    return f"{mm:02d}:{ss:02d}.{int(round(frac * 100)) % 100:02d}"


MODE_COLORS = {
    "punch": QColor("#3bb6ff"),   # CapCut-like cyan-blue
    "dance": QColor("#f59e0b"),
    "line": QColor("#22d3ee"),
    "relax": QColor("#a78bfa"),
    "combo": QColor("#ec4899"),
}


# ---------------------------------------------------------------------------
# Zoom range — controls the timeline's pixels-per-second (pps) values.
#
# We express the range in user-meaningful terms: "how many seconds are
# represented by one major ruler tick".  CapCut-style:
#
#   • Min zoom (slider all the way LEFT)  → 1 major tick = 5 minutes (300s)
#   • Max zoom (slider all the way RIGHT) → 1 major tick = 0.01 seconds
#
# The slider operates in log-space so the user gets equally fine-grained
# control across the whole 30 000× range.
# ---------------------------------------------------------------------------
TARGET_MAJOR_PX = 80.0           # visual spacing between major ticks
ZOOM_MIN_STEP_SEC = 0.01         # tightest zoom: 0.01s/tick
ZOOM_MAX_STEP_SEC = 300.0        # loosest zoom: 5 min/tick
ZOOM_MIN_PPS = TARGET_MAJOR_PX / ZOOM_MAX_STEP_SEC   # ≈ 0.267 px/s
ZOOM_MAX_PPS = TARGET_MAJOR_PX / ZOOM_MIN_STEP_SEC   # = 8000  px/s
ZOOM_SLIDER_RES = 1000           # slider granularity (0..1000)
# "Nice" major-tick intervals in seconds; we always pick the smallest
# value >= raw target.  Goes from 10 ms up to 5 min.
_NICE_MAJOR_STEPS = (
    0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
    1, 2, 5, 10, 15, 30, 60, 120, 300,
)


def pick_major_step(pps: float) -> float:
    """Choose a nice major-tick interval for a given pixels-per-second."""
    if pps <= 0:
        return 5.0
    raw = TARGET_MAJOR_PX / pps
    for step in _NICE_MAJOR_STEPS:
        if step >= raw:
            return float(step)
    return float(_NICE_MAJOR_STEPS[-1])


def slider_value_to_pps(value: int) -> float:
    """Map a 0..ZOOM_SLIDER_RES integer → pps (log-scale)."""
    t = max(0, min(ZOOM_SLIDER_RES, int(value))) / ZOOM_SLIDER_RES
    return ZOOM_MIN_PPS * (ZOOM_MAX_PPS / ZOOM_MIN_PPS) ** t


def pps_to_slider_value(pps: float) -> int:
    """Inverse of `slider_value_to_pps` — clamps OOB pps into range."""
    pps = max(ZOOM_MIN_PPS, min(ZOOM_MAX_PPS, pps))
    t = math.log(pps / ZOOM_MIN_PPS) / math.log(ZOOM_MAX_PPS / ZOOM_MIN_PPS)
    return int(round(t * ZOOM_SLIDER_RES))


class OverviewBar(QWidget):
    """Compact horizontal strip showing all project segments at a glance.

    The whole project timeline is mapped to the bar's width so users always
    see every segment. Click a block to focus the main timeline on that
    segment; click empty space to exit focus.
    """

    segment_clicked = Signal(str)  # segment_id
    empty_clicked = Signal()

    HEIGHT = 28

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._selected_id: Optional[str] = None
        self._focused_id: Optional[str] = None
        self._segment_rects: list[tuple[QRect, str]] = []
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Overview — click a segment to focus, click empty space to exit focus")

    def set_project(self, project: Optional[Project]) -> None:
        self._project = project
        self.update()

    def set_selected(self, segment_id: Optional[str]) -> None:
        if segment_id == self._selected_id:
            return
        self._selected_id = segment_id
        self.update()

    def set_focused(self, segment_id: Optional[str]) -> None:
        if segment_id == self._focused_id:
            return
        self._focused_id = segment_id
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        painter.fillRect(self.rect(), QColor("#0e0e0e"))
        painter.setPen(QPen(QColor("#1f1f1f")))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

        self._segment_rects = []

        if (
            self._project is None
            or not self._project.segments
        ):
            painter.setPen(QColor("#4a4a4a"))
            painter.drawText(
                self.rect(),
                int(Qt.AlignmentFlag.AlignCenter),
                "No segments yet",
            )
            return

        max_end = max((s.end_time_sec for s in self._project.segments), default=0.0)
        if max_end <= 0:
            return

        margin = 4
        usable_w = max(1, self.width() - margin * 2)
        block_y = 5
        block_h = self.height() - 10

        for seg in self._project.segments:
            x = margin + int(seg.start_time_sec / max_end * usable_w)
            w = max(3, int(seg.duration_sec / max_end * usable_w))
            base = MODE_COLORS.get(seg.mode, QColor("#3bb6ff"))
            color = QColor(base)
            border = QColor("#0a0a0a")
            border_w = 1
            if seg.id == self._focused_id:
                color = color.lighter(125)
                border = QColor("#ffffff")
                border_w = 2
            elif seg.id == self._selected_id:
                border = QColor("#ffffff")
                border_w = 1
            painter.setPen(QPen(border, border_w))
            painter.setBrush(QBrush(color))
            rect = QRect(x, block_y, w, block_h)
            painter.drawRect(rect)
            self._segment_rects.append((rect, seg.id))

            # "Has rendered video" badge — small green disc with a white
            # play triangle, drawn in the top-right corner of the block.
            # Lets users tell at a glance which segments already have a
            # rendered video on disk (segment.video_path is set) versus
            # those still pending render.  Falls back to a plain green
            # dot when the block is too narrow to fit a triangle.
            if getattr(seg, "video_path", None):
                self._paint_rendered_badge(painter, rect)

    def _paint_rendered_badge(self, painter: QPainter, rect: QRect) -> None:
        """Draw a "video rendered" badge in the top-left of ``rect``.

        Top-left was chosen over top-right because the right edge often
        butts up against the next segment block and can be partially
        obscured by the 2-px white border drawn in focus mode.  Saves
        and restores painter state so the caller's loop pen/brush
        (per-segment color) isn't disturbed.
        """
        # Sizing: scale with block height so the badge stays proportional
        # on tall overview bars while still being readable on short ones.
        # Hard floor: drop the badge entirely if the block is so narrow
        # that a 4-px disc would dominate it.
        max_dim = min(rect.width() - 2, rect.height() - 2)
        if max_dim < 5:
            return
        badge_d = min(14, max(6, int(rect.height() * 0.7)))
        # Inset by 2 px from the top-left corner so the badge sits inside
        # the selection/focus border.
        bx = rect.left() + 2
        by = rect.top() + 2
        badge_rect = QRect(bx, by, badge_d, badge_d)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # White outer ring for high-contrast against any segment color.
        painter.setPen(QPen(QColor("#ffffff"), 1.5))
        painter.setBrush(QBrush(QColor("#22c55e")))  # green = "ready"
        painter.drawEllipse(badge_rect)

        # White play triangle inside the disc.  Skip when the disc is too
        # small for a legible triangle (just leave the green dot).
        if badge_d >= 8:
            cx = badge_rect.center().x() + 0.5
            cy = badge_rect.center().y() + 0.5
            s = badge_d * 0.28
            tri = QPainterPath()
            tri.moveTo(cx - s * 0.7, cy - s)
            tri.lineTo(cx - s * 0.7, cy + s)
            tri.lineTo(cx + s, cy)
            tri.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.drawPath(tri)

        painter.restore()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.pos()
            for rect, sid in self._segment_rects:
                if rect.contains(pos):
                    self.segment_clicked.emit(sid)
                    event.accept()
                    return
            self.empty_clicked.emit()
        super().mousePressEvent(event)


@dataclass
class SegmentBlockMeta:
    """Metadata for timeline segment block."""

    segment_id: str


class SegmentRectItem(QGraphicsRectItem):
    """Draggable timeline block bound to one segment."""

    def __init__(self, segment: Segment, pixels_per_second: float):
        super().__init__()
        self.segment_id = segment.id
        self._pixels_per_second = pixels_per_second
        self.setFlags(
            QGraphicsRectItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsRectItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)

    # Y locked to the segment track body (ruler_h + 2px padding).
    SEGMENT_Y = 40.0

    def itemChange(self, change, value):  # type: ignore[override]
        if change == QGraphicsRectItem.GraphicsItemChange.ItemPositionChange:
            new_pos = value
            return QPointF(max(0.0, new_pos.x()), self.SEGMENT_Y)
        return super().itemChange(change, value)


class TimelineView(QGraphicsView):
    """Graphics view that accepts media drop and selection events.

    `_pps` and `_offset_sec` describe how scene-x maps back to time:
        time_sec = scene_x / _pps + _offset_sec
    In the default (overview) mode `_offset_sec` is 0 and `_pps` matches the
    panel's `pixels_per_second`. In focus mode, the panel rescales these so a
    single segment fills the viewport and clicks still resolve to absolute
    project time.
    """

    media_dropped_at = Signal(str, float)  # media_id, time_sec
    empty_clicked = Signal()
    playhead_scrubbed = Signal(float)  # time_sec
    segment_double_clicked = Signal(str)  # segment_id
    viewport_resized = Signal()  # emitted whenever the view's viewport changes size
    zoom_requested = Signal(float, float)  # factor, viewport_x (cursor pos)

    def __init__(self, scene: QGraphicsScene, pixels_per_second: float, parent=None):
        super().__init__(scene, parent)
        self._pps = pixels_per_second
        self._offset_sec = 0.0
        self._playhead_x = 0.0
        self._dragging_playhead = False
        self.setAcceptDrops(True)
        self.setRenderHints(self.renderHints())
        # Default QGraphicsView aligns the scene to AlignCenter — that left
        # symmetric black gutters on both sides whenever the scene was even
        # slightly narrower than the viewport (e.g. in focus mode where we
        # want the segment to perfectly fill the viewport). Force top-left so
        # any leftover slack is harmless and scenes stick to the left edge.
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.viewport_resized.emit()

    def set_playhead_x(self, x: float) -> None:
        """Update cached playhead X for drag hit-test."""
        self._playhead_x = x

    def _x_to_time(self, x: float) -> float:
        return x / max(0.001, self._pps) + self._offset_sec

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasFormat(MEDIA_ID_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasFormat(MEDIA_ID_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        if not event.mimeData().hasFormat(MEDIA_ID_MIME):
            event.ignore()
            return
        media_id = bytes(event.mimeData().data(MEDIA_ID_MIME)).decode("utf-8")
        scene_pos = self.mapToScene(event.position().toPoint())
        time_sec = max(0.0, self._x_to_time(scene_pos.x()))
        self.media_dropped_at.emit(media_id, time_sec)
        event.acceptProposedAction()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            if abs(scene_pos.x() - self._playhead_x) <= 8:
                self._dragging_playhead = True
                self.playhead_scrubbed.emit(max(0.0, self._x_to_time(scene_pos.x())))
                event.accept()
                return
            if scene_pos.y() <= self.sceneRect().height():
                self.playhead_scrubbed.emit(max(0.0, self._x_to_time(scene_pos.x())))
        item = self.itemAt(event.position().toPoint())
        if item is None:
            self.empty_clicked.emit()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._dragging_playhead:
            scene_pos = self.mapToScene(event.position().toPoint())
            self.playhead_scrubbed.emit(max(0.0, self._x_to_time(scene_pos.x())))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging_playhead = False
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        """Ctrl+scroll → zoom timeline in/out around the cursor position."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # angleDelta().y() is +120 per notch forward, -120 backward.
            delta = event.angleDelta().y()
            if delta == 0:
                event.ignore()
                return
            # Zoom step: ~15 % per scroll notch.
            factor = 1.15 if delta > 0 else (1.0 / 1.15)
            self.zoom_requested.emit(factor, event.position().x())
            event.accept()
        else:
            super().wheelEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        """Double-click on a segment block enters focus mode for that segment."""
        item = self.itemAt(event.position().toPoint())
        # If the user hit a child label, walk up to the segment block.
        while item is not None and not isinstance(item, SegmentRectItem):
            item = item.parentItem()
        if isinstance(item, SegmentRectItem):
            self.segment_double_clicked.emit(item.segment_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class TimelinePanel(QWidget):
    """Segment timeline editor and selection source."""

    segment_selected = Signal(object)  # Segment | None
    segment_changed = Signal(str)  # segment_id
    create_segment_requested = Signal(str, float)  # media_id, start_time
    playhead_seek_requested = Signal(float)  # time_sec
    segment_split = Signal(str, str)  # original_id, new_id (for MainWindow to handle)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._block_map: dict[str, SegmentRectItem] = {}
        self.pixels_per_second = 60.0  # base zoom in overview mode
        self._effective_pps = 60.0     # scaled when in focus mode
        self._offset_sec = 0.0          # >0 in focus mode (focused segment start)
        self._focus_segment_id: Optional[str] = None
        self._selected_segment_id: str | None = None
        self._playhead_time_sec = 0.0
        self._playhead_x = 0.0
        # Waveform state - set via set_waveform(audio_path, peaks, duration_sec).
        self._waveform_audio: Optional[str] = None
        self._waveform_peaks: list[tuple[float, float]] = []
        self._waveform_duration_sec: float = 0.0
        self._waveform_peaks_per_sec: int = 100
        self._waveform_loading: bool = False
        self._waveform_loading_dots: int = 0
        self._refresh_pending: bool = False  # guard against re-entrant refresh
        self._build_ui()

    # -- Coordinate helpers --------------------------------------------------
    def _time_to_x(self, time_sec: float) -> float:
        return (time_sec - self._offset_sec) * self._effective_pps

    def _x_to_time(self, x: float) -> float:
        return x / max(0.001, self._effective_pps) + self._offset_sec

    def set_project(self, project: Project) -> None:
        """Attach project and draw timeline content."""
        self._project = project
        self.overview_bar.set_project(project)
        # Exit focus mode on project switch — focused id may not exist anymore.
        self._focus_segment_id = None
        self._offset_sec = 0.0
        self._effective_pps = self.pixels_per_second
        self.view._offset_sec = 0.0
        self.view._pps = self.pixels_per_second
        self.refresh()

    def refresh(self) -> None:
        """Rebuild scene from project segments.

        We block the scene's `selectionChanged` signal during the rebuild
        because `scene.clear()` triggers it (the previous selection vanishes
        with the items), which would falsely tell the rest of the app that
        "no segment is selected". After the rebuild we re-apply the previous
        selection silently so listeners don't see a spurious deselect-then-
        reselect flicker (which previously wiped the waveform every refresh).

        A re-entrancy guard (`_refresh_pending`) prevents the scene from being
        cleared while a previous refresh is still on the call-stack.  This can
        happen when `scene.setSceneRect()` causes Qt to resize the viewport
        scrollbars, which fires `resizeEvent`, which would otherwise emit
        `viewport_resized` and trigger another `refresh()` before the first
        one has returned.
        """
        if self._refresh_pending:
            return
        self._refresh_pending = True
        try:
            prev_selected_id = self._selected_segment_id
            # scene.clear() deletes all C++ items, so drop stale playhead reference first
            self._playhead = None
            self.scene.blockSignals(True)
            try:
                self.scene.clear()
                self._block_map.clear()
                self._update_scene_width()
                self._draw_ruler()
                self._draw_tracks()
                self._draw_waveform()
                if self._project:
                    for segment in self._project.sorted_segments():
                        self._draw_segment(segment)
                    # Restore selection silently while signals are blocked.
                    if prev_selected_id and prev_selected_id in self._block_map:
                        self._block_map[prev_selected_id].setSelected(True)
                    self._draw_playhead(self._playhead_time_sec)
            finally:
                self.scene.blockSignals(False)
            # Sync overview bar highlights with current state.
            self.overview_bar.set_selected(self._selected_segment_id)
            self.overview_bar.set_focused(self._focus_segment_id)
            self.overview_bar.update()
        finally:
            self._refresh_pending = False

    # -- Focus mode ----------------------------------------------------------
    def enter_focus_mode(self, segment_id: str) -> None:
        """Zoom-to-fit a single segment, hiding all others.

        Triggered by double-clicking a segment block or clicking an overview
        block. Idempotent: focusing the same segment twice is a no-op.
        """
        if not self._project:
            return
        segment = self._project.get_segment(segment_id)
        if segment is None or segment.duration_sec <= 0:
            return
        if self._focus_segment_id == segment_id:
            return
        # Initial zoom so the segment fills the viewport. _update_scene_width
        # re-fits this on every refresh, so it stays correct after resizes.
        viewport_w = max(200, self.view.viewport().width())
        focus_pps = viewport_w / segment.duration_sec
        self._effective_pps = max(ZOOM_MIN_PPS, min(ZOOM_MAX_PPS, focus_pps))
        self._offset_sec = segment.start_time_sec
        self._focus_segment_id = segment_id
        # Keep TimelineView's mouse-coord helpers consistent with our scaling.
        self.view._pps = self._effective_pps
        self.view._offset_sec = self._offset_sec
        # Auto-select the focused segment so inspector + preview follow.
        self._selected_segment_id = segment_id
        self.refresh()
        # Scroll the view to the very beginning so the segment always starts
        # at the left edge — regardless of where the user had previously scrolled.
        self.view.horizontalScrollBar().setValue(0)
        self.segment_selected.emit(segment)

    def exit_focus_mode(self) -> None:
        """Return to the full project overview view."""
        if self._focus_segment_id is None:
            return
        # Remember segment start so overview scrolls to keep it in view.
        prev_offset = self._offset_sec
        self._focus_segment_id = None
        self._offset_sec = 0.0
        self._effective_pps = self.pixels_per_second
        self.view._pps = self.pixels_per_second
        self.view._offset_sec = 0.0
        self.refresh()
        # Scroll overview so the previously focused segment is visible.
        target_x = int(prev_offset * self.pixels_per_second)
        self.view.horizontalScrollBar().setValue(
            max(0, target_x - self.view.viewport().width() // 4)
        )

    def set_waveform_loading(self) -> None:
        """Show a loading animation in the waveform track while extracting."""
        self._waveform_loading = True
        self._waveform_loading_dots = 0
        self._waveform_peaks = []
        self._waveform_duration_sec = 0.0
        self.refresh()
        if not self._waveform_loading_timer.isActive():
            self._waveform_loading_timer.start()

    def set_waveform(
        self,
        audio_path: str,
        peaks: list[tuple[float, float]],
        duration_sec: float,
        peaks_per_sec: int = 100,
    ) -> None:
        """Attach waveform data and redraw the waveform track."""
        self._waveform_loading = False
        self._waveform_loading_timer.stop()
        self._waveform_audio = audio_path
        self._waveform_peaks = peaks
        self._waveform_duration_sec = duration_sec
        self._waveform_peaks_per_sec = max(1, peaks_per_sec)
        self.refresh()

    def clear_waveform(self) -> None:
        """Clear any cached waveform and redraw empty track."""
        self._waveform_loading = False
        self._waveform_loading_timer.stop()
        self._waveform_audio = None
        self._waveform_peaks = []
        self._waveform_duration_sec = 0.0
        self.refresh()

    def set_playhead(self, time_sec: float) -> None:
        """Move playhead according to preview playback."""
        self._playhead_time_sec = max(0.0, time_sec)
        self._draw_playhead(time_sec)
        self._playhead_label.setText(format_seconds(self._playhead_time_sec))

    def _build_ui(self) -> None:
        self.setObjectName("PanelRoot")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header strip "Timeline" with toolbar
        header = QWidget()
        header.setObjectName("panelHeader")
        top = QHBoxLayout(header)
        top.setContentsMargins(10, 6, 10, 6)
        top.setSpacing(6)

        title = QLabel("Timeline")
        title.setObjectName("panelTitle")
        top.addWidget(title)
        top.addSpacing(10)

        self.split_button = QPushButton("Split")
        self.split_button.setToolTip(
            "Split selected segment at current playhead position (S)"
        )
        self.split_button.setEnabled(False)
        self.split_button.setObjectName("splitButton")
        self.split_button.clicked.connect(self._on_split_clicked)
        top.addWidget(self.split_button)

        top.addStretch()

        # CapCut-style zoom bar:  [Fit]  [−]  [====O======]  [+]
        # The slider is log-scale so each pixel of slider travel feels
        # like a roughly equal "zoom step" across the whole 30 000× range.
        self.zoom_fit_button = QPushButton("Fit")
        self.zoom_fit_button.setObjectName("zoomButton")
        self.zoom_fit_button.setFixedWidth(36)
        self.zoom_fit_button.setToolTip(
            "Zoom to fit: scale the timeline so the whole project is\n"
            "visible from start to end."
        )
        self.zoom_fit_button.clicked.connect(self._on_zoom_fit_clicked)
        top.addWidget(self.zoom_fit_button)

        self.zoom_out_button = QPushButton("−")
        self.zoom_out_button.setObjectName("zoomButton")
        self.zoom_out_button.setFixedWidth(28)
        self.zoom_out_button.setToolTip(
            f"Zoom out (max step: {ZOOM_MAX_STEP_SEC/60:.0f} min)"
        )
        self.zoom_out_button.clicked.connect(self._on_zoom_out_clicked)
        top.addWidget(self.zoom_out_button)

        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setObjectName("zoomSlider")
        self.zoom_slider.setRange(0, ZOOM_SLIDER_RES)
        self.zoom_slider.setFixedWidth(140)
        self.zoom_slider.setSingleStep(max(1, ZOOM_SLIDER_RES // 100))
        self.zoom_slider.setPageStep(max(1, ZOOM_SLIDER_RES // 20))
        self.zoom_slider.setValue(pps_to_slider_value(self.pixels_per_second))
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider_changed)
        top.addWidget(self.zoom_slider)

        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setObjectName("zoomButton")
        self.zoom_in_button.setFixedWidth(28)
        self.zoom_in_button.setToolTip(
            f"Zoom in (max step: {ZOOM_MIN_STEP_SEC*1000:.0f} ms)"
        )
        self.zoom_in_button.clicked.connect(self._on_zoom_in_clicked)
        top.addWidget(self.zoom_in_button)

        self._update_zoom_slider_tooltip()

        self._playhead_label = QLabel("00:00")
        self._playhead_label.setObjectName("playheadLabel")
        self._playhead_label.setToolTip("Current playhead position")
        top.addWidget(self._playhead_label)

        outer.addWidget(header)

        # Overview bar — compact strip showing all segments, used for fast
        # navigation and as the focus-mode entry/exit affordance.
        self.overview_bar = OverviewBar()
        self.overview_bar.segment_clicked.connect(self._on_overview_segment_clicked)
        self.overview_bar.empty_clicked.connect(self._on_overview_empty_clicked)
        outer.addWidget(self.overview_bar)

        # Body with timeline view
        body = QWidget()
        body.setObjectName("PanelRoot")
        root = QVBoxLayout(body)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)
        outer.addWidget(body, 1)

        self.scene = QGraphicsScene(self)
        self.scene.setBackgroundBrush(QColor("#141414"))
        self.scene.setSceneRect(0, 0, 3600, self._SCENE_H)
        self.view = TimelineView(self.scene, self.pixels_per_second, self)
        self.view.setObjectName("timelineView")
        self.view.media_dropped_at.connect(self.create_segment_requested.emit)
        self.view.empty_clicked.connect(self._on_empty_clicked)
        self.view.playhead_scrubbed.connect(self._on_playhead_scrubbed)
        self.view.segment_double_clicked.connect(self._on_segment_double_clicked)
        self.view.viewport_resized.connect(self._on_view_resized)
        self.view.zoom_requested.connect(self._on_zoom_requested)
        self.scene.selectionChanged.connect(self._on_selection_changed)
        root.addWidget(self.view, 1)

        self._playhead: QGraphicsLineItem | None = None

        # Loading animation timer for waveform extraction.
        self._waveform_loading_timer = QTimer(self)
        self._waveform_loading_timer.setInterval(400)
        self._waveform_loading_timer.timeout.connect(self._tick_waveform_loading)

    # ------------------------------------------------------------------
    # Zoom — log-scale, tied to the header slider/+/−/Fit controls.
    # ------------------------------------------------------------------
    def _apply_zoom(self, pps: float, *, exit_focus: bool = True) -> None:
        """Set the timeline's pixels-per-second and refresh the scene.

        Always clamps into [`ZOOM_MIN_PPS`, `ZOOM_MAX_PPS`] and keeps the
        header slider in sync.  ``exit_focus`` is False only for the
        Ctrl+wheel path which manages its own focus-exit + scrollbar
        adjustment around the zoom.
        """
        if exit_focus and self._focus_segment_id is not None:
            self.exit_focus_mode()
        pps = max(ZOOM_MIN_PPS, min(ZOOM_MAX_PPS, float(pps)))
        self.pixels_per_second = pps
        self._effective_pps = pps
        self.view._pps = pps
        self._sync_zoom_slider()
        self.refresh()

    def _sync_zoom_slider(self) -> None:
        """Push the current pps to the slider without re-firing the handler."""
        if not hasattr(self, "zoom_slider"):
            return
        target = pps_to_slider_value(self.pixels_per_second)
        if self.zoom_slider.value() == target:
            self._update_zoom_slider_tooltip()
            return
        blocked = self.zoom_slider.blockSignals(True)
        try:
            self.zoom_slider.setValue(target)
        finally:
            self.zoom_slider.blockSignals(blocked)
        self._update_zoom_slider_tooltip()

    def _update_zoom_slider_tooltip(self) -> None:
        if not hasattr(self, "zoom_slider"):
            return
        step = pick_major_step(self.pixels_per_second)
        if step >= 60:
            label = f"{step/60:g} min/tick"
        elif step >= 1:
            label = f"{step:g}s/tick"
        else:
            label = f"{step*1000:g} ms/tick"
        self.zoom_slider.setToolTip(
            f"Zoom: 1 ruler tick = {label}\n"
            f"(min: {ZOOM_MAX_STEP_SEC/60:.0f} min/tick, "
            f"max: {ZOOM_MIN_STEP_SEC*1000:.0f} ms/tick)"
        )

    def _on_zoom_slider_changed(self, value: int) -> None:
        self._apply_zoom(slider_value_to_pps(value))

    def _on_zoom_in_clicked(self) -> None:
        # 1.25× per click — log-scale step that matches the slider's feel.
        self._apply_zoom(self.pixels_per_second * 1.25)

    def _on_zoom_out_clicked(self) -> None:
        self._apply_zoom(self.pixels_per_second / 1.25)

    def _on_zoom_fit_clicked(self) -> None:
        """Compute a pps that makes the whole timeline visible at once."""
        if self._focus_segment_id is not None:
            self.exit_focus_mode()
        end_sec = self._timeline_end_sec()
        if end_sec <= 0:
            # Nothing on the timeline yet — fall back to a comfortable default.
            self._apply_zoom(60.0)
            return
        viewport_w = max(200, self.view.viewport().width())
        # Reserve a bit of left/right padding so the start/end aren't flush
        # against the viewport edges.
        padding_px = 32.0
        usable_px = max(40.0, viewport_w - padding_px * 2)
        pps = usable_px / max(0.01, end_sec)
        self._apply_zoom(pps)
        self.view.horizontalScrollBar().setValue(0)

    def _timeline_end_sec(self) -> float:
        """Furthest time that has any content (segments or waveform)."""
        end = 0.0
        if self._project:
            for segment in self._project.segments:
                end = max(end, segment.end_time_sec)
        if self._waveform_duration_sec > 0:
            end = max(end, self._waveform_duration_sec)
        return end

    def _add_label(self, text: str, x: float, y: float, color: str) -> None:
        """Add a simple text label using an explicit constructor.

        We use this instead of QGraphicsScene.addSimpleText() because under
        certain Qt builds the convenience wrapper has been observed to return
        a wrong item subclass, breaking subsequent setBrush() calls.
        """
        item = QGraphicsSimpleTextItem(text)
        item.setBrush(QBrush(QColor(color)))
        item.setPos(x, y)
        self.scene.addItem(item)

    def _draw_ruler(self) -> None:
        width = self.scene.sceneRect().width()
        # Ruler background strip
        self.scene.addRect(
            0, 0, width, 22,
            QPen(QColor("#1a1a1a")),
            QBrush(QColor("#171717")),
        )
        # Pick a "nice" major-tick interval that lands roughly every
        # TARGET_MAJOR_PX pixels at the current pps.  Minor ticks are
        # 1/4 of a major step.  This works seamlessly across the entire
        # zoom range (5 min/tick … 0.01s/tick).
        pps = max(0.001, self._effective_pps)
        major = pick_major_step(pps)
        minor = major / 4.0
        start_sec = max(0.0, self._offset_sec)
        end_sec = self._offset_sec + width / pps
        # Snap the iteration start to the closest minor tick at/before
        # ``start_sec`` so labels stay aligned even after horizontal
        # scrolling.  Iterating by minor (not by 1s) keeps us O(N) in the
        # number of *visible* ticks regardless of zoom.
        first = math.floor(start_sec / minor) * minor
        # Use an integer counter so floating-point drift doesn't slowly
        # offset every tick by a fraction of a pixel.
        idx = int(round(first / minor))
        end_idx = int(math.ceil(end_sec / minor)) + 1
        major_per_minor = int(round(major / minor))  # = 4
        pen_tick = QPen(QColor("#3a3a3a"))
        while idx <= end_idx:
            t = idx * minor
            x = self._time_to_x(t)
            if 0 <= x <= width:
                is_major = (idx % major_per_minor) == 0
                line_h = 14 if is_major else 7
                self.scene.addLine(x, 22 - line_h, x, 22, pen_tick)
                if is_major:
                    self._add_label(
                        format_ruler_time(t, major), x + 3, 3, "#8a8a8a"
                    )
            idx += 1

    # Timeline Y layout constants (shared by draw methods).
    _RULER_H = 22
    _SEGMENT_TRACK_Y = 24
    _SEGMENT_TRACK_H = 80
    _WAVE_TRACK_Y = 108
    _WAVE_TRACK_H = 160   # doubled from 80
    _SCENE_H = 280        # ruler + segment + waveform + padding

    def _draw_tracks(self) -> None:
        width = self.scene.sceneRect().width()
        # Segment track
        self.scene.addRect(
            0, self._SEGMENT_TRACK_Y, width, self._SEGMENT_TRACK_H,
            QPen(QColor("#1f1f1f")),
            QBrush(QColor("#181818")),
        )
        # "Segments" label — small, z above segment blocks.
        lbl = QGraphicsSimpleTextItem("Segments")
        lbl.setBrush(QBrush(QColor("#ffffff")))
        lbl.setOpacity(0.25)
        lbl.setPos(4, self._SEGMENT_TRACK_Y + 2)
        lbl.setZValue(5)
        self.scene.addItem(lbl)

        # Waveform track background
        self.scene.addRect(
            0, self._WAVE_TRACK_Y, width, self._WAVE_TRACK_H,
            QPen(QColor("#1f1f1f")),
            QBrush(QColor("#151515")),
        )
        # "Waveform" label overlay — top-left, semi-transparent.
        wlbl = QGraphicsSimpleTextItem("Waveform")
        wlbl.setBrush(QBrush(QColor("#ffffff")))
        wlbl.setOpacity(0.25)
        wlbl.setPos(4, self._WAVE_TRACK_Y + 2)
        wlbl.setZValue(5)
        self.scene.addItem(wlbl)

    def _draw_waveform(self) -> None:
        """Draw audio peaks inside the Waveform track.

        In overview mode the waveform spans from t=0 to the audio end,
        positioned so that scene-x equals `time * pps`. In focus mode the
        peaks are sliced to the focused segment's [start, end] window and
        rebased so x=0 corresponds to that segment's start (matching the
        focused segment block's position).
        """
        if not self._waveform_peaks:
            self._draw_waveform_placeholder()
            return

        peaks = self._waveform_peaks
        n = len(peaks)
        pps = self._effective_pps
        peaks_per_sec = self._waveform_peaks_per_sec
        px_per_peak = pps / peaks_per_sec
        if px_per_peak <= 0:
            return

        # Vertical geometry — vertically centred in the waveform track.
        y_top = float(self._WAVE_TRACK_Y) + 2
        y_bottom = float(self._WAVE_TRACK_Y + self._WAVE_TRACK_H) - 2
        y_center = (y_top + y_bottom) / 2.0
        half_h = (y_bottom - y_top) / 2.0

        scene_width = self.scene.sceneRect().width()

        # Time window currently visible in the scene.
        start_sec = self._offset_sec
        end_sec = start_sec + scene_width / pps
        start_idx = max(0, int(start_sec * peaks_per_sec))
        end_idx = min(n, int(end_sec * peaks_per_sec) + 1)
        if start_idx >= end_idx:
            return

        # Zoom-out: aggregate multiple peaks per pixel column.
        step = max(1, int(round(1.0 / px_per_peak))) if px_per_peak < 1.0 else 1

        # Centre zero-line spanning the visible peak window only.
        wave_end_x = (end_idx - start_idx) * px_per_peak
        self.scene.addLine(
            0, y_center, min(wave_end_x, scene_width), y_center,
            QPen(QColor("#2a2a2a"), 1),
        ).setZValue(1)

        path = QPainterPath()
        for i in range(start_idx, end_idx, step):
            end = min(end_idx, i + step)
            mn = peaks[i][0]
            mx = peaks[i][1]
            for j in range(i + 1, end):
                if peaks[j][0] < mn:
                    mn = peaks[j][0]
                if peaks[j][1] > mx:
                    mx = peaks[j][1]
            # Re-base x to 0 at the start of the visible window.
            x = (i - start_idx) * px_per_peak
            if x >= scene_width:
                break
            y_up = y_center - max(0.01, mx) * half_h
            y_dn = y_center - min(-0.01, mn) * half_h
            path.moveTo(x, y_up)
            path.lineTo(x, y_dn)

        waveform_item = QGraphicsPathItem(path)
        pen = QPen(QColor("#3bb6ff"))
        pen.setCosmetic(True)
        pen.setWidth(1)
        waveform_item.setPen(pen)
        waveform_item.setZValue(2)
        self.scene.addItem(waveform_item)

    def _draw_waveform_placeholder(self) -> None:
        """Show loading or empty placeholder in the waveform track."""
        y = self._WAVE_TRACK_Y + (self._WAVE_TRACK_H // 2) - 6
        width = self.scene.sceneRect().width()

        if self._waveform_loading:
            # Animated progress bar stub + dots text.
            dots = "." * (self._waveform_loading_dots % 4)
            self._add_label(f"Extracting waveform{dots}", 8, y, "#6b9fd4")

            # Draw a thin animated dash-line across the track center.
            y_center = float(self._WAVE_TRACK_Y + self._WAVE_TRACK_H // 2)
            seg_w = 12.0
            gap_w = 8.0
            filled = (self._waveform_loading_dots % 6) * 30.0
            x = 0.0
            while x < width:
                seg_end = min(x + seg_w, width)
                alpha = 180 if x < filled else 60
                color = QColor("#3b82f6")
                color.setAlpha(alpha)
                pen = QPen(color, 2)
                self.scene.addLine(x, y_center, seg_end, y_center, pen).setZValue(2)
                x += seg_w + gap_w
        else:
            self._add_label(
                "Drop an audio file onto the timeline to see waveform",
                8, y, "#4a4a4a",
            )

    def _tick_waveform_loading(self) -> None:
        """Called by timer to advance loading animation."""
        self._waveform_loading_dots += 1
        # Only redraw the waveform track area to avoid full scene rebuild.
        if self._waveform_loading:
            self.refresh()

    def _update_scene_width(self) -> None:
        """Resize scene to fit the visible time window.

        In overview mode the scene spans 0..(max_end + 10s padding) at
        `pixels_per_second`. In focus mode the scene is sized to exactly the
        viewport's current width and `_effective_pps` is re-derived so the
        focused segment fills the viewport with no scrolling and no stray
        space on either side. Re-fitting on every refresh keeps the segment
        snug after the user resizes the panel/splitter.
        """
        if self._focus_segment_id and self._project is not None:
            segment = self._project.get_segment(self._focus_segment_id)
            if segment is not None and segment.duration_sec > 0:
                viewport_w = max(200, self.view.viewport().width())
                new_pps = max(ZOOM_MIN_PPS, viewport_w / segment.duration_sec)
                new_pps = min(ZOOM_MAX_PPS, new_pps)
                if abs(new_pps - self._effective_pps) > 0.01:
                    self._effective_pps = new_pps
                    self.view._pps = new_pps
                self.scene.setSceneRect(0, 0, float(viewport_w), self._SCENE_H)
                return
        # The scene must be at least as wide as one viewport (so the ruler
        # always covers the visible area) and at most just enough to hold
        # the actual content + small trailing padding — guards against
        # ridiculously huge scenes when the user zooms way in/out.
        viewport_w = max(200, self.view.viewport().width())
        max_end_sec = self._timeline_end_sec()
        if max_end_sec <= 0:
            max_end_sec = 60.0  # empty project — show a default 1-min strip
        # Trailing padding scales with zoom: at low pps a fixed 10-px
        # padding is more useful than 10 seconds; at high pps we need at
        # least a viewport's worth so users can scroll past the end.
        padding_px = max(160.0, viewport_w * 0.25)
        content_px = max_end_sec * self._effective_pps + padding_px
        width_px = max(float(viewport_w), content_px)
        self.scene.setSceneRect(0, 0, width_px, self._SCENE_H)

    def _draw_segment(self, segment: Segment) -> None:
        # In focus mode hide every other segment — the user explicitly asked
        # the timeline to show "only that segment and its waveform".
        if self._focus_segment_id and segment.id != self._focus_segment_id:
            return
        x = self._time_to_x(segment.start_time_sec)
        width = max(20.0, segment.duration_sec * self._effective_pps)
        block = SegmentRectItem(segment, self._effective_pps)
        # Disable drag in focus mode so the user can't accidentally move the
        # only visible segment off the viewport while zoomed-in.
        if self._focus_segment_id is not None:
            block.setFlag(
                QGraphicsRectItem.GraphicsItemFlag.ItemIsMovable, False
            )
        block_h = self._SEGMENT_TRACK_H - 8  # fills track with 4px top+bottom margin
        block.setRect(0, 0, width, block_h)
        block.setPos(x, SegmentRectItem.SEGMENT_Y)
        color = MODE_COLORS.get(segment.mode, QColor("#3bb6ff"))
        block.setBrush(color)
        block.setPen(QPen(QColor("#0b0b0b"), 1))
        self.scene.addItem(block)
        self._block_map[segment.id] = block

        # "Has rendered video" indicator — green disc with white play
        # triangle, anchored to the block's top-left so it stays visible
        # regardless of how much the user has scrolled/zoomed.  Acts as a
        # mirror of the OverviewBar badge so the main editing surface
        # also tells the user at a glance which segments are renderable.
        label_x = 6
        if segment.video_path:
            badge_d = 14.0
            badge_x = 6.0
            badge_y = (block_h - badge_d) / 2.0
            disc = QGraphicsEllipseItem(badge_x, badge_y, badge_d, badge_d, block)
            disc.setBrush(QBrush(QColor("#22c55e")))
            disc.setPen(QPen(QColor("#ffffff"), 1.2))
            cx = badge_x + badge_d / 2.0 + 0.5
            cy = badge_y + badge_d / 2.0 + 0.5
            s = badge_d * 0.28
            tri_path = QPainterPath()
            tri_path.moveTo(cx - s * 0.7, cy - s)
            tri_path.lineTo(cx - s * 0.7, cy + s)
            tri_path.lineTo(cx + s, cy)
            tri_path.closeSubpath()
            tri = QGraphicsPathItem(tri_path, block)
            tri.setBrush(QBrush(QColor("#ffffff")))
            tri.setPen(QPen(Qt.PenStyle.NoPen))
            tri.setToolTip("This segment has a rendered video")
            disc.setToolTip("This segment has a rendered video")
            # Push label past the badge so the two don't overlap.
            label_x = int(badge_x + badge_d + 6)

        label = QGraphicsSimpleTextItem(
            f"{segment.name}  {format_seconds(segment.start_time_sec)}", block
        )
        label.setBrush(QColor("#0b0b0b"))
        label.setPos(label_x, 4)

    def _draw_playhead(self, time_sec: float) -> None:
        x = self._time_to_x(time_sec)
        self._playhead_x = x
        self.view.set_playhead_x(x)
        if self._playhead is None:
            self._playhead = self.scene.addLine(
                x,
                0,
                x,
                self.scene.sceneRect().height(),
                QPen(QColor("#ef4444"), 2),
            )
            self._playhead.setZValue(10)
            return
        try:
            self._playhead.setLine(x, 0, x, self.scene.sceneRect().height())
        except RuntimeError:
            # Defensive path when Qt object was deleted by a recent scene.clear().
            self._playhead = self.scene.addLine(
                x,
                0,
                x,
                self.scene.sceneRect().height(),
                QPen(QColor("#ef4444"), 2),
            )
            self._playhead.setZValue(10)

    def _on_selection_changed(self) -> None:
        if not self._project:
            return
        selected = self.scene.selectedItems()
        if not selected:
            self._selected_segment_id = None
            self.split_button.setEnabled(False)
            self.overview_bar.set_selected(None)
            self.segment_selected.emit(None)
            return
        block = selected[0]
        if not isinstance(block, SegmentRectItem):
            return
        segment = self._project.get_segment(block.segment_id)
        if segment is None:
            return
        self._selected_segment_id = segment.id
        self.split_button.setEnabled(True)
        self.overview_bar.set_selected(segment.id)
        self.segment_selected.emit(segment)

    def _on_empty_clicked(self) -> None:
        self.scene.clearSelection()
        self._selected_segment_id = None
        self.split_button.setEnabled(False)
        self.overview_bar.set_selected(None)
        self.segment_selected.emit(None)

    def _on_split_clicked(self) -> None:
        """Split the selected segment at the current playhead position."""
        if not self._project or not self._selected_segment_id:
            return
        segment = self._project.get_segment(self._selected_segment_id)
        if segment is None:
            return
        split_time = self._playhead_time_sec
        # Must be strictly inside the segment with at least 0.1s margin each side.
        if split_time <= segment.start_time_sec + 0.1:
            return
        if split_time >= segment.end_time_sec - 0.1:
            return
        self._do_split(segment, split_time)

    def _do_split(self, segment: "Segment", split_time: float) -> None:
        """Perform the actual split, mutate project, emit signal."""
        from copy import deepcopy
        from uuid import uuid4

        right = deepcopy(segment)
        right.id = str(uuid4())
        right.name = f"{segment.name} B"
        right.start_time_sec = split_time
        # right.end_time_sec stays unchanged (original end)
        right.render_status = segment.render_status.__class__.IDLE
        right.video_path = None
        right.last_rendered_at = None
        right.last_render_error = None
        right.thumbnail_path = None

        # Shorten the original segment to end at split point.
        original_name = segment.name
        segment.name = f"{original_name} A"
        segment.end_time_sec = split_time

        self._project.segments.append(right)
        self.segment_split.emit(segment.id, right.id)
        self.refresh()

    def sync_segment_positions(self) -> None:
        """Apply moved block positions back to segment start times.

        In focus mode segments are not draggable so this is a no-op.
        """
        if not self._project or self._focus_segment_id is not None:
            return
        for segment in self._project.segments:
            block = self._block_map.get(segment.id)
            if block is None:
                continue
            new_start = self._x_to_time(block.pos().x())
            snapped = round(new_start * 10) / 10.0
            if abs(snapped - segment.start_time_sec) > 1e-6:
                duration = segment.duration_sec or 8.0
                segment.start_time_sec = max(0.0, snapped)
                segment.end_time_sec = segment.start_time_sec + duration
                self.segment_changed.emit(segment.id)

    def _on_playhead_scrubbed(self, time_sec: float) -> None:
        """Handle user dragging/clicking playhead on timeline."""
        self.set_playhead(time_sec)
        self.playhead_seek_requested.emit(time_sec)

    # -- Focus mode signal handlers -----------------------------------------
    def _on_segment_double_clicked(self, segment_id: str) -> None:
        """Toggle focus mode: focusing the same segment twice exits focus."""
        if self._focus_segment_id == segment_id:
            self.exit_focus_mode()
            return
        self.enter_focus_mode(segment_id)

    def _on_overview_segment_clicked(self, segment_id: str) -> None:
        """Click on an overview block focuses that segment in the main view."""
        self.enter_focus_mode(segment_id)

    def _on_overview_empty_clicked(self) -> None:
        """Click empty area of overview exits focus mode (back to overview)."""
        self.exit_focus_mode()

    def _on_zoom_requested(self, factor: float, viewport_x: float) -> None:
        """Ctrl+scroll zoom, keeping the time under the cursor stationary.

        In focus mode the zoom changes the base pps so the focused segment
        is no longer zoom-to-fit; the user is now manually zoomed. We exit
        focus mode transparently so normal scrolling works afterwards.
        """
        if self._focus_segment_id is not None:
            # Exit focus without re-centering so the current view stays.
            self._focus_segment_id = None

        # Time under the cursor before zoom (scene_x / pps + offset).
        scene_x = self.view.mapToScene(int(viewport_x), 0).x()
        time_at_cursor = self._x_to_time(scene_x)

        new_pps = max(ZOOM_MIN_PPS, min(ZOOM_MAX_PPS, self.pixels_per_second * factor))
        # Update state via the shared path so the slider stays in sync;
        # ``exit_focus=False`` because we just exited above without the
        # full re-center the helper would otherwise apply.
        self._apply_zoom(new_pps, exit_focus=False)
        # Keep the same time at the cursor after the refresh:
        #   new_scene_x = (time - offset) * new_pps
        # We want new_scene_x == viewport_x + scrollbar_value, so solve.
        new_scene_x = (time_at_cursor - self._offset_sec) * new_pps
        new_scroll = max(0, int(new_scene_x - viewport_x))
        self.view.horizontalScrollBar().setValue(new_scroll)

    def _on_view_resized(self) -> None:
        """Re-fit the focused segment when the viewport changes size.

        Only matters in focus mode where the segment must always span the
        full viewport — overview mode uses a static `pixels_per_second` and
        relies on scrollbars for navigation.

        We defer with `QTimer.singleShot(0, ...)` so Qt can finish all its
        internal resize bookkeeping (scrollbar adjustment, layout flushes)
        before we clear and rebuild the scene.  Calling `scene.clear()` while
        Qt is still mid-resize can crash the app.
        """
        if self._focus_segment_id is not None:
            QTimer.singleShot(0, self._refresh_and_reset_scroll)

    def _refresh_and_reset_scroll(self) -> None:
        """Refresh then reset scroll to x=0 (used in focus mode after resize)."""
        self.refresh()
        self.view.horizontalScrollBar().setValue(0)

