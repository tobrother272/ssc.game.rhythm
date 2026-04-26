"""Timeline panel built on QGraphicsView."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from PySide6.QtCore import QPointF, QRect, QRectF, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMenu,
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
    """Draggable timeline block bound to one segment.

    The visible fill / outline / selection halo are *not* drawn by
    this item — they are painted by the scene's ``drawBackground``
    pass (see :meth:`TimelinePanel._paint_segment_blocks`).  The
    item itself only carries the geometry, drag/select flags and
    child items (status badge + name label).

    Why?  An earlier Qt regression caused translucent / decorated
    ``QGraphicsItem`` instances to occasionally drop their painted
    output from the cache when a mouse press landed on them — the
    user could see the segment's fill briefly vanish on click.
    Moving the visuals into the background pass guarantees they are
    re-rendered on every paint event regardless of mouse handling.
    """

    # Y locked to the segment track body (ruler_h + 2px padding).
    SEGMENT_Y = 40.0

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
        # Item is invisible — visuals come from ``drawBackground``.
        # We still need a non-empty rect (set later by ``_draw_segment``)
        # so Qt's hit-testing and drag mechanics work as expected.
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setPen(QPen(Qt.PenStyle.NoPen))

    def itemChange(self, change, value):  # type: ignore[override]
        if change == QGraphicsRectItem.GraphicsItemChange.ItemPositionChange:
            new_pos = value
            return QPointF(max(0.0, new_pos.x()), self.SEGMENT_Y)
        if change == QGraphicsRectItem.GraphicsItemChange.ItemPositionHasChanged:
            # Drag updates pos but no item paint runs in our scene
            # (the item itself is invisible) — invalidate the
            # background layer so the block's painted fill follows
            # the drag in real time.
            scene = self.scene()
            if scene is not None:
                try:
                    scene.invalidate(
                        scene.sceneRect(),
                        QGraphicsScene.SceneLayer.BackgroundLayer,
                    )
                except RuntimeError:
                    pass
        if change == QGraphicsRectItem.GraphicsItemChange.ItemSelectedHasChanged:
            # Selection halo is painted in ``_paint_segment_blocks``
            # — repaint when select state flips so the halo appears
            # / disappears immediately.
            scene = self.scene()
            if scene is not None:
                try:
                    scene.invalidate(
                        scene.sceneRect(),
                        QGraphicsScene.SceneLayer.BackgroundLayer,
                    )
                except RuntimeError:
                    pass
        return super().itemChange(change, value)

    def paint(self, painter, option, widget=None):  # type: ignore[override]
        # No-op: fill, outline, and selection state are painted by
        # :meth:`TimelinePanel._paint_segment_blocks` during the
        # scene's ``drawBackground`` pass so the visuals can never be
        # hidden by mouse events on this item.  Children (badge,
        # label) paint themselves on top of the background layer
        # via Qt's normal item recursion.
        return


class WaveformThresholdLine(QGraphicsRectItem):
    """Threshold line over the waveform — **double-click to focus**, then drag.

    The bar is thin (1 px, one-third of the old 3-px stroke) so it
    does not obscure the audio.  ``ItemIsMovable`` stays **off** until
    the user double-clicks the line: then a yellow halo highlights it
    and vertical dragging is enabled.  Double-click again on the same
    line to drop focus (and end drag mode).

    Y is constrained to ``[wy_top .. wy_bottom - LINE_BASELINE_OFFSET]``.
    X is locked to the segment's visible-clipped range.

    Live drag updates ``panel._on_threshold_line_moved``; persistence
    still batches on release / blur via ``_on_threshold_line_drag_finished``.
    """

    # Cosmetic stroke — bumped to 2 px (2× the previous 1-px) so the
    # bar is easy to see and click without obscuring the audio.
    LINE_THICKNESS        = 2.0
    HIT_HALF_HEIGHT       = 12.0
    HANDLE_SIZE           = 12.0
    LINE_BASELINE_OFFSET  = 3.0    # min px above baseline so it stays visible
    LABEL_BG_HEIGHT       = 16.0   # pill label height (drawn above the bar)
    LINE_COLOR            = QColor(255, 60, 60)
    FOCUS_HALO_COLOR      = QColor(255, 214, 10, 110)

    def __init__(
        self,
        panel: "TimelinePanel",
        segment_id: str,
        x_left: float,
        x_right: float,
        wy_top: float,
        wy_bottom: float,
        threshold: float,
    ) -> None:
        width = max(1.0, float(x_right) - float(x_left))
        super().__init__(
            0.0,
            -self.HIT_HALF_HEIGHT,
            width,
            2 * self.HIT_HALF_HEIGHT,
        )
        self._panel = panel
        self._segment_id = segment_id
        self._x_left = float(x_left)
        self._wy_top = float(wy_top)
        self._wy_bottom = float(wy_bottom)
        self._width = float(width)
        self._threshold = max(0.0, min(1.0, float(threshold)))
        self._suppress_emit = False
        # The line is always interactive — no double-click toggle, no
        # disappearing on focus changes.  ``_interaction_focused`` is
        # kept as a constant ``True`` purely so legacy code paths
        # (paint halo / drag-finish gating) still see the same state.
        self._interaction_focused = True
        self._drag_dirty = False
        self.setBrush(QBrush(QColor(0, 0, 0, 0)))
        self.setPen(Qt.PenStyle.NoPen)
        self.setZValue(15)  # above waveform fill (3) + outline (4)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True
        )
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True
        )
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip(
            "Drag vertically to set the amplitude threshold.\n"
            "Beats under the line are dimmed and skipped at render."
        )
        self.set_threshold(threshold)

    # ── Custom paint (no child items) ───────────────────────────────
    # Drawing the line / handles / pill ourselves keeps the entire
    # WaveformThresholdLine a single QGraphicsItem from Qt's POV —
    # which is critical because child items intercept mouse presses
    # and Qt's scene does NOT auto-fall-through to the parent when a
    # child has ``setAcceptedMouseButtons(Qt.NoButton)`` (it walks
    # SIBLINGS, not the parent chain).  With zero children, every
    # click in the 20-px-tall drag zone reaches our ``itemChange``
    # directly via ``ItemIsMovable``.
    def boundingRect(self):  # type: ignore[override]
        # Extend the bounding rect upward so the "thr 0.42" pill drawn
        # above the bar isn't clipped (and gets repainted on threshold
        # changes).  Width is ``self._width``; height is the 20-px hit
        # zone plus the pill above plus a 1-px gap.
        pill_h = self.LABEL_BG_HEIGHT + 1.0
        return QRectF(
            -2.0,
            -self.HIT_HALF_HEIGHT - pill_h,
            self._width + 4.0,
            2 * self.HIT_HALF_HEIGHT + pill_h,
        )

    def shape(self):  # type: ignore[override]
        # ``QGraphicsRectItem.shape()`` still follows the *constructor*
        # rect, not our wider :meth:`boundingRect`, so clicks on the
        # pill / handles would miss the item.  Match hit-testing to the
        # painted bounds so the whole control is one draggable surface.
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def paint(self, painter, option, widget=None):  # type: ignore[override]
        # No-op: the focus halo, main red stroke, end-handle squares
        # and "thr 0.42" pill are all painted by
        # :meth:`TimelinePanel._paint_threshold_lines` during the
        # scene's ``drawBackground`` pass.  Painting in the
        # background guarantees the visuals are regenerated on every
        # repaint so the bar can never be hidden by a click landing
        # on it (the same Qt cache regression that motivated the
        # waveform / segment-block / beat-tick refactors).
        return

    # ── Geometry helpers ────────────────────────────────────────────
    def _y_for_threshold(self, threshold: float) -> float:
        """Map a 0..1 threshold to a scene Y in the waveform track.

        ``threshold = 0`` → bar sits ``LINE_BASELINE_OFFSET`` px above
        ``wy_bottom`` (so it never melts into the baseline);
        ``threshold = 1`` → bar sits at ``wy_top`` (filter everything).
        """
        thr = max(0.0, min(1.0, float(threshold)))
        usable_bottom = self._wy_bottom - self.LINE_BASELINE_OFFSET
        return usable_bottom - thr * (usable_bottom - self._wy_top)

    def _threshold_for_y(self, y: float) -> float:
        usable_bottom = self._wy_bottom - self.LINE_BASELINE_OFFSET
        span = max(1e-6, usable_bottom - self._wy_top)
        thr = (usable_bottom - float(y)) / span
        return max(0.0, min(1.0, thr))

    def _update_label(self, threshold: float) -> None:
        """Cache the new threshold value and force a repaint of the pill.

        With the visuals painted in the scene's ``drawBackground`` pass
        we invalidate the background layer instead of calling
        :meth:`QGraphicsItem.update` (which would only refresh the
        item's own paint output — and that is intentionally a no-op).
        """
        self._threshold = max(0.0, min(1.0, float(threshold)))
        scene = self.scene()
        if scene is not None:
            try:
                scene.invalidate(
                    scene.sceneRect(),
                    QGraphicsScene.SceneLayer.BackgroundLayer,
                )
            except RuntimeError:
                pass

    def set_threshold(self, threshold: float) -> None:
        """Reposition without firing the panel callback (used during refresh)."""
        self._suppress_emit = True
        try:
            self.setPos(self._x_left, self._y_for_threshold(threshold))
            self._update_label(threshold)
        finally:
            self._suppress_emit = False

    # ── Qt overrides ────────────────────────────────────────────────
    def itemChange(self, change, value):  # type: ignore[override]
        if change == QGraphicsRectItem.GraphicsItemChange.ItemPositionChange:
            new_pos = value
            usable_bottom = self._wy_bottom - self.LINE_BASELINE_OFFSET
            clamped_y = max(self._wy_top,
                            min(usable_bottom, float(new_pos.y())))
            return QPointF(self._x_left, clamped_y)
        if change == QGraphicsRectItem.GraphicsItemChange.ItemPositionHasChanged:
            if (
                not self._suppress_emit
                and self._interaction_focused
            ):
                self._drag_dirty = True
                thr = self._threshold_for_y(self.pos().y())
                self._update_label(thr)
                self._panel._on_threshold_line_moved(
                    self._segment_id, thr
                )
        try:
            return super().itemChange(change, value)
        except RuntimeError:
            # The scene can clear / delete this line mid-gesture; Qt may
            # still call ``itemChange`` (e.g. on mouse move) on the C++ item.
            return value

    def set_interaction_focus(self, on: bool) -> None:
        """No-op kept for legacy callers.

        The line is always interactive (always movable, always shows
        the focus halo / handles).  Older code paths called this to
        toggle a double-click "focus mode" — that mode caused the
        line to vanish on double-click in some corner cases, so it
        was removed.  We still flush persistence on a *blur* request
        if a drag is dirty so the bar's last position is saved.
        """
        if not on and self._drag_dirty:
            self._panel._on_threshold_line_drag_finished(self._segment_id)
            self._drag_dirty = False

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        """After a drag, flush project save **after** Qt unwinds.

        Persistence triggers ``MainWindow._on_project_changed`` which
        ``scene.clear()``s every item — including this one.  Calling
        ``super().mouseReleaseEvent`` on an already-deleted C++ wrapper
        then raises ``RuntimeError`` (PySide6).  We let Qt's release
        path complete first by deferring the emit to the next event-
        loop tick, and we wrap ``super`` in a ``try/except`` so the
        rare case of a refresh fired from elsewhere never bubbles up.
        """
        flush = (
            event.button() == Qt.MouseButton.LeftButton
            and self._drag_dirty
        )
        if flush:
            self._drag_dirty = False
            seg_id = self._segment_id
            panel = self._panel
            QTimer.singleShot(
                0,
                lambda: panel._on_threshold_line_drag_finished(seg_id),
            )
        try:
            super().mouseReleaseEvent(event)
        except RuntimeError:
            pass


class BeatStripBgItem(QGraphicsRectItem):
    """Invisible hit-test rectangle for one segment's beat-event row.

    The visible RGB 65/65/65 strip + RGB 140/140/140 1-px border that
    used to be painted by this item is now drawn during the scene's
    ``drawBackground`` pass (see
    :meth:`TimelinePanel._paint_beat_strip_decorations`).  This item
    keeps the same geometry purely so Qt can route mouse events to
    it: the user's double-click "insert beat" gesture and the
    right-click "add beat block" menu both rely on the press landing
    on a real :class:`QGraphicsRectItem`.

    Painting moved to the background to dodge a Qt regression where
    a click on a translucent / decorated item would occasionally
    remove the item's paint output until the next forced repaint.
    The decoration was the entire visible part of the strip; with
    that gone, the only Qt item left here is invisible and clicks
    cannot "hide" anything visually.
    """

    def __init__(
        self,
        panel: "TimelinePanel",
        segment_id: str,
        rect: QRectF,
    ) -> None:
        super().__init__(rect)
        self._panel = panel
        self._segment_id = segment_id
        # Fully transparent — visuals come from drawBackground.
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setZValue(10)
        self.setToolTip("Double-click to insert a beat event")

    def mouseDoubleClickEvent(self, event):  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        scene_x = float(event.scenePos().x())
        self._panel._on_beat_strip_double_clicked(
            self._segment_id, scene_x
        )
        event.accept()

    def contextMenuEvent(self, event):  # type: ignore[override]
        """Right-click on the strip → "Add Beat Block" menu.

        Mirrors the double-click "insert here" gesture but is more
        discoverable: every desktop user knows to right-click for a
        contextual menu, while double-clicking the empty strip is a
        hidden shortcut.  The action ALWAYS inserts (no near-existing
        guard) because picking a menu entry is an explicit choice —
        unlike a stray double-click that may have been meant for a
        nearby tick.
        """
        menu = QMenu()
        act_add = menu.addAction("Add Beat Block")
        chosen = menu.exec(event.screenPos())
        event.accept()
        if chosen is act_add:
            scene_x = float(event.scenePos().x())
            self._panel._on_beat_strip_add_requested(
                self._segment_id, scene_x
            )


class BeatTickItem(QGraphicsRectItem):
    """Interactive vertical tick on the BEAT-DBG strip.

    The item itself is **invisible** — its only job is to host the
    drag / select / focus logic for one beat event.  The vertical
    stroke (3 px idle, 5 px selected) and optional index-number
    label are painted by the scene's ``drawBackground`` pass via
    :meth:`TimelinePanel._paint_beat_ticks`.  The bounding rect
    stays a ±``HIT_HALF_WIDTH``-pixel hit zone so dragging is
    comfortable even when ticks pile up at high zoom.

    Why move the visuals?  An earlier user video showed beat-strip
    ticks vanishing when the user clicked on the strip — Qt's hit
    path occasionally drops translucent / decorated child items
    (the ``QGraphicsLineItem`` we used to nest here) from the paint
    cache when a press lands on their parent.  Painting in
    ``drawBackground`` makes the stroke immune to that regression
    because it is regenerated on every paint event.

    The item is movable on the X axis only (Y is locked to
    ``tick_top`` in scene coords) and selectable so the Delete-key
    shortcut hooks in cleanly.  All commits are routed back to the
    owning :class:`TimelinePanel` which mutates ``_beat_events`` and
    emits the persistence signal — the item itself stays
    display-only.
    """

    # Hit halo stays comfortable (12 px on each side ⇒ 24-px wide
    # drag zone) so dragging is easy even when ticks pile up at high
    # zoom.  The stroke itself was halved from 6/10 px to 3/5 px so
    # the markers don't dominate the beat strip while remaining
    # easy to see (3-px idle, 5-px when selected).
    HIT_HALF_WIDTH = 12.0
    TICK_WIDTH_IDLE = 3.0
    TICK_WIDTH_SELECTED = 5.0
    # Pixel distance the cursor must travel before a click is treated as
    # a real drag-retime.  Anything smaller is absorbed as click-jitter
    # (and during double-clicks the cursor easily wanders 2-4 px between
    # the two presses).  Below this threshold, on release we also SNAP
    # the tick back to its press-x so any micro-shift Qt's default drag
    # handler imposed never reaches the user as a "tick disappearing
    # for a frame and reappearing one pixel over" flicker.
    DRAG_COMMIT_PX = 6.0

    def __init__(
        self,
        panel: "TimelinePanel",
        segment_id: str,
        event_idx: int,
        kind: str,
        color: QColor,
        tick_top: float,
        tick_bottom: float,
        x_min: float,
        x_max: float,
        idx_label: Optional[str] = None,
        num_y: Optional[float] = None,
    ) -> None:
        # Local coords: the item is positioned via ``setPos(scene_x,
        # tick_top)`` so local origin (0, 0) aligns with the top of the
        # tick line. The bounding rect must include the label area
        # above so the painted label isn't clipped.
        line_height = float(tick_bottom) - float(tick_top)
        if idx_label and num_y is not None:
            label_top_local = float(num_y) - float(tick_top) - 2.0
        else:
            label_top_local = 0.0
        local_top = min(0.0, label_top_local)
        local_height = line_height - local_top
        super().__init__(
            -self.HIT_HALF_WIDTH,
            local_top,
            2 * self.HIT_HALF_WIDTH,
            local_height,
        )
        self._panel = panel
        self._segment_id = segment_id
        self._event_idx = event_idx
        self._kind = kind
        self._base_color = QColor(color)
        self._x_min = float(x_min)
        self._x_max = float(x_max)
        self._scene_y_top = float(tick_top)
        # Visual data read by ``_paint_beat_ticks`` — line height,
        # label text, and label offset are computed once here and
        # painted from the scene's background pass on every repaint
        # so the stroke can never be lost to Qt's hit-test cache.
        self._line_height = float(line_height)
        self._label_text: Optional[str] = idx_label
        self._label_top_local = float(label_top_local)
        # Fully transparent — visuals come from drawBackground.
        self.setPen(Qt.PenStyle.NoPen)
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True
        )
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True
        )
        self.setFlag(
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True
        )
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setZValue(12)

    # -- Qt overrides ---------------------------------------------------
    def _invalidate_background(self) -> None:
        """Force the scene's background pass to repaint this tick."""
        scene = self.scene()
        if scene is not None:
            try:
                scene.invalidate(
                    scene.sceneRect(),
                    QGraphicsScene.SceneLayer.BackgroundLayer,
                )
            except RuntimeError:
                pass

    def itemChange(self, change, value):  # type: ignore[override]
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            new = QPointF(value)
            x = max(self._x_min, min(self._x_max, float(new.x())))
            return QPointF(x, self._scene_y_top)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            # Live-drag: the painted stroke / label live in
            # drawBackground, so refresh the background layer to
            # follow the drag in real time.
            self._invalidate_background()
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            # Stroke width depends on selection state — repaint the
            # background so the bold/un-bold flips immediately.
            self._invalidate_background()
        try:
            return super().itemChange(change, value)
        except RuntimeError:
            return value

    def paint(self, painter, option, widget=None):  # type: ignore[override]
        # No-op: the visible stroke + label are painted by
        # :meth:`TimelinePanel._paint_beat_ticks` during the scene's
        # ``drawBackground`` pass so they cannot be hidden by mouse
        # events on this item.
        return

    def mousePressEvent(self, event):  # type: ignore[override]
        """Take focus when the user clicks the tick.

        Clicking a tick is the primary "I want to edit this beat"
        gesture — it stamps the panel's :attr:`_focused_beat` so
        arrow-key nudging knows which event to retime, and it pulls
        keyboard focus onto the view so those arrow presses actually
        reach our :meth:`TimelineView.keyPressEvent` instead of being
        eaten by whatever widget last had focus (typically the
        toolbar buttons).  Selection / drag arming is then handed
        back to Qt's default handler.

        We also remember the press X so the matching release can
        decide whether the user actually dragged or merely clicked
        — see :meth:`mouseReleaseEvent`.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_press_x = float(self.pos().x())
            events = self._panel._beat_events.get(self._segment_id, [])
            if 0 <= self._event_idx < len(events):
                t_local = float(events[self._event_idx][0])
                self._panel._set_focused_beat(
                    self._segment_id, t_local
                )
            scene = self.scene()
            if scene is not None:
                for v in scene.views():
                    v.setFocus(Qt.FocusReason.MouseFocusReason)
                    break
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        super().mouseReleaseEvent(event)
        # Only commit if the tick actually moved by more than
        # :attr:`DRAG_COMMIT_PX` since the press.  Below that we treat
        # the gesture as a click (single or part of a double-click)
        # and SNAP the tick back to the press-x so Qt's default drag
        # handler can't leave the tick sitting a couple of pixels off
        # while we wait for the next refresh.  Without the snap-back,
        # a stray 2-3 px jitter during a double-click landed on
        # ``mouseReleaseEvent`` first, scheduled a deferred
        # ``refresh()`` (destroying and recreating the tick), and the
        # user perceived the tick as "vanishing then reappearing
        # shifted" — exactly the bug reported.
        new_x = float(self.pos().x())
        press_x = getattr(self, "_drag_press_x", None)
        self._drag_press_x = None
        if press_x is None or abs(new_x - press_x) < self.DRAG_COMMIT_PX:
            if press_x is not None and abs(new_x - press_x) > 0.05:
                # Snap back inside ``ItemSendsGeometryChanges`` —
                # ``itemChange`` will simply pass the value through
                # since the X is already inside ``_x_min/_x_max``.
                try:
                    self.setPos(
                        QPointF(float(press_x), float(self._scene_y_top))
                    )
                except RuntimeError:
                    pass
            return
        self._panel._on_beat_tick_drag_finished(
            self._segment_id, self._event_idx, new_x
        )

    def mouseDoubleClickEvent(self, event):  # type: ignore[override]
        """Stay focused on double-click without flickering.

        Qt's default :meth:`QGraphicsItem.mouseDoubleClickEvent`
        re-fires :meth:`mousePressEvent`, which makes the scene
        re-run its selection state machine and re-arms the drag
        tracker.  On an already-selected movable item that
        manifests as a brief
        ``Selected → (cleared) → Selected`` toggle — each toggle
        re-invalidates the background pass that paints the tick, so
        the user briefly sees the line collapse from the 10-px
        "selected" stroke to the 6-px "idle" one and back.
        Combined with any sub-pixel cursor jiggle (which arms a tiny
        drag and triggers a deferred :meth:`refresh` that destroys +
        recreates the C++ item), the tick *visually disappears* for
        one or two frames.

        We override this to a no-op (apart from re-asserting the
        focus / selection state) so the double-click is now a pure
        "enter edit mode" gesture: the tick stays selected, the
        panel records the focused beat, the view grabs keyboard
        focus for arrow-key nudging, and Qt never re-runs scene
        selection on this item.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            if not self.isSelected():
                self.setSelected(True)
            events = self._panel._beat_events.get(self._segment_id, [])
            if 0 <= self._event_idx < len(events):
                t_local = float(events[self._event_idx][0])
                self._panel._set_focused_beat(
                    self._segment_id, t_local
                )
            scene = self.scene()
            if scene is not None:
                for v in scene.views():
                    v.setFocus(Qt.FocusReason.MouseFocusReason)
                    break
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):  # type: ignore[override]
        menu = QMenu()
        act_delete = menu.addAction("Delete")
        kind_menu = menu.addMenu("Set kind")
        # Common rhythm-mode kinds covering punch + dance + relax + wall.
        # Line-mode kinds (ZL*/ZR*/ZS*) are intentionally omitted — they
        # are rare and the user can edit the JSON directly if needed.
        common_kinds = (
            "L", "R", "LL", "RR",
            "DL", "DR", "JL", "JR",
            "PL", "PR", "JP", "SQ", "W",
        )
        for k in common_kinds:
            act = kind_menu.addAction(k)
            act.setCheckable(True)
            act.setChecked(k == self._kind)
            act.setData(k)
        chosen = menu.exec(event.screenPos())
        event.accept()
        if chosen is None:
            return
        if chosen is act_delete:
            self._panel._on_beat_tick_delete_requested(
                self._segment_id, self._event_idx
            )
            return
        new_kind = chosen.data()
        if new_kind:
            self._panel._on_beat_tick_kind_changed(
                self._segment_id, self._event_idx, str(new_kind)
            )


