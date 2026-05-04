"""Preview player panel using Qt multimedia."""

from __future__ import annotations

import math
import struct
import tempfile
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import cv2
from PySide6.QtCore import QPoint, QRect, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
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


class FloorWallOverlay(QWidget):
    """Drag handles to adjust camera perspective.

    Handle types
    ------------
    - **Floor line** (cyan)    → ``floor_hit_frac``   (drag up/down)
    - **Horizon line** (amber) → ``horizon_frac``     (drag up/down)
    - **Near wall** (magenta, bottom) — INDEPENDENT → ``floor_spread_frac``
    - **Far wall**  (magenta, top)    — INDEPENDENT → ``far_spread_frac``

    Near and far handle pairs are connected by dashed lines.
    Moving near does NOT move far, and vice versa.
    """

    # 12 floats:
    # hit_frac, horizon_frac, near_spread, far_spread, wall_floor_gap_frac,
    # countdown_x, countdown_y, countdown_w, countdown_h, rail_height,
    # start_gate_h, chevron_width_frac
    changing  = Signal(float, float, float, float, float, float, float, float, float, float, float, float)
    committed = Signal(float, float, float, float, float, float, float, float, float, float, float, float)

    _HANDLE_R    = 10
    _HIT_CLR     = QColor(0,   210, 210)
    _HORIZON_CLR = QColor(220, 170,   0)
    _WALL_CLR    = QColor(220,  60, 220)
    _GAP_CLR     = QColor(255, 150,  50)   # orange gap handles
    # Far handles display scale: far_spread displayed at this fraction from centre
    _FAR_DISP    = 0.40

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)

        self._hit_frac:            float = 0.86
        self._horizon_frac:        float = 0.45
        self._near_spread:         float = 0.65   # controls floor_spread_frac
        self._far_spread:          float = 0.65   # controls far_spread_frac (independent)
        self._wall_floor_gap_frac: float = 0.0    # near-wall Y lifted above floor line
        self._rail_height: float = 0.15
        self._countdown_enabled: bool = False
        self._cd_x: float = 0.88
        self._cd_y: float = 0.04
        self._cd_w: float = 0.10
        self._cd_h: float = 0.16

        self._drag: str | None = None  # + 'cd_move'|'cd_tl'|'cd_tr'|'cd_bl'|'cd_br'
        self._drag_anchor: QPoint = QPoint()
        self._drag_cd_x0 = 0.0
        self._drag_cd_y0 = 0.0
        self._drag_cd_w0 = 0.0
        self._drag_cd_h0 = 0.0
        self._drag_rail_h0 = 0.15
        self._drag_gate_h0 = 0.14
        self._start_gate_enabled: bool = False
        self._start_gate_h: float = 0.14
        self._sg_x: float = 0.0
        self._sg_y: float = 0.0
        self._sg_w: float = 0.0
        self._sg_h: float = 0.0
        self._chevron_width_frac: float = 0.45
        self._drag_chevron_w0 = 0.45
        self._cached_frame_idx: int = 0

    def set_frame_idx(self, frame_idx: int) -> None:
        """Update tile-scroll frame index so hit-block overlay matches renderer."""
        fi = int(frame_idx)
        if fi != self._cached_frame_idx:
            self._cached_frame_idx = fi
            self.update()

    # ── public API ──────────────────────────────────────────────────────
    def _hit_frac_bounds(self) -> tuple[float, float]:
        """Dynamic Floor-handle bounds so the circle can touch screen edges."""
        h = max(1, int(self.height()))
        margin = float(self._HANDLE_R) / float(h)
        lo = max(0.0, min(0.49, margin))
        hi = min(1.0, max(0.51, 1.0 - margin))
        return (lo, hi)

    def set_fractions(
        self,
        hit_frac: float,
        horizon_frac: float,
        near_spread: float,
        far_spread: float,
        wall_floor_gap_frac: float = 0.0,
        rail_height: float = 0.15,
        countdown_enabled: bool = False,
        countdown_x: float = 0.88,
        countdown_y: float = 0.04,
        countdown_w: float = 0.10,
        countdown_h: float = 0.16,
        chevron_width_frac: float = 0.45,
    ) -> None:
        hit_lo, hit_hi = self._hit_frac_bounds()
        self._hit_frac             = max(hit_lo, min(hit_hi, float(hit_frac)))
        self._horizon_frac         = max(0.20, min(0.60, float(horizon_frac)))
        self._near_spread          = max(0.20, min(3.00, float(near_spread)))
        self._far_spread           = max(0.05, min(3.00, float(far_spread)))
        self._wall_floor_gap_frac  = max(0.00, min(0.30, float(wall_floor_gap_frac)))
        self._rail_height          = max(0.15, float(rail_height))
        self._countdown_enabled = bool(countdown_enabled)
        self._cd_x = max(0.0, min(0.98, float(countdown_x)))
        self._cd_y = max(0.0, min(0.98, float(countdown_y)))
        self._cd_w = max(0.02, min(1.0 - self._cd_x, float(countdown_w)))
        self._cd_h = max(0.02, min(1.0 - self._cd_y, float(countdown_h)))
        self._chevron_width_frac = max(0.05, min(0.95, float(chevron_width_frac)))
        self.update()

    def get_fractions(self) -> tuple[float, float, float, float, float, float, float, float, float, float, float, float]:
        return (self._hit_frac, self._horizon_frac,
                self._near_spread, self._far_spread,
                self._wall_floor_gap_frac,
                self._cd_x, self._cd_y, self._cd_w, self._cd_h, self._rail_height,
                self._start_gate_h, self._chevron_width_frac)

    def set_start_gate_proxy(
        self,
        *,
        enabled: bool,
        gate_height: float,
        x: float,
        y: float,
        w: float,
        h: float,
    ) -> None:
        self._start_gate_enabled = bool(enabled)
        self._start_gate_h = max(0.03, float(gate_height))
        self._sg_x = max(0.0, min(1.0, float(x)))
        self._sg_y = max(0.0, min(1.0, float(y)))
        self._sg_w = max(0.0, min(1.0, float(w)))
        self._sg_h = max(0.0, min(1.0, float(h)))
        self.update()

    # ── geometry helpers ────────────────────────────────────────────────
    def _hit_y(self)   -> int: return int(self._hit_frac     * self.height())
    def _hz_y(self)    -> int: return int(self._horizon_frac * self.height())
    # Near handles: at hit_y (floor line), near_spread from centre.
    # "raw" = unclamped world position (may be outside widget).
    # "display" = clamped to widget edge so the circle is always clickable.
    def _near_rx_raw(self)     -> int: return int((0.5 + self._near_spread * 0.5) * self.width())
    def _near_lx_raw(self)     -> int: return int((0.5 - self._near_spread * 0.5) * self.width())
    def _near_rx(self)  -> int:
        return max(self._HANDLE_R, min(self.width() - self._HANDLE_R, self._near_rx_raw()))
    def _near_lx(self)  -> int:
        return max(self._HANDLE_R, min(self.width() - self._HANDLE_R, self._near_lx_raw()))
    # Far handles: 22% below horizon, far_spread displayed at _FAR_DISP scale.
    # Display positions are clamped to widget edge so handles remain clickable.
    def _far_y(self)   -> int: return int(self._hz_y() + (self._hit_y() - self._hz_y()) * 0.22)
    def _far_rx_raw(self) -> int: return int((0.5 + self._far_spread * 0.5 * self._FAR_DISP) * self.width())
    def _far_lx_raw(self) -> int: return int((0.5 - self._far_spread * 0.5 * self._FAR_DISP) * self.width())
    def _far_rx(self)  -> int:
        return max(self._HANDLE_R, min(self.width() - self._HANDLE_R, self._far_rx_raw()))
    def _far_lx(self)  -> int:
        return max(self._HANDLE_R, min(self.width() - self._HANDLE_R, self._far_lx_raw()))

    # Gap handles sit at the two front-bottom wall corners:
    # the intersection between front wall edge and wall bottom edge.
    # Keep gap handles as close to the edge as possible while ensuring
    # the whole circle remains visible/clickable inside the overlay.
    _GAP_MARGIN = _HANDLE_R
    def _gap_y(self)  -> int:
        return int(self._hit_y() - self._wall_floor_gap_frac * self.height())
    def _gap_lx(self) -> int: return self._GAP_MARGIN
    def _gap_rx(self) -> int: return max(0, self.width() - 1 - self._GAP_MARGIN)

    def _rail_handle_y(self) -> int:
        # Visual proxy for rail height: higher rail => handle moves upward.
        y = int(self._hit_y() - self._rail_height * self.height() * 0.55)
        return max(self._hz_y() + self._HANDLE_R, min(self._hit_y() - self._HANDLE_R, y))

    def _countdown_rect_px(self) -> QRect:
        w = self.width()
        h = self.height()
        return QRect(
            int(self._cd_x * w),
            int(self._cd_y * h),
            max(1, int(self._cd_w * w)),
            max(1, int(self._cd_h * h)),
        )

    def _start_gate_rect_px(self) -> QRect:
        w = self.width()
        h = self.height()
        return QRect(
            int(self._sg_x * w),
            int(self._sg_y * h),
            max(1, int(self._sg_w * w)),
            max(1, int(self._sg_h * h)),
        )

    def _floor_footprint_points(self) -> list[QPoint]:
        """Approximate floor trapezoid points for overlay visualization."""
        y_bot = self._hit_y()
        y_top = self._hz_y()
        return [
            QPoint(self._near_lx(), y_bot),
            QPoint(self._near_rx(), y_bot),
            QPoint(self._far_rx(), y_top),
            QPoint(self._far_lx(), y_top),
        ]

    def _chevron_width_handle_pos(self) -> QPoint:
        """Middle handle for floor chevron width control."""
        y_mid = int((self._hit_y() + self._hz_y()) * 0.5)
        return QPoint(self.width() // 2, y_mid)

    def _hit_block_polys(self, n: int = 4, frame_idx: int = 0) -> list:
        """Trapezoid polygons using pinhole projection matching cam.project()
        in rhythm.py — exact equivalent of _draw_floor_tiles_legacy (lane_tiles).

        All constants mirror PerspectiveCamera.__init__ and
        _draw_floor_tiles_legacy exactly:
          Z_NEAR=2.5, fov_deg=55, tile_len=1.6, z_slots=[3.0, 5.5, …]
          tile_w = max(0.25, step_world * 0.80)
          clip: wz < Z_NEAR + 0.2  (rhythm.py line 1196)

        ``frame_idx`` enables tile-scroll sync; pass 0 for a static snapshot.
        """
        H_w = self.height()
        W_w = self.width()
        if H_w <= 0 or W_w <= 0 or n < 2:
            return []
        hy = self._hit_y()    # y_hit in widget pixels
        cy = self._hz_y()     # horizon (cy_v) in widget pixels
        if hy <= cy:
            return []         # invalid: floor above horizon

        # ── Mirror PerspectiveCamera constants ────────────────────────────
        Z_NEAR   = 2.5
        fov_deg  = 55.0
        cx_pix   = W_w / 2.0
        cy_pix   = float(cy)
        fx       = W_w / 2.0 / math.tan(math.radians(fov_deg) / 2.0)
        fy       = fx

        # FLOOR_WORLD_Y — same formula as renderer
        FLOOR_WORLD_Y = (float(hy) - cy_pix) * Z_NEAR / fy

        # Lane world positions — match cam.lane_world_x(i)
        lane_half_spread_px = W_w * self._near_spread * 0.5
        LANE_WORLD_X        = lane_half_spread_px * Z_NEAR / fx
        spacing             = 2.0 * LANE_WORLD_X / (n - 1)
        x_centers           = [-LANE_WORLD_X + i * spacing for i in range(n)]

        # Tile geometry — match _draw_floor_tiles_legacy
        z_slots   = [3.0, 5.5, 8.5, 12.5, 17.5]
        tile_len  = 1.6
        step_w    = abs(x_centers[1] - x_centers[0]) if n >= 2 else 0.0
        tile_w    = max(0.25, step_w * 0.80)
        scroll    = (float(frame_idx) * 0.30) % (z_slots[1] - z_slots[0])
        z_clip    = Z_NEAR + 0.2

        def _proj(wx: float, wy: float, wz: float):
            if wz <= 1e-6:
                return None
            return QPoint(int(round(cx_pix + fx * wx / wz)),
                          int(round(cy_pix + fy * wy / wz)))

        polys = []
        for xc in x_centers:
            # Pick first visible tile (nearest, not clipped)
            wz = None
            for z_c in z_slots:
                candidate = z_c - scroll
                if candidate >= z_clip:
                    wz = candidate
                    break
            if wz is None:
                continue
            corners = [
                (xc - tile_w / 2.0, FLOOR_WORLD_Y, wz - tile_len / 2.0),
                (xc + tile_w / 2.0, FLOOR_WORLD_Y, wz - tile_len / 2.0),
                (xc + tile_w / 2.0, FLOOR_WORLD_Y, wz + tile_len / 2.0),
                (xc - tile_w / 2.0, FLOOR_WORLD_Y, wz + tile_len / 2.0),
            ]
            pts = [_proj(*c) for c in corners]
            if all(p is not None for p in pts):
                polys.append(pts)
        return polys

    def _handle_at(self, pos: QPoint) -> str | None:
        r = self._HANDLE_R + 7
        if self._countdown_enabled:
            cd = self._countdown_rect_px()
            hs = 14
            corners = {
                "cd_tl": QRect(cd.left() - hs // 2, cd.top() - hs // 2, hs, hs),
                "cd_tr": QRect(cd.right() - hs // 2, cd.top() - hs // 2, hs, hs),
                "cd_bl": QRect(cd.left() - hs // 2, cd.bottom() - hs // 2, hs, hs),
                "cd_br": QRect(cd.right() - hs // 2, cd.bottom() - hs // 2, hs, hs),
            }
            for kind, rr in corners.items():
                if rr.contains(pos):
                    return kind
            if cd.contains(pos):
                return "cd_move"
        if self._start_gate_enabled:
            sg = self._start_gate_rect_px()
            hs = 14
            top_mid = QRect(
                sg.center().x() - hs // 2,
                sg.top() - hs // 2,
                hs,
                hs,
            )
            if top_mid.contains(pos):
                return "gate_top"
        ch = self._chevron_width_handle_pos()
        cr = self._HANDLE_R - 1
        if abs(pos.x() - ch.x()) <= cr and abs(pos.y() - ch.y()) <= cr:
            return "chevron_width"
        # Side-rail height handles (left/right), dragged vertically in sync.
        r2 = self._HANDLE_R + 6
        rhy = self._rail_handle_y()
        if abs(pos.y() - rhy) <= r2 and (
            abs(pos.x() - self._near_lx()) <= r2 or abs(pos.x() - self._near_rx()) <= r2
        ):
            return "rail_height"
        # Gap handles on the wall diagonal lines — checked before near handles
        gy = self._gap_y()
        if abs(pos.y() - gy) <= r and (
            abs(pos.x() - self._gap_lx()) <= r or abs(pos.x() - self._gap_rx()) <= r
        ):
            return "gap_side"
        ny = self._hit_y()
        if abs(pos.y() - ny) <= r and abs(pos.x() - self.width() // 2) <= r:
            return "hit"
        if abs(pos.y() - self._hz_y()) <= r and abs(pos.x() - self.width() // 2) <= r:
            return "horizon"
        fy = self._far_y()
        if abs(pos.y() - fy) <= r and (
            abs(pos.x() - self._far_rx()) <= r or abs(pos.x() - self._far_lx()) <= r
        ):
            return "wall_far"
        # Use clamped (display) positions for hit detection so handles at edge are clickable
        if abs(pos.y() - ny) <= r + 4 and (
            abs(pos.x() - self._near_rx()) <= r or abs(pos.x() - self._near_lx()) <= r
        ):
            return "wall_near"
        return None

    # ── Qt events ───────────────────────────────────────────────────────
    def mousePressEvent(self, ev) -> None:
        self._drag = self._handle_at(ev.pos())
        if self._drag in ("wall_near", "wall_far"):
            self.grabMouse()
        if self._drag and self._drag.startswith("cd_"):
            self._drag_anchor = ev.pos()
            self._drag_cd_x0 = self._cd_x
            self._drag_cd_y0 = self._cd_y
            self._drag_cd_w0 = self._cd_w
            self._drag_cd_h0 = self._cd_h
        if self._drag == "rail_height":
            self._drag_anchor = ev.pos()
            self._drag_rail_h0 = self._rail_height
        if self._drag == "gate_top":
            self._drag_anchor = ev.pos()
            self._drag_gate_h0 = self._start_gate_h
        if self._drag == "chevron_width":
            self._drag_anchor = ev.pos()
            self._drag_chevron_w0 = self._chevron_width_frac

    def mouseReleaseEvent(self, ev) -> None:
        if self._drag in ("wall_near", "wall_far"):
            self.releaseMouse()
        if self._drag:
            self._drag = None
            self.committed.emit(
                self._hit_frac, self._horizon_frac, self._near_spread,
                self._far_spread, self._wall_floor_gap_frac,
                self._cd_x, self._cd_y, self._cd_w, self._cd_h,
                self._rail_height,
                self._start_gate_h,
                self._chevron_width_frac,
            )

    def mouseMoveEvent(self, ev) -> None:
        hover = self._handle_at(ev.pos()) if self._drag is None else None
        if hover:
            if hover in ("hit", "horizon", "gap_side", "rail_height", "gate_top"):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif hover == "chevron_width":
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif hover == "wall_near":
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif hover in ("cd_tl", "cd_br"):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif hover in ("cd_tr", "cd_bl"):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif hover == "cd_move":
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif self._drag is None:
            self.setCursor(Qt.CursorShape.ArrowCursor)

        if self._drag is None:
            return
        h, w = self.height(), self.width()

        if self._drag == "hit":
            hit_lo, hit_hi = self._hit_frac_bounds()
            self._hit_frac = max(hit_lo, min(hit_hi, ev.pos().y() / h))
        elif self._drag == "gap_side":
            # Drag the wall-bottom corner vertically: convert to wall-floor gap frac
            gap_frac = (self._hit_y() - float(ev.pos().y())) / h
            self._wall_floor_gap_frac = max(0.00, min(0.30, gap_frac))
        elif self._drag == "horizon":
            v = max(0.20, min(0.60, ev.pos().y() / h))
            if v < self._hit_frac - 0.05:
                self._horizon_frac = v
        elif self._drag == "wall_near":
            rx = float(ev.pos().x())
            self._near_spread = max(0.20, min(3.00, (rx / w - 0.5) * 2))
        elif self._drag == "wall_far":
            # Horizontal drag — grabMouse() keeps events firing outside widget
            rx = float(ev.pos().x())
            self._far_spread = max(0.05, min(3.00, (rx / w - 0.5) * 2 / self._FAR_DISP))
        elif self._drag and self._drag.startswith("cd_"):
            dx = (ev.pos().x() - self._drag_anchor.x()) / float(max(1, w))
            dy = (ev.pos().y() - self._drag_anchor.y()) / float(max(1, h))
            x, y, ww, hh = self._drag_cd_x0, self._drag_cd_y0, self._drag_cd_w0, self._drag_cd_h0
            minf = 0.02
            if self._drag == "cd_move":
                x = max(0.0, min(1.0 - ww, x + dx))
                y = max(0.0, min(1.0 - hh, y + dy))
            elif self._drag == "cd_tl":
                nx = max(0.0, min(x + ww - minf, x + dx))
                ny = max(0.0, min(y + hh - minf, y + dy))
                ww = ww - (nx - x)
                hh = hh - (ny - y)
                x, y = nx, ny
            elif self._drag == "cd_tr":
                ny = max(0.0, min(y + hh - minf, y + dy))
                nw = max(minf, min(1.0 - x, ww + dx))
                hh = hh - (ny - y)
                y, ww = ny, nw
            elif self._drag == "cd_bl":
                nx = max(0.0, min(x + ww - minf, x + dx))
                nh = max(minf, min(1.0 - y, hh + dy))
                ww = ww - (nx - x)
                x, hh = nx, nh
            elif self._drag == "cd_br":
                ww = max(minf, min(1.0 - x, ww + dx))
                hh = max(minf, min(1.0 - y, hh + dy))
            self._cd_x, self._cd_y, self._cd_w, self._cd_h = x, y, ww, hh
        elif self._drag == "rail_height":
            dy = float(ev.pos().y() - self._drag_anchor.y())
            h = max(1.0, float(self.height()))
            self._rail_height = max(0.15, self._drag_rail_h0 - (dy / h) * 1.8)
        elif self._drag == "gate_top":
            dy = float(ev.pos().y() - self._drag_anchor.y())
            h = max(1.0, float(self.height()))
            self._start_gate_h = max(0.03, self._drag_gate_h0 - (dy / h) * 1.8)
        elif self._drag == "chevron_width":
            dx = float(ev.pos().x() - self._drag_anchor.x())
            w = max(1.0, float(self.width()))
            self._chevron_width_frac = max(
                0.05,
                min(0.95, self._drag_chevron_w0 + (dx / w) * 1.6),
            )
        self.update()
        self.changing.emit(
            self._hit_frac, self._horizon_frac, self._near_spread,
            self._far_spread, self._wall_floor_gap_frac,
            self._cd_x, self._cd_y, self._cd_w, self._cd_h,
            self._rail_height,
            self._start_gate_h,
            self._chevron_width_frac,
        )

    # ── paint ────────────────────────────────────────────────────────────
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = self._HANDLE_R

        def _h_line_with_handle(y: int, color: QColor, label: str) -> None:
            p.setPen(QPen(color, 1.5, Qt.PenStyle.DashLine))
            p.drawLine(0, y, w, y)
            p.setBrush(QBrush(color))
            p.setPen(QPen(QColor(255, 255, 255), 1.5))
            cx = w // 2
            p.drawEllipse(cx - r, y - r, r * 2, r * 2)
            p.setPen(QPen(color))
            p.drawText(cx + r + 6, y + 5, label)

        _h_line_with_handle(self._hit_y(), self._HIT_CLR,     "Floor")
        _h_line_with_handle(self._hz_y(),  self._HORIZON_CLR, "Horizon")

        ny = self._hit_y()
        fy = self._far_y()

        # Floor footprint outline (visual-only).
        floor_pts = self._floor_footprint_points()
        p.setBrush(QBrush(QColor(8, 145, 178, 30)))
        p.setPen(QPen(QColor(8, 145, 178, 150), 1.2, Qt.PenStyle.DashLine))
        p.drawPolygon(floor_pts)

        # Hit-zone blocks — 4 lane trapezoids (pinhole projection).
        _hit_fill = QColor(90, 70, 20, 65)       # dark amber, translucent fill
        _hit_edge = QColor(90, 140, 160, 210)    # CLR_LANE_EDGE in RGB
        p.setBrush(QBrush(_hit_fill))
        p.setPen(QPen(_hit_edge, 1.5))
        for _poly in self._hit_block_polys(n=4, frame_idx=self._cached_frame_idx):
            p.drawPolygon(_poly)

        # Dashed lines connecting far ↔ near handles (left and right)
        p.setPen(QPen(self._WALL_CLR, 1.2, Qt.PenStyle.DashLine))
        p.drawLine(self._far_lx(), fy, self._near_lx(), ny)
        p.drawLine(self._far_rx(), fy, self._near_rx(), ny)

        # Far handles (smaller circles, labelled "Far")
        rf = max(6, r - 3)
        p.setBrush(QBrush(self._WALL_CLR))
        p.setPen(QPen(QColor(255, 255, 255), 1.2))
        for fx_pos in (self._far_lx(), self._far_rx()):
            p.drawEllipse(fx_pos - rf, fy - rf, rf * 2, rf * 2)
        p.setPen(QPen(self._WALL_CLR))
        p.drawText(self._far_rx() + rf + 4, fy + 4, "Far")

        # Near handles — horizontal spread only
        p.setBrush(QBrush(self._WALL_CLR))
        p.setPen(QPen(QColor(255, 255, 255), 1.5))
        for nx_pos in (self._near_lx(), self._near_rx()):
            p.drawEllipse(nx_pos - r, ny - r, r * 2, r * 2)
        p.setPen(QPen(self._WALL_CLR))
        p.drawText(self._near_rx() + r + 4, ny + 5, "Near")

        # Rail-height handles (left/right, dragged vertically in sync).
        rhy = self._rail_handle_y()
        rrh = max(6, r - 2)
        rail_col = QColor(255, 90, 90)
        p.setBrush(QBrush(rail_col))
        p.setPen(QPen(QColor(255, 255, 255), 1.5))
        for rxh in (self._near_lx(), self._near_rx()):
            p.drawEllipse(rxh - rrh, rhy - rrh, rrh * 2, rrh * 2)
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.drawLine(rxh, rhy - rrh + 2, rxh, rhy + rrh - 2)
            p.drawLine(rxh - 4, rhy - rrh + 6, rxh, rhy - rrh + 2)
            p.drawLine(rxh + 4, rhy - rrh + 6, rxh, rhy - rrh + 2)
            p.drawLine(rxh - 4, rhy + rrh - 6, rxh, rhy + rrh - 2)
            p.drawLine(rxh + 4, rhy + rrh - 6, rxh, rhy + rrh - 2)
            p.setBrush(QBrush(rail_col))
            p.setPen(QPen(QColor(255, 255, 255), 1.5))
        p.setPen(QPen(rail_col))
        p.drawText(self._near_rx() + rrh + 6, rhy + 5, f"Rail H {self._rail_height:.2f}")

        # Gap handles — front-bottom wall corners (left/right)
        # Drag vertically to change wall-floor gap.
        rg = r + 2
        gy = self._gap_y()
        p.setBrush(QBrush(self._GAP_CLR))
        p.setPen(QPen(QColor(255, 255, 255), 1.5))
        for gx in (self._gap_lx(), self._gap_rx()):
            p.drawEllipse(gx - rg, gy - rg, rg * 2, rg * 2)
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.drawLine(gx, gy - rg + 3, gx, gy + rg - 3)
            p.drawLine(gx - 4, gy - rg + 7, gx, gy - rg + 3)
            p.drawLine(gx + 4, gy - rg + 7, gx, gy - rg + 3)
            p.drawLine(gx - 4, gy + rg - 7, gx, gy + rg - 3)
            p.drawLine(gx + 4, gy + rg - 7, gx, gy + rg - 3)
            p.setBrush(QBrush(self._GAP_CLR))
            p.setPen(QPen(QColor(255, 255, 255), 1.5))
        p.setPen(QPen(self._GAP_CLR))
        p.drawText(self._gap_rx() + rg + 4, gy + 5, "Gap")

        # Chevron width handle (horizontal drag).
        ch = self._chevron_width_handle_pos()
        cmid_w = max(8, int((self._near_rx() - self._near_lx()) * self._chevron_width_frac))
        x_l = ch.x() - cmid_w // 2
        x_r = ch.x() + cmid_w // 2
        p.setPen(QPen(QColor(8, 145, 178), 1.4))
        p.drawLine(x_l, ch.y() - 6, x_l, ch.y() + 6)
        p.drawLine(x_r, ch.y() - 6, x_r, ch.y() + 6)
        p.setBrush(QBrush(QColor(8, 145, 178)))
        p.setPen(QPen(QColor(255, 255, 255), 1.4))
        p.drawEllipse(ch.x() - 7, ch.y() - 7, 14, 14)
        p.setPen(QPen(QColor(8, 145, 178)))
        p.drawText(ch.x() + 10, ch.y() + 4, f"Chevron W {self._chevron_width_frac:.2f}")

        if self._start_gate_enabled:
            sg = self._start_gate_rect_px()
            gate_col = QColor(255, 70, 70)
            p.setBrush(QBrush(QColor(255, 70, 70, 26)))
            p.setPen(QPen(gate_col, 1.8, Qt.PenStyle.DashLine))
            p.drawRect(sg)
            hs = 8
            hx = sg.center().x()
            hy = sg.top()
            p.setBrush(QBrush(gate_col))
            p.setPen(QPen(QColor(255, 255, 255), 1.4))
            p.drawRect(hx - hs, hy - hs, hs * 2, hs * 2)
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.drawLine(hx, hy - hs + 2, hx, hy + hs - 2)
            p.drawLine(hx - 4, hy - hs + 6, hx, hy - hs + 2)
            p.drawLine(hx + 4, hy - hs + 6, hx, hy - hs + 2)
            p.drawLine(hx - 4, hy + hs - 6, hx, hy + hs - 2)
            p.drawLine(hx + 4, hy + hs - 6, hx, hy + hs - 2)
            p.setPen(QPen(gate_col))
            p.drawText(sg.left() + 6, max(14, sg.top() - 8), f"Gate H {self._start_gate_h:.2f}")

        if self._countdown_enabled:
            cd = self._countdown_rect_px()
            p.setBrush(QBrush(QColor(255, 80, 220, 36)))
            p.setPen(QPen(QColor(255, 120, 235), 2.0, Qt.PenStyle.DashLine))
            p.drawRect(cd)
            hs = 7
            p.setBrush(QBrush(QColor(255, 120, 235)))
            p.setPen(QPen(QColor(255, 255, 255), 1.2))
            for cx, cy in (
                (cd.left(), cd.top()),
                (cd.right(), cd.top()),
                (cd.left(), cd.bottom()),
                (cd.right(), cd.bottom()),
            ):
                p.drawRect(cx - hs, cy - hs, hs * 2, hs * 2)
            p.setPen(QPen(QColor(255, 120, 235)))
            p.drawText(cd.left() + 6, max(14, cd.top() - 6), "Countdown")


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
    # Emitted on mouse-release when the user finishes dragging floor/wall
    # handles or the relax countdown box.
    # MainWindow saves these into segment.render_settings and requests a preview update.
    floor_wall_committed = Signal(
        float, float, float, float, float, float, float, float, float, float, float, float
    )
    # Emitted whenever the panel exits live-preview mode, whether the
    # caller explicitly invoked ``stop_live_preview`` or the panel
    # auto-stopped because a different source was loaded (user
    # selected another segment / switched the source-combo / a
    # rendered video auto-loaded after export).  MainWindow listens
    # so its ``_preview_mode_active`` flag stays in sync with the
    # panel's actual state.
    live_preview_stopped = Signal()
    # Emitted when full-preview auto-advance picks a next segment.
    # Empty id ("") means we reached end-of-timeline.
    segment_auto_advanced = Signal(str)
    # Emitted whenever the full-preview checkbox toggles.
    full_preview_mode_changed = Signal(bool)
    # Emitted when full-preview seek crosses into another segment while live
    # preview is active. MainWindow should rebuild renderer using that segment's
    # config, then seek to the requested project time.
    segment_seek_requested = Signal(str, float)  # segment_id, project_time_sec

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
        # Full-preview mode: when True, EndOfMedia auto-advances to the next
        # timeline segment instead of stopping at the current segment end.
        self._full_preview_mode: bool = False
        # Auto project playback mode: enabled when user presses Play while
        # no segment/source is selected. Behaves like full-preview chaining
        # without requiring the checkbox to be toggled manually.
        self._project_playback_mode: bool = False
        # Countdown audio (preview-only playback of countdown ticks).
        self._countdown_audio_enabled: bool = False
        self._countdown_audio_mode: str = "default"
        self._countdown_audio_file: str = ""
        self._countdown_audio_volume: float = 0.65
        self._countdown_audio_last_mode: str = "default"
        self._countdown_audio_last_file: str = ""
        self._countdown_prev_text: str | None = None
        self._default_countdown_sound: str = self._ensure_countdown_default_sound(
            "ssc_countdown_regular.wav", hz=940.0, duration_sec=0.09
        )
        self._default_countdown_last_sound: str = self._ensure_countdown_default_sound(
            "ssc_countdown_last.wav", hz=1260.0, duration_sec=0.13
        )
        # Cached timeline order pushed by MainWindow.
        self._project_segments: list[Segment] = []
        # Shared project layers reference for layer-first visual resolves.
        self._project_layers: list = []
        # Last observed playback state (used to decide auto-resume on advance).
        self._was_playing: bool = False
        # While user drags the seek slider in full-preview mode, we defer the
        # expensive cross-segment source-switch to slider release.
        self._full_seek_dragging: bool = False
        self._pending_full_seek_ms: int = -1
        self._build_ui()
        if hasattr(self, "_full_preview_cb"):
            self._full_preview_cb.blockSignals(True)
            self._full_preview_cb.setChecked(False)
            self._full_preview_cb.blockSignals(False)

    def set_source_media(self, media: MediaItem | None) -> None:
        """Set selected media source and load it for preview."""
        self._selected_media = media
        # Raw media items always live at project-time 0.
        self._playhead_offset_sec = 0.0
        if self.source_combo.currentData() == "media":
            self._load_active_source()

    def set_project_segments(self, segments: list[Segment]) -> None:
        """Set sorted segment cache used by full-preview auto-advance."""
        self._project_segments = sorted(
            list(segments or []), key=lambda s: float(s.start_time_sec or 0.0)
        )
        # If no source is currently loaded, allow Play to start project-wide
        # playback from the first segment.
        if self._current_url.isEmpty() and not self._media_ready:
            self.play_button.setEnabled(bool(self._project_segments))
            if self._project_segments:
                self._show_empty("No segment selected - press Play to preview project")
        self._refresh_seek_ui(self.player.position(), self.player.duration())

    def set_project_layers(self, layers: list) -> None:
        """Set project layers reference used by overlay resolution."""
        self._project_layers = layers if isinstance(layers, list) else []

    def _is_project_timeline_mode(self) -> bool:
        return bool(self._full_preview_mode or self._project_playback_mode)

    def is_full_preview_mode(self) -> bool:
        return bool(self._full_preview_mode)

    def _on_full_preview_toggled(self, checked: bool) -> None:
        self._full_preview_mode = bool(checked)
        self._refresh_seek_ui(self.player.position(), self.player.duration())
        self.full_preview_mode_changed.emit(self._full_preview_mode)

    def _timeline_total_duration_ms(self) -> int:
        """Total project duration from cached segments (ms)."""
        if not self._project_segments:
            return 0
        end_sec = max(float(getattr(s, "end_time_sec", 0.0) or 0.0) for s in self._project_segments)
        return max(0, int(round(end_sec * 1000.0)))

    def _refresh_time_label(self, position_ms: int, media_duration_ms: int) -> None:
        """Refresh time label for segment-mode or full-project mode."""
        show_project_timeline = (
            self._selected_segment is not None
            and bool(self._project_segments)
        )
        if show_project_timeline:
            current_project_ms = int(
                round((self._playhead_offset_sec + max(0, position_ms) / 1000.0) * 1000.0)
            )
            total_project_ms = self._timeline_total_duration_ms()
            if total_project_ms > 0:
                self.time_label.setText(
                    f"{format_ms(current_project_ms)} / {format_ms(total_project_ms)}"
                )
                return
        self.time_label.setText(
            f"{format_ms(max(0, position_ms))} / {format_ms(max(0, media_duration_ms))}"
        )

    def _project_ms_from_local_ms(self, local_ms: int) -> int:
        return int(
            round((self._playhead_offset_sec + max(0, local_ms) / 1000.0) * 1000.0)
        )

    def _refresh_seek_ui(self, position_ms: int, media_duration_ms: int) -> None:
        """Refresh both seek range/value and time label for current mode."""
        self._refresh_time_label(position_ms, media_duration_ms)
        if self._is_project_timeline_mode() and self._project_segments:
            total_ms = self._timeline_total_duration_ms()
            proj_ms = self._project_ms_from_local_ms(position_ms)
            self.seek_slider.blockSignals(True)
            self.seek_slider.setRange(0, max(0, total_ms))
            self.seek_slider.setValue(max(0, min(total_ms, proj_ms)))
            self.seek_slider.blockSignals(False)
            return
        self.seek_slider.blockSignals(True)
        self.seek_slider.setRange(0, max(0, media_duration_ms))
        self.seek_slider.setValue(max(0, position_ms))
        self.seek_slider.blockSignals(False)

    def _segment_for_project_time(self, t_sec: float) -> Optional[Segment]:
        if not self._project_segments:
            return None
        t = float(max(0.0, t_sec))
        # Primary: segment containing t.
        for seg in self._project_segments:
            s = float(seg.start_time_sec or 0.0)
            e = float(seg.end_time_sec or s)
            if s <= t < e:
                return seg
        # If inside a gap, pick next segment; if beyond end, clamp to last.
        for seg in self._project_segments:
            if float(seg.start_time_sec or 0.0) >= t:
                return seg
        return self._project_segments[-1]

    def _seek_project_time_sec(self, t_sec: float, *, resume_play: bool = False) -> None:
        """Seek by project time; switches source segment if needed."""
        target = self._segment_for_project_time(t_sec)
        if target is None:
            return
        if (
            self._is_project_timeline_mode()
            and self._live_active
            and self._selected_segment is not None
            and self._selected_segment.id != target.id
        ):
            # Do NOT swap source in-place here: live renderer config would still
            # belong to the old segment. Delegate to MainWindow to rebuild with
            # the target segment's settings.
            self.segment_seek_requested.emit(target.id, float(max(0.0, t_sec)))
            self.playhead_changed.emit(float(max(0.0, t_sec)))
            return
        local_sec = max(0.0, float(t_sec) - float(target.start_time_sec or 0.0))
        local_ms = int(round(local_sec * 1000.0))
        was_playing = (
            self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        ) or bool(resume_play)

        if self._selected_segment is None or self._selected_segment.id != target.id:
            self.set_source_segment(target)

        if self._media_ready:
            self.player.setPosition(local_ms)
            if was_playing:
                self.player.play()
        else:
            self._pending_seek_ms = local_ms
            if was_playing:
                self._pending_play = True
                self._set_play_button_state(playing=True)

        self.playhead_changed.emit(float(max(0.0, t_sec)))

    def _on_seek_slider_moved(self, value: int) -> None:
        if not (self._is_project_timeline_mode() and self._project_segments):
            self.player.setPosition(int(value))
            return
        self._full_seek_dragging = True
        self._pending_full_seek_ms = int(value)
        total_ms = self._timeline_total_duration_ms()
        self.time_label.setText(
            f"{format_ms(max(0, value))} / {format_ms(max(0, total_ms))}"
        )

    def _on_seek_slider_pressed(self) -> None:
        """Any seek interaction forces playback into paused state."""
        self._pending_play = False
        self.player.pause()
        if self._is_project_timeline_mode() and self._project_segments:
            self._full_seek_dragging = True
            self._pending_full_seek_ms = int(self.seek_slider.value())

    def _on_seek_slider_released(self) -> None:
        if not (self._is_project_timeline_mode() and self._project_segments):
            return
        self._full_seek_dragging = False
        target_ms = (
            self._pending_full_seek_ms
            if self._pending_full_seek_ms >= 0
            else int(self.seek_slider.value())
        )
        self._pending_full_seek_ms = -1
        self._seek_project_time_sec(float(target_ms) / 1000.0, resume_play=False)

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
        """Sync stickman overlay state with current segment."""
        seg = self._selected_segment
        enabled = self._segment_stickman_enabled(seg)
        # Stick box is controlled by the shared Floor/Wall toggle.
        self.stickman_button.setVisible(False)
        self.stickman_button.setEnabled(False)
        if not enabled and self._stickman_edit_active:
            self._on_stickman_edit_toggled(False)
        if enabled and seg is not None:
            self.stickman_overlay.set_normalized(
                *self._segment_stick_fractions(seg)
            )
        # Floor/Wall button is available whenever a segment is loaded (live or not)
        self.floor_wall_button.setEnabled(seg is not None)

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
                self._stickman_edit_active = False
                self._stickman_pos_timer.stop()
                self.stickman_overlay.hide()
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

    # ── Floor/Wall overlay methods ──────────────────────────────────────
    def _sync_floor_wall_overlay_pos(self) -> None:
        if not self._floor_wall_edit_active:
            return
        rect = self._rendered_image_rect_global()
        if rect is None:
            tl = self.stage_stack.mapToGlobal(QPoint(0, 0))
            sz = self.stage_stack.size()
            self.floor_wall_overlay.setGeometry(
                tl.x(), tl.y(), sz.width(), sz.height()
            )
            return
        self.floor_wall_overlay.setGeometry(rect)

    def _on_floor_wall_edit_toggled(self, checked: bool) -> None:
        self._floor_wall_edit_active = bool(checked)
        if checked:
            seg = self._selected_segment
            # Selection can become stale after undo/redo paths that replace
            # project segment objects. Rebind by id to the latest instance.
            if seg is not None and self._project_segments:
                seg_latest = next(
                    (s for s in self._project_segments if s.id == seg.id),
                    seg,
                )
                if seg_latest is not seg:
                    self._selected_segment = seg_latest
                    seg = seg_latest
            rs: dict = {}
            if seg is not None:
                # Use layer-resolved config so Adjust opens with the exact
                # currently effective values (layer-first projects included).
                try:
                    from studio.models.layer import resolve_segment_config
                    rs = dict(resolve_segment_config(seg, self._project_layers))
                except Exception:
                    rs = dict(getattr(seg, "render_settings", {}) or {})
            # Default camera values (must match PerspectiveCamera defaults).
            _CAM_HIT_DEFAULT   = 0.86
            _CAM_HZ_DEFAULT    = 0.45
            _CAM_NEAR_DEFAULT  = 0.65
            try:
                hit     = float(rs["floor_hit_frac"])  if rs.get("floor_hit_frac")  is not None else _CAM_HIT_DEFAULT
                hz      = float(rs["horizon_frac"])     if rs.get("horizon_frac")     is not None else _CAM_HZ_DEFAULT
                near_sp = float(rs["floor_spread_frac"])if rs.get("floor_spread_frac")is not None else _CAM_NEAR_DEFAULT
            except (TypeError, ValueError):
                hit, hz, near_sp = _CAM_HIT_DEFAULT, _CAM_HZ_DEFAULT, _CAM_NEAR_DEFAULT
            try:
                far_sp = float(rs["far_spread_frac"])   if rs.get("far_spread_frac")  is not None else near_sp
                gap    = float(rs["wall_floor_gap_frac"])if rs.get("wall_floor_gap_frac")is not None else 0.0
            except (TypeError, ValueError):
                far_sp, gap = near_sp, 0.0
            has_relax = False
            if seg is not None:
                if str(seg.mode) == "relax":
                    has_relax = True
                elif str(seg.mode) == "combo":
                    modes = rs.get("mode_list") or []
                    has_relax = "relax" in [str(m) for m in modes]
            cdx, cdy, cdw, cdh = self._get_countdown_bbox(seg)
            rail_h = float(rs.get("rail_height", 0.14) or 0.14)
            gate_enabled = bool(rs.get("start_gate_enabled", False))
            gate_h = float(rs.get("start_gate_h", 0.14) or 0.14)
            chevron_w = self._get_floor_chevron_width(seg, rs=rs)
            gate_rect = None
            # Live renderer is the ground truth: its camera was already built
            # with the resolved values, so always prefer it over stale rs.
            if self._live_active and self._live_renderer is not None:
                lr = self._live_renderer
                _fhf = getattr(lr, "_floor_hit_frac", None)
                if _fhf is not None:
                    hit = float(_fhf)
                _hzf = getattr(lr, "_horizon_frac", None)
                if _hzf is not None:
                    hz = float(_hzf)
                _nsf = getattr(lr, "_floor_spread_frac", None)
                if _nsf is not None:
                    near_sp = float(_nsf)
                _fsf = getattr(lr, "_far_spread_frac", None)
                if _fsf is not None:
                    far_sp = float(_fsf)
                _wgf = getattr(lr, "_wall_floor_gap_frac", None)
                if _wgf is not None:
                    gap = float(_wgf)
                cdx = float(getattr(lr, "_relax_countdown_x", cdx))
                cdy = float(getattr(lr, "_relax_countdown_y", cdy))
                cdw = float(getattr(lr, "_relax_countdown_w", cdw))
                cdh = float(getattr(lr, "_relax_countdown_h", cdh))
                rail_h = float(getattr(lr, "_rail_height", rail_h))
                gate_enabled = bool(getattr(lr, "_start_gate_enabled", gate_enabled))
                gate_h = float(getattr(lr, "_start_gate_h", gate_h))
                chevron_w = float(getattr(lr, "_chevron_width_frac", chevron_w))
                gate_rect = lr.get_start_gate_rect()
            # Set geometry BEFORE set_fractions so _hit_frac_bounds() has the
            # correct widget height. Without this, the overlay height is 0 at
            # construction time, which makes the margin huge and clamps any
            # hit_frac value to ~0.51 (screen middle).
            self._sync_floor_wall_overlay_pos()
            self.floor_wall_overlay.set_fractions(
                hit, hz, near_sp, far_sp, gap, rail_h,
                has_relax, cdx, cdy, cdw, cdh, chevron_w,
            )
            if gate_rect is not None:
                gx, gy, gw, gh = gate_rect
            else:
                gx, gy, gw, gh = (0.30, 0.18, 0.40, 0.22)
            self.floor_wall_overlay.set_start_gate_proxy(
                enabled=gate_enabled,
                gate_height=gate_h,
                x=gx,
                y=gy,
                w=gw,
                h=gh,
            )
            self._on_stickman_edit_toggled(
                seg is not None and self._segment_stickman_enabled(seg)
            )
            self.floor_wall_overlay.show()
            self._floor_wall_pos_timer.start()
        else:
            self._floor_wall_pos_timer.stop()
            self.floor_wall_overlay.hide()
            self._on_stickman_edit_toggled(False)

    def _on_floor_wall_changing(
        self,
        hit: float, hz: float, near_sp: float, far_sp: float, gap: float,
        cdx: float, cdy: float, cdw: float, cdh: float, rail_h: float,
        start_gate_h: float,
        chevron_width_frac: float,
    ) -> None:
        """Live-update the renderer while the user drags a handle."""
        if not self._live_active or self._live_renderer is None:
            return
        self._live_renderer.update_floor_wall(
            floor_hit_frac=hit,
            horizon_frac=hz,
            floor_spread_frac=near_sp,
            far_spread_frac=far_sp,
            wall_floor_gap_frac=gap,
        )
        self._live_renderer.update_side_rail_height(rail_h)
        self._live_renderer.update_start_gate_height(start_gate_h)
        self._live_renderer.update_floor_chevron_width(chevron_width_frac)
        self._live_renderer.update_countdown_box(x=cdx, y=cdy, w=cdw, h=cdh)
        gate_rect = self._live_renderer.get_start_gate_rect()
        if gate_rect is not None:
            gx, gy, gw, gh = gate_rect
            self.floor_wall_overlay.set_start_gate_proxy(
                enabled=bool(getattr(self._live_renderer, "_start_gate_enabled", False)),
                gate_height=float(getattr(self._live_renderer, "_start_gate_h", start_gate_h)),
                x=gx,
                y=gy,
                w=gw,
                h=gh,
            )
        self._render_live_frame(self.player.position() / 1000.0)

    def _on_floor_wall_committed(
        self,
        hit: float, hz: float, near_sp: float, far_sp: float, gap: float,
        cdx: float, cdy: float, cdw: float, cdh: float, rail_h: float,
        start_gate_h: float,
        chevron_width_frac: float,
    ) -> None:
        """Persist the final drag result and notify MainWindow."""
        seg = self._selected_segment
        if seg is not None:
            seg.render_settings["floor_hit_frac"]       = round(hit,     4)
            seg.render_settings["horizon_frac"]         = round(hz,      4)
            seg.render_settings["floor_spread_frac"]    = round(near_sp, 4)
            seg.render_settings["far_spread_frac"]      = round(far_sp,  4)
            seg.render_settings["wall_floor_gap_frac"]  = round(gap,     4)
            seg.render_settings["rail_height"]          = round(max(0.15, rail_h), 4)
            seg.render_settings["start_gate_h"]         = round(max(0.03, start_gate_h), 4)
            seg.render_settings["chevron_width_frac"]   = round(
                max(0.05, min(0.95, chevron_width_frac)), 4
            )
        self.floor_wall_committed.emit(
            hit, hz, near_sp, far_sp, gap, cdx, cdy, cdw, cdh,
            max(0.15, rail_h), max(0.03, start_gate_h),
            max(0.05, min(0.95, chevron_width_frac)),
        )

    def _get_countdown_bbox(
        self, seg: Segment | None
    ) -> tuple[float, float, float, float]:
        """Resolve countdown box from countdown layer config."""
        if seg is None or not self._project_layers:
            return (0.88, 0.04, 0.10, 0.16)
        seg_start = float(getattr(seg, "start_time_sec", 0.0) or 0.0)
        seg_end = float(getattr(seg, "end_time_sec", 0.0) or 0.0)
        cd_layers = [
            la for la in self._project_layers
            if getattr(la, "kind", "") == "countdown"
            and la.overlaps(seg_start, seg_end)
        ]
        if not cd_layers:
            return (0.88, 0.04, 0.10, 0.16)
        top = max(cd_layers, key=lambda la: int(getattr(la, "z_index", 0)))
        cfg = getattr(top, "config", {}) or {}
        return (
            float(cfg.get("relax_countdown_x", 0.88) or 0.88),
            float(cfg.get("relax_countdown_y", 0.04) or 0.04),
            float(cfg.get("relax_countdown_w", 0.10) or 0.10),
            float(cfg.get("relax_countdown_h", 0.16) or 0.16),
        )

    def _get_floor_chevron_width(
        self,
        seg: Segment | None,
        *,
        rs: Optional[dict] = None,
    ) -> float:
        """Resolve floor chevron width from floor layer config."""
        base = 0.45
        if rs is not None:
            try:
                base = float(rs.get("chevron_width_frac", base) or base)
            except (TypeError, ValueError):
                base = 0.45
        if seg is None or not self._project_layers:
            return max(0.05, min(0.95, base))
        seg_start = float(getattr(seg, "start_time_sec", 0.0) or 0.0)
        seg_end = float(getattr(seg, "end_time_sec", 0.0) or 0.0)
        floor_layers = [
            la for la in self._project_layers
            if getattr(la, "kind", "") == "floor"
            and la.overlaps(seg_start, seg_end)
        ]
        if not floor_layers:
            return max(0.05, min(0.95, base))
        top = max(floor_layers, key=lambda la: int(getattr(la, "z_index", 0)))
        cfg = getattr(top, "config", {}) or {}
        try:
            return max(0.05, min(0.95, float(cfg.get("chevron_width_frac", base) or base)))
        except (TypeError, ValueError):
            return max(0.05, min(0.95, base))

    def set_floor_wall_edit_enabled(self, enabled: bool) -> None:
        """Enable or disable the Floor/Wall button (only meaningful in live-preview)."""
        self.floor_wall_button.setEnabled(enabled)
        if not enabled and self._floor_wall_edit_active:
            self.floor_wall_button.setChecked(False)

    def set_source_segment(
        self,
        segment: Segment | None,
        *,
        keep_project_mode: bool = False,
    ) -> None:
        """Set selected segment and load the most useful source for preview.

        Priority:
        1. **Rendered video on disk** (``segment.video_path`` exists AND
           the file actually exists) — plays the finished output.
        2. **Pre-trimmed audio** (``segment.trimmed_audio_path``) — the
           exact audio window for this segment, starts at t=0, no seek.
        3. **Raw source audio** (``segment.audio_path``) — seeks to
           ``audio_offset_sec`` so playback begins at the segment's
           content, not at the start of the full source file.
        4. Nothing → clear.
        """
        if not keep_project_mode:
            self._project_playback_mode = False
        was_live_preview = self.is_live_preview_active()
        keep_live = was_live_preview and self._full_preview_mode
        self._selected_segment = segment

        if segment is None:
            if not keep_live:
                self.clear()
            return

        rendered_ready = bool(
            segment.video_path and Path(segment.video_path).exists()
        )

        if rendered_ready:
            self._set_source_combo_silently("segment")
            self._playhead_offset_sec = float(segment.start_time_sec or 0.0)
            self._load_path(
                segment.video_path,  # type: ignore[arg-type]
                keep_live_preview=keep_live,
            )
            self._refresh_stickman_button_state()
            return

        # Prefer the pre-trimmed file: it covers exactly the segment's audio
        # window starting at t=0.  The playhead offset must be the segment's
        # timeline start so that player position 0 maps to start_time_sec on
        # the project timeline (same convention as the rendered-video branch).
        trimmed = getattr(segment, "trimmed_audio_path", None)
        if trimmed and Path(trimmed).exists():
            self._set_source_combo_silently("media")
            self._playhead_offset_sec = float(segment.start_time_sec or 0.0)
            self._load_path(trimmed, keep_live_preview=keep_live)
            self._refresh_stickman_button_state()
            return

        if segment.audio_path and Path(segment.audio_path).exists():
            # No trimmed file yet (trim still in-flight or not triggered).
            # Load the full source file; duration will reflect the whole file
            # but at least audio plays.  The player will reload automatically
            # once the trim completes via _on_trim_ready → set_source_segment.
            self._set_source_combo_silently("media")
            self._playhead_offset_sec = 0.0
            self._load_path(segment.audio_path, keep_live_preview=keep_live)
            self._refresh_stickman_button_state()
            return

        self.clear()
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
        # Also hide floor/wall overlay
        if hasattr(self, "_floor_wall_pos_timer"):
            self._floor_wall_pos_timer.stop()
        if hasattr(self, "floor_wall_overlay"):
            self._floor_wall_edit_active = False
            self.floor_wall_overlay.hide()
        if hasattr(self, "floor_wall_button"):
            if self.floor_wall_button.isChecked():
                self.floor_wall_button.blockSignals(True)
                self.floor_wall_button.setChecked(False)
                self.floor_wall_button.blockSignals(False)
            self.floor_wall_button.setEnabled(False)

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

        # Floor/Wall drag overlay (same floating-Tool-window pattern).
        self.floor_wall_overlay = FloorWallOverlay(self)
        self.floor_wall_overlay.changing.connect(self._on_floor_wall_changing)
        self.floor_wall_overlay.committed.connect(self._on_floor_wall_committed)
        self._floor_wall_edit_active: bool = False
        self._floor_wall_pos_timer = QTimer(self)
        self._floor_wall_pos_timer.setInterval(50)
        self._floor_wall_pos_timer.timeout.connect(self._sync_floor_wall_overlay_pos)

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

        # Dedicated media players for countdown SFX so play/pause of the main
        # preview audio track does not interrupt short per-tick sounds.
        self._countdown_sfx_out = QAudioOutput(self)
        self._countdown_sfx_out.setVolume(self._countdown_audio_volume)
        self._countdown_sfx_player = QMediaPlayer(self)
        self._countdown_sfx_player.setAudioOutput(self._countdown_sfx_out)

        self._countdown_sfx_last_out = QAudioOutput(self)
        self._countdown_sfx_last_out.setVolume(self._countdown_audio_volume)
        self._countdown_sfx_last_player = QMediaPlayer(self)
        self._countdown_sfx_last_player.setAudioOutput(self._countdown_sfx_last_out)

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
        self.seek_slider.sliderPressed.connect(self._on_seek_slider_pressed)
        self.seek_slider.sliderMoved.connect(self._on_seek_slider_moved)
        self.seek_slider.sliderReleased.connect(self._on_seek_slider_released)
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
        self.stickman_button.setVisible(False)

        self.floor_wall_button = QPushButton("Edit Layout")
        self.floor_wall_button.setCheckable(True)
        self.floor_wall_button.setToolTip(
            "Toggle drag handles to adjust floor/wall and stick box.\n"
            "Drag directly on the player."
        )
        self.floor_wall_button.toggled.connect(self._on_floor_wall_edit_toggled)
        self.floor_wall_button.setEnabled(False)
        self._full_preview_cb = QCheckBox("Preview full")
        self._full_preview_cb.setToolTip(
            "Auto-play next segment when current reaches end.\n"
            "When selecting another segment on timeline, keep preview mode on."
        )
        self._full_preview_cb.setChecked(self._full_preview_mode)
        self._full_preview_cb.toggled.connect(self._on_full_preview_toggled)

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
        control_row.addWidget(self._full_preview_cb)
        control_row.addWidget(self.floor_wall_button)
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

    def _load_path(
        self,
        raw_path: str,
        *,
        force_reload: bool = False,
        keep_live_preview: bool = False,
    ) -> None:
        # When live-preview is active, ANY call to load a different
        # video/audio source is a deliberate choice by the caller
        # (user picked another segment, switched the source-combo,
        # auto-loaded a freshly-rendered file…) that supersedes live
        # mode.  Tear it down first so the renderer + frame timer
        # release cleanly before we start a new probe.
        if self._live_active and not keep_live_preview:
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
        if self._live_active and keep_live_preview:
            # Keep live page visible while swapping audio source in-place.
            self.stage_stack.setCurrentIndex(self._live_page_index)
            self._loading_timer.stop()
        else:
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

        # Case 4: no source loaded, but project has segments -> start project playback.
        if (
            status == MS.NoMedia
            and self._current_url.isEmpty()
            and self._project_segments
        ):
            first = next(
                (seg for seg in self._project_segments if self._is_segment_playable(seg)),
                None,
            )
            if first is None:
                self._show_empty("No playable segment found")
                self.play_button.setEnabled(False)
                return
            self._project_playback_mode = True
            self._pending_play = True
            self._pending_seek_ms = 0
            self._set_play_button_state(playing=True)
            self.set_source_segment(first, keep_project_mode=True)
            return

        # Case 5: no media loaded but we have a cached url -> reattach source.
        if status == MS.NoMedia and not self._current_url.isEmpty():
            self._pending_play = True
            self._set_play_button_state(playing=True)
            self.player.setSource(self._current_url)
            return

        self.player.play()

    def _on_stop_clicked(self) -> None:
        """Hard-stop player and drop current source selection/cache.

        User intent for this button is "reset player state completely".
        We therefore clear the loaded media URL and selected source refs
        so pressing Play cannot revive a stale/deleted segment by cache.
        """
        self.clear()
        self._selected_media = None
        self._selected_segment = None
        self._project_playback_mode = False
        self._pending_play = False
        self._pending_seek_ms = -1
        self._playhead_offset_sec = 0.0
        self._full_seek_dragging = False
        self.source_combo.blockSignals(True)
        try:
            idx = self.source_combo.findData("media")
            if idx >= 0:
                self.source_combo.setCurrentIndex(idx)
        finally:
            self.source_combo.blockSignals(False)
        self.play_button.setEnabled(bool(self._project_segments))
        if self._project_segments:
            self._show_empty("No segment selected - press Play to preview project")

    def _on_position_changed(self, value: int) -> None:
        if self._is_project_timeline_mode() and self._full_seek_dragging:
            # Keep user's dragged thumb position untouched while dragging.
            self._refresh_time_label(self.player.position(), self.player.duration())
        else:
            self._refresh_seek_ui(value, self.player.duration())
        # Translate media-local time → project timeline time so the timeline
        # red playhead tracks correctly even when we're playing a rendered
        # segment video that starts mid-project (offset = segment.start).
        self.playhead_changed.emit(value / 1000.0 + self._playhead_offset_sec)

    def _on_duration_changed(self, value: int) -> None:
        self._refresh_seek_ui(self.player.position(), value)

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
                if self.stage_stack.currentIndex() == 1:
                    self.stage_stack.setCurrentIndex(self._live_page_index)
            self.play_button.setEnabled(True)
            if self._pending_seek_ms >= 0:
                self.player.setPosition(self._pending_seek_ms)
                self._pending_seek_ms = -1
            if self._pending_play:
                self._pending_play = False
                self.player.play()
            return

        if status == MS.EndOfMedia:
            # Full-preview mode: advance only on genuine end-position events.
            if self._is_project_timeline_mode():
                dur = int(self.player.duration())
                pos = int(self.player.position())
                at_end = (dur <= 0) or (pos >= max(0, dur - 120))
                if at_end and self._auto_advance_to_next_segment():
                    return
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
        self._was_playing = bool(is_playing)
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

    def _is_segment_playable(self, segment: Segment | None) -> bool:
        if segment is None:
            return False
        if segment.video_path and Path(segment.video_path).exists():
            return True
        p = segment.trimmed_audio_path or segment.audio_path
        return bool(p) and Path(str(p)).exists()

    def _auto_advance_to_next_segment(self) -> bool:
        """Advance to next playable segment; returns True if advanced."""
        current = self._selected_segment
        if current is None or not self._project_segments:
            return False
        cur_idx = next(
            (i for i, s in enumerate(self._project_segments) if s.id == current.id),
            -1,
        )
        if cur_idx < 0:
            return False

        next_idx = cur_idx + 1
        while next_idx < len(self._project_segments):
            cand = self._project_segments[next_idx]
            if self._is_segment_playable(cand):
                next_seg = cand
                self.segment_auto_advanced.emit(next_seg.id)
                self.set_source_segment(next_seg, keep_project_mode=True)
                # EndOfMedia-triggered advance should continue playing.
                if self._media_ready:
                    self.player.play()
                else:
                    self._pending_play = True
                    self._set_play_button_state(playing=True)
                return True
            next_idx += 1

        # End reached (or no later playable segment).
        self.segment_auto_advanced.emit("")
        return False

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

    @staticmethod
    def _normalize_countdown_audio_mode(value: object) -> str:
        raw = str(value or "").strip().lower()
        return "file" if raw == "file" else "default"

    @staticmethod
    def _normalize_countdown_last_mode(value: object) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"file", "same"}:
            return raw
        return "default"

    @staticmethod
    def _ensure_countdown_default_sound(
        filename: str, *, hz: float, duration_sec: float
    ) -> str:
        """Create a tiny wav beep file in temp dir if missing."""
        out = Path(tempfile.gettempdir()) / filename
        if out.exists():
            return str(out)
        sr = 44100
        n = max(1, int(sr * float(duration_sec)))
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            frames = bytearray()
            for i in range(n):
                t = i / float(sr)
                env = 1.0 - (i / float(n))
                s = math.sin(2.0 * math.pi * float(hz) * t) * env
                amp = int(max(-1.0, min(1.0, s)) * 28000)
                frames.extend(struct.pack("<h", amp))
            wf.writeframes(bytes(frames))
        return str(out)

    def _resolve_countdown_sfx_path(self, *, is_last: bool) -> str:
        if is_last:
            mode = self._normalize_countdown_last_mode(self._countdown_audio_last_mode)
            if mode == "same":
                return self._resolve_countdown_sfx_path(is_last=False)
            if mode == "file" and self._countdown_audio_last_file:
                p = Path(self._countdown_audio_last_file)
                if p.exists():
                    return str(p)
            return self._default_countdown_last_sound

        mode = self._normalize_countdown_audio_mode(self._countdown_audio_mode)
        if mode == "file" and self._countdown_audio_file:
            p = Path(self._countdown_audio_file)
            if p.exists():
                return str(p)
        return self._default_countdown_sound

    def _play_countdown_tick_sound(self, tick_text: str) -> None:
        if not self._countdown_audio_enabled:
            return
        is_last = str(tick_text).strip() == "1"
        path = self._resolve_countdown_sfx_path(is_last=is_last)
        if not path:
            return
        player = self._countdown_sfx_last_player if is_last else self._countdown_sfx_player
        out = self._countdown_sfx_last_out if is_last else self._countdown_sfx_out
        out.setVolume(float(max(0.0, min(1.0, self._countdown_audio_volume))))
        url = QUrl.fromLocalFile(str(Path(path).resolve()))
        if player.source() != url:
            player.setSource(url)
        else:
            player.setPosition(0)
        player.play()

    def _tick_countdown_audio(self) -> None:
        """Play countdown SFX in live preview when the number changes."""
        if not self._live_active or self._live_renderer is None:
            self._countdown_prev_text = None
            return
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return
        hud = getattr(self._live_renderer, "_countdown_hud", None)
        text = getattr(hud, "_last_text", None) if hud is not None else None
        if text != self._countdown_prev_text:
            if text is not None:
                self._play_countdown_tick_sound(str(text))
            self._countdown_prev_text = text

    def _sync_countdown_audio_config_from_renderer(self) -> None:
        rdr = self._live_renderer
        if rdr is None:
            return
        self._countdown_audio_enabled = bool(
            getattr(rdr, "_relax_countdown_audio_enabled", self._countdown_audio_enabled)
        )
        self._countdown_audio_mode = str(
            getattr(rdr, "_relax_countdown_audio_mode", self._countdown_audio_mode)
        )
        self._countdown_audio_file = str(
            getattr(rdr, "_relax_countdown_audio_file", self._countdown_audio_file) or ""
        )
        self._countdown_audio_volume = float(max(
            0.0, min(1.0, float(getattr(rdr, "_relax_countdown_audio_volume", self._countdown_audio_volume)))
        ))
        self._countdown_audio_last_mode = str(
            getattr(rdr, "_relax_countdown_audio_last_mode", self._countdown_audio_last_mode)
        )
        self._countdown_audio_last_file = str(
            getattr(rdr, "_relax_countdown_audio_last_file", self._countdown_audio_last_file) or ""
        )

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
        if self._is_project_timeline_mode() and self._project_segments:
            self._seek_project_time_sec(float(time_sec), resume_play=False)
            return
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
        auto_play: bool = True,
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
        self._sync_countdown_audio_config_from_renderer()
        self._countdown_prev_text = None
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
        self._pending_play = bool(auto_play)
        self.play_button.setEnabled(False)
        self._set_play_button_state(playing=bool(auto_play))
        self.player.setSource(audio_url)
        self._load_watchdog.start()

        # Switch the stage stack to the live drawing page IMMEDIATELY
        # so the user gets the first frame even before the audio probe
        # finishes (which on a fresh MP3 can take 200–800 ms).
        self.stage_stack.setCurrentIndex(self._live_page_index)
        self._loading_timer.stop()
        self._render_live_frame(start_local_sec)

        # Enable Floor/Wall drag button now that live preview is running.
        if hasattr(self, "floor_wall_button"):
            self.floor_wall_button.setEnabled(True)

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
        self._countdown_prev_text = None
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
        # Hide floor/wall overlay when live preview stops
        if hasattr(self, "floor_wall_overlay"):
            self._floor_wall_edit_active = False
            self._floor_wall_pos_timer.stop()
            self.floor_wall_overlay.hide()
        if hasattr(self, "floor_wall_button"):
            self.floor_wall_button.blockSignals(True)
            self.floor_wall_button.setChecked(False)
            self.floor_wall_button.blockSignals(False)
            self.floor_wall_button.setEnabled(False)
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
        floor_panel_color: Optional[str] = None,
        floor_panel_opacity: Optional[float] = None,
        floor_panel_blink: Optional[bool] = None,
        floor_panel_image: Optional[str] = None,
        floor_full_static_image: Optional[bool] = None,
        floor_layout: Optional[str] = None,
        floor_bg_color: Optional[str] = None,
        floor_bg_opacity: Optional[float] = None,
        background_type: Optional[str] = None,
        background_color: Optional[str] = None,
        background_image: Optional[str] = None,
        background_video: Optional[str] = None,
        chevron_color: Optional[str] = None,
        chevron_scroll: Optional[bool] = None,
        chevron_blink: Optional[bool] = None,
        chevron_width_frac: Optional[float] = None,
        chevron_count: Optional[int] = None,
        show_side_rails: Optional[bool] = None,
        rail_color: Optional[str] = None,
        rail_shape: Optional[str] = None,
        rail_height: Optional[float] = None,
        rail_offset_x: Optional[float] = None,
        rail_image: Optional[str] = None,
        rail_texture_non_loop: Optional[bool] = None,
        rail_pulse: Optional[str] = None,
        rail_pulse_intensity: Optional[float] = None,
        rail_chevron_depth: Optional[float] = None,
        rail_chevron_density: Optional[int] = None,
        rail_pillar_count: Optional[int] = None,
        rail_pillar_highlight_count: Optional[int] = None,
        rail_pillar_radius: Optional[float] = None,
        rail_chase_mode: Optional[str] = None,
        rail_chase_speed_frames: Optional[int] = None,
        rail_dot_count: Optional[int] = None,
        rail_dot_lines: Optional[int] = None,
        rail_dot_size_px: Optional[int] = None,
        rail_dot_anim_mode: Optional[str] = None,
        rail_dot_color_near: Optional[str] = None,
        rail_dot_color_far: Optional[str] = None,
        relax_interval: Optional[float] = None,
        relax_travel_sec: Optional[float] = None,
        relax_wait_sec: Optional[float] = None,
        relax_texture_low: Optional[str] = None,
        relax_texture_high: Optional[str] = None,
        relax_texture_middle: Optional[str] = None,
        relax_hole_mask_path: Optional[str] = None,
        relax_kind_ratio_middle: Optional[float] = None,
        relax_show_low: Optional[bool] = None,
        relax_show_high: Optional[bool] = None,
        relax_show_middle: Optional[bool] = None,
        relax_countdown_enabled: Optional[bool] = None,
        relax_countdown_color: Optional[str] = None,
        relax_countdown_max_sec: Optional[float] = None,
        relax_countdown_anim: Optional[str] = None,
        relax_countdown_audio_enabled: Optional[bool] = None,
        relax_countdown_audio_mode: Optional[str] = None,
        relax_countdown_audio_file: Optional[str] = None,
        relax_countdown_audio_volume: Optional[float] = None,
        relax_countdown_audio_last_mode: Optional[str] = None,
        relax_countdown_audio_last_file: Optional[str] = None,
        relax_countdown_x: Optional[float] = None,
        relax_countdown_y: Optional[float] = None,
        relax_countdown_w: Optional[float] = None,
        relax_countdown_h: Optional[float] = None,
        relax_countdown_border_thickness: Optional[float] = None,
        relax_countdown_glow_strength: Optional[float] = None,
        start_gate_enabled: Optional[bool] = None,
        start_gate_type: Optional[str] = None,
        start_gate_color: Optional[str] = None,
        start_gate_border_color: Optional[str] = None,
        start_gate_border_thickness: Optional[float] = None,
        start_gate_image: Optional[str] = None,
        start_gate_video: Optional[str] = None,
        start_gate_x: Optional[float] = None,
        start_gate_y: Optional[float] = None,
        start_gate_w: Optional[float] = None,
        start_gate_h: Optional[float] = None,
        max_per_lane: Optional[int] = None,
    ) -> None:
        """Hot-reload the renderer's gameplay mode + decor and redraw."""
        if not self._live_active or self._live_renderer is None:
            return
        if relax_countdown_audio_enabled is not None:
            self._countdown_audio_enabled = bool(relax_countdown_audio_enabled)
        if relax_countdown_audio_mode is not None:
            self._countdown_audio_mode = str(relax_countdown_audio_mode or "default")
        if relax_countdown_audio_file is not None:
            self._countdown_audio_file = str(relax_countdown_audio_file or "")
        if relax_countdown_audio_volume is not None:
            self._countdown_audio_volume = float(
                max(0.0, min(1.0, float(relax_countdown_audio_volume)))
            )
        if relax_countdown_audio_last_mode is not None:
            self._countdown_audio_last_mode = str(
                relax_countdown_audio_last_mode or "default"
            )
        if relax_countdown_audio_last_file is not None:
            self._countdown_audio_last_file = str(relax_countdown_audio_last_file or "")

        self._live_renderer.update_mode(
            mode,
            show_stickman=show_stickman,
            stickman_box=stickman_box,
            show_floor_panels=show_floor_panels,
            floor_panel_color=floor_panel_color,
            floor_panel_opacity=floor_panel_opacity,
            floor_panel_blink=floor_panel_blink,
            floor_panel_image=floor_panel_image,
            floor_full_static_image=floor_full_static_image,
            floor_layout=floor_layout,
            floor_bg_color=floor_bg_color,
            floor_bg_opacity=floor_bg_opacity,
            background_type=background_type,
            background_color=background_color,
            background_image=background_image,
            background_video=background_video,
            chevron_color=chevron_color,
            chevron_scroll=chevron_scroll,
            chevron_blink=chevron_blink,
            chevron_width_frac=chevron_width_frac,
            chevron_count=chevron_count,
            show_side_rails=show_side_rails,
            rail_color=rail_color,
            rail_shape=rail_shape,
            rail_height=rail_height,
            rail_offset_x=rail_offset_x,
            rail_image=rail_image,
            rail_texture_non_loop=rail_texture_non_loop,
            rail_pulse=rail_pulse,
            rail_pulse_intensity=rail_pulse_intensity,
            rail_chevron_depth=rail_chevron_depth,
            rail_chevron_density=rail_chevron_density,
            rail_pillar_count=rail_pillar_count,
            rail_pillar_highlight_count=rail_pillar_highlight_count,
            rail_pillar_radius=rail_pillar_radius,
            rail_chase_mode=rail_chase_mode,
            rail_chase_speed_frames=rail_chase_speed_frames,
            rail_dot_count=rail_dot_count,
            rail_dot_lines=rail_dot_lines,
            rail_dot_size_px=rail_dot_size_px,
            rail_dot_anim_mode=rail_dot_anim_mode,
            rail_dot_color_near=rail_dot_color_near,
            rail_dot_color_far=rail_dot_color_far,
            relax_interval=relax_interval,
            relax_travel_sec=relax_travel_sec,
            relax_wait_sec=relax_wait_sec,
            relax_texture_low=relax_texture_low,
            relax_texture_high=relax_texture_high,
            relax_texture_middle=relax_texture_middle,
            relax_hole_mask_path=relax_hole_mask_path,
            relax_kind_ratio_middle=relax_kind_ratio_middle,
            relax_show_low=relax_show_low,
            relax_show_high=relax_show_high,
            relax_show_middle=relax_show_middle,
            relax_countdown_enabled=relax_countdown_enabled,
            relax_countdown_color=relax_countdown_color,
            relax_countdown_max_sec=relax_countdown_max_sec,
            relax_countdown_anim=relax_countdown_anim,
            relax_countdown_audio_enabled=relax_countdown_audio_enabled,
            relax_countdown_audio_mode=relax_countdown_audio_mode,
            relax_countdown_audio_file=relax_countdown_audio_file,
            relax_countdown_audio_volume=relax_countdown_audio_volume,
            relax_countdown_audio_last_mode=relax_countdown_audio_last_mode,
            relax_countdown_audio_last_file=relax_countdown_audio_last_file,
            relax_countdown_x=relax_countdown_x,
            relax_countdown_y=relax_countdown_y,
            relax_countdown_w=relax_countdown_w,
            relax_countdown_h=relax_countdown_h,
            relax_countdown_border_thickness=relax_countdown_border_thickness,
            relax_countdown_glow_strength=relax_countdown_glow_strength,
            start_gate_enabled=start_gate_enabled,
            start_gate_type=start_gate_type,
            start_gate_color=start_gate_color,
            start_gate_border_color=start_gate_border_color,
            start_gate_border_thickness=start_gate_border_thickness,
            start_gate_image=start_gate_image,
            start_gate_video=start_gate_video,
            start_gate_x=start_gate_x,
            start_gate_y=start_gate_y,
            start_gate_w=start_gate_w,
            start_gate_h=start_gate_h,
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
        self._tick_countdown_audio()
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
        # Keep hit-block overlay in sync with tile scroll animation.
        if self._floor_wall_edit_active and hasattr(self, "floor_wall_overlay"):
            fi = int(round(float(t_sec) * float(getattr(rdr, "fps", 30))))
            self.floor_wall_overlay.set_frame_idx(fi)

