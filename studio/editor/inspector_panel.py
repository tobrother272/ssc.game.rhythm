"""InspectorPanel — polymorphic right-side panel (CapCut-style).

Content switches based on what is selected on the timeline:
  KIND_SEGMENT → segment beat/mode config (SegmentConfigPanel)
  KIND_LAYER   → layer section widget (Background / Floor / Rails / Stickman / Countdown)
  KIND_NONE    → placeholder hint

All existing SegmentConfigPanel public API (set_segment, set_project,
set_preview_active, set_preview_loading, signals) is forwarded so
MainWindow does not need large surgery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QColor, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from studio.models.layer import Layer
    from studio.models.project import Project
    from studio.models.segment import Segment


# ---------------------------------------------------------------------------
# Simple undo command for layer config edits
# ---------------------------------------------------------------------------

class _Cmd(QUndoCommand):
    def __init__(self, text: str, undo_fn, redo_fn) -> None:
        super().__init__(text)
        self._undo_fn = undo_fn
        self._redo_fn = redo_fn

    def undo(self) -> None:
        self._undo_fn()

    def redo(self) -> None:
        self._redo_fn()


# ---------------------------------------------------------------------------
# InspectorPanel
# ---------------------------------------------------------------------------

class InspectorPanel(QWidget):
    """Polymorphic Inspector panel that mirrors the current timeline selection.

    Wraps SegmentConfigPanel (segment mode) and inline layer section widgets
    (layer mode) in a QStackedWidget so swapping content is a single call.
    """

    KIND_NONE = "none"
    KIND_SEGMENT = "segment"
    KIND_LAYER = "layer"

    # ── Signals ─────────────────────────────────────────────────────────
    # Forwarded from SegmentConfigPanel (backward compat)
    segment_changed = Signal(str)      # segment_id
    render_requested = Signal(str)     # segment_id
    preview_requested = Signal(str)    # segment_id
    # New — emitted when a layer config is committed via the inline form
    layer_changed = Signal(str)        # layer_id

    # ── Page indices ─────────────────────────────────────────────────────
    _PAGE_EMPTY = 0
    _PAGE_SEGMENT = 1
    _PAGE_LAYER = 2

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._selection_kind = self.KIND_NONE
        self._selected_obj = None          # Segment | Layer | None

        # Undo stack (injected by MainWindow after construction)
        self._undo_stack: Optional[QUndoStack] = None

        # Edit-session undo state
        self._edit_session_layer: Optional[Layer] = None
        self._edit_session_start_config: Optional[dict] = None
        self._edit_session_dirty: bool = False

        # Debounce for live layer edits
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._commit_pending_edit)
        self._pending_layer: Optional[Layer] = None
        self._pending_section: Optional[QWidget] = None

        # Currently active layer section widget (inside the scroll area)
        self._active_layer_section: Optional[QWidget] = None

        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._stacked = QStackedWidget()
        layout.addWidget(self._stacked)

        # Page 0: empty / placeholder
        self._empty_widget = self._make_empty_page()
        self._stacked.addWidget(self._empty_widget)

        # Page 1: SegmentConfigPanel (import deferred to avoid circular)
        from studio.editor.segment_config_panel import SegmentConfigPanel
        self._segment_panel = SegmentConfigPanel()
        # Forward signals
        self._segment_panel.segment_changed.connect(self.segment_changed)
        self._segment_panel.render_requested.connect(self.render_requested)
        self._segment_panel.preview_requested.connect(self.preview_requested)
        self._stacked.addWidget(self._segment_panel)

        # Page 2: layer form (scroll area, content replaced per selection)
        self._layer_scroll = QScrollArea()
        self._layer_scroll.setWidgetResizable(True)
        self._layer_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._layer_page_container = QWidget()
        self._layer_page_layout = QVBoxLayout(self._layer_page_container)
        self._layer_page_layout.setContentsMargins(0, 0, 0, 0)
        self._layer_page_layout.setSpacing(0)
        # Header label (kind + range info)
        self._layer_header = QLabel()
        self._layer_header.setStyleSheet(
            "font-weight: bold; padding: 8px 10px 4px 10px;"
        )
        self._layer_page_layout.addWidget(self._layer_header)
        # Range sub-label
        self._layer_range_label = QLabel()
        self._layer_range_label.setStyleSheet(
            "color: #888; font-size: 10px; padding: 0 10px 6px 10px;"
        )
        self._layer_page_layout.addWidget(self._layer_range_label)
        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("QFrame { color: #333; margin: 0 8px; }")
        self._layer_page_layout.addWidget(sep)
        # Placeholder — replaced by actual section widget
        self._layer_section_placeholder = QLabel(
            "Select a layer to see its config."
        )
        self._layer_section_placeholder.setStyleSheet("color: #666; padding: 20px;")
        self._layer_page_layout.addWidget(self._layer_section_placeholder)
        self._layer_page_layout.addStretch(1)
        self._layer_scroll.setWidget(self._layer_page_container)
        self._stacked.addWidget(self._layer_scroll)

        # Shared Preview / Render bar pinned below stacked content.
        # Reuses the exact button instances from SegmentConfigPanel so
        # segment + layer selections always operate through one control set.
        self._action_bar = QWidget()
        _bar_layout = QHBoxLayout(self._action_bar)
        _bar_layout.setContentsMargins(8, 6, 8, 8)
        _bar_layout.setSpacing(6)
        _bar_layout.addWidget(self._segment_panel.preview_button)
        _bar_layout.addWidget(self._segment_panel.render_button)
        self._action_bar.hide()

        outer_layout = self.layout()
        outer_layout.addWidget(self._action_bar)

        self._stacked.setCurrentIndex(self._PAGE_EMPTY)

    def _make_empty_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 24, 16, 16)
        lbl = QLabel("Click a segment or layer on the\ntimeline to edit its properties.")
        lbl.setStyleSheet("color: #666; font-size: 11px;")
        v.addWidget(lbl)
        v.addStretch(1)
        return w

    # ── Public API ───────────────────────────────────────────────────────

    def set_project(self, project: "Project") -> None:
        self._project = project
        self._segment_panel.set_project(project)

    def set_undo_stack(self, stack: QUndoStack) -> None:
        self._undo_stack = stack

    def set_selection(self, kind: str, obj=None) -> None:
        """Switch panel content.  kind: KIND_NONE / KIND_SEGMENT / KIND_LAYER."""
        # Flush any pending layer edit session before switching
        self._flush_edit_session()

        # If the exact same layer is being re-selected (e.g. because the
        # timeline refreshed and re-emitted layer_selected), only update the
        # metadata labels — do NOT rebuild the section widget.  Rebuilding
        # would recreate the section from layer.config which may still hold
        # the old value if the debounce hasn't committed yet, causing the
        # color picker to visually revert to the old color.
        if kind == self.KIND_LAYER and obj is self._selected_obj and obj is not None:
            self._refresh_layer_metadata(obj)
            return

        self._selection_kind = kind
        self._selected_obj = obj

        if kind == self.KIND_SEGMENT:
            self._setup_segment_page(obj)
        elif kind == self.KIND_LAYER:
            self._setup_layer_page(obj)
        else:
            self._setup_empty_page()

    # Backward-compat shortcut used by MainWindow
    def set_segment(self, segment: Optional["Segment"]) -> None:
        if segment is None:
            self.set_selection(self.KIND_NONE, None)
        else:
            self.set_selection(self.KIND_SEGMENT, segment)

    # ── SegmentConfigPanel proxy methods/properties (backward compat) ────

    @property
    def current_segment(self):
        """Return the currently bound segment (mirrors SegmentConfigPanel API)."""
        return self._segment_panel._segment

    @property
    def render_button(self):
        """Direct access to SegmentConfigPanel's render button for enable/disable."""
        return self._segment_panel.render_button

    def refresh_status_only(self, segment) -> None:
        self._segment_panel.refresh_status_only(segment)

    def set_preview_active(self, active: bool) -> None:
        self._segment_panel.set_preview_active(active)

    def set_preview_loading(self, loading: bool) -> None:
        self._segment_panel.set_preview_loading(loading)

    # ── Page builders ────────────────────────────────────────────────────

    def _refresh_layer_metadata(self, layer: "Layer") -> None:
        """Update header / range labels for the currently shown layer without
        rebuilding the section widget.  Called when the same layer object is
        re-selected (e.g. after timeline refresh) to keep labels in sync while
        preserving any in-progress UI state (color picker value, etc.)."""
        kind_label = layer.kind.replace("_", " ").title()
        name = layer.name or kind_label
        self._layer_header.setText(f"Layer: {name}")
        start_s = layer.start_time_sec
        end_s = layer.end_time_sec
        self._layer_range_label.setText(
            f"Range: {start_s:.2f}s → {end_s:.2f}s  "
            f"({end_s - start_s:.2f}s)"
        )

    def _setup_empty_page(self) -> None:
        self._clear_layer_section()
        self._action_bar.hide()
        self._stacked.setCurrentIndex(self._PAGE_EMPTY)

    def _setup_segment_page(self, segment: "Segment") -> None:
        self._clear_layer_section()
        self._segment_panel.set_segment(segment)
        self._action_bar.show()
        self._stacked.setCurrentIndex(self._PAGE_SEGMENT)

    def _setup_layer_page(self, layer: "Layer") -> None:
        # Clear any previous section widget first
        self._clear_layer_section()

        if layer is None:
            self._action_bar.hide()
            self._stacked.setCurrentIndex(self._PAGE_LAYER)
            return

        # Update header + range labels
        kind_label = layer.kind.replace("_", " ").title()
        name = layer.name or kind_label
        self._layer_header.setText(f"Layer: {name}")
        start_s = layer.start_time_sec
        end_s = layer.end_time_sec
        self._layer_range_label.setText(
            f"Range: {start_s:.2f}s → {end_s:.2f}s  "
            f"({end_s - start_s:.2f}s)"
        )

        # Build kind-specific section widget
        section = self._make_section_for_layer(layer)
        if section is None:
            self._action_bar.hide()
            self._stacked.setCurrentIndex(self._PAGE_LAYER)
            return

        # Remove placeholder and add section
        self._layer_section_placeholder.hide()
        self._layer_page_layout.insertWidget(
            self._layer_page_layout.count() - 1,  # before stretch
            section,
        )
        self._active_layer_section = section

        # Wire live-edit debounce
        section.changed.connect(  # type: ignore[attr-defined]
            lambda: self._on_layer_section_changed(layer, section)
        )

        # Undo session: save starting config
        self._edit_session_layer = layer
        self._edit_session_start_config = dict(layer.config)
        self._edit_session_dirty = False

        # Bind shared Preview/Render buttons to the owning segment.
        self._bind_action_buttons_to_layer_owner(layer)

        self._stacked.setCurrentIndex(self._PAGE_LAYER)

    def _bind_action_buttons_to_layer_owner(self, layer: "Layer") -> None:
        """Point shared Preview/Render buttons at this layer's owning segment."""
        seg = self._find_owning_segment(layer)
        if seg is not None:
            # Keep SegmentConfigPanel as the owner of button behavior/signals;
            # we only rebind its current segment while its page is hidden.
            self._segment_panel.set_segment(seg)
            self._action_bar.show()
        else:
            self._segment_panel.set_segment(None)
            self._action_bar.hide()

    def _find_owning_segment(self, layer: "Layer") -> "Optional[Segment]":
        """Return the segment whose time range best contains this layer."""
        if self._project is None:
            return None
        candidates = [
            s for s in self._project.segments
            if s.start_time_sec <= layer.start_time_sec + 1e-6
            and s.end_time_sec >= layer.end_time_sec - 1e-6
        ]
        if not candidates:
            # Fallback: any segment that overlaps the layer
            candidates = [
                s for s in self._project.segments
                if s.start_time_sec < layer.end_time_sec
                and s.end_time_sec > layer.start_time_sec
            ]
        if not candidates:
            return None
        # Prefer the segment whose range most tightly wraps the layer
        return min(
            candidates,
            key=lambda s: (s.end_time_sec - s.start_time_sec),
        )

    def _make_section_for_layer(self, layer: "Layer") -> Optional[QWidget]:
        """Create the appropriate section widget for layer.kind."""
        kind = layer.kind
        cfg = dict(layer.config)

        try:
            if kind == "background":
                from studio.editor.layer_edit_dialog import _BackgroundSection
                return _BackgroundSection(cfg, self)
            elif kind == "floor":
                from studio.editor.segment_config_panel import _FloorPanelSection
                return _FloorPanelSection(
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
                from studio.editor.segment_config_panel import _SideRailSection
                return _SideRailSection(
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
                from studio.editor.layer_edit_dialog import _StickmanSection
                return _StickmanSection(cfg, self)
            elif kind == "countdown":
                from studio.editor.layer_edit_dialog import _CountdownSection
                return _CountdownSection(cfg, self)
        except Exception:
            pass
        return None

    def _clear_layer_section(self) -> None:
        """Remove the current layer section widget from the layout."""
        # Cancel any in-flight debounce
        self._debounce.stop()
        self._pending_layer = None
        self._pending_section = None

        if self._active_layer_section is not None:
            self._layer_page_layout.removeWidget(self._active_layer_section)
            self._active_layer_section.deleteLater()
            self._active_layer_section = None

        self._layer_section_placeholder.show()

    # ── Debounced live edit ───────────────────────────────────────────────

    def _on_layer_section_changed(
        self, layer: "Layer", section: QWidget
    ) -> None:
        """Section emitted `changed` — queue a debounced commit."""
        self._pending_layer = layer
        self._pending_section = section
        self._edit_session_dirty = True
        self._debounce.start()

    def _commit_pending_edit(self) -> None:
        """Apply pending config after debounce expires."""
        layer = self._pending_layer
        section = self._pending_section
        if layer is None or section is None:
            return
        try:
            new_cfg = section.get_config()  # type: ignore[attr-defined]
        except Exception:
            return
        layer.config = new_cfg
        self._pending_layer = None
        self._pending_section = None
        self.layer_changed.emit(layer.id)

    # ── Edit-session compound undo ────────────────────────────────────────

    def _flush_edit_session(self) -> None:
        """Flush any in-flight debounce then push a single compound undo."""
        # First commit any pending debounce immediately
        if self._debounce.isActive():
            self._debounce.stop()
            self._commit_pending_edit()

        if not self._edit_session_dirty:
            self._edit_session_layer = None
            self._edit_session_start_config = None
            return

        layer = self._edit_session_layer
        old_cfg = self._edit_session_start_config
        if layer is None or old_cfg is None:
            self._edit_session_dirty = False
            return

        new_cfg = dict(layer.config)
        if new_cfg == old_cfg:
            self._edit_session_dirty = False
            return

        if self._undo_stack is not None:
            sig = self.layer_changed

            def _undo(l=layer, c=old_cfg, s=sig) -> None:
                l.config = dict(c)
                s.emit(l.id)

            def _redo(l=layer, c=new_cfg, s=sig) -> None:
                l.config = dict(c)
                s.emit(l.id)

            self._undo_stack.push(
                _Cmd(f"Edit {layer.kind} layer", _undo, _redo)
            )

        self._edit_session_dirty = False
        self._edit_session_layer = None
        self._edit_session_start_config = None