class TimelineScene(QGraphicsScene):
    """Scene that paints the waveform chart in :meth:`drawBackground`.

    The waveform RMS envelope used to be assembled out of four
    ``QGraphicsItem`` instances (track-bg rectangle, baseline, fill
    polygon, outline polyline).  Single-clicks inside the waveform
    area kept "removing" one of those layers per click — fill first,
    then outline, until only the dark track background remained;
    resizing the panel or clicking the timeline header brought them
    all back.  The chart was *visually* gone but the items were still
    in the scene — a Qt rendering / hit-test regression where mouse
    presses on a translucent item with ``setAcceptedMouseButtons(NoButton)``
    occasionally invalidates the item's paint cache without a matching
    repaint.

    Painting the chart in the scene's *background pass* avoids the
    issue entirely: ``drawBackground`` runs on every viewport paint
    event, has no per-item state Qt can lose, and is not affected by
    selection / focus / hit-test paths.  The user's reported "click
    hides the waveform" bug becomes structurally impossible because
    the waveform is no longer an item that *can* be hidden.

    The interactive overlays (red ``WaveformThresholdLine``, beat
    ticks, segment blocks, playhead) stay as scene items so their
    drag / select behaviour is unchanged.
    """

    def __init__(self, panel: "TimelinePanel") -> None:
        super().__init__(panel)
        self._panel = panel

    def drawBackground(self, painter, rect):  # type: ignore[override]
        super().drawBackground(painter, rect)
        # ``_panel`` is set before any paint event happens (the panel
        # constructs the scene first, then wires the view), but guard
        # anyway so a stray paint during teardown can't crash.
        try:
            # Paint *all* non-interactive decorations here so they can
            # never be hidden / removed by mouse events on scene items
            # (Qt's hit-test path occasionally drops translucent items
            # from the paint cache when an empty press lands on them —
            # see :class:`TimelineScene` docstring).  Order matches
            # zValue stacking the items used to have:
            #   1. segment / waveform track strips + section labels
            #   2. segment block fills + outlines + selection halos
            #   3. waveform chart (bg + baseline + fill + outline)
            #   4. per-segment beat-strip backgrounds
            #   5. rule-mode dashed guide lines
            #   6. white beat-strip "now" cursor
            #   7. beat-event tick strokes + index labels
            #   8. waveform threshold line (halo + stroke + handles + pill)
            # Interactive overlays (segment block hit zones, beat
            # tick hit zones, threshold-line hit zones, playhead,
            # segment children like badge / label) are still scene
            # items and paint on top of this background pass.
            self._panel._paint_track_decorations(painter, rect)
            self._panel._paint_segment_blocks(painter, rect)
            self._panel._paint_waveform_background(painter, rect)
            self._panel._paint_beat_strip_decorations(painter, rect)
            self._panel._paint_beat_ticks(painter, rect)
            self._panel._paint_threshold_lines(painter, rect)
        except RuntimeError:
            pass


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
        # Set right after construction by :class:`TimelinePanel` —
        # ``self.parent()`` does NOT return the panel because adding
        # the view to a layout reparents it to the layout's owner
        # (an internal PanelRoot QWidget), so we keep a direct
        # reference here for keyboard / focus dispatch.
        self._panel_ref: Optional["TimelinePanel"] = None
        # When False, mouse clicks/drags on the timeline do NOT scrub the
        # red playhead.  Toggled by :class:`MainWindow` whenever the
        # preview player enters / leaves the StoppedState so a stopped
        # video doesn't surprise-jump every time the user clicks the
        # ruler or waveform area while doing other work.  Defaults to
        # False because Qt's QMediaPlayer starts in StoppedState and
        # ``playbackStateChanged`` won't fire for that initial value.
        self._scrub_enabled = False
        self.setAcceptDrops(True)
        self.setRenderHints(self.renderHints())
        # Default QGraphicsView aligns the scene to AlignCenter — that left
        # symmetric black gutters on both sides whenever the scene was even
        # slightly narrower than the viewport (e.g. in focus mode where we
        # want the segment to perfectly fill the viewport). Force top-left so
        # any leftover slack is harmless and scenes stick to the left edge.
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        # ── Force full-viewport repaints ─────────────────────────────
        # Qt's default ``MinimalViewportUpdate`` only invalidates the
        # bounding rect of items that explicitly changed, then unions
        # the resulting region.  That works for opaque scenes, but our
        # waveform is layered translucent items (background ``#151515``
        # + 35 %-alpha brown fill + 1 px outline) with the
        # ``WaveformThresholdLine`` pulsing on top (its bounding rect
        # extends well above the bar to host the "thr 0.42" pill, so
        # every drag invalidates a tall slice of the chart).  The
        # combination triggers a Qt regression where the translucent
        # fill is *not* recomposited under the moved line — the user
        # sees the chart "vanish" leaving only the dark bg until the
        # next forced full repaint (panel resize, scrollbar appear,
        # clicking the segment chrome above the chart).  Switching to
        # ``FullViewportUpdate`` paints the whole visible area on every
        # change, which is the price of correctness here — the
        # timeline scene is small (a few hundred items), so the extra
        # CPU is negligible vs. the bug.
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate
        )
        # StrongFocus is the QGraphicsView default but we re-assert it
        # here so future Qt versions can't quietly downgrade us — the
        # arrow-key nudging in :meth:`keyPressEvent` only fires while
        # this view holds keyboard focus, and our tick-click handlers
        # explicitly route focus here via ``setFocus(MouseFocusReason)``.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

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
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        scene_pos = self.mapToScene(event.position().toPoint())
        hit_item = self.itemAt(event.position().toPoint())
        on_tick = isinstance(hit_item, BeatTickItem)
        on_segment = isinstance(hit_item, SegmentRectItem)
        # Threshold line (no child items — hit is the line itself).
        thr_line_hit: Optional[WaveformThresholdLine] = None
        _probe = hit_item
        while _probe is not None:
            if isinstance(_probe, WaveformThresholdLine):
                thr_line_hit = _probe
                break
            _probe = _probe.parentItem()
        on_threshold = thr_line_hit is not None

        # ── Waveform-area guard ───────────────────────────────────────
        # The user explicitly asked that clicks on the waveform render
        # never alter the chart (no color change, no fill loss, no
        # selection drop, no scrub).  Detect any press whose scene-Y
        # falls inside the waveform track and which is **not** routed
        # to a real interactive overlay (beat tick or threshold line)
        # — and turn it into a complete no-op.  Decoration items
        # (bg / fill / outline / baseline) all carry
        # ``setAcceptedMouseButtons(NoButton)`` already, so this guard
        # also matches when ``itemAt`` returns ``None`` because Qt
        # filtered them out of hit-testing.
        panel = self._panel_ref
        if panel is not None and not on_tick and not on_threshold and not on_segment:
            wave_top = float(panel._WAVE_TRACK_Y)
            wave_bot = wave_top + float(panel._WAVE_TRACK_H)
            if wave_top <= scene_pos.y() <= wave_bot:
                event.accept()
                return

        if self._panel_ref is not None:
            self._panel_ref._defocus_other_threshold_lines(thr_line_hit)

        if (
            self._scrub_enabled
            and not on_tick
            and not on_threshold
            and abs(scene_pos.x() - self._playhead_x) <= 8
        ):
            self._dragging_playhead = True
            self.playhead_scrubbed.emit(
                max(0.0, self._x_to_time(scene_pos.x()))
            )
            event.accept()
            return

        if (
            self._scrub_enabled
            and not on_tick
            and not on_threshold
            and scene_pos.y() <= self.sceneRect().height()
        ):
            self.playhead_scrubbed.emit(
                max(0.0, self._x_to_time(scene_pos.x()))
            )

        if not on_tick and self._panel_ref is not None:
            self._panel_ref._clear_focused_beat()

        if on_segment or on_tick or on_threshold:
            super().mousePressEvent(event)
            return

        if hit_item is None:
            self.empty_clicked.emit()

        event.accept()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._dragging_playhead and self._scrub_enabled:
            scene_pos = self.mapToScene(event.position().toPoint())
            self.playhead_scrubbed.emit(max(0.0, self._x_to_time(scene_pos.x())))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging_playhead = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        """Keyboard shortcuts for the focused / selected beat ticks.

        ``Delete`` / ``Backspace``
            Drop every selected tick in a single keystroke.  Multi-
            select via Ctrl+click is honoured — the actual mutation is
            routed through
            :meth:`TimelinePanel._on_delete_selected_beat_ticks` which
            updates ``_beat_events`` safely (descending index order per
            segment) and emits the persistence signal.

        ``←`` / ``→``
            Nudge the *focused* beat (set on click / double-click of a
            tick — see :meth:`BeatTickItem.mousePressEvent`) by 1 px
            along the X axis (or 10 px when Shift is held).  When no
            beat is focused, the keys fall through to the View's
            default scroll behaviour.  Pixel steps are converted to
            seconds via the current zoom in
            :meth:`TimelinePanel._on_arrow_nudge_selected_ticks`, so
            the on-screen feel stays consistent across zoom levels.
        """
        panel = self._panel_ref

        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            scene = self.scene()
            beat_ticks = [
                it for it in (scene.selectedItems() if scene else [])
                if isinstance(it, BeatTickItem)
            ]
            if beat_ticks:
                beat_ticks[0]._panel._on_delete_selected_beat_ticks()
                event.accept()
                return

        if (
            event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right)
            and panel is not None
            and panel._focused_beat is not None
        ):
            shift = bool(
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            )
            step_px = 10.0 if shift else 1.0
            direction = -1.0 if event.key() == Qt.Key.Key_Left else 1.0
            panel._on_arrow_nudge_selected_ticks(direction * step_px)
            event.accept()
            return

        super().keyPressEvent(event)

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
        """Double-click handling for ticks (focus) and segments (zoom-fit).

        Tick double-click is intercepted *here* — before forwarding
        to the scene — so Qt's default
        ``QGraphicsItem.mouseDoubleClickEvent`` never re-fires
        ``mousePressEvent`` on the tick.  That re-fire was the
        ultimate cause of the brief "tick disappears" flicker the
        user reported (it re-armed the drag tracker and any
        sub-pixel cursor jiggle then committed a tiny drag → refresh
        → tick destroyed and recreated).
        """
        item = self.itemAt(event.position().toPoint())

        # Walk up to find a tick (label child → tick) before checking
        # for SegmentRectItem so we don't accidentally bubble through
        # a tick into the segment underneath.
        walk = item
        tick: Optional[BeatTickItem] = None
        while walk is not None:
            if isinstance(walk, BeatTickItem):
                tick = walk
                break
            walk = walk.parentItem()
        if tick is not None:
            if not tick.isSelected():
                tick.setSelected(True)
            panel = self._panel_ref
            if panel is not None:
                events = panel._beat_events.get(tick._segment_id, [])
                if 0 <= tick._event_idx < len(events):
                    t_local = float(events[tick._event_idx][0])
                    panel._set_focused_beat(tick._segment_id, t_local)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return

        walk_thr: Optional[WaveformThresholdLine] = None
        _w = self.itemAt(event.position().toPoint())
        while _w is not None:
            if isinstance(_w, WaveformThresholdLine):
                walk_thr = _w
                break
            _w = _w.parentItem()
        if walk_thr is not None:
            event.accept()
            return

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
    # Manual beat-detection trigger.  Beat-detect runs only when the user
    # explicitly clicks the "Auto Gen Block" toolbar button — never on
    # selection / drag / form-change — so the user controls when the
    # subprocess spawns.  Carries the currently selected segment_id (or
    # empty string when nothing is selected; MainWindow ignores those).
    auto_gen_block_requested = Signal(str)  # segment_id

    # Emitted after the user mutates a segment's beat-event list via the
    # timeline strip (drag / delete / kind change / insert). The receiver
    # is responsible for copying ``timeline_panel._beat_events[segment_id]``
    # back into ``Segment.beat_events`` and triggering autosave so the
    # edits survive a reload.
    beat_events_edited = Signal(str)  # segment_id

    # Emitted when the user finishes dragging the red waveform threshold
    # line and the resulting :pyattr:`Segment.beat_height_threshold`
    # actually moved.  MainWindow listens for this and re-runs
    # :class:`BeatDetectService` so the rhythm core returns a freshly
    # filtered events list — the timeline preview then matches the
    # filtered set the rendered video will use, with no client-side
    # opacity hacks needed.  Carries ``(segment_id, threshold)``.
    beat_threshold_changed = Signal(str, float)  # segment_id, 0..1

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._block_map: dict[str, SegmentRectItem] = {}
        # Maps ``(segment_id, event_idx)`` → the BeatTickItem so the
        # background paint pass can read each tick's current
        # position / selection state without scanning ``scene.items()``.
        # Cleared in lockstep with ``_block_map`` whenever the scene
        # is rebuilt.
        self._tick_map: dict[tuple[str, int], "BeatTickItem"] = {}
        # Maps ``segment_id`` → the WaveformThresholdLine so the
        # background paint pass can render the red line + handles +
        # "thr 0.42" pill at the line's current scene position.
        # Cleared together with ``_block_map`` / ``_tick_map``.
        self._threshold_map: dict[str, "WaveformThresholdLine"] = {}
        self.pixels_per_second = 60.0  # base zoom in overview mode
        self._effective_pps = 60.0     # scaled when in focus mode
        self._offset_sec = 0.0          # >0 in focus mode (focused segment start)
        self._focus_segment_id: Optional[str] = None
        # True after the user explicitly Ctrl+wheel-zoomed inside focus
        # mode.  Once set, ``_update_scene_width`` stops auto-fitting
        # the focused segment to the viewport (which would clobber the
        # user's manual zoom on every refresh) and instead sizes the
        # scene to ``segment_duration * _effective_pps`` so they can
        # scroll horizontally at the new zoom level.  Reset whenever
        # we enter or leave focus mode so re-entering always starts
        # from a clean fit-to-viewport.
        self._focus_manual_zoom: bool = False
        self._selected_segment_id: str | None = None
        self._playhead_time_sec = 0.0
        self._playhead_x = 0.0
        # Waveform state — RMS envelope only (matches rhythm.py LINE-DEBUG).
        # Each value in [0..1], one per (1 / _waveform_rms_per_sec) seconds.
        self._waveform_audio: Optional[str] = None
        self._waveform_rms: list[float] = []
        self._waveform_rms_per_sec: int = 100
        self._waveform_duration_sec: float = 0.0
        self._waveform_loading: bool = False
        self._waveform_loading_dots: int = 0

        # Beat-event preview overlay.  Maps ``segment_id`` → list of
        # ``(time_sec_local, kind, height_0_1)`` produced by
        # ``rhythm.py --detect_only``.  ``time_sec_local`` is relative
        # to the segment's trimmed audio (== ``Segment.start_time_sec``
        # in project time). ``height_0_1`` is the per-beat audio
        # amplitude (1.0 == loudest peak in the segment) used by the
        # waveform threshold slider; legacy 2-tuples are normalised to
        # height=1.0 on entry so older projects keep every tick.
        self._beat_events: dict[
            str, list[tuple[float, str, float]]
        ] = {}
        self._beat_events_loading: set[str] = set()
        # Toggle for the "Rule" button. When ON, every beat tick draws a
        # dashed vertical guide line that extends below the strip down
        # through the waveform — makes it trivial to see whether a tick
        # lines up with the audio peaks beneath it.  Pure visual aid;
        # nothing else in the model changes.
        self._rule_mode_enabled: bool = False
        # Used by arrow-key nudges: a list of ``(segment_id, target_t)``
        # pairs marking ticks that must be re-selected after the next
        # ``refresh()`` rebuilds the scene (the QGraphicsItem instances
        # are destroyed and recreated, so Qt's selection is wiped).
        # Re-applied (not cleared) on every commit so a stream of rapid
        # arrow-key taps queueing multiple commits still ends with the
        # nudged tick selected — every other edit handler clears the
        # list to avoid stale re-selects.
        self._pending_tick_select_after_refresh: list[tuple[str, float]] = []
        # Identity of the currently *focused* beat — i.e. the one the
        # user is editing.  Stored as ``(segment_id, t_local)`` because
        # ``refresh()`` recreates every :class:`BeatTickItem` (so we
        # can't keep a Python reference to the item) and the event's
        # index in ``_beat_events[seg_id]`` may shift whenever a sort
        # runs.  ``t_local`` is the canonical key — when we need the
        # current event we look up the closest match.
        # Lifecycle:
        #   • Set on single-click, double-click and post-insert of a
        #     beat tick (see :class:`BeatTickItem` overrides + the
        #     strip double-click handler).
        #   • Updated by every commit that retimes the focused tick
        #     (drag-finished, arrow-key nudge) so the focus survives
        #     each refresh and follows the tick to its new position.
        #   • Cleared whenever the user clicks somewhere that's not a
        #     beat tick (empty area, segment block, decoration) and
        #     when the focused tick is deleted.
        self._focused_beat: Optional[tuple[str, float]] = None
        self._refresh_pending: bool = False  # guard against re-entrant refresh
        # Set True only in :meth:`_on_empty_clicked` *before*
        # ``clearSelection()``.  Used to distinguish a deliberate
        # click-to-deselect from a spurious "nothing selected" signal
        # (e.g.  ``QGraphicsItem`` was deleted mid-gesture) so we never
        # broadcast :signal:`segment_selected` ``None`` to
        # :class:`MainWindow` unless the user *actually* deselected.
        # Otherwise ``_request_waveform_for(None)`` would wipe the RMS
        # data and the waveform / inner track fill would *vanish* on
        # a harmless click on a non-selectable surface (waveform fill,
        # track chrome, etc.).
        self._intentional_segment_deselect: bool = False
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
                self._tick_map.clear()
                self._threshold_map.clear()
                self._update_scene_width()
                self._draw_ruler()
                self._draw_tracks()
                self._draw_waveform()
                self._draw_beat_events()
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
        # Fresh focus session — start in auto-fit mode until the user
        # Ctrl+wheels, at which point the manual-zoom branch takes over.
        self._focus_manual_zoom = False
        # Keep TimelineView's mouse-coord helpers consistent with our scaling.
        self.view._pps = self._effective_pps
        self.view._offset_sec = self._offset_sec
        # Auto-select the focused segment so inspector + preview follow.
        self._selected_segment_id = segment_id
        self.refresh()
        # ``refresh()`` re-applies the selection while signals are blocked,
        # so ``_on_selection_changed`` never fires for this entry path.
        # Sync action-button enable state explicitly.
        self.split_button.setEnabled(True)
        self.auto_gen_button.setEnabled(True)
        self.gen_by_chart_button.setEnabled(True)
        self.clear_beats_button.setEnabled(True)
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
        self._focus_manual_zoom = False
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
        self._waveform_rms = []
        self._waveform_duration_sec = 0.0
        self.refresh()
        if not self._waveform_loading_timer.isActive():
            self._waveform_loading_timer.start()

    def set_waveform(
        self,
        audio_path: str,
        rms: list,
        duration_sec: float,
        rms_per_sec: int = 100,
    ) -> None:
        """Attach a normalised RMS envelope and redraw the waveform track.

        ``rms`` must contain values in ``[0, 1]`` with one sample every
        ``1/rms_per_sec`` seconds (same convention as ``rhythm.py``'s
        ``line_dbg_wave``).
        """
        self._waveform_loading = False
        self._waveform_loading_timer.stop()
        self._waveform_audio = audio_path
        self._waveform_rms = list(rms) if rms is not None else []
        self._waveform_duration_sec = duration_sec
        self._waveform_rms_per_sec = max(1, rms_per_sec)
        self.refresh()

    def clear_waveform(self) -> None:
        """Clear any cached waveform and redraw empty track."""
        self._waveform_loading = False
        self._waveform_loading_timer.stop()
        self._waveform_audio = None
        self._waveform_rms = []
        self._waveform_duration_sec = 0.0
        self.refresh()

    # ── Beat-event preview API ──────────────────────────────────────────
    @staticmethod
    def _normalise_event(ev: tuple) -> tuple[float, str, float]:
        """Coerce a 2-/3-tuple beat row to canonical ``(t, kind, h)``.

        Legacy projects (and older detect runs) only carry ``(t, kind)``;
        we treat their height as 1.0 so existing ticks stay visible
        regardless of the threshold slider.  Heights coming in from
        :class:`BeatDetectService` or the project store are clamped to
        [0,1] so a corrupt JSON / stale value can't push the slider's
        domain out of range.
        """
        if len(ev) >= 3:
            t = float(ev[0]); k = str(ev[1])
            try:
                h = max(0.0, min(1.0, float(ev[2])))
            except (TypeError, ValueError):
                h = 1.0
            return (t, k, h)
        return (float(ev[0]), str(ev[1]), 1.0)

    def set_beat_events(
        self,
        segment_id: str,
        events: list,
    ) -> None:
        """Attach detected beat events for ``segment_id`` and redraw.

        ``events`` is a list of ``(time_sec, kind[, height])`` where
        ``time_sec`` is the offset within the segment's trimmed audio
        (the same convention ``rhythm.py --export_events`` uses) and
        the optional ``height`` is the audio amplitude (0..1) used by
        the waveform threshold slider.  Older 2-tuple rows are
        normalised to height=1.0 so they always pass the threshold.
        """
        self._beat_events[segment_id] = (
            [self._normalise_event(ev) for ev in events]
            if events else []
        )
        self._beat_events_loading.discard(segment_id)
        self.refresh()

    def set_beat_events_loading(self, segment_id: str) -> None:
        """Show a "detecting beats…" hint for ``segment_id``."""
        self._beat_events_loading.add(segment_id)
        self.refresh()

    def clear_beat_events(self, segment_id: Optional[str] = None) -> None:
        """Drop cached beat events for ``segment_id`` (or all if None)."""
        if segment_id is None:
            self._beat_events.clear()
            self._beat_events_loading.clear()
        else:
            self._beat_events.pop(segment_id, None)
            self._beat_events_loading.discard(segment_id)
        self.refresh()

    def get_beat_events(
        self, segment_id: str
    ) -> list[tuple[float, str]]:
        """Return a copy of the in-memory beat events for ``segment_id``.

        Used by :class:`MainWindow` after it receives a
        ``beat_events_edited`` signal so it can write the edited list
        back into ``Segment.beat_events`` and trigger autosave.
        """
        return list(self._beat_events.get(segment_id, []))

    # ── Beat-event edit hooks (called by BeatTickItem / BeatStripBgItem)
    def _set_focused_beat(self, segment_id: str, t_local: float) -> None:
        """Mark a beat as the focused / edited one.

        Called from :class:`BeatTickItem` mouse handlers (single &
        double-click) and after a strip-double-click insertion.
        Stored by ``(segment_id, t_local)`` so the focus survives the
        ``refresh()`` that wipes scene items, and so subsequent
        arrow-key nudges can always look up the current event by its
        time even after a sort has shuffled indices.
        """
        if self._project is None:
            return
        seg = self._project.get_segment(segment_id)
        if seg is None:
            return
        self._focused_beat = (segment_id, float(t_local))

    def _clear_focused_beat(self) -> None:
        """Drop the focused-beat marker.

        Triggered when the user clicks somewhere outside any beat
        tick (empty area, segment block, decoration) or when the
        focused tick itself is deleted.  Subsequent arrow-key
        presses will fall through to the View's default handler
        (which scrolls the timeline) — i.e. the timeline behaves
        normally again until a tick is re-focused.
        """
        self._focused_beat = None

    def _defocus_other_threshold_lines(
        self, keep: Optional[WaveformThresholdLine]
    ) -> None:
        """Clear edit-focus on every :class:`WaveformThresholdLine` except *keep*.

        *keep* is the line (if any) under the current mouse press; it
        keeps its focused/unfocused state so a double-click can still
        arm it.  Passing ``None`` clears **all** bars (empty click /
        segment selection).
        """
        for it in self.scene.items():
            if isinstance(it, WaveformThresholdLine) and it is not keep:
                it.set_interaction_focus(False)

    def _set_threshold_line_interaction_focus(
        self, line: WaveformThresholdLine
    ) -> None:
        """Double-click entry: solo-focus *line* and enable vertical drag."""
        self._defocus_other_threshold_lines(line)
        line.set_interaction_focus(True)

    def _focused_event_idx(self, seg_id: str) -> int:
        """Resolve the focused beat's *current* index in ``_beat_events``.

        We index by closest-time match because:

        - ``_event_idx`` carried by a :class:`BeatTickItem` becomes
          stale the instant another edit (drag, sort, insert)
          renumbers the list.
        - The focus key ``(seg_id, t_local)`` always points at the
          last position the focused tick was committed at, so the
          closest match is the safe lookup.

        Returns ``-1`` when no focus is set or the segment has no
        events.
        """
        if self._focused_beat is None:
            return -1
        focused_seg, target_t = self._focused_beat
        if focused_seg != seg_id:
            return -1
        events = self._beat_events.get(seg_id, [])
        if not events:
            return -1
        best_idx = -1
        best_dt = float("inf")
        for i, ev in enumerate(events):
            t = float(ev[0])
            dt = abs(t - float(target_t))
            if dt < best_dt:
                best_dt = dt
                best_idx = i
        return best_idx

    def _commit_beat_edit(self, segment_id: str) -> None:
        """Refresh + announce a beat-events change.

        Both the scene rebuild and the persistence signal are routed
        through here and run on the *next* event-loop cycle (see
        :meth:`_schedule_beat_commit`).  Doing them later ensures Qt
        has fully unwound the mouse-release / context-menu event of
        the very :class:`BeatTickItem` we just mutated — calling
        ``scene.clear()`` mid-event would delete the C++ item Qt is
        still dispatching to and risk a crash.

        Pending arrow-key selection is *re-applied* (not cleared)
        after every refresh — see
        :attr:`_pending_tick_select_after_refresh` for the rationale.
        Other edit handlers clear the list themselves before doing
        anything else, so non-arrow edits never accidentally
        re-select a stale tick.

        After the rebuild we also re-select the focused tick (if any)
        so the visual highlight follows the tick across every
        ``scene.clear()`` — keeping the user's edit cursor anchored.
        """
        self.refresh()
        for seg_id, target_t in self._pending_tick_select_after_refresh:
            self._select_tick_at_time(seg_id, target_t)
        if self._focused_beat is not None:
            seg_id, target_t = self._focused_beat
            self._select_tick_at_time(seg_id, target_t)
        self.beat_events_edited.emit(segment_id)

    def _schedule_beat_commit(self, segment_id: str) -> None:
        QTimer.singleShot(
            0, lambda sid=segment_id: self._commit_beat_edit(sid)
        )

    def _select_tick_at_time(
        self, segment_id: str, target_t: float
    ) -> None:
        """Select the freshly-rebuilt :class:`BeatTickItem` whose
        underlying event sits at ``target_t`` (segment-local seconds).

        Used by arrow-key nudging to keep the moved tick selected
        across the ``scene.clear()`` that happens inside ``refresh``.
        Match tolerance is 1 µs since ``target_t`` is read straight
        from the just-stored event tuple — anything beyond floating-
        point noise indicates the event was deleted or reordered out
        from under us, in which case we silently do nothing rather
        than grab the wrong neighbour.
        """
        events = self._beat_events.get(segment_id, [])
        if not events:
            return
        best_idx = -1
        best_dt = 1e-6
        for i, ev in enumerate(events):
            t = float(ev[0])
            dt = abs(t - float(target_t))
            if dt < best_dt:
                best_dt = dt
                best_idx = i
        if best_idx < 0:
            return
        for it in self.scene.items():
            if (
                isinstance(it, BeatTickItem)
                and it._segment_id == segment_id
                and it._event_idx == best_idx
            ):
                it.setSelected(True)
                return

    def _on_beat_tick_drag_finished(
        self, segment_id: str, event_idx: int, new_scene_x: float
    ) -> None:
        """Commit a tick drag — convert scene-x → local time and persist.

        Re-sorts the event list by time so the next refresh assigns
        index labels in chronological order.  ``_focused_beat`` is
        moved to follow the dragged tick to its new ``t_local`` so
        subsequent arrow-key nudges stay anchored on the same beat.
        """
        if self._project is None:
            return
        seg = self._project.get_segment(segment_id)
        if seg is None:
            return
        events = self._beat_events.get(segment_id)
        if events is None or not (0 <= event_idx < len(events)):
            return
        new_t_proj = self._x_to_time(float(new_scene_x))
        new_t_local = max(
            0.0,
            min(seg.duration_sec, new_t_proj - seg.start_time_sec),
        )
        old_ev = events[event_idx]
        old_t = float(old_ev[0])
        kind = str(old_ev[1])
        height = float(old_ev[2]) if len(old_ev) >= 3 else 1.0
        if abs(new_t_local - old_t) < 1e-4:
            return
        self._pending_tick_select_after_refresh = []
        events[event_idx] = (new_t_local, kind, height)
        events.sort(key=lambda e: e[0])
        # Keep focus on the dragged tick so arrow keys keep targeting
        # it (the index may have shifted after the sort, but the
        # closest-time lookup in :meth:`_focused_event_idx` resolves
        # that transparently).
        if (
            self._focused_beat is not None
            and self._focused_beat[0] == segment_id
            and abs(self._focused_beat[1] - old_t) < 1e-6
        ):
            self._focused_beat = (segment_id, new_t_local)
        self._schedule_beat_commit(segment_id)

    def _on_beat_tick_delete_requested(
        self, segment_id: str, event_idx: int
    ) -> None:
        events = self._beat_events.get(segment_id)
        if events is None or not (0 <= event_idx < len(events)):
            return
        deleted_t = float(events[event_idx][0])
        self._pending_tick_select_after_refresh = []
        del events[event_idx]
        if (
            self._focused_beat is not None
            and self._focused_beat[0] == segment_id
            and abs(self._focused_beat[1] - deleted_t) < 1e-6
        ):
            self._focused_beat = None
        self._schedule_beat_commit(segment_id)

    def _on_delete_selected_beat_ticks(self) -> None:
        """Delete every selected :class:`BeatTickItem` in the scene.

        Bound to Delete / Backspace at :class:`TimelineView` level so the
        user can drop one or more selected ticks in a single keystroke.
        Multi-select drops are handled by collecting (segment_id,
        event_idx) pairs first and popping them in *descending* order
        per segment — this keeps every remaining ``event_idx`` valid
        through the loop without needing to re-resolve indices after
        each pop. A single deferred refresh + persistence signal is
        emitted per affected segment.

        ``_focused_beat`` is cleared if its event sat in the deleted
        set so the timeline returns to default arrow-key behaviour.
        """
        selected = [
            it for it in self.scene.selectedItems()
            if isinstance(it, BeatTickItem)
        ]
        if not selected:
            return
        self._pending_tick_select_after_refresh = []
        by_seg: dict[str, list[int]] = {}
        focused_t: dict[str, float] = {}
        if self._focused_beat is not None:
            focused_t[self._focused_beat[0]] = float(self._focused_beat[1])
        for tick in selected:
            by_seg.setdefault(tick._segment_id, []).append(
                tick._event_idx
            )
        touched: list[str] = []
        for seg_id, indices in by_seg.items():
            events = self._beat_events.get(seg_id)
            if events is None:
                continue
            for idx in sorted(set(indices), reverse=True):
                if 0 <= idx < len(events):
                    if (
                        seg_id in focused_t
                        and abs(events[idx][0] - focused_t[seg_id]) < 1e-6
                    ):
                        self._focused_beat = None
                    del events[idx]
            touched.append(seg_id)
        for seg_id in touched:
            self._schedule_beat_commit(seg_id)

    def _on_arrow_nudge_selected_ticks(self, delta_px: float) -> None:
        """Move the *focused* beat by ``delta_px`` pixels along X.

        Bound to ←/→ at :class:`TimelineView` level. ``delta_px`` is
        negative for left, positive for right; Shift multiplies the
        step (handled in the view).  The nudge is converted to
        seconds via the *current* effective zoom so a fixed pixel
        step always feels the same on screen regardless of the
        timeline scale.

        We resolve the target event by ``(segment_id, t_local)``
        each call rather than by ``BeatTickItem._event_idx``: rapid
        arrow presses queue multiple commits, and the events list is
        re-sorted on every commit, so the index a stale tick carries
        may point at a different event by the time the next press
        arrives. ``_focused_beat`` is updated to the new ``t_local``
        after every nudge so subsequent presses keep retiming the
        same beat.

        When no beat is focused, this is a no-op — arrow keys fall
        through to the View's default scroll behaviour.
        """
        if self._project is None or abs(delta_px) < 1e-6:
            return
        if self._focused_beat is None:
            return
        seg_id, target_t = self._focused_beat
        seg = self._project.get_segment(seg_id)
        if seg is None:
            return
        events = self._beat_events.get(seg_id)
        if not events:
            return

        idx = self._focused_event_idx(seg_id)
        if idx < 0:
            return

        delta_sec = float(delta_px) / max(1.0, float(self._effective_pps))
        ev = events[idx]
        t = float(ev[0])
        kind = str(ev[1])
        height = float(ev[2]) if len(ev) >= 3 else 1.0
        new_t = max(
            0.0,
            min(float(seg.duration_sec), t + delta_sec),
        )
        if abs(new_t - t) < 1e-9:
            self._focused_beat = (seg_id, t)
            return

        events[idx] = (new_t, kind, height)
        events.sort(key=lambda e: e[0])
        # Track new target for re-selection across the deferred
        # refresh, *and* update the focused-beat marker so further
        # arrow taps look up the same event by its new t_local.
        self._focused_beat = (seg_id, new_t)
        self._pending_tick_select_after_refresh = [(seg_id, new_t)]
        self._schedule_beat_commit(seg_id)

    def _on_beat_tick_kind_changed(
        self, segment_id: str, event_idx: int, kind: str
    ) -> None:
        events = self._beat_events.get(segment_id)
        if events is None or not (0 <= event_idx < len(events)):
            return
        self._pending_tick_select_after_refresh = []
        ev = events[event_idx]
        t = float(ev[0])
        height = float(ev[2]) if len(ev) >= 3 else 1.0
        events[event_idx] = (t, kind, height)
        self._schedule_beat_commit(segment_id)

    def _on_threshold_line_moved(
        self, segment_id: str, threshold: float
    ) -> None:
        """Live drag of the red waveform line — update model + tick opacity.

        ``threshold`` is the normalised 0..1 value emitted by
        :class:`WaveformThresholdLine`.  We mutate the segment model
        directly (the line is bound to a single segment) and re-tint
        each ``BeatTickItem`` so dimmed/visible ticks track the line.

        We deliberately do **not** emit ``beat_events_edited`` here:
        that signal is connected to :meth:`MainWindow._on_project_changed`,
        which calls :meth:`refresh` and would rebuild the whole scene
        on every pixel of vertical motion — destroying the line item
        that owns the mouse grab and breaking the drag.  Persistence
        is deferred to :meth:`_on_threshold_line_drag_finished` on
        mouse release.
        """
        if self._project is None:
            return
        seg = self._project.get_segment(segment_id)
        if seg is None:
            return
        thr = max(0.0, min(1.0, float(threshold)))
        if abs(getattr(seg, "beat_height_threshold", 0.0) - thr) < 1e-4:
            return
        seg.beat_height_threshold = thr
        self._update_beat_strip_opacity(segment_id, thr)

    def _on_threshold_line_drag_finished(self, segment_id: str) -> None:
        """Left-button release after interacting with the threshold line.

        Two things happen here, in order:

        1. ``beat_events_edited`` is emitted so the host
           :class:`MainWindow` copies the panel's ``_beat_events``
           dict into :pyattr:`Segment.beat_events` and runs the
           normal dirty / autosave path — exactly once per
           completed drag gesture.
        2. ``beat_threshold_changed`` is emitted with the new
           threshold value so the host re-fires
           :class:`BeatDetectService` against the rhythm core; the
           subprocess applies ``--beat_height_threshold`` and
           returns a fresh, server-filtered events list.  When the
           ``ready`` signal fires, the timeline ticks rebuild from
           that list — no client-side dim-and-keep hack needed.
        """
        if self._project is None:
            return
        seg = self._project.get_segment(segment_id)
        if seg is None:
            return
        self.beat_events_edited.emit(segment_id)
        thr = float(getattr(seg, "beat_height_threshold", 0.0) or 0.0)
        self.beat_threshold_changed.emit(segment_id, thr)

    def _update_beat_strip_opacity(
        self, segment_id: str, threshold: float
    ) -> None:
        """Re-apply the threshold-driven opacity to existing tick items.

        Used during a live drag of the red threshold line so the
        scene doesn't have to be rebuilt (which would destroy the
        line's mouse grab).  Walks the scene once and toggles each
        :class:`BeatTickItem` belonging to ``segment_id``.
        """
        events = self._beat_events.get(segment_id, [])
        if not events:
            return
        # Build {event_idx → height} lookup since the tick stores
        # only its event_idx + segment_id, not the height directly.
        idx_to_height = {
            i: (float(ev[2]) if len(ev) >= 3 else 1.0)
            for i, ev in enumerate(events)
        }
        for it in self.scene.items():
            if (
                isinstance(it, BeatTickItem)
                and it._segment_id == segment_id
            ):
                h = idx_to_height.get(it._event_idx, 1.0)
                it.setOpacity(0.25 if h < threshold - 1e-6 else 1.0)

    def _insert_beat_at(
        self,
        segment_id: str,
        scene_x: float,
        *,
        skip_if_near_existing: bool = False,
    ) -> bool:
        """Insert a new beat event at scene-x for the given segment.

        Shared body for every "add a beat here" gesture:

        - Strip double-click (uses ``skip_if_near_existing=True`` to
          guard against accidental stacked duplicates when the user
          actually wanted to double-click an existing tick).
        - Strip right-click → "Add Beat Block" menu (uses
          ``skip_if_near_existing=False`` because the user explicitly
          chose the action — even adding right next to an existing
          tick is a deliberate request).

        The new tick inherits its ``kind`` from the nearest existing
        event in the same segment (falling back to ``"L"`` when the
        list is empty), so re-using the same hand/foot for nearby
        beats requires no extra clicks. The freshly-inserted tick
        becomes the focused beat so the user can immediately drag
        it / nudge it with arrow keys.

        Returns ``True`` when a beat was inserted, ``False`` when the
        guard skipped the insertion or the segment was missing.
        """
        if self._project is None:
            return False
        seg = self._project.get_segment(segment_id)
        if seg is None:
            return False
        self._pending_tick_select_after_refresh = []
        t_proj = self._x_to_time(float(scene_x))
        t_local = max(
            0.0,
            min(seg.duration_sec, t_proj - seg.start_time_sec),
        )
        events = self._beat_events.setdefault(segment_id, [])

        if skip_if_near_existing and events:
            min_gap_px = 2.0 * float(BeatTickItem.HIT_HALF_WIDTH)
            min_gap_sec = min_gap_px / max(1.0, self._effective_pps)
            if any(abs(float(ev[0]) - t_local) <= min_gap_sec
                   for ev in events):
                return False

        nearest_kind = "L"
        if events:
            nearest = min(events, key=lambda e: abs(float(e[0]) - t_local))
            nearest_kind = str(nearest[1]) or "L"
        # User-inserted ticks always carry full amplitude (1.0) so
        # they're never silently filtered out by the threshold slider
        # — the user explicitly asked for this beat to exist.
        events.append((t_local, nearest_kind, 1.0))
        events.sort(key=lambda e: e[0])
        self._set_focused_beat(segment_id, t_local)
        self._schedule_beat_commit(segment_id)
        return True

    def _on_beat_strip_double_clicked(
        self, segment_id: str, scene_x: float
    ) -> None:
        """Double-click on the strip → insert a new beat event.

        Skips insertion when an existing tick sits within roughly
        the tick's hit footprint (``2 × HIT_HALF_WIDTH`` pixels in
        current zoom) — without this guard, double-clicking *on* an
        existing tick (which Qt may deliver to the strip background
        instead of the tick itself whenever the cursor lands a few
        pixels off-centre) would create a stacked duplicate the
        user almost never wants.
        """
        self._insert_beat_at(
            segment_id, scene_x, skip_if_near_existing=True
        )

    def _on_beat_strip_add_requested(
        self, segment_id: str, scene_x: float
    ) -> None:
        """Right-click → "Add Beat Block" menu → insert here.

        Bypasses the near-existing guard used by the double-click
        path: the user picked the action explicitly, so we honour
        the click position even when it overlaps another tick.
        """
        self._insert_beat_at(
            segment_id, scene_x, skip_if_near_existing=False
        )

    def set_playhead(self, time_sec: float) -> None:
        """Move playhead according to preview playback."""
        self._playhead_time_sec = max(0.0, time_sec)
        self._draw_playhead(time_sec)
        self._playhead_label.setText(format_seconds(self._playhead_time_sec))

    def set_scrub_enabled(self, enabled: bool) -> None:
        """Allow / disallow mouse-driven playhead scrubbing.

        Disabled while the preview player is in ``StoppedState`` so the
        red playhead doesn't lurch to wherever the user clicks while
        the video is parked — only Play / Pause states keep scrubbing
        available.  Drag-in-progress is also cancelled defensively so
        a stop-while-dragging doesn't leave the playhead frozen
        mid-drag.
        """
        self.view._scrub_enabled = bool(enabled)
        if not enabled:
            self.view._dragging_playhead = False

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

        # "Auto Gen Block" — manual trigger for ``rhythm.py --detect_only``.
        # Detection no longer fires on segment selection / drag / form
        # change so the user can iterate on settings without paying for a
        # subprocess each tweak.  Disabled until a segment is selected;
        # enabled in ``_on_selection_changed``.
        self.auto_gen_button = QPushButton("Auto Gen Block")
        self.auto_gen_button.setObjectName("autoGenButton")
        self.auto_gen_button.setEnabled(False)
        self.auto_gen_button.setToolTip(
            "Generate the predicted block-spawn markers for the selected\n"
            "segment.  Runs rhythm.py --detect_only on the trimmed audio\n"
            "so the timeline preview matches the eventual render — without\n"
            "spending the full render time."
        )
        self.auto_gen_button.clicked.connect(self._on_auto_gen_clicked)
        top.addWidget(self.auto_gen_button)

        # "Gen by Chart" — local peak-detector that derives one beat
        # tick per RMS chart peak, no rhythm.py subprocess.  Useful
        # when the user wants strict 1-tick-per-visible-peak (the
        # spawner's lane-spacing rules in rhythm.py drop some peaks
        # on dense passages, which manifests as missing ticks even
        # above the threshold line).  Operates entirely on the
        # already-loaded ``_waveform_rms`` so it's instant.
        self.gen_by_chart_button = QPushButton("Gen by Chart")
        self.gen_by_chart_button.setObjectName("genByChartButton")
        self.gen_by_chart_button.setEnabled(False)
        self.gen_by_chart_button.setToolTip(
            "Generate beat ticks straight from the waveform chart\n"
            "below: one tick per RMS peak, computed locally without\n"
            "spawning rhythm.py.  Honours the segment's threshold\n"
            "slider — peaks below the red line are skipped."
        )
        self.gen_by_chart_button.clicked.connect(
            self._on_gen_by_chart_clicked
        )
        top.addWidget(self.gen_by_chart_button)

        self.clear_beats_button = QPushButton("Clear Beats")
        self.clear_beats_button.setObjectName("clearBeatsButton")
        self.clear_beats_button.setEnabled(False)
        self.clear_beats_button.setToolTip(
            "Remove ALL beat block markers from the selected segment.\n"
            "This action cannot be undone."
        )
        self.clear_beats_button.clicked.connect(self._on_clear_beats_clicked)
        top.addWidget(self.clear_beats_button)

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

        # Rule mode toggle — pulls each beat tick downward as a dashed
        # vertical guide that overlays the waveform, making it easy to
        # eyeball whether a tick lines up with the audio peaks beneath
        # it. Toggling refreshes the timeline; no model change.
        self.rule_button = QPushButton("Rule")
        self.rule_button.setObjectName("ruleButton")
        self.rule_button.setFixedWidth(44)
        self.rule_button.setCheckable(True)
        self.rule_button.setChecked(False)
        self.rule_button.setToolTip(
            "Rule mode: extend each beat tick as a dashed vertical\n"
            "guide line down through the waveform so you can verify\n"
            "tick positions against the audio envelope."
        )
        self.rule_button.toggled.connect(self._on_rule_toggled)
        top.addWidget(self.rule_button)

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

        self.scene = TimelineScene(self)
        self.scene.setBackgroundBrush(QColor("#141414"))
        self.scene.setSceneRect(0, 0, 3600, self._SCENE_H)
        # ── Disable BSP indexing ──────────────────────────────────────
        # Qt's default ``BspTreeIndex`` aggressively prunes hit-tests
        # by bucketing items into a binary-space-partition tree.  The
        # tree is rebuilt lazily on the next ``itemAt`` / paint event,
        # but if the tree is stale at the moment a click is dispatched
        # it can return *no* item even when one is sitting at the
        # cursor — and Qt's default ``mousePressEvent`` then takes the
        # "empty space click" branch (which clears the selection and,
        # in some regressions, schedules an internal repaint that
        # skips the translucent waveform fill).  ``NoIndex`` simply
        # iterates every item on every hit-test; the scene only has
        # a few hundred items so the linear scan is unmeasurable, and
        # the click semantics are now deterministic — the user clicks
        # the fill, ``itemAt`` returns the fill, our wave-area guard
        # turns it into a no-op, and the chart never gets repainted
        # without its translucent layer.
        self.scene.setItemIndexMethod(
            QGraphicsScene.ItemIndexMethod.NoIndex
        )
        self.view = TimelineView(self.scene, self.pixels_per_second, self)
        # Adding the view into the layout below reparents it to the
        # layout's owner widget (PanelRoot), so ``self.view.parent()``
        # is no longer the panel.  Stash the panel reference so
        # :class:`TimelineView` can reach our beat-edit / focus
        # helpers without having to walk the widget tree.
        self.view._panel_ref = self
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
        """Compute a pps that makes the whole timeline visible at once.

        Context-aware:
          * In focus mode → re-fits the FOCUSED SEGMENT to the viewport
            (stays in focus, just clears any manual Ctrl+wheel zoom and
            snaps the segment back to spanning edge-to-edge).  This is
            the natural "reset zoom" affordance for the focus session.
          * In overview mode → fits the WHOLE PROJECT timeline to the
            viewport, same as before.
        """
        if self._focus_segment_id is not None:
            # Drop any manual Ctrl+wheel zoom, reset scroll, and let
            # ``_update_scene_width`` recompute ``_effective_pps`` from
            # the current viewport width × segment duration.
            self._focus_manual_zoom = False
            self.refresh()
            self.view.horizontalScrollBar().setValue(0)
            return
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

    def _on_rule_toggled(self, checked: bool) -> None:
        """Toggle the dashed waveform-guide overlay for beat ticks.

        Rule mode is purely visual — it doesn't touch ``_beat_events``
        or any persistence path; we just rebuild the scene so each
        existing tick adds (or drops) its long vertical companion line.
        """
        self._rule_mode_enabled = bool(checked)
        self.refresh()

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
    # The BEAT-DBG strip sits between the segment track and the waveform.
    # Strip body and tick overhang were doubled vs the original
    # rhythm.py-mirroring layout to make ticks easy to target with a
    # mouse — the strip is now 16 px tall and ticks extend 12 px above
    # and below it (40 px total).  Numbers go ~6 px above the tick top.
    # The waveform track was nudged 10 px down to keep a comfortable gap
    # below the lengthened ticks; scene height grew accordingly.
    _BEAT_STRIP_Y = 114
    _BEAT_STRIP_H = 16
    _WAVE_TRACK_Y = 144
    _WAVE_TRACK_H = 160   # doubled from 80
    _SCENE_H = 314        # ruler + segment + beat-strip + waveform + padding

    def _draw_tracks(self) -> None:
        """No-op kept for call-site compatibility.

        The segment track background, the "Segments" / "Waveform"
        lane labels, and the waveform-track strip are all painted in
        the scene's ``drawBackground`` pass now (see
        :meth:`_paint_track_decorations` and
        :meth:`_paint_waveform_background`).  Painting them as
        background instead of stacking ``QGraphicsItem`` instances
        sidesteps a Qt regression where mouse presses inside a track
        would occasionally remove the track's translucent / opaque
        decorations from the paint cache (the user could "click the
        chart away" and only get it back with a panel resize).

        We still trigger a background-layer invalidation here so the
        chrome re-renders even when nothing else in the scene
        changed (e.g. zoom-only updates).
        """
        try:
            self.scene.invalidate(
                self.scene.sceneRect(),
                QGraphicsScene.SceneLayer.BackgroundLayer,
            )
        except RuntimeError:
            pass

    def _draw_waveform(self) -> None:
        """No-op kept for call-site compatibility.

        The waveform is now painted by :meth:`_paint_waveform_background`
        during the scene's ``drawBackground`` pass — see
        :class:`TimelineScene` for the rationale (clicks inside the
        waveform area used to remove the fill / outline scene items
        layer-by-layer; making the chart part of the background pass
        sidesteps every item-level interaction path).

        ``refresh()`` still calls this; we just trigger a background
        repaint via :meth:`QGraphicsScene.invalidate` so the chart
        re-renders with the latest RMS data after a reload / zoom /
        scroll without going through the scene-item tree.
        """
        try:
            self.scene.invalidate(
                self.scene.sceneRect(),
                QGraphicsScene.SceneLayer.BackgroundLayer,
            )
        except RuntimeError:
            pass
        if not self._waveform_rms:
            self._draw_waveform_placeholder()

    def _paint_track_decorations(self, painter, rect) -> None:
        """Paint non-interactive track chrome during the background pass.

        Covers the dark **Segment** track strip and the **Segments** /
        **Waveform** lane labels.  These used to be ``QGraphicsItem``
        instances added in :meth:`_draw_tracks`, but the same Qt
        regression that affected the waveform fill / outline also
        affected these decorations: clicks inside the segment row
        could remove the lane background, leaving the dark scene
        backdrop visible until the next forced repaint.  Painting
        here makes the chrome part of the background pass — it is
        always rendered before any item, on every paint event, and
        cannot be hidden by mouse handling.
        """
        scene_w = self.scene.sceneRect().width()
        if scene_w <= 0:
            return
        painter.save()
        try:
            # ── Segment track strip ─────────────────────────────────
            painter.setBrush(QBrush(QColor("#181818")))
            painter.setPen(QPen(QColor("#1f1f1f")))
            painter.drawRect(QRectF(
                0.0,
                float(self._SEGMENT_TRACK_Y),
                float(scene_w),
                float(self._SEGMENT_TRACK_H),
            ))

            # ── Lane labels ("Segments" / "Waveform") ───────────────
            painter.setPen(QPen(QColor(255, 255, 255, int(255 * 0.25))))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            fm = painter.fontMetrics()
            label_baseline_offset = float(fm.ascent()) + 2.0
            painter.drawText(
                QPointF(4.0, float(self._SEGMENT_TRACK_Y) + label_baseline_offset),
                "Segments",
            )
            painter.drawText(
                QPointF(4.0, float(self._WAVE_TRACK_Y) + label_baseline_offset),
                "Waveform",
            )
        finally:
            painter.restore()

    def _paint_segment_blocks(self, painter, rect) -> None:
        """Paint segment-block fills, outlines and selection halos.

        :class:`SegmentRectItem` instances themselves render nothing
        (their ``paint`` is a no-op) — this method walks
        ``_block_map`` and paints each block at the item's *current*
        scene position, so the visual follows live drag input via
        the ``ItemPositionHasChanged`` invalidation in
        :meth:`SegmentRectItem.itemChange`.

        Painting here makes the fill / outline immune to a Qt
        regression where a click on a translucent / decorated
        ``QGraphicsItem`` could remove its visual output from the
        paint cache (the user reported clicking a segment made its
        cyan / pink / orange fill disappear, leaving only the
        outline).  The background pass runs unconditionally on every
        repaint so the fill cannot be "lost" by mouse handling.
        """
        if not self._block_map:
            return
        painter.save()
        try:
            for seg_id, block in list(self._block_map.items()):
                try:
                    rect_local = block.rect()
                    pos = block.pos()
                except RuntimeError:
                    # Item was deleted between scene.clear() and the
                    # next refresh — skip it.
                    continue
                if rect_local.isEmpty():
                    continue
                scene_rect = QRectF(
                    pos.x() + rect_local.x(),
                    pos.y() + rect_local.y(),
                    rect_local.width(),
                    rect_local.height(),
                )
                fill = getattr(block, "_fill_color", None)
                if fill is None:
                    fill = QColor("#3bb6ff")
                painter.setBrush(QBrush(fill))
                painter.setPen(QPen(QColor("#0b0b0b"), 1))
                painter.drawRect(scene_rect)
                # Selection halo — Qt's default
                # ``QGraphicsRectItem.paint`` would draw a 1-px
                # dashed outline when ``ItemIsSelectable`` & state is
                # selected; we replicate that here so the user still
                # sees which block they have selected.
                try:
                    is_selected = bool(block.isSelected())
                except RuntimeError:
                    is_selected = False
                if is_selected:
                    halo_pen = QPen(QColor("#ffffff"))
                    halo_pen.setWidth(1)
                    halo_pen.setStyle(Qt.PenStyle.DashLine)
                    painter.setPen(halo_pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(scene_rect.adjusted(0.5, 0.5, -0.5, -0.5))
        finally:
            painter.restore()

    def _paint_threshold_lines(self, painter, rect) -> None:
        """Paint each segment's red threshold bar in the background pass.

        :class:`WaveformThresholdLine` is invisible (its ``paint``
        is a no-op) — we walk ``_threshold_map`` here and render
        the focus halo + main red stroke + end-handle squares +
        "thr 0.42" pill at the line's current scene position so
        live drags follow without delay.

        Visual layers (matches the original
        :meth:`WaveformThresholdLine.paint` we replaced):

        1. 9-px focus halo (yellow translucent) when focused.
        2. 2-px main red stroke (cosmetic) at the bar Y.
        3. End-handle squares (red fill, white border) when focused.
        4. "thr 0.42" pill (red fill, white text) above the bar.

        Painting in the background pass makes the bar immune to the
        Qt cache regression where a click on the line previously
        left the user staring at empty space until the next forced
        repaint (resize / scroll).
        """
        if not self._threshold_map:
            return
        painter.save()
        try:
            base_opacity = painter.opacity()
            for seg_id, line in list(self._threshold_map.items()):
                try:
                    pos = line.pos()
                    width = float(line._width)
                except (RuntimeError, AttributeError):
                    continue
                if width <= 0:
                    continue

                x_left = float(pos.x())
                y = float(pos.y())
                x_right = x_left + width

                # Mirror the item's own opacity (always 1.0 today,
                # but future code might dim it like beat ticks).
                try:
                    item_opacity = float(line.opacity())
                except RuntimeError:
                    item_opacity = 1.0
                painter.setOpacity(base_opacity * item_opacity)

                focused = bool(getattr(line, "_interaction_focused", True))

                # 1. Focus halo — wide soft stroke.
                if focused:
                    halo = QPen(line.FOCUS_HALO_COLOR)
                    halo.setCosmetic(True)
                    halo.setWidthF(9.0)
                    painter.setPen(halo)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawLine(QPointF(x_left, y), QPointF(x_right, y))

                # 2. Main threshold stroke.
                col = line.LINE_COLOR
                if not focused:
                    col = QColor(255, 130, 130)
                pen = QPen(col)
                pen.setCosmetic(True)
                pen.setWidthF(line.LINE_THICKNESS)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawLine(QPointF(x_left, y), QPointF(x_right, y))

                # 3. Grab handles only while focused.
                if focused:
                    hs = line.HANDLE_SIZE
                    painter.setBrush(QBrush(line.LINE_COLOR))
                    painter.setPen(QPen(QColor(255, 255, 255), 1.5))
                    painter.drawRect(
                        QRectF(x_left - hs / 2, y - hs / 2, hs, hs)
                    )
                    painter.drawRect(
                        QRectF(x_right - hs / 2, y - hs / 2, hs, hs)
                    )

                # 4. "thr 0.42" pill.
                threshold = float(getattr(line, "_threshold", 0.0))
                text = f"thr {threshold:.2f}"
                fm = painter.fontMetrics()
                text_w = fm.horizontalAdvance(text)
                text_h = fm.height()
                pad_x = 4.0
                pad_y = 1.0
                bg_w = text_w + 2 * pad_x
                bg_h = text_h + 2 * pad_y
                bg_x = x_left + 4.0
                bg_y = y - line.HIT_HALF_HEIGHT - bg_h - 1.0
                painter.setBrush(QBrush(line.LINE_COLOR))
                painter.setPen(QPen(QColor(255, 255, 255), 1))
                painter.drawRect(QRectF(bg_x, bg_y, bg_w, bg_h))
                painter.setPen(QPen(QColor(255, 255, 255)))
                painter.drawText(
                    QPointF(bg_x + pad_x, bg_y + pad_y + fm.ascent()),
                    text,
                )

            painter.setOpacity(base_opacity)
        finally:
            painter.restore()

    def _paint_beat_ticks(self, painter, rect) -> None:
        """Paint beat-event tick strokes + index labels.

        :class:`BeatTickItem` instances are invisible (their
        ``paint`` is a no-op) — we walk ``_tick_map`` here and paint
        each tick at the item's *current* scene position so live
        drags follow without delay.  The stroke width depends on
        the item's selection state (10 px when selected, 6 px idle)
        and below-threshold ticks are dimmed to 25 % via
        ``QPainter.setOpacity`` to mirror the old
        ``QGraphicsItem.setOpacity(0.25)`` behaviour.

        Painting in the background pass is what kept the strokes
        from disappearing on click in the user's video — Qt's
        hit-test path used to drop the child :class:`QGraphicsLineItem`
        from the paint cache when a press landed on the parent
        ``BeatTickItem``.  With the visual generated fresh on every
        repaint, that regression is no longer reachable.
        """
        if not self._tick_map:
            return
        scene_w = self.scene.sceneRect().width()
        if scene_w <= 0:
            return
        painter.save()
        try:
            base_opacity = painter.opacity()
            for (seg_id, event_idx), tick in list(self._tick_map.items()):
                try:
                    pos = tick.pos()
                    rect_local = tick.rect()
                except RuntimeError:
                    # Item was deleted between scene.clear() and the
                    # next refresh — skip silently.
                    continue
                if rect_local.isEmpty():
                    continue

                x = float(pos.x())
                y_top = float(pos.y())
                line_height = float(getattr(tick, "_line_height", 0.0))
                if line_height <= 0:
                    continue
                if x < -tick.HIT_HALF_WIDTH or x > scene_w + tick.HIT_HALF_WIDTH:
                    continue

                # Per-tick opacity — below-threshold ticks set
                # ``QGraphicsItem.setOpacity(0.25)`` on the item,
                # mirror that on the painter so the stroke matches.
                try:
                    item_opacity = float(tick.opacity())
                except RuntimeError:
                    item_opacity = 1.0
                painter.setOpacity(base_opacity * item_opacity)

                try:
                    is_selected = bool(tick.isSelected())
                except RuntimeError:
                    is_selected = False

                pen = QPen(tick._base_color)
                pen.setCosmetic(True)
                pen.setWidthF(
                    BeatTickItem.TICK_WIDTH_SELECTED
                    if is_selected
                    else BeatTickItem.TICK_WIDTH_IDLE
                )
                pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawLine(
                    QPointF(x, y_top),
                    QPointF(x, y_top + line_height),
                )

                label_text = getattr(tick, "_label_text", None)
                if label_text:
                    label_top = y_top + float(
                        getattr(tick, "_label_top_local", 0.0)
                    )
                    f = painter.font()
                    f.setPointSize(7)
                    painter.setFont(f)
                    painter.setPen(QPen(tick._base_color))
                    fm = painter.fontMetrics()
                    painter.drawText(
                        QPointF(x - 4.0, label_top + float(fm.ascent())),
                        label_text,
                    )

            painter.setOpacity(base_opacity)
        finally:
            painter.restore()

    def _paint_beat_strip_decorations(self, painter, rect) -> None:
        """Paint per-segment beat-strip backgrounds, rule guides and cursor.

        These decorations track project state (segment ranges, beat
        events, playhead position) but are *not* interactive — the
        actual hit zones for inserting / dragging beats live on
        :class:`BeatStripBgItem` and :class:`BeatTickItem` scene
        items.  Painting the visuals here keeps them immune to the
        same click-hides-item bug that the waveform fix sidestepped
        (see :class:`TimelineScene` docstring).

        Order matches the original z-stack:

        1. Beat-strip background rectangle (RGB 65/65/65 fill + 1-px
           grey border) for every visible segment.
        2. Rule-mode dashed guide line per beat event, coloured by
           the same upcoming / active / passed state as the tick.
        3. White "now" cursor through the strip when the playhead is
           inside a segment's time range.
        """
        if self._project is None:
            return
        scene_w = self.scene.sceneRect().width()
        if scene_w <= 0:
            return

        y0 = float(self._BEAT_STRIP_Y)
        y1 = y0 + float(self._BEAT_STRIP_H)
        tick_bottom = y1 + 12.0
        cursor_top    = y0 - 16.0
        cursor_bottom = y1 + 16.0
        wave_bottom = float(
            self._WAVE_TRACK_Y + self._WAVE_TRACK_H
        ) - 2.0

        fps = float(getattr(self._project, "output_fps", 30) or 30)
        t_now = float(self._playhead_time_sec)

        painter.save()
        try:
            for seg in self._project.sorted_segments():
                seg_id = seg.id
                base_t = float(seg.start_time_sec)
                end_t  = float(seg.end_time_sec)
                full_x0 = self._time_to_x(base_t)
                full_x1 = self._time_to_x(end_t)
                sx0 = max(0.0, full_x0)
                sx1 = min(scene_w, full_x1)
                if sx1 < 0 or full_x0 > scene_w:
                    continue
                if sx1 - sx0 < 2.0:
                    continue

                # 1. Beat-strip background (replicates the old
                #    ``BeatStripBgItem`` visual — same RGB 65/65/65
                #    fill, RGB 140/140/140 1-px border).
                painter.setBrush(QBrush(QColor(65, 65, 65)))
                painter.setPen(QPen(QColor(140, 140, 140), 1))
                painter.drawRect(QRectF(sx0, y0, sx1 - sx0, y1 - y0))

                events = self._beat_events.get(seg_id, [])
                threshold = float(getattr(seg, "beat_height_threshold", 0.0))

                # 2. Rule-mode dashed guides.
                if self._rule_mode_enabled and events and wave_bottom > tick_bottom + 1.0:
                    for ev in events:
                        t_local = float(ev[0])
                        height = float(ev[2]) if len(ev) >= 3 else 1.0
                        t_proj = base_t + t_local
                        if t_proj > end_t + 1e-3:
                            continue
                        x = self._time_to_x(t_proj)
                        if x < sx0 - 4 or x > sx1 + 4:
                            continue
                        below_thresh = height < threshold - 1e-6
                        col = QColor(self._beat_strip_color(t_proj, t_now, fps))
                        col.setAlphaF(0.2 if below_thresh else 0.7)
                        guide_pen = QPen(col)
                        guide_pen.setCosmetic(True)
                        guide_pen.setWidthF(1.0)
                        guide_pen.setStyle(Qt.PenStyle.DashLine)
                        painter.setPen(guide_pen)
                        painter.drawLine(
                            QPointF(x, tick_bottom),
                            QPointF(x, wave_bottom),
                        )

                # 3. White "now" cursor through the strip.
                if base_t - 1e-3 <= t_now <= end_t + 1e-3:
                    px_now = self._time_to_x(t_now)
                    if sx0 - 4 <= px_now <= sx1 + 4:
                        cur_pen = QPen(self._BEAT_COL_CURSOR)
                        cur_pen.setCosmetic(True)
                        cur_pen.setWidthF(1.0)
                        painter.setPen(cur_pen)
                        painter.drawLine(
                            QPointF(px_now, cursor_top),
                            QPointF(px_now, cursor_bottom),
                        )
        finally:
            painter.restore()

    def _paint_waveform_background(self, painter, rect) -> None:
        """Paint the waveform chart directly during the background pass.

        Called from :meth:`TimelineScene.drawBackground` on every
        repaint of the visible viewport.  ``painter`` is positioned in
        scene coordinates; ``rect`` is the scene-rect being painted.

        Two passes:

        1. **Track background** — the dark ``#151515`` strip that used
           to be a scene item in ``_draw_tracks``.  Always painted,
           even before any RMS data has loaded, so the user sees the
           dedicated waveform lane regardless of state.
        2. **Chart** — bg-rect, baseline, translucent fill polygon,
           outline polyline.  Only painted when ``_waveform_rms`` has
           samples.  Mirrors the original ``_draw_waveform`` geometry
           exactly so the visual is identical to the previous
           scene-item version.

        Layout reference (game ``src/rhythm.py``):

            wy0 = y1 + 30
            wy1 = wy0 + int(HEIGHT * 0.10)
            cv2.rectangle(canvas, (x0, wy0), (x1, wy1), (40,40,40), -1)
            cv2.rectangle(canvas, (x0, wy0), (x1, wy1), (120,120,120), 1)
            cv2.line(canvas, (x0, wy1), (x1, wy1), (90,90,90), 1)
            cv2.polylines(canvas, [pts], False, (130,170,255), 1)
            cv2.fillPoly(ov, [poly], (70,110,170)); addWeighted(ov, 0.35, ...)
        """
        scene_width = self.scene.sceneRect().width()
        if scene_width <= 0:
            return

        # ── Pass 1: dark waveform-track strip (always present). ──────
        painter.save()
        try:
            painter.setBrush(QBrush(QColor("#151515")))
            painter.setPen(QPen(QColor("#1f1f1f")))
            painter.drawRect(QRectF(
                0.0,
                float(self._WAVE_TRACK_Y),
                float(scene_width),
                float(self._WAVE_TRACK_H),
            ))
        finally:
            painter.restore()

        if not self._waveform_rms:
            return

        rms = self._waveform_rms
        n = len(rms)
        pps = self._effective_pps
        rms_per_sec = self._waveform_rms_per_sec
        if pps <= 0 or rms_per_sec <= 0:
            return
        px_per_tick = pps / rms_per_sec
        if px_per_tick <= 0:
            return

        start_sec = self._offset_sec
        end_sec   = start_sec + scene_width / pps
        start_idx = max(0, int(start_sec * rms_per_sec))
        end_idx   = min(n, int(end_sec * rms_per_sec) + 1)
        if start_idx >= end_idx:
            return

        wy0 = float(self._WAVE_TRACK_Y) + 2
        wy1 = float(self._WAVE_TRACK_Y + self._WAVE_TRACK_H) - 2

        x0 = 0.0
        wave_end_x = (end_idx - start_idx) * px_per_tick
        x1 = float(min(wave_end_x, scene_width))
        if x1 <= x0:
            return

        span_px = max(1, int(x1 - x0))
        step    = max(1, span_px // 360)

        rms_window  = rms[start_idx:end_idx]
        total_ticks = len(rms_window)
        if total_ticks < 1:
            return

        pts: list[tuple[float, float]] = []
        for x in range(int(x0), int(x1) + 1, step):
            frac = (x - x0) / float(max(1, x1 - x0))
            wf_i = min(total_ticks - 1,
                       max(0, int(round(frac * (total_ticks - 1)))))
            amp = float(rms_window[wf_i])
            yv  = wy1 - amp * (wy1 - wy0 - 2)
            pts.append((float(x), yv))

        if len(pts) < 2:
            return

        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            # Background rectangle + 1-px outline.
            painter.setBrush(QBrush(QColor(40, 40, 40)))
            painter.setPen(QPen(QColor(120, 120, 120), 1))
            painter.drawRect(QRectF(x0, wy0, x1 - x0, wy1 - wy0))

            # Baseline at wy1.
            painter.setPen(QPen(QColor(90, 90, 90), 1))
            painter.drawLine(QPointF(x0, wy1), QPointF(x1, wy1))

            # Translucent fill polygon under the peaks.
            fill_path = QPainterPath()
            fill_path.moveTo(x0, wy1)
            for x, y in pts:
                fill_path.lineTo(x, y)
            fill_path.lineTo(x1, wy1)
            fill_path.closeSubpath()
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.setBrush(
                QBrush(QColor(170, 110, 70, int(255 * 0.35)))
            )
            painter.drawPath(fill_path)

            # 1-px cosmetic outline polyline tracing the peaks.
            outline_path = QPainterPath()
            outline_path.moveTo(pts[0][0], pts[0][1])
            for x, y in pts[1:]:
                outline_path.lineTo(x, y)
            pen = QPen(QColor(255, 170, 130))
            pen.setCosmetic(True)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(outline_path)
        finally:
            painter.restore()

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

    # ── Beat-event preview overlay (mirrors rhythm.py BEAT-DBG strip) ───
    #
    # Reference (``src/rhythm.py``):
    #
    #     x0, x1 = 0.06*W, 0.94*W
    #     y0     = 0.055*H
    #     y1     = y0 + 8
    #     cv2.rectangle(canvas, (x0,y0), (x1,y1), (65,65,65), -1)
    #     cv2.rectangle(canvas, (x0,y0), (x1,y1), (140,140,140), 1)
    #     for idx, (f_ev, kind) in enumerate(events, 1):
    #         px  = x0 + (x1-x0) * f_ev/(total_frames-1)
    #         col = (120,120,120)  if upcoming
    #             | (80,240,255)   if active   (|fi-f_ev|<=1)
    #             | (80,220,120)   if passed
    #         cv2.line(canvas, (px, y0-6), (px, y1+6), col, 1)
    #         if idx<=12: cv2.putText(str(idx), (px-5, y0-10), 0.32, col)
    #     # White current-frame cursor
    #     cv2.line(canvas, (px_now, y0-10), (px_now, y1+10), (255,255,255))
    #
    # OpenCV uses BGR, so the colours below are converted to RGB.
    _BEAT_COL_UPCOMING = QColor(120, 120, 120)   # BGR (120,120,120) — grey
    _BEAT_COL_ACTIVE   = QColor(255, 240,  80)   # BGR ( 80,240,255) — yellow
    _BEAT_COL_PASSED   = QColor( 80, 220, 120)   # BGR ( 80,220,120) — green
    _BEAT_COL_CURSOR   = QColor(255, 255, 255)   # white playhead cursor

    def _beat_strip_color(self, t_event: float, t_now: float,
                          fps: float) -> QColor:
        """Replicate rhythm.py's per-frame state colouring.

        ``rhythm.py`` compares **frames**, not raw seconds.  We do the
        same so the studio behaves identically near beat boundaries:
        ``|fi - f_ev| <= 1`` → active, otherwise upcoming/passed.
        """
        f_ev  = int(round(t_event * fps))
        f_now = int(round(t_now   * fps))
        if f_now < f_ev - 1:
            return self._BEAT_COL_UPCOMING
        if abs(f_now - f_ev) <= 1:
            return self._BEAT_COL_ACTIVE
        return self._BEAT_COL_PASSED

    def _draw_beat_events(self) -> None:
        """Render the BEAT-DBG strip identical to ``rhythm.py``'s overlay.

        Every segment of the project draws its own strip + threshold
        line spanning the segment's [start, end] window, **even when no
        beats have been detected yet**: the threshold line is the
        primary tool for tuning ``beat_height_threshold`` BEFORE Auto
        Gen Block runs, so it must be available immediately.

        Strip background and per-event ticks are interactive
        (:class:`BeatStripBgItem` + :class:`BeatTickItem`) so the user
        can double-click an empty area to insert an event, drag a tick
        horizontally to retime, or right-click for a context menu
        (delete / set kind). All edits flow through ``_on_beat_tick_*``
        / ``_on_beat_strip_*`` handlers below.
        """
        if self._project is None:
            return

        scene_w = self.scene.sceneRect().width()
        if scene_w <= 0:
            return

        # Strip vertical anchors — doubled vs the original rhythm.py
        # mapping for easier mouse targeting: 16-px-tall strip, ticks
        # extend 12 px above and below (40 px total), now-cursor a
        # further 4 px on each side, and numbers sit 6 px above the
        # tick top (label_top - tick_top kept at -9 like before).
        y0 = float(self._BEAT_STRIP_Y)
        y1 = y0 + float(self._BEAT_STRIP_H)
        tick_top    = y0 - 12.0
        tick_bottom = y1 + 12.0
        cursor_top    = y0 - 16.0
        cursor_bottom = y1 + 16.0
        num_y         = y0 - 19.0  # 16 px above + 3 px font ascent slack

        fps = float(getattr(self._project, "output_fps", 30) or 30)
        t_now = float(self._playhead_time_sec)

        # ── Loading placeholder(s) ─────────────────────────────────────
        # Drawn for every segment currently in ``_beat_events_loading``
        # (set by *Auto Gen Block* and *Gen by Chart* before they kick
        # off generation).  The placeholder spans the segment's full
        # beat-strip width with a translucent fill, a dashed border and
        # a centred "Generating beats… (segment_name)" caption so the
        # user can clearly see *which* segment is being processed and
        # that the strip is intentionally empty for the duration.
        loading_top    = float(tick_top)
        loading_bottom = float(tick_bottom)
        loading_height = max(1.0, loading_bottom - loading_top)
        for seg_id in list(self._beat_events_loading):
            seg = self._project.get_segment(seg_id)
            if seg is None:
                continue
            x_lo = max(0.0, self._time_to_x(seg.start_time_sec))
            x_hi = min(float(scene_w), self._time_to_x(seg.end_time_sec))
            if x_hi <= x_lo:
                continue

            holder = QGraphicsRectItem(
                x_lo, loading_top, x_hi - x_lo, loading_height
            )
            holder.setBrush(QBrush(QColor(138, 180, 248, 55)))
            pen = QPen(QColor(138, 180, 248, 200))
            pen.setWidthF(1.2)
            pen.setStyle(Qt.PenStyle.DashLine)
            holder.setPen(pen)
            holder.setZValue(10.5)
            holder.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            holder.setData(0, "beat_loading_holder")
            self.scene.addItem(holder)

            caption = QGraphicsSimpleTextItem(
                f"Generating beats…  ({seg.name})"
            )
            caption.setBrush(QColor("#e8eaed"))
            cap_rect = caption.boundingRect()
            cx = x_lo + (x_hi - x_lo) * 0.5 - cap_rect.width() * 0.5
            cy = loading_top + (loading_height - cap_rect.height()) * 0.5
            caption.setPos(cx, cy)
            caption.setZValue(11)
            caption.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.scene.addItem(caption)

        # ── One BEAT-DBG strip + threshold line per segment ─────────────
        # We iterate ALL segments — not just the ones that already have
        # detected events — so the threshold line is available before
        # Auto Gen Block has ever run.  ``events`` falls back to an
        # empty list for segments without an entry in ``_beat_events``.
        for seg in self._project.sorted_segments():
            seg_id = seg.id
            events = self._beat_events.get(seg_id, [])
            base_t = float(seg.start_time_sec)
            end_t  = float(seg.end_time_sec)

            full_x0 = self._time_to_x(base_t)
            full_x1 = self._time_to_x(end_t)
            sx0 = max(0.0, full_x0)
            sx1 = min(scene_w, full_x1)
            if sx1 < 0 or full_x0 > scene_w:
                continue
            if sx1 - sx0 < 2.0:
                continue

            strip = BeatStripBgItem(
                self, seg_id, QRectF(sx0, y0, sx1 - sx0, y1 - y0),
            )
            self.scene.addItem(strip)

            # 1b. Draggable red threshold line over the waveform.
            #     Always drawn so the user can tune
            #     ``beat_height_threshold`` BEFORE running Auto Gen
            #     Block — that's the threshold the detector uses to
            #     decide which onsets become beats in the first place.
            wy_top = float(self._WAVE_TRACK_Y) + 2.0
            wy_bot = float(
                self._WAVE_TRACK_Y + self._WAVE_TRACK_H
            ) - 2.0
            if wy_bot > wy_top + 4.0 and sx1 > sx0 + 4.0:
                thr_line = WaveformThresholdLine(
                    self,
                    seg_id,
                    x_left=sx0,
                    x_right=sx1,
                    wy_top=wy_top,
                    wy_bottom=wy_bot,
                    threshold=float(getattr(
                        seg, "beat_height_threshold", 0.0
                    )),
                )
                self.scene.addItem(thr_line)
                self._threshold_map[seg_id] = thr_line

            # 2. Per-event interactive tick (movable + selectable).
            #    Tick travel is clamped to the segment's full range
            #    [full_x0, full_x1] (not the visible-clipped one) so
            #    dragging out of the viewport doesn't truncate at the
            #    visible edge.
            # Threshold from the segment model — beats below it are
            # drawn dimmed (so the user still sees what's being
            # filtered) but excluded from rendering downstream.
            threshold = float(getattr(seg, "beat_height_threshold", 0.0))
            for event_idx, ev in enumerate(events):
                t_local = float(ev[0])
                kind = str(ev[1])
                height = float(ev[2]) if len(ev) >= 3 else 1.0
                t_proj = base_t + t_local
                if t_proj > end_t + 1e-3:
                    continue
                x = self._time_to_x(t_proj)
                if x < sx0 - 4 or x > sx1 + 4:
                    continue

                below_thresh = height < threshold - 1e-6
                col = self._beat_strip_color(t_proj, t_now, fps)
                display_idx = event_idx + 1
                label = str(display_idx) if display_idx <= 12 else None

                tick = BeatTickItem(
                    panel=self,
                    segment_id=seg_id,
                    event_idx=event_idx,
                    kind=kind,
                    color=col,
                    tick_top=tick_top,
                    tick_bottom=tick_bottom,
                    x_min=full_x0,
                    x_max=full_x1,
                    idx_label=label,
                    num_y=num_y,
                )
                tick.setPos(x, tick_top)
                tick.setToolTip(
                    f"#{display_idx} {kind} @ {t_proj:.3f}s "
                    f"(local {t_local:.3f}s)  height={height:.2f}\n"
                    "Drag to retime · Right-click for menu"
                )
                # Beats whose audio amplitude is below the user's
                # threshold are drawn dimmed (25 %) so the user can
                # still see WHAT got filtered out — they remain
                # interactive (drag / kind change / delete) so dragging
                # the threshold line back down restores them at 100 %
                # opacity without re-running detect.
                if below_thresh:
                    tick.setOpacity(0.25)
                self.scene.addItem(tick)
                self._tick_map[(seg_id, event_idx)] = tick

                # Rule-mode dashed guide lines and the white "now"
                # cursor through the strip are painted in
                # :meth:`_paint_beat_strip_decorations` (the scene's
                # background pass) — keeping them out of the scene's
                # item list means a click on the strip can never
                # accidentally hide them via the same Qt regression
                # that motivated the waveform refactor.

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
                if not self._focus_manual_zoom:
                    # Auto-fit branch: derive pps so the segment exactly
                    # fills the viewport and the scene is no wider than
                    # what's visible (no scrollbar in pristine focus).
                    new_pps = max(ZOOM_MIN_PPS, viewport_w / segment.duration_sec)
                    new_pps = min(ZOOM_MAX_PPS, new_pps)
                    if abs(new_pps - self._effective_pps) > 0.01:
                        self._effective_pps = new_pps
                        self.view._pps = new_pps
                    self.scene.setSceneRect(0, 0, float(viewport_w), self._SCENE_H)
                else:
                    # Manual-zoom branch: respect the pps the user dialled
                    # in via Ctrl+wheel and grow the scene so they can
                    # scroll horizontally to inspect the zoomed-in segment.
                    seg_w = segment.duration_sec * self._effective_pps
                    # A bit of trailing room so the segment's last frame
                    # isn't flush against the right edge when scrolled
                    # all the way over.
                    padding_px = max(40.0, viewport_w * 0.1)
                    scene_w = max(float(viewport_w), seg_w + padding_px)
                    self.scene.setSceneRect(0, 0, scene_w, self._SCENE_H)
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
        # Brush / pen are intentionally NOT set here — the visible
        # fill + outline are painted by :meth:`_paint_segment_blocks`
        # during the scene's ``drawBackground`` pass so they cannot
        # be hidden by mouse handling.  We still stash the mode
        # colour on the item so that pass can read it back without
        # another segment lookup.
        color = MODE_COLORS.get(segment.mode, QColor("#3bb6ff"))
        block._fill_color = color  # type: ignore[attr-defined]
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

    @Slot()
    def _on_selection_changed(self) -> None:
        """Reconcile Qt's scene selection with our segment focus state.

        Two important invariants:

        1. *Segment focus* (which segment's waveform / inspector /
           preview is shown) lives in ``_selected_segment_id`` and
           must NOT be dropped when the user clicks an unrelated piece
           of decoration (waveform background, beat strip, ruler, …).
           Without this, every click on the waveform area would
           silently nuke the waveform — exactly the bug the user
           reported.

        2. A deliberate "click the void" deselect goes through
           :meth:`_on_empty_clicked` — it sets
           ``_intentional_segment_deselect = True`` and
           ``_selected_segment_id = None`` *before* calling
           ``clearSelection()``.  The handler must **not** call
           :signal:`segment_selected` ``(None)`` a second time (that
           is reserved for the panel method itself) and must not treat
           a *failed* re-select as a deselect, or the preview would
           clear the waveform on every non-selectable hit.
        """
        if not self._project:
            return
        selected = self.scene.selectedItems()
        seg_blocks = [it for it in selected if isinstance(it, SegmentRectItem)]

        if not selected:
            if self._selected_segment_id and self._selected_segment_id in self._block_map:
                # Selection was nuked by a click on a non-selectable
                # decoration — restore it silently so the segment
                # block's "selected" highlight (and downstream context
                # like the waveform) survives.
                block = self._block_map[self._selected_segment_id]
                self.scene.blockSignals(True)
                try:
                    block.setSelected(True)
                finally:
                    self.scene.blockSignals(False)
                return
            if (
                self._selected_segment_id
                and self._selected_segment_id not in self._block_map
            ):
                # The panel still has a *logical* current segment, but
                # :attr:`_block_map` is stale (race, mid-refresh) — *never*
                # broadcast a fake deselect.  Rebuild the scene on the next
                # event-loop tick (not inside this ``selectionChanged``) so
                # we do not re-enter a full ``refresh`` mid-callback.  Skip
                # when the user is intentionally clearing via empty-click
                # (we already nulled :attr:`_selected_segment_id`).
                if (
                    not self._intentional_segment_deselect
                    and self._project is not None
                ):
                    seg = self._project.get_segment(
                        self._selected_segment_id
                    )
                    if seg is not None:
                        def _deferred_rebuild_selection() -> None:
                            if self._intentional_segment_deselect:
                                return
                            sid = self._selected_segment_id
                            if not sid:
                                return
                            if sid in self._block_map:
                                b = self._block_map[sid]
                                self.scene.blockSignals(True)
                                try:
                                    b.setSelected(True)
                                finally:
                                    self.scene.blockSignals(False)
                                return
                            self.refresh()
                            if (
                                self._selected_segment_id == sid
                                and sid in self._block_map
                            ):
                                b2 = self._block_map[sid]
                                self.scene.blockSignals(True)
                                try:
                                    b2.setSelected(True)
                                finally:
                                    self.scene.blockSignals(False)

                        QTimer.singleShot(0, _deferred_rebuild_selection)
            if self._intentional_segment_deselect:
                self._intentional_segment_deselect = False
            return

        if not seg_blocks:
            # Selection contains only non-segment items (typically a
            # beat tick that the user grabbed).  Don't touch segment
            # focus — but make sure the segment block is *also*
            # selected so its highlight stays visible while the user
            # works on the tick.
            if (
                self._selected_segment_id
                and self._selected_segment_id in self._block_map
            ):
                block = self._block_map[self._selected_segment_id]
                if not block.isSelected():
                    self.scene.blockSignals(True)
                    try:
                        block.setSelected(True)
                    finally:
                        self.scene.blockSignals(False)
            return

        block = seg_blocks[0]
        segment = self._project.get_segment(block.segment_id)
        if segment is None:
            return
        self._selected_segment_id = segment.id
        self.split_button.setEnabled(True)
        self.auto_gen_button.setEnabled(True)
        self.gen_by_chart_button.setEnabled(True)
        self.clear_beats_button.setEnabled(True)
        self.overview_bar.set_selected(segment.id)
        self.segment_selected.emit(segment)

    def _on_empty_clicked(self) -> None:
        # Order matters — clear the focus state BEFORE the scene's
        # clearSelection() so the resulting selectionChanged hook
        # recognises this as a genuine deselect.  See
        # :meth:`_on_selection_changed` for the contract.
        self._intentional_segment_deselect = True
        self._selected_segment_id = None
        self._focused_beat = None
        self._defocus_other_threshold_lines(None)
        self.scene.clearSelection()
        self.split_button.setEnabled(False)
        self.auto_gen_button.setEnabled(False)
        self.gen_by_chart_button.setEnabled(False)
        self.clear_beats_button.setEnabled(False)
        self.overview_bar.set_selected(None)
        self.segment_selected.emit(None)

    def _on_auto_gen_clicked(self) -> None:
        """Forward the click as :pyattr:`auto_gen_block_requested`.

        Wipes existing beats (panel + persisted segment list) and
        flips the segment into the "Detecting beats…" loading hint
        *before* the request fires, so the user never sees a frame
        of stale ticks while the rhythm-core subprocess spins up.
        See :meth:`_wipe_beats_and_show_loading` for the wipe
        rationale.

        Guards against the (theoretically impossible because the button is
        disabled then) "no selected segment" case so MainWindow never sees
        a stray empty-id signal.
        """
        if self._selected_segment_id is None:
            return
        seg_id = self._selected_segment_id
        self._wipe_beats_and_show_loading(seg_id)
        self.auto_gen_block_requested.emit(seg_id)

    def _on_clear_beats_clicked(self) -> None:
        """Remove every beat event from the currently selected segment."""
        if self._selected_segment_id is None:
            return
        seg_id = self._selected_segment_id
        events = self._beat_events.get(seg_id)
        if not events:
            return
        events.clear()
        self._focused_beat = None
        self._pending_tick_select_after_refresh = []
        self._schedule_beat_commit(seg_id)

    def _on_gen_by_chart_clicked(self) -> None:
        """Derive beat ticks 1-for-1 from every peak in the RMS chart.

        Click flow (the ``…WIPE → LOADING → COMPUTE → COMMIT`` chain
        is shared with *Auto Gen Block* — see :meth:`_wipe_beats_and_show_loading`
        for the wipe details):

        1. Wipe both ``_beat_events[seg_id]`` and the *persisted*
           :pyattr:`Segment.beat_events` so the user sees a clean
           strip the moment the click registers — no half-stale
           ticks lingering during generation.
        2. Flip the segment into the "Detecting beats…" loading
           hint via :meth:`set_beat_events_loading`.
        3. Defer the actual peak-detection work to the next
           event-loop tick using ``QTimer.singleShot(0, …)``.  The
           defer is essential: the wipe + loading repaint won't
           reach the screen until Qt drains the current event,
           and the synchronous numpy work below would block that
           paint indefinitely without it.

        Detection itself: every ``rms[i]`` sample where
        ``rms[i] > rms[i-1] AND rms[i] >= rms[i+1]`` is a "peak
        tip" on the chart.  No ``distance`` / ``prominence``
        filter — the goal is one tick per visible peak — only a
        small ``AMP_FLOOR`` to ignore floating-point ripple in
        silent stretches.  Honours
        :pyattr:`Segment.beat_height_threshold` (red slider).
        """
        if self._selected_segment_id is None:
            return
        seg_id = self._selected_segment_id
        if not self._waveform_rms or self._waveform_rms_per_sec <= 0:
            return
        if self._project is None:
            return
        if self._project.get_segment(seg_id) is None:
            return

        # Step 1+2: wipe + loading hint, repaint immediately.
        self._wipe_beats_and_show_loading(seg_id)

        # Step 3: defer compute so the loading hint paints first.
        QTimer.singleShot(
            0, lambda sid=seg_id: self._run_gen_by_chart(sid)
        )

    def _run_gen_by_chart(self, seg_id: str) -> None:
        """Synchronous body of the *Gen by Chart* peak detector.

        Always called from a deferred ``QTimer.singleShot`` callback
        so the prior wipe + loading-hint paint has had a chance to
        flush.  Safe to call once per click; no-ops if the segment
        was deleted while we were waiting our turn.

        Detection algorithm — *prominence-based peak picking*:

        1. **Light smoothing** (3-sample uniform moving average,
           ≈30 ms at 100 Hz RMS) suppresses single-sample
           quantisation noise without blurring real transients.
        2. ``scipy.signal.find_peaks`` with a **prominence** floor
           keeps every peak that rises at least
           ``PROMINENCE_FLOOR`` above its neighbouring valleys —
           the same definition humans use when eyeballing "is
           that a real peak".  Crucially we do **not** pass
           ``distance`` (or pass only a 2-sample minimum) so peaks
           clustered tightly together are *all* preserved.
        3. **Threshold filter** (red bar) on the *original* RMS
           amplitude so the user-visible chart and the detected
           ticks share the same vertical scale.

        ``PROMINENCE_FLOOR`` is picked as
        ``max(ABSOLUTE_FLOOR, RELATIVE * np.std(rms))`` so quiet
        clips (low std) still get picky-enough detection while
        loud clips don't drown in micro-bumps.
        """
        if self._project is None:
            return
        seg = self._project.get_segment(seg_id)
        if seg is None:
            self._beat_events_loading.discard(seg_id)
            self.refresh()
            return
        rms_full = np.asarray(self._waveform_rms, dtype=np.float32)
        rps = float(self._waveform_rms_per_sec)
        seg_dur = float(seg.duration_sec or 0.0)
        if rms_full.size < 5 or rps <= 0 or seg_dur <= 0.0:
            self._beat_events_loading.discard(seg_id)
            self.refresh()
            return

        # ── Slice rms to JUST the segment's media-time window ────────
        # ``_waveform_rms`` holds the *entire* media file (indexed by
        # media-time), but the waveform-track painter only renders the
        # slice ``rms[_offset_sec * rps … _offset_sec * rps + view]``
        # which, in focus mode, equals ``rms[start_time_sec * rps : …]``.
        # Detecting peaks on the full array would (a) emit ticks for
        # peaks outside the segment's window and (b) yield ``t_local``
        # values that don't line up with the visible waveform when
        # ``seg.start_time_sec > 0`` — exactly the misalignment the
        # user reported.  Slicing here makes the local peak index
        # directly equal ``t_local * rps`` so every detected tick lands
        # under its peak regardless of where the segment sits on the
        # timeline.
        start_sample = int(round(float(seg.start_time_sec) * rps))
        end_sample   = int(round(
            (float(seg.start_time_sec) + seg_dur) * rps
        ))
        start_sample = max(0, min(int(rms_full.size), start_sample))
        end_sample   = max(start_sample, min(int(rms_full.size), end_sample))
        rms = rms_full[start_sample:end_sample]
        if rms.size < 5:
            self._beat_events_loading.discard(seg_id)
            self.refresh()
            return

        # ── 1. Smooth (3-sample box, mode='same' keeps length) ───────
        kernel = np.ones(3, dtype=np.float32) / 3.0
        smoothed = np.convolve(rms, kernel, mode="same")

        # ── 2. Prominence floor — adaptive but with a hard minimum ──
        # ABSOLUTE_FLOOR = 1.5 % of full-scale amplitude (peaks
        # smaller than this are imperceptible on a 0-1 chart).
        # RELATIVE  = ¼ of the signal std-dev so a track with
        # large dynamic range still demands somewhat-prominent
        # peaks while a near-silent track keeps the absolute floor.
        ABSOLUTE_FLOOR = 0.015
        RELATIVE = 0.25
        sig_std = float(np.std(smoothed))
        prominence = max(ABSOLUTE_FLOOR, RELATIVE * sig_std)
        # Tiny distance gate: 2 samples (~20 ms) prevents adjacent-
        # sample double counting after smoothing. Kept tight so
        # tightly-spaced real transients (rolls, fast hi-hats) are
        # still emitted as separate ticks.
        min_distance = max(1, int(round(rps * 0.02)))

        try:
            from scipy.signal import find_peaks  # type: ignore
            peak_idx_arr, _props = find_peaks(
                smoothed,
                prominence=prominence,
                distance=min_distance,
            )
        except Exception:
            # Defensive fallback: pure-numpy local max if scipy is
            # unavailable for any reason. Same prominence intent
            # but cheaper — use the smoothed signal and reject
            # peaks whose left/right valley delta is below floor.
            is_peak = (smoothed[1:-1] > smoothed[:-2]) & (
                smoothed[1:-1] >= smoothed[2:]
            )
            cand = np.flatnonzero(is_peak) + 1
            kept: list[int] = []
            for p in cand:
                lo = max(0, int(p) - max(2, min_distance))
                hi = min(int(smoothed.size), int(p) + max(2, min_distance) + 1)
                local_min = float(np.min(smoothed[lo:hi]))
                if float(smoothed[int(p)]) - local_min >= prominence:
                    kept.append(int(p))
            peak_idx_arr = np.asarray(kept, dtype=np.int64)

        new_events: list[tuple[float, str, float]] = []
        if peak_idx_arr.size > 0:
            AMP_FLOOR = 0.01
            max_h = float(np.max(rms))
            max_h = max(max_h, 1e-9)
            thr = float(getattr(seg, "beat_height_threshold", 0.0) or 0.0)
            thr = max(0.0, min(1.0, thr))
            for p in peak_idx_arr:
                # Use the raw (un-smoothed) RMS for amplitude so
                # the height stored on the event matches what the
                # waveform chart shows — smoothing was only used
                # to find the *position* of the peak.
                amp = float(rms[int(p)])
                if amp < AMP_FLOOR:
                    continue
                t_local = float(int(p)) / rps
                if t_local < 0.0 or t_local >= seg_dur:
                    continue
                h_norm = max(0.0, min(1.0, amp / max_h))
                if h_norm < thr - 1e-6:
                    continue
                new_events.append((t_local, "L", h_norm))
            new_events.sort(key=lambda e: e[0])

        self._beat_events[seg_id] = new_events
        self._beat_events_loading.discard(seg_id)
        self._focused_beat = None
        self._pending_tick_select_after_refresh = []
        self._schedule_beat_commit(seg_id)

    def _wipe_beats_and_show_loading(self, seg_id: str) -> None:
        """Empty both panel and segment beat lists, then show loading.

        Called by *both* gen entrypoints (*Auto Gen Block* in
        :class:`MainWindow`, *Gen by Chart* in this panel) so the
        user always sees a clean strip + "Detecting beats…" hint
        the moment they click — never a frame of stale ticks while
        the new ones are being computed.

        The persistent :pyattr:`Segment.beat_events` is also
        cleared and ``beat_events_edited`` is emitted so MainWindow
        syncs the empty list to disk via autosave.  If the
        downstream gen never produces a result (subprocess crash,
        no waveform, etc.) the segment is left empty — same end
        state the user would get from clicking *Clear Beats*
        directly, which matches the explicit "wipe to regenerate"
        intent.
        """
        if self._project is None:
            return
        seg = self._project.get_segment(seg_id)
        if seg is None:
            return
        self._beat_events[seg_id] = []
        if seg.beat_events:
            seg.beat_events = []
        self._focused_beat = None
        self._pending_tick_select_after_refresh = []
        self.set_beat_events_loading(seg_id)
        # Persist the wipe immediately so the project is dirty
        # even if gen never completes; autosave snapshots the
        # cleared state.
        self.beat_events_edited.emit(seg_id)

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

        In focus mode this scales ``_effective_pps`` IN PLACE — focus is
        preserved so the user can keep inspecting the focused segment at
        a tighter or looser zoom.  ``pixels_per_second`` (and therefore
        the header zoom slider) is left untouched in focus mode because
        that value represents the OVERVIEW zoom we'll restore when the
        user eventually exits focus via Esc / Fit / clicking outside.

        In overview mode the zoom updates the shared ``pixels_per_second``
        through ``_apply_zoom`` so the slider stays in sync.
        """
        in_focus = self._focus_segment_id is not None
        cur_pps = self._effective_pps if in_focus else self.pixels_per_second

        scene_x = self.view.mapToScene(int(viewport_x), 0).x()
        time_at_cursor = self._x_to_time(scene_x)

        new_pps = max(ZOOM_MIN_PPS, min(ZOOM_MAX_PPS, cur_pps * factor))
        if abs(new_pps - cur_pps) < 1e-6:
            # Already clamped at the limit — nothing to do, and skipping
            # the refresh avoids a useless scrollbar jiggle.
            return

        if in_focus:
            self._focus_manual_zoom = True
            self._effective_pps = new_pps
            self.view._pps = new_pps
            self.refresh()
        else:
            # Goes through the shared helper so pixels_per_second and
            # the slider stay in sync.  ``exit_focus=False`` is safe
            # here because we already know we're not in focus.
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

        When the user has Ctrl+wheel-zoomed into the focused segment we
        keep their scroll position so a viewport resize doesn't yank them
        back to the segment's start.

        We defer with `QTimer.singleShot(0, ...)` so Qt can finish all its
        internal resize bookkeeping (scrollbar adjustment, layout flushes)
        before we clear and rebuild the scene.  Calling `scene.clear()` while
        Qt is still mid-resize can crash the app.
        """
        if self._focus_segment_id is not None:
            if self._focus_manual_zoom:
                QTimer.singleShot(0, self.refresh)
            else:
                QTimer.singleShot(0, self._refresh_and_reset_scroll)

    def _refresh_and_reset_scroll(self) -> None:
        """Refresh then reset scroll to x=0 (used in focus mode after resize)."""
        self.refresh()
        self.view.horizontalScrollBar().setValue(0)

