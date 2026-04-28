"""Preview player panel using Qt multimedia."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

import cv2
from PySide6.QtCore import QPoint, QRect, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from studio.models import MediaItem, Segment

if TYPE_CHECKING:
    # Imported only at type-check time so the heavyweight ``rhythm.py``
    # dependency chain (librosa, OpenCV, ffmpeg-python) doesn't get
    # pulled in just by importing :mod:`preview_panel`.  At runtime the
    # ``LiveFrameRenderer`` instance arrives via ``start_live_preview``
    # so we never need the type at module-load.
    from src.live_renderer import LiveFrameRenderer


# ---------------------------------------------------------------------------
# Stickman draw-box editor widget
# ---------------------------------------------------------------------------
class StickmanBoxOverlay(QWidget):
    """Draggable + resizable rectangle for setting the stickman draw-box.

    This widget is used as a **dedicated page inside the player
    stage_stack** so it is never layered over the native QVideoWidget
    HWND (which would make it invisible on Windows).  When the user
    toggles "Stick Box" the stage switches to this page; toggling off
    restores the previous page.

    Coordinates are stored as fractions (0..1) of the widget rect so
    the value is resolution-independent.  The same fractions are saved
    on :class:`Segment.stickman_location` and forwarded to
    ``rhythm.py`` as ``--stick_x0/y0/w/h`` pixels at render time.

    Emits ``box_committed`` on mouse release; ``box_changing`` while
    dragging.  Both carry ``(x, y, w, h)`` floats in [0, 1].
    """

    box_committed = Signal(float, float, float, float)
    box_changing = Signal(float, float, float, float)

    _HANDLE_SIZE = 14
    _MIN_FRAC = 0.02

    def __init__(self, parent: QWidget) -> None:
        # Tool window owned by `parent` so it is destroyed with the
        # panel, but floating independently of the Qt widget hierarchy.
        # FramelessWindowHint + WA_TranslucentBackground → a DWM-
        # composited layered HWND that can be placed above QVideoWidget's
        # native HWND on Windows.  WindowStaysOnTopHint keeps it in
        # front even while the video is playing.
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._x: float = 0.010
        self._y: float = 0.090
        self._w: float = 0.135
        self._h: float = 0.540
        self._drag_kind: Optional[str] = None
        self._drag_anchor: QPoint = QPoint()
        self._drag_x0 = 0.0
        self._drag_y0 = 0.0
        self._drag_w0 = 0.0
        self._drag_h0 = 0.0

    # ----- public API ---------------------------------------------------
    def set_normalized(self, x: float, y: float, w: float, h: float) -> None:
        """Set the box to the given fractional rect and repaint."""
        x = max(0.0, min(1.0 - self._MIN_FRAC, float(x)))
        y = max(0.0, min(1.0 - self._MIN_FRAC, float(y)))
        w = max(self._MIN_FRAC, min(1.0 - x, float(w)))
        h = max(self._MIN_FRAC, min(1.0 - y, float(h)))
        self._x, self._y, self._w, self._h = x, y, w, h
        self.update()

    def normalized(self) -> tuple[float, float, float, float]:
        """Return the current box as ``(x, y, w, h)`` fractions."""
        return self._x, self._y, self._w, self._h

    # ----- internals ----------------------------------------------------
    def _box_rect_px(self) -> QRect:
        s = self.rect()
        return QRect(
            int(self._x * s.width()),
            int(self._y * s.height()),
            max(1, int(self._w * s.width())),
            max(1, int(self._h * s.height())),
        )

    def _hit_test(self, pos: QPoint) -> Optional[str]:
        bx = self._box_rect_px()
        hs = self._HANDLE_SIZE
        # Corner handles centered on each corner — listed before the
        # body check so resizing a tiny box from a corner takes
        # precedence over moving it.
        corners = {
            "tl": QRect(bx.left() - hs // 2, bx.top() - hs // 2, hs, hs),
            "tr": QRect(bx.right() - hs // 2, bx.top() - hs // 2, hs, hs),
            "bl": QRect(bx.left() - hs // 2, bx.bottom() - hs // 2, hs, hs),
            "br": QRect(bx.right() - hs // 2, bx.bottom() - hs // 2, hs, hs),
        }
        for kind, r in corners.items():
            if r.contains(pos):
                return kind
        if bx.contains(pos):
            return "move"
        return None

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        kind = self._hit_test(event.position().toPoint())
        if kind is None:
            return
        self._drag_kind = kind
        self._drag_anchor = event.position().toPoint()
        self._drag_x0, self._drag_y0 = self._x, self._y
        self._drag_w0, self._drag_h0 = self._w, self._h
        event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_kind is None:
            kind = self._hit_test(event.position().toPoint())
            cursors = {
                "tl": Qt.CursorShape.SizeFDiagCursor,
                "br": Qt.CursorShape.SizeFDiagCursor,
                "tr": Qt.CursorShape.SizeBDiagCursor,
                "bl": Qt.CursorShape.SizeBDiagCursor,
                "move": Qt.CursorShape.SizeAllCursor,
            }
            self.setCursor(cursors.get(kind, Qt.CursorShape.ArrowCursor))
            return
        s = self.rect()
        if s.width() <= 0 or s.height() <= 0:
            return
        cur = event.position().toPoint()
        dx = (cur.x() - self._drag_anchor.x()) / float(s.width())
        dy = (cur.y() - self._drag_anchor.y()) / float(s.height())
        x, y, w, h = self._drag_x0, self._drag_y0, self._drag_w0, self._drag_h0
        if self._drag_kind == "move":
            x = max(0.0, min(1.0 - w, x + dx))
            y = max(0.0, min(1.0 - h, y + dy))
        elif self._drag_kind == "tl":
            new_x = max(0.0, min(x + w - self._MIN_FRAC, x + dx))
            new_y = max(0.0, min(y + h - self._MIN_FRAC, y + dy))
            w = w - (new_x - x)
            h = h - (new_y - y)
            x, y = new_x, new_y
        elif self._drag_kind == "tr":
            new_y = max(0.0, min(y + h - self._MIN_FRAC, y + dy))
            new_w = max(self._MIN_FRAC, min(1.0 - x, w + dx))
            h = h - (new_y - y)
            y, w = new_y, new_w
        elif self._drag_kind == "bl":
            new_x = max(0.0, min(x + w - self._MIN_FRAC, x + dx))
            new_h = max(self._MIN_FRAC, min(1.0 - y, h + dy))
            w = w - (new_x - x)
            x, h = new_x, new_h
        elif self._drag_kind == "br":
            w = max(self._MIN_FRAC, min(1.0 - x, w + dx))
            h = max(self._MIN_FRAC, min(1.0 - y, h + dy))
        self._x, self._y, self._w, self._h = x, y, w, h
        self.update()
        self.box_changing.emit(self._x, self._y, self._w, self._h)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drag_kind is None:
            return
        self._drag_kind = None
        self.box_committed.emit(self._x, self._y, self._w, self._h)

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # WA_TranslucentBackground: the widget window is fully transparent
        # except for what we explicitly paint here.  The video playing
        # behind shows through the unpainted areas.
        bx = self._box_rect_px()

        # Semi-transparent cyan fill so the region is recognisable but
        # the video underneath is still legible (~50% opacity).
        p.fillRect(bx, QBrush(QColor(0, 200, 255, 80)))

        # Bright dashed outline
        pen = QPen(QColor(0, 230, 255), 2.5, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(bx)

        # Corner resize handles — solid so they are easy to grab
        hs = self._HANDLE_SIZE
        p.setPen(QPen(QColor(255, 255, 255), 1.5))
        p.setBrush(QBrush(QColor(0, 210, 255, 230)))
        for cx, cy in [
            (bx.left(), bx.top()),
            (bx.right(), bx.top()),
            (bx.left(), bx.bottom()),
            (bx.right(), bx.bottom()),
        ]:
            p.drawRect(cx - hs // 2, cy - hs // 2, hs, hs)

        # Coordinate label — dark shadow then bright text for readability
        # regardless of what's playing beneath.
        label = (f"x:{self._x*100:.1f}%  y:{self._y*100:.1f}%  "
                 f"w:{self._w*100:.1f}%  h:{self._h*100:.1f}%")
        lx, ly = bx.left() + 7, bx.top() + 17
        p.setPen(QColor(0, 0, 0, 160))
        p.drawText(lx + 1, ly + 1, label)
        p.setPen(QColor(220, 245, 255))
        p.drawText(lx, ly, label)


def format_ms(ms: int) -> str:
    """Format milliseconds to mm:ss string."""
    seconds = max(0, ms // 1000)
    mm, ss = divmod(seconds, 60)
    return f"{mm:02d}:{ss:02d}"


class PreviewPanel(QWidget):
    """Media preview with source selector and playback controls."""

    playhead_changed = Signal(float)
    # Forward Qt's playbackStateChanged signal as a plain int (the
    # ``QMediaPlayer.PlaybackState`` enum value).  Consumed by
    # :class:`MainWindow` to enable / disable timeline scrubbing — we
    # only allow the user to scrub the red playhead while the preview
    # is in Playing or Paused state, never when it's Stopped.
    playback_state_changed = Signal(int)
    # Emitted when the user finishes adjusting the stickman draw-box
    # overlay (mouse release).  Carries ``(segment_id, location_dict)``
    # where ``location_dict`` is ``{"x", "y", "w", "h"}`` fractions
    # (0..1) of the rendered video frame.  MainWindow listens, writes
    # to ``segment.stickman_location``, and triggers a project save.
    stickman_location_changed = Signal(str, dict)
    # Emitted whenever the panel exits live-preview mode, whether the
    # caller explicitly invoked ``stop_live_preview`` or the panel
    # auto-stopped because a different source was loaded (user
    # selected another segment / switched the source-combo / a
    # rendered video auto-loaded after export).  MainWindow listens
    # so its ``_preview_mode_active`` flag stays in sync with the
    # panel's actual state.
    live_preview_stopped = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._selected_media: MediaItem | None = None
        self._selected_segment: Segment | None = None
        # True if user clicked Play but source is still loading.
        self._pending_play = False
        # Position (ms) to seek to once media becomes ready; -1 = none pending.
        self._pending_seek_ms: int = -1
        # Tracks current media URL so we can reload if play() after end fails.
        self._current_url: QUrl = QUrl()
        # Becomes True once LoadedMedia/BufferedMedia fires for the current source.
        # Used to prevent intermediate Buffering transitions from re-disabling the button.
        self._media_ready = False
        # Number of times we've already retried the current URL (for stuck loads).
        self._load_retries = 0
        # Offset (project timeline seconds) corresponding to position 0 of
        # the currently-loaded media.  Raw audio = 0.  A segment-rendered
        # video = segment.start_time_sec, so the project-level red playhead
        # tracks the *project* time as the video plays, not the video's
        # internal 0..duration_sec range.
        self._playhead_offset_sec: float = 0.0
        # ----- live-preview state -----
        # When ``_live_active`` is True the panel is in "draw-on-demand"
        # mode: the QMediaPlayer plays only the audio track of the
        # active segment, while a QTimer polls the player position and
        # asks ``_live_renderer.render_at(pos)`` for the frame to show
        # in ``_live_label``.  All of this lives next to the existing
        # video-playback path (which still owns ``self.video_widget``)
        # so users can toggle Preview on/off without us tearing down
        # the panel.  See ``start_live_preview`` / ``stop_live_preview``
        # for the lifecycle.
        self._live_active: bool = False
        self._live_renderer: Optional["LiveFrameRenderer"] = None
        self._live_buffer_rgb: object = None  # holds QImage backing array alive
        self._build_ui()

    def set_source_media(self, media: MediaItem | None) -> None:
        """Set selected media source and load it for preview."""
        self._selected_media = media
        # Raw media items always live at project-time 0.
        self._playhead_offset_sec = 0.0
        if self.source_combo.currentData() == "media":
            self._load_active_source()

    # ------------------------------------------------------------------
    # Stickman overlay
    # ------------------------------------------------------------------
    def _segment_stick_fractions(
        self, segment: Optional[Segment]
    ) -> tuple[float, float, float, float]:
        """Resolve a segment's stickman fractions or return safe defaults."""
        defaults = (0.010, 0.090, 0.135, 0.540)
        if segment is None:
            return defaults
        loc = getattr(segment, "stickman_location", None) or {}
        try:
            return (
                float(loc.get("x", defaults[0])),
                float(loc.get("y", defaults[1])),
                float(loc.get("w", defaults[2])),
                float(loc.get("h", defaults[3])),
            )
        except (TypeError, ValueError):
            return defaults

    def _segment_stickman_enabled(self, segment: Optional[Segment]) -> bool:
        """True when the segment's render settings have stickman on."""
        if segment is None:
            return False
        rs = getattr(segment, "render_settings", None) or {}
        # Default in BaseRenderSettings is True, so a missing key is
        # treated as enabled — only an explicit ``False`` disables it.
        val = rs.get("stickman", True)
        return bool(val)

    def _refresh_stickman_button_state(self) -> None:
        """Enable/disable the toolbar toggle based on current segment."""
        seg = self._selected_segment
        enabled = self._segment_stickman_enabled(seg)
        self.stickman_button.setEnabled(enabled)
        if not enabled and self.stickman_button.isChecked():
            # Auto-untoggle when leaving a stickman-enabled segment so a
            # stale overlay doesn't linger over an unrelated source.
            self.stickman_button.setChecked(False)
        if enabled and seg is not None:
            self.stickman_overlay.set_normalized(
                *self._segment_stick_fractions(seg)
            )

    def _sync_stickman_overlay_pos(self) -> None:
        """Keep the floating overlay snapped to the rendered-image rect.

        IMPORTANT: snap to the **letterboxed image rect**, not the full
        ``stage_stack``.  The live frame is rendered at the renderer's
        canvas size (typically 1280×720) and shown via
        :meth:`QPixmap.scaled` with ``Qt.KeepAspectRatio`` inside
        ``live_label`` — which centres the pixmap and pads the
        non-matching dimension with empty space.  If the overlay
        covered the full stage_stack the user's fractional box would
        be interpreted against a LARGER rect than the renderer uses,
        and the dashed cyan box would visibly drift away from the
        actual stickman (e.g. user puts box at x=8% of the panel, but
        the renderer draws stickman at x=8% of only the centred image
        area, which sits ~10–60 px to the right of the panel's left
        edge depending on aspect mismatch).

        Re-runs at ~50 ms via ``_stickman_pos_timer`` so window
        resizes / splitter drags are tracked automatically.
        """
        if not self._stickman_edit_active:
            return
        rect = self._rendered_image_rect_global()
        if rect is None:
            # Fallback: cover the full stage_stack so the box is at
            # least visible — happens before the first frame is
            # rendered or when the live renderer isn't attached yet.
            tl = self.stage_stack.mapToGlobal(QPoint(0, 0))
            sz = self.stage_stack.size()
            self.stickman_overlay.setGeometry(
                tl.x(), tl.y(), sz.width(), sz.height()
            )
            return
        self.stickman_overlay.setGeometry(rect)

    def _rendered_image_rect_global(self) -> Optional["QRect"]:
        """Compute the on-screen rect of the centred rendered image.

        Returns global-coordinate ``QRect`` matching where the live
        frame's pixels actually appear inside ``stage_stack``.  Returns
        ``None`` if the renderer isn't running or the stage has zero
        area — caller falls back to a full-stage overlay.

        Math mirrors :meth:`QPixmap.scaled` with ``KeepAspectRatio``:
        scale the renderer's native (W, H) into the stage rect by the
        smaller of the two ratios, then centre the result.
        """
        sz = self.stage_stack.size()
        if sz.width() <= 0 or sz.height() <= 0:
            return None
        # Renderer aspect — fall back to 16:9 when nothing is loaded
        # so toggling the overlay before the first frame still places
        # a sensible rect (the renderer's default canvas is 1280×720).
        rdr = self._live_renderer
        if rdr is not None:
            src_w = max(1, int(rdr.width))
            src_h = max(1, int(rdr.height))
        else:
            src_w, src_h = 1280, 720
        scale = min(sz.width() / src_w, sz.height() / src_h)
        img_w = max(1, int(src_w * scale))
        img_h = max(1, int(src_h * scale))
        off_x = (sz.width() - img_w) // 2
        off_y = (sz.height() - img_h) // 2
        tl = self.stage_stack.mapToGlobal(QPoint(off_x, off_y))
        return QRect(tl.x(), tl.y(), img_w, img_h)

    def _on_stickman_edit_toggled(self, checked: bool) -> None:
        """Show / hide the floating transparent stickman draw-box overlay."""
        self._stickman_edit_active = bool(checked)
        if checked:
            seg = self._selected_segment
            if seg is None or not self._segment_stickman_enabled(seg):
                self.stickman_button.setChecked(False)
                return
            self.stickman_overlay.set_normalized(
                *self._segment_stick_fractions(seg)
            )
            self._sync_stickman_overlay_pos()
            self.stickman_overlay.show()
            self._stickman_pos_timer.start()
        else:
            self._stickman_pos_timer.stop()
            self.stickman_overlay.hide()

    def _on_stickman_box_committed(
        self, x: float, y: float, w: float, h: float
    ) -> None:
        """Persist the user's drag/resize back onto the segment."""
        seg = self._selected_segment
        if seg is None:
            return
        location = {
            "x": float(x),
            "y": float(y),
            "w": float(w),
            "h": float(h),
        }
        # Update the in-memory segment immediately so subsequent UI
        # reads (e.g. switching sources and back) see the new values
        # even before MainWindow finishes the project save.
        try:
            seg.stickman_location = location
        except AttributeError:
            pass
        self.stickman_location_changed.emit(seg.id, location)

    def set_source_segment(self, segment: Segment | None) -> None:
        """Set selected segment and load the most useful source for preview.

        Priority:
        1. **Rendered video on disk** (``segment.video_path`` exists AND
           the file actually exists) — this is the user-friendly default.
           Once a segment has been rendered, clicking it should play the
           video, not the raw audio.  This holds across project reopens:
           if the saved ``.htproj`` brings back a ``video_path`` whose
           file is still in ``temps/``, we auto-load it.
        2. Raw source audio — fallback when no render exists yet.
        3. Nothing → clear.

        The source combo is synced to whatever ends up loaded, so the
        dropdown honestly reflects the player content.  The user can still
        manually switch to "Selected media" (= raw media-library item) or
        "Main timeline (stitched)" via the combo; those paths run through
        ``_load_active_source`` and override this default.
        """
        self._selected_segment = segment

        if segment is None:
            self.clear()
            return

        rendered_ready = bool(
            segment.video_path and Path(segment.video_path).exists()
        )

        if rendered_ready:
            # Sync combo so the dropdown matches what's actually playing.
            self._set_source_combo_silently("segment")
            # The rendered video starts at project-time = segment.start.
            self._playhead_offset_sec = float(segment.start_time_sec or 0.0)
            self._load_path(segment.video_path)  # type: ignore[arg-type]
            self._refresh_stickman_button_state()
            return

        if segment.audio_path:
            # Falling back to raw audio because no render exists yet.
            self._set_source_combo_silently("media")
            self._playhead_offset_sec = 0.0
            self._load_path(segment.audio_path)
            self._refresh_stickman_button_state()
            return

        self.clear()
        # ``clear()`` resets the stickman toggle as a safety net when
        # the player is genuinely empty, but a segment is still
        # selected here — re-enable the toggle so the user can adjust
        # the box even before any audio / render is attached.
        self._refresh_stickman_button_state()

    def _set_source_combo_silently(self, data_value: str) -> None:
        """Set the source combo to the given userData value without firing
        ``currentIndexChanged`` (which would re-trigger ``_load_active_source``
        and double-load the source we're already loading)."""
        idx = self.source_combo.findData(data_value)
        if idx < 0 or idx == self.source_combo.currentIndex():
            return
        self.source_combo.blockSignals(True)
        try:
            self.source_combo.setCurrentIndex(idx)
        finally:
            self.source_combo.blockSignals(False)

    def clear(self) -> None:
        """Clear loaded media and reset player state."""
        # If we're currently in live-preview mode, route through the
        # dedicated tear-down so the renderer + frame timer + ndarray
        # buffer reference all get released cleanly.  ``stop_live_preview``
        # re-enters ``_show_empty`` for us so we can early-return.
        if self._live_active or self._live_renderer is not None:
            self.stop_live_preview()
            return
        self._pending_play = False
        self._media_ready = False
        self._current_url = QUrl()
        self._playhead_offset_sec = 0.0
        self.player.stop()
        self.player.setSource(QUrl())
        self._show_empty("No preview source selected")
        self._set_play_button_state(playing=False)
        self.play_button.setEnabled(False)
        self.seek_slider.setRange(0, 0)
        self.time_label.setText("00:00 / 00:00")
        # Hide the floating stickman overlay and disable the toggle button.
        if hasattr(self, "_stickman_pos_timer"):
            self._stickman_pos_timer.stop()
        if hasattr(self, "stickman_overlay"):
            self._stickman_edit_active = False
            self.stickman_overlay.hide()
        if hasattr(self, "stickman_button"):
            if self.stickman_button.isChecked():
                self.stickman_button.blockSignals(True)
                self.stickman_button.setChecked(False)
                self.stickman_button.blockSignals(False)
            self.stickman_button.setEnabled(False)

    def _build_ui(self) -> None:
        self.setObjectName("PanelRoot")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header strip - "Player"
        header = QWidget()
        header.setObjectName("panelHeader")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(10, 6, 10, 6)
        header_row.setSpacing(8)
        title = QLabel("Player")
        title.setObjectName("panelTitle")
        header_row.addWidget(title)
        header_row.addStretch()
        self.source_combo = QComboBox()
        self.source_combo.addItem("Selected media", "media")
        self.source_combo.addItem("Selected segment (rendered)", "segment")
        self.source_combo.addItem("Main timeline (stitched)", "timeline")
        self.source_combo.setFixedWidth(200)
        self.source_combo.currentIndexChanged.connect(self._load_active_source)
        header_row.addWidget(self.source_combo)
        layout.addWidget(header)

        # Body with padding
        body = QWidget()
        body.setObjectName("PanelRoot")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 10)
        body_layout.setSpacing(8)
        layout.addWidget(body, 1)

        # Stage uses a QStackedWidget to overlay:  0=video  1=loading  2=empty
        self.stage_stack = QStackedWidget()
        self.stage_stack.setObjectName("previewStage")
        self.stage_stack.setMinimumHeight(220)

        # Page 0 — video player
        self.video_widget = QVideoWidget()
        self.stage_stack.addWidget(self.video_widget)          # index 0

        # Page 1 — loading indicator
        loading_page = QWidget()
        loading_page.setStyleSheet("background:#0a0a0a;")
        lp_layout = QVBoxLayout(loading_page)
        lp_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label = QLabel("Loading media")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.setStyleSheet(
            "color:#6b6b6b; font-size:13px; background:transparent;"
        )
        lp_layout.addWidget(self.loading_label)
        self.stage_stack.addWidget(loading_page)               # index 1

        # Page 2 — empty / no source
        empty_page = QWidget()
        empty_page.setStyleSheet("background:#0a0a0a;")
        ep_layout = QVBoxLayout(empty_page)
        ep_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label = QLabel("No preview source selected")
        self.empty_label.setObjectName("previewEmptyLabel")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ep_layout.addWidget(self.empty_label)
        self.stage_stack.addWidget(empty_page)                 # index 2

        # Page 3 — render-progress page (big "Rendering NN%" centered).
        # Implemented as a real stack page (rather than a floating child of
        # stage_stack) because: (a) QStackedLayout only manages registered
        # pages, so a free-standing child wouldn't get sized/shown reliably,
        # and (b) on Windows QVideoWidget uses a native HWND that regular
        # Qt children cannot overlay.  Switching to a dedicated page side-
        # steps both issues completely.
        render_page = QWidget()
        render_page.setObjectName("previewRenderingPage")
        render_page.setStyleSheet(
            "QWidget#previewRenderingPage { background: #0a0a0a; }"
            "QLabel#renderOverlayTitle { color:#5cc8ff; font-size:28px; "
            "font-weight:600; background:transparent; }"
            "QLabel#renderOverlaySubtitle { color:#aaaaaa; font-size:13px; "
            "background:transparent; }"
        )
        rp_layout = QVBoxLayout(render_page)
        rp_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rp_layout.setContentsMargins(20, 20, 20, 20)
        rp_layout.setSpacing(8)
        self._render_overlay_title = QLabel("Rendering 0%")
        self._render_overlay_title.setObjectName("renderOverlayTitle")
        self._render_overlay_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._render_overlay_subtitle = QLabel("")
        self._render_overlay_subtitle.setObjectName("renderOverlaySubtitle")
        self._render_overlay_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rp_layout.addStretch(1)
        rp_layout.addWidget(self._render_overlay_title)
        rp_layout.addWidget(self._render_overlay_subtitle)
        rp_layout.addStretch(1)
        self.stage_stack.addWidget(render_page)                # index 3
        self._render_page_index = 3

        # Page 4 — live drawing surface.  A plain QLabel that owns the
        # most-recently-rendered ``QPixmap`` from
        # :class:`LiveFrameRenderer`.  Sized to fill the stage area;
        # the per-tick render scales the pixmap to the label's current
        # size with ``KeepAspectRatio`` so the preview never stretches.
        # ``setScaledContents(False)`` ensures the pixmap rasterisation
        # we do (smooth-scale on the QPixmap itself) is not undone by
        # Qt's much cheaper nearest-neighbour stretch path.
        live_page = QWidget()
        live_page.setStyleSheet("background:#0a0a0a;")
        lvp_layout = QVBoxLayout(live_page)
        lvp_layout.setContentsMargins(0, 0, 0, 0)
        lvp_layout.setSpacing(0)
        self.live_label = QLabel()
        self.live_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_label.setStyleSheet("background:#0a0a0a;")
        # Keep our own scaling; let the label stay at its natural ratio.
        self.live_label.setScaledContents(False)
        lvp_layout.addWidget(self.live_label, 1)
        self.stage_stack.addWidget(live_page)                  # index 4
        self._live_page_index = 4

        # Frame-pump timer for live preview.  Driven by ``renderer.fps``
        # so we don't render faster than the renderer can compose
        # (wasted CPU) or slower (visible stutter).  Stays stopped
        # outside of live-preview mode.
        self._live_frame_timer = QTimer(self)
        self._live_frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._live_frame_timer.timeout.connect(self._on_live_frame_tick)

        self.stage_stack.setCurrentIndex(2)  # start on empty page
        body_layout.addWidget(self.stage_stack, 1)

        # Stickman draw-box overlay — a floating Tool window with
        # WA_TranslucentBackground so the video playing on QVideoWidget
        # (native HWND) shows through at ~50% opacity while the user
        # drags the cyan box to set the stickman position.
        # Position is kept in sync with stage_stack via a 50ms timer
        # (``_stickman_pos_timer``) so the box tracks the video area
        # even as the application window is moved or resized.
        self.stickman_overlay = StickmanBoxOverlay(self)
        self.stickman_overlay.box_committed.connect(
            self._on_stickman_box_committed
        )
        self._stickman_edit_active: bool = False
        # Timer that keeps the floating overlay snapped to stage_stack.
        self._stickman_pos_timer = QTimer(self)
        self._stickman_pos_timer.setInterval(50)
        self._stickman_pos_timer.timeout.connect(
            self._sync_stickman_overlay_pos
        )

        # Dots animation timer for loading label.
        self._loading_dots = 0
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(450)
        self._loading_timer.timeout.connect(self._tick_loading_dots)

        # Watchdog: if a load takes too long (Qt FFmpeg stuck on MP3 probe etc.)
        # we retry once. Single-shot, started by _load_path, cancelled by
        # _on_media_status_changed when the media becomes ready / invalid.
        self._load_watchdog = QTimer(self)
        self._load_watchdog.setSingleShot(True)
        self._load_watchdog.setInterval(12000)  # 12s — enough for ~10 min MP3 probe.
        self._load_watchdog.timeout.connect(self._on_load_watchdog_fired)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.6)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.player.errorOccurred.connect(self._on_player_error)

        control_row = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self._toggle_play)
        self.stop_button = QPushButton("Stop")
        # Use Pause + rewind-to-0 instead of QMediaPlayer.stop().  A real
        # ``stop()`` resets ``duration()`` to 0, which collapses the seek
        # slider's range to ``(0, 0)`` and makes scrubbing impossible
        # until the user hits Play again.  Pausing keeps the duration so
        # the user can drag the slider around even when "stopped".
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self.player.setPosition)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet(
            "color:#c0c0c0;font-family:Consolas,Menlo,monospace;"
        )
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(60)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        self.full_button = QPushButton("Fullscreen")
        self.full_button.clicked.connect(self._toggle_fullscreen)
        # Checkable toggle that exposes the draggable stickman draw-box
        # overlay on top of the player.  Disabled when no segment is
        # selected (or the segment has stickman rendering turned off).
        self.stickman_button = QPushButton("Stick Box")
        self.stickman_button.setCheckable(True)
        self.stickman_button.setToolTip(
            "Toggle the stickman draw-box overlay on the player.\n"
            "Drag the box to move it; drag the corner handles to resize."
        )
        self.stickman_button.toggled.connect(
            self._on_stickman_edit_toggled
        )
        self.stickman_button.setEnabled(False)

        self.play_button.setEnabled(False)

        # FPS selector for live preview.  Options kept deliberately small
        # (6 / 12 / 24 / 30) — higher means more CPU per second on the
        # render thread.  Default 24 matches what users consider "smooth".
        self.fps_combo = QComboBox()
        self.fps_combo.setToolTip("Live preview frame-rate (higher = smoother but more CPU)")
        self.fps_combo.setFixedWidth(72)
        for fps_val in (6, 12, 24, 30):
            self.fps_combo.addItem(f"{fps_val} fps", fps_val)
        self.fps_combo.setCurrentIndex(2)  # default 24 fps
        self.fps_combo.currentIndexChanged.connect(self._on_fps_changed)

        control_row.addWidget(self.play_button)
        control_row.addWidget(self.stop_button)
        control_row.addWidget(self.seek_slider, 1)
        control_row.addWidget(self.time_label)
        vol_label = QLabel("Vol")
        vol_label.setStyleSheet("color:#8a8a8a;")
        control_row.addWidget(vol_label)
        control_row.addWidget(self.volume_slider)
        control_row.addWidget(self.fps_combo)
        control_row.addWidget(self.stickman_button)
        control_row.addWidget(self.full_button)
        body_layout.addLayout(control_row)

    def _load_active_source(self) -> None:
        source = self.source_combo.currentData()
        if source == "media":
            if self._selected_media is None:
                self.clear()
                return
            # Raw media plays at project-time 0.
            self._playhead_offset_sec = 0.0
            self._load_path(self._selected_media.source_path)
            return
        if source == "segment":
            if self._selected_segment is None or not self._selected_segment.video_path:
                self.clear()
                self.empty_label.setText("Chua render")
                return
            # Rendered segment video → its position 0 is project-time
            # segment.start_time_sec, so the timeline playhead lines up.
            self._playhead_offset_sec = float(
                self._selected_segment.start_time_sec or 0.0
            )
            self._load_path(self._selected_segment.video_path)
            return
        self.clear()
        self.empty_label.setText("Main timeline preview is TODO in skeleton")

    def _load_path(self, raw_path: str, *, force_reload: bool = False) -> None:
        # When live-preview is active, ANY call to load a different
        # video/audio source is a deliberate choice by the caller
        # (user picked another segment, switched the source-combo,
        # auto-loaded a freshly-rendered file…) that supersedes live
        # mode.  Tear it down first so the renderer + frame timer
        # release cleanly before we start a new probe.
        if self._live_active:
            self.stop_live_preview()
        # ``raw_path`` is always a local filesystem path now that live
        # preview is in-process and HLS streaming has been removed.
        path = Path(raw_path)
        if not path.exists():
            self.clear()
            self._show_empty("Source not found")
            return
        url = QUrl.fromLocalFile(str(path.resolve()))

        # Dedup: same URL already requested. Avoid calling setSource() again
        # because each setSource() cancels and restarts the probe — for VBR
        # MP3s this can take several seconds, making the UI feel stuck when
        # the user selects the same segment / audio twice.
        #
        # IMPORTANT: dedup is unsafe when the file content has changed under
        # the same path (e.g. after a re-render that overwrites
        # `segment_<id>.mp4` in place).  In that case ffmpeg keeps the old
        # parsed sample-tables / extradata while reading new bytes — that's
        # exactly the "Invalid NAL unit size" / "Reserved bit set" cascade
        # we hit after auto-loading a freshly rendered video.  Callers that
        # know the file just changed must pass force_reload=True to bypass.
        if url == self._current_url and not force_reload:
            if self._media_ready:
                # Source is fully loaded → just show the player and re-enable Play.
                self._show_video()
                self.play_button.setEnabled(True)
            else:
                # A load is already in-flight — leave it running, keep loading UI.
                self._show_loading()
            return

        # Genuinely new source (or forced reload of a possibly-overwritten
        # file) — fully reset and re-probe.  Detaching the source first
        # makes the backend release the old file/sample-tables so the next
        # setSource() actually re-reads the bytes from disk.
        if force_reload:
            # Caller (load_video) has already set _pending_play /
            # _pending_seek_ms for the new source.  Detaching the player
            # below fires a transient NoMedia status, whose handler would
            # otherwise clobber those flags — snapshot and restore them
            # around the detach so auto-play after render finish actually
            # works without the user clicking Play.
            saved_pending_play = self._pending_play
            saved_pending_seek_ms = self._pending_seek_ms
            self.player.stop()
            self.player.setSource(QUrl())
            self._pending_play = saved_pending_play
            self._pending_seek_ms = saved_pending_seek_ms
        else:
            self._pending_play = False
            self._pending_seek_ms = -1
        self._media_ready = False
        self._load_retries = 0
        self.play_button.setEnabled(False)
        self._set_play_button_state(playing=False)
        self._current_url = url
        self._show_loading()
        self.player.setSource(url)
        self._load_watchdog.start()

    def _toggle_play(self) -> None:
        state = self.player.playbackState()
        status = self.player.mediaStatus()
        MS = QMediaPlayer.MediaStatus
        PS = QMediaPlayer.PlaybackState

        # Case 1: currently playing -> pause.
        if state == PS.PlayingState:
            self.player.pause()
            return

        # Case 2: source still loading -> schedule auto-play.
        if status in {MS.LoadingMedia, MS.BufferingMedia, MS.StalledMedia}:
            self._pending_play = True
            self._set_play_button_state(playing=True)
            return

        # Case 3: end of media or stopped at end -> rewind then play.
        duration = self.player.duration()
        at_end = (
            status == MS.EndOfMedia
            or (duration > 0 and self.player.position() >= duration - 50)
        )
        if at_end:
            # Some backends (Windows ffmpeg) won't auto-rewind; reset position.
            self.player.setPosition(0)

        # Case 4: no media loaded but we have a cached url -> reattach source.
        if status == MS.NoMedia and not self._current_url.isEmpty():
            self._pending_play = True
            self._set_play_button_state(playing=True)
            self.player.setSource(self._current_url)
            return

        self.player.play()

    def _on_stop_clicked(self) -> None:
        """Stop button: pause and rewind to the start.

        ``QMediaPlayer.stop()`` would reset ``duration()`` to 0 and
        collapse the seek slider to an unusable ``(0, 0)`` range.  By
        pausing instead we keep the duration so the user can still
        scrub the slider while playback is parked.
        """
        self.player.pause()
        self.player.setPosition(0)

    def _on_position_changed(self, value: int) -> None:
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(value)
        self.seek_slider.blockSignals(False)
        self.time_label.setText(
            f"{format_ms(value)} / {format_ms(self.player.duration())}"
        )
        # Translate media-local time → project timeline time so the timeline
        # red playhead tracks correctly even when we're playing a rendered
        # segment video that starts mid-project (offset = segment.start).
        self.playhead_changed.emit(value / 1000.0 + self._playhead_offset_sec)

    def _on_duration_changed(self, value: int) -> None:
        self.seek_slider.setRange(0, max(0, value))
        self.time_label.setText(
            f"{format_ms(self.player.position())} / {format_ms(value)}"
        )

    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        MS = QMediaPlayer.MediaStatus

        if status in {MS.LoadedMedia, MS.BufferedMedia}:
            # Source is ready — switch to video page (unless we're in
            # live-render mode, which owns the stage_stack with its own
            # QLabel page; flipping to the QVideoWidget page would
            # paint a black native HWND over our live frames) and
            # enable playback.
            self._load_watchdog.stop()
            self._media_ready = True
            self._load_retries = 0
            if not self._live_active:
                self._show_video()
            else:
                # Stop the loading-dots animation; the live page is
                # already current and we don't want it switched away.
                self._loading_timer.stop()
            self.play_button.setEnabled(True)
            if self._pending_seek_ms >= 0:
                self.player.setPosition(self._pending_seek_ms)
                self._pending_seek_ms = -1
            if self._pending_play:
                self._pending_play = False
                self.player.play()
            return

        if status == MS.EndOfMedia:
            # File finished - keep button enabled so user can replay.
            self._load_watchdog.stop()
            self._media_ready = True
            self._pending_play = False
            self.play_button.setEnabled(True)
            self._set_play_button_state(playing=False)
            return

        if status == MS.InvalidMedia:
            self._load_watchdog.stop()
            self._media_ready = False
            self._pending_play = False
            self._set_play_button_state(playing=False)
            self.play_button.setEnabled(False)
            self._show_empty("Invalid media")
            return

        if status == MS.NoMedia:
            # NoMedia fires transiently between setSource(QUrl()) and
            # setSource(real_url) inside _load_path's force-reload path.
            # If a real URL is queued (current_url is non-empty), do NOT
            # touch the pending-play / pending-seek flags — the caller
            # (load_video) needs them to fire auto-play once LoadedMedia
            # arrives.  Otherwise this is a genuine "no source" state.
            self._media_ready = False
            self._set_play_button_state(playing=False)
            if self._current_url.isEmpty():
                self._pending_play = False
                self._pending_seek_ms = -1
                self.play_button.setEnabled(False)
            return

        # LoadingMedia / BufferingMedia / StalledMedia
        # Only disable and show loading page during the *initial* load.
        # Once _media_ready is True, don't interrupt playback view.
        # In live mode the live-page (rendered frames) is already the
        # current stack page and must not be replaced by the loading
        # placeholder — the user has been clicking edits and the
        # frame from the prior renderer is more useful than "Loading
        # media..." spinner text.
        if not self._media_ready:
            self.play_button.setEnabled(False)
            if not self._live_active:
                self._show_loading()

    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        """Keep play/pause button label in sync with the actual player state."""
        is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._set_play_button_state(playing=is_playing)
        # Ensure button stays enabled whenever we have a usable source.
        if self._media_ready or is_playing:
            self.play_button.setEnabled(True)
        # Broadcast the new state so MainWindow can gate timeline
        # scrubbing on Playing / Paused only.  We forward the enum's
        # underlying integer (``.value``) rather than ``int(state)``
        # because PySide6's ``QMediaPlayer.PlaybackState`` is not an
        # ``IntEnum`` in every build (CPython 3.13 + Qt 6.7+ raises
        # ``TypeError: int() argument must be ... not 'PlaybackState'``
        # on direct ``int(...)`` conversion).
        self.playback_state_changed.emit(int(state.value))

    @property
    def preview_fps(self) -> int:
        """Currently selected live-preview frame-rate."""
        return int(self.fps_combo.currentData())

    def _on_fps_changed(self) -> None:
        """Apply new FPS to the running live-frame timer immediately."""
        if self._live_active and self._live_frame_timer.isActive():
            fps = self.preview_fps
            interval_ms = max(8, int(round(1000.0 / max(1, fps))))
            self._live_frame_timer.setInterval(interval_ms)

    def _show_loading(self) -> None:
        """Switch stage to loading page and start dot animation."""
        self._loading_dots = 0
        self.loading_label.setText("Loading media")
        self.stage_stack.setCurrentIndex(1)
        if not self._loading_timer.isActive():
            self._loading_timer.start()

    def _show_video(self) -> None:
        """Switch stage to video/audio player page."""
        self._loading_timer.stop()
        self.stage_stack.setCurrentIndex(0)

    def _show_empty(self, message: str = "No preview source selected") -> None:
        """Switch stage to empty page with given message."""
        self._loading_timer.stop()
        self.empty_label.setText(message)
        self.stage_stack.setCurrentIndex(2)

    def _tick_loading_dots(self) -> None:
        """Animate '...' on loading label."""
        self._loading_dots = (self._loading_dots + 1) % 4
        self.loading_label.setText("Loading media" + "." * self._loading_dots)

    def _on_player_error(self, error: QMediaPlayer.Error, msg: str) -> None:
        self._load_watchdog.stop()
        self._pending_play = False
        self.play_button.setEnabled(False)
        self._show_empty(f"Error: {msg}")

    def _on_load_watchdog_fired(self) -> None:
        """Triggered when a load() has been pending too long.

        Most often caused by Qt's ffmpeg backend stalling on VBR MP3 probes.
        We retry the same URL exactly once before giving up so the user isn't
        stuck on "Loading media..." indefinitely.
        """
        if self._media_ready or self._current_url.isEmpty():
            return
        if self._load_retries >= 1:
            print(
                f"[preview] watchdog: media still not ready after retry: "
                f"{self._current_url.toLocalFile()}"
            )
            self._show_empty(
                "Loading is taking longer than expected.\n"
                "Try removing and re-adding the media."
            )
            return
        self._load_retries += 1
        print(
            f"[preview] watchdog: retrying stuck load: "
            f"{self._current_url.toLocalFile()}"
        )
        url = self._current_url
        self.player.setSource(QUrl())
        self.player.setSource(url)
        self._load_watchdog.start()

    def _set_play_button_state(self, *, playing: bool) -> None:
        self.play_button.setText("Pause" if playing else "Play")

    def _on_volume_changed(self, value: int) -> None:
        self.audio_output.setVolume(value / 100.0)

    def play(self) -> None:
        """Start playback; queues if media is still loading."""
        if self._media_ready:
            self.player.play()
            self._set_play_button_state(playing=True)
        else:
            self._pending_play = True

    def load_video(self, path: str, *, playhead_offset_sec: float = 0.0) -> None:
        """Load an arbitrary video file and play it from the beginning.

        Used after a preview/render completes to show the rendered output.
        Any existing source is replaced; playback starts automatically once
        the player reports the media is ready.

        ``playhead_offset_sec`` is the project-timeline second that maps to
        position 0 of the loaded file.  For a rendered segment video this
        is ``segment.start_time_sec`` so the project timeline's red playhead
        tracks correctly while the video plays.  For arbitrary stand-alone
        videos pass 0.

        ``force_reload=True`` is passed to ``_load_path`` because the render
        pipeline overwrites ``segment_<id>.mp4`` / ``preview_<id>.mp4`` in
        place — without forcing a fresh probe, ffmpeg would keep the old
        sample-tables and produce "Invalid NAL unit size" decode errors.
        """
        self._pending_play = True
        self._pending_seek_ms = 0
        self._playhead_offset_sec = float(playhead_offset_sec)
        self._load_path(path, force_reload=True)

    # ------------------------------------------------------------------
    # Render-progress overlay API
    # ------------------------------------------------------------------
    def set_render_progress(
        self,
        pct: Optional[int],
        *,
        label: str = "Rendering",
        subtitle: str = "",
    ) -> None:
        """Show a "Rendering NN%" page on the player while a render runs.

        Switches the stage stack to the dedicated rendering page (a real
        page, not a floating overlay — see _build_ui).  Pass ``pct=None``
        or pct >= 100 to hide it (caller is expected to follow up with
        ``load_video(...)`` to show the result, or with
        ``hide_render_overlay()`` on failure).
        """
        if pct is None:
            self.hide_render_overlay()
            return
        try:
            pct_int = max(0, min(100, int(pct)))
        except (TypeError, ValueError):
            pct_int = 0
        if pct_int >= 100:
            self.hide_render_overlay()
            return
        self._render_overlay_title.setText(f"{label} {pct_int}%")
        self._render_overlay_subtitle.setText(subtitle)
        self.stage_stack.setCurrentIndex(self._render_page_index)

    def hide_render_overlay(self) -> None:
        """Leave the rendering page if it's currently shown.

        Falls back to the most appropriate page: the video page if a source
        is loaded, otherwise the empty page.  Callers who follow up with
        ``load_video(...)`` don't strictly need this — the next
        ``setSource`` will move the stack to the loading page on its own —
        but it's harmless to call and matters on render failure where no
        load follows.
        """
        if self.stage_stack.currentIndex() != self._render_page_index:
            return
        if self._media_ready and not self._current_url.isEmpty():
            self._show_video()
        else:
            self._show_empty("No preview source selected")

    def seek_to_seconds(self, time_sec: float) -> None:
        """Seek media player to the given project-timeline time (in seconds).

        Translates project time → media-local time using the current
        playhead offset.  This way a click on the project timeline at
        t = 35.0 sec lands on the right frame of a segment video whose
        position 0 corresponds to t = segment.start_time_sec.

        If the media source is still loading, the seek is queued and applied
        automatically once the player signals it is ready.
        """
        local_sec = max(0.0, time_sec - self._playhead_offset_sec)
        ms = int(local_sec * 1000)
        if self._media_ready:
            self.player.setPosition(ms)
        else:
            # Queue it — applied in _on_media_status_changed when ready.
            self._pending_seek_ms = ms

    def _toggle_fullscreen(self) -> None:
        """Toggle preview video fullscreen mode."""
        self.video_widget.setFullScreen(not self.video_widget.isFullScreen())

    # ------------------------------------------------------------------
    # Live preview (in-process draw-on-demand)
    # ------------------------------------------------------------------
    def start_live_preview(
        self,
        renderer: "LiveFrameRenderer",
        audio_path: str,
        *,
        start_local_sec: float = 0.0,
        project_offset_sec: float = 0.0,
    ) -> None:
        """Switch the panel into live-render mode.

        ``renderer`` is a fully-constructed :class:`LiveFrameRenderer`
        already loaded with audio + initial beats.  We treat its
        ``render_at`` as the source of every frame from now on; the
        existing ``QMediaPlayer`` is repurposed to play **only the
        audio track** of ``audio_path`` so the live frames stay locked
        to the OS audio clock without us having to roll our own
        ``QAudioSink`` pipeline.

        Why we reuse ``self.player`` (audio-only) instead of spawning a
        second player:
          * one source-of-truth playhead means ``seek_slider`` /
            ``time_label`` / ``playback_state_changed`` Just Work
            without duplicating wiring,
          * pause/resume/scrub controls already wired to the player
            instantly behave as expected,
          * ``QMediaPlayer`` is happy with a video output attached even
            for an audio-only source — it simply emits no frames, and
            the QVideoWidget stays hidden behind ``stage_stack``'s
            live-page index.

        Parameters
        ----------
        start_local_sec:
            Where in the segment audio (segment-local seconds) playback
            should resume.  Mapped to ``QMediaPlayer.setPosition`` once
            the audio source is ready.
        project_offset_sec:
            Project-timeline seconds that map to ``start_local_sec``;
            used so the timeline's red playhead tracks correctly during
            live preview.
        """
        # Tear down any in-flight video playback so we don't compete
        # with the live-mode audio source for the same player.
        self.player.stop()

        self._live_active = True
        self._live_renderer = renderer
        self._playhead_offset_sec = float(project_offset_sec)

        # Hand the audio track to the existing player.  We deliberately
        # bypass ``_load_path`` here because we already know the file
        # exists (the caller analysed it for the renderer) and we do
        # NOT want ``_load_active_source`` paths to fire when the user
        # later flips the source-combo back to "Selected media".
        audio_url = QUrl.fromLocalFile(str(Path(audio_path).resolve()))
        self._current_url = audio_url
        self._media_ready = False
        self._load_retries = 0
        self._pending_seek_ms = max(0, int(start_local_sec * 1000))
        self._pending_play = True
        self.play_button.setEnabled(False)
        self._set_play_button_state(playing=True)
        self.player.setSource(audio_url)
        self._load_watchdog.start()

        # Switch the stage stack to the live drawing page IMMEDIATELY
        # so the user gets the first frame even before the audio probe
        # finishes (which on a fresh MP3 can take 200–800 ms).
        self.stage_stack.setCurrentIndex(self._live_page_index)
        self._loading_timer.stop()
        self._render_live_frame(start_local_sec)

        # Pump frames at the user-selected preview fps (fps_combo).
        interval_ms = max(8, int(round(1000.0 / max(1, self.preview_fps))))
        self._live_frame_timer.setInterval(interval_ms)
        self._live_frame_timer.start()

    def stop_live_preview(self) -> None:
        """Tear down live-render mode and return the panel to idle.

        Idempotent.  Releases the renderer (its NumPy buffers add up to
        ~50–150 MB on long segments) and stops the audio player.  The
        stage_stack reverts to the empty page; the next interaction
        (e.g. selecting a segment) will repopulate it through the
        normal video / audio-source paths.
        """
        if not self._live_active and self._live_renderer is None:
            return
        self._live_active = False
        self._live_frame_timer.stop()
        # Stop audio playback before dropping the renderer so the timer
        # tick that already fired (if any) doesn't land in a half-torn
        # state.
        self.player.stop()
        self.player.setSource(QUrl())
        if self._live_renderer is not None:
            self._live_renderer.close()
            self._live_renderer = None
        # Drop the QImage backing buffer reference so the underlying
        # ndarray can be garbage-collected.
        self._live_buffer_rgb = None
        self.live_label.clear()
        # Reset all the QMediaPlayer-tracked state to "idle" so the
        # user can pick a new source without inheriting flags from the
        # live session.
        self._media_ready = False
        self._pending_play = False
        self._pending_seek_ms = -1
        self._current_url = QUrl()
        self._show_empty("No preview source selected")
        self.play_button.setEnabled(False)
        self._set_play_button_state(playing=False)
        # Notify MainWindow so its toggle state mirrors ours regardless
        # of who initiated the stop.  ``Qt.QueuedConnection`` is
        # implicit between thread boundaries, but we're on the GUI
        # thread here so the slot runs synchronously after this line.
        self.live_preview_stopped.emit()

    def update_live_beats(self, beat_times: list[float]) -> None:
        """Hot-reload the renderer's beat schedule and redraw current frame.

        No-op when not in live mode.  The first frame after this call
        replays the schedule from the segment start which can take a
        few hundred ms on long clips — well below the 200 ms latency
        target for editor-style preview.
        """
        if not self._live_active or self._live_renderer is None:
            return
        self._live_renderer.update_beats(list(beat_times))
        self._render_live_frame(self.player.position() / 1000.0)

    def update_live_mode(
        self,
        mode: str,
        *,
        show_stickman: Optional[bool] = None,
        stickman_box: Optional[tuple[int, int, int, int]] = None,
        show_floor_panels: Optional[bool] = None,
        max_per_lane: Optional[int] = None,
    ) -> None:
        """Hot-reload the renderer's gameplay mode + decor and redraw.

        ``show_stickman`` / ``stickman_box`` / ``show_floor_panels`` /
        ``max_per_lane`` are pass-through overrides for
        :meth:`LiveFrameRenderer.update_mode`; ``None`` keeps the
        renderer's current value.  The editor folds the segment-config
        "Sticky Man / Floor panels / mode / Max per lane" form edits
        into a single call so the scene is rebuilt exactly once.
        """
        if not self._live_active or self._live_renderer is None:
            return
        self._live_renderer.update_mode(
            mode,
            show_stickman=show_stickman,
            stickman_box=stickman_box,
            show_floor_panels=show_floor_panels,
            max_per_lane=max_per_lane,
        )
        self._render_live_frame(self.player.position() / 1000.0)

    def is_live_preview_active(self) -> bool:
        """``True`` while live-render mode is currently on."""
        return self._live_active

    def _on_live_frame_tick(self) -> None:
        """Timer callback: render the frame matching the audio playhead."""
        if not self._live_active or self._live_renderer is None:
            self._live_frame_timer.stop()
            return
        # Audio is the master clock — render whichever frame matches
        # ``player.position()`` so visuals stay locked to the audio
        # output even if the OS scheduler skews the timer interval.
        pos_ms = self.player.position()
        # ``QMediaPlayer.position`` may briefly overshoot duration on
        # EndOfMedia; LiveFrameRenderer.render_at clamps internally so
        # we don't have to repeat that here.
        self._render_live_frame(pos_ms / 1000.0)

    def _render_live_frame(self, t_sec: float) -> None:
        """Pull one frame from the renderer and show it on ``live_label``.

        Heavy-lifting funnel for both the timer tick and the
        ``update_*`` hot-reload paths.  Skipped if the live renderer
        has been torn down between scheduling and execution.
        """
        rdr = self._live_renderer
        if rdr is None:
            return
        # Render returns BGR uint8 — convert to RGB for QImage.  We
        # could pre-allocate a contiguous swap buffer to avoid the
        # alloc each tick, but at 720p the BGR→RGB cost is ~0.4 ms in
        # OpenCV and negligible compared to the actual compose.
        bgr = rdr.render_at(float(t_sec))
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        # Force a contiguous C-order copy so QImage's memory view has
        # a deterministic stride; without this, ``cvtColor`` can in
        # rare cases return a view that QImage's stride probe rejects.
        rgb = rgb.copy()
        self._live_buffer_rgb = rgb  # keep alive for QImage lifetime
        h, w, _ = rgb.shape
        bytes_per_line = w * 3
        img = QImage(
            rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888
        )
        pix = QPixmap.fromImage(img)
        # Scale to the label size with aspect-fit so resizing the
        # window or dragging splitters reflows cleanly.
        target = self.live_label.size()
        if target.width() > 0 and target.height() > 0:
            pix = pix.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.live_label.setPixmap(pix)

