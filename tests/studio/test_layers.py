"""Tests for multi-track timeline layer system — Phase 1 acceptance criteria."""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from studio.models import Layer, Project, auto_create_default_layers, resolve_segment_config
from studio.models.layer import _default_floor_config, LAYER_KIND_COLORS
from studio.models.segment import Segment
from studio.persistence.project_store import ProjectStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seg(start: float, end: float, rs: dict | None = None) -> Segment:
    return Segment(
        name="S",
        start_time_sec=start,
        end_time_sec=end,
        mode="punch",
        render_settings=rs or {},
    )


def _proj_with_seg(start=0.0, end=30.0):
    proj = Project(name="T")
    seg = _seg(start, end)
    proj.segments.append(seg)
    return proj, seg


# ---------------------------------------------------------------------------
# Layer dataclass
# ---------------------------------------------------------------------------

def test_layer_overlaps_basic():
    la = Layer(kind="background", start_time_sec=5.0, end_time_sec=20.0)
    assert la.overlaps(0.0, 10.0)   # partial left
    assert la.overlaps(10.0, 30.0)  # partial right
    assert la.overlaps(6.0, 8.0)    # contained inside
    assert la.overlaps(0.0, 30.0)   # layer inside query
    assert not la.overlaps(0.0, 5.0)  # exactly at start (non-inclusive)
    assert not la.overlaps(20.0, 30.0)  # exactly at end (non-inclusive)


def test_layer_duration():
    la = Layer(start_time_sec=3.0, end_time_sec=13.0)
    assert la.duration_sec == 10.0


# ---------------------------------------------------------------------------
# Default configs
# ---------------------------------------------------------------------------

def test_default_floor_config_fields():
    cfg = _default_floor_config()
    assert cfg["floor_panels"] is True
    assert cfg["floor_layout"] == "auto"
    assert cfg["chevron_color"] == "#FFD700"


def test_default_background_config():
    # Matches spec: bg_type=solid, bg_color=#000000
    from studio.models.layer import auto_create_default_layers
    proj, seg = _proj_with_seg()
    auto_create_default_layers(proj, seg)
    bg_layers = [la for la in proj.layers if la.kind == "background"]
    assert len(bg_layers) == 1
    assert bg_layers[0].config["bg_type"] == "solid"
    assert bg_layers[0].config["bg_color"] == "#000000"


# ---------------------------------------------------------------------------
# auto_create_default_layers
# ---------------------------------------------------------------------------

def test_auto_create_creates_background_and_floor():
    proj, seg = _proj_with_seg(0.0, 30.0)
    auto_create_default_layers(proj, seg)
    kinds = {la.kind for la in proj.layers}
    assert "background" in kinds
    assert "floor" in kinds


def test_auto_create_covers_segment_range():
    proj, seg = _proj_with_seg(5.0, 25.0)
    auto_create_default_layers(proj, seg)
    for la in proj.layers:
        assert la.start_time_sec == 5.0
        assert la.end_time_sec == 25.0


def test_auto_create_skips_if_overlap_exists():
    """Creating a second segment adjacent to the first should NOT stack."""
    proj = Project(name="T")
    seg1 = _seg(0.0, 10.0)
    proj.segments.append(seg1)
    auto_create_default_layers(proj, seg1)
    assert len(proj.layers) == 2

    # Extend background layer to cover full project
    for la in proj.layers:
        la.end_time_sec = 30.0

    # Second segment overlaps the extended layers → skip creation
    seg2 = _seg(10.0, 20.0)
    proj.segments.append(seg2)
    auto_create_default_layers(proj, seg2)
    assert len(proj.layers) == 2  # no new layers added


def test_auto_create_no_skip_if_adjacent_no_overlap():
    """Adjacent (non-overlapping) segment → new layers created."""
    proj = Project(name="T")
    seg1 = _seg(0.0, 10.0)
    proj.segments.append(seg1)
    auto_create_default_layers(proj, seg1)
    assert len(proj.layers) == 2

    seg2 = _seg(10.0, 20.0)
    proj.segments.append(seg2)
    auto_create_default_layers(proj, seg2)
    assert len(proj.layers) == 4  # 2 more layers for seg2


# ---------------------------------------------------------------------------
# resolve_segment_config
# ---------------------------------------------------------------------------

def test_resolve_no_layers_returns_render_settings():
    seg = _seg(0.0, 30.0, rs={"bg_color": "blue", "floor_panels": False})
    effective = resolve_segment_config(seg, [])
    assert effective["bg_color"] == "blue"
    assert effective["floor_panels"] is False


def test_resolve_layer_overrides_render_settings():
    seg = _seg(0.0, 30.0, rs={"bg_color": "blue"})
    la = Layer(kind="background", start_time_sec=0.0, end_time_sec=30.0,
               config={"bg_color": "#FF0000"})
    effective = resolve_segment_config(seg, [la])
    assert effective["bg_color"] == "#FF0000"


def test_resolve_non_overlapping_layer_ignored():
    seg = _seg(20.0, 30.0, rs={"bg_color": "blue"})
    la = Layer(kind="background", start_time_sec=0.0, end_time_sec=15.0,
               config={"bg_color": "#FF0000"})
    effective = resolve_segment_config(seg, [la])
    assert effective["bg_color"] == "blue"  # layer does not overlap


def test_resolve_highest_z_index_wins():
    seg = _seg(0.0, 30.0, rs={})
    la_low = Layer(kind="background", start_time_sec=0.0, end_time_sec=30.0,
                   z_index=0, config={"bg_color": "red"})
    la_high = Layer(kind="background", start_time_sec=0.0, end_time_sec=30.0,
                    z_index=5, config={"bg_color": "green"})
    effective = resolve_segment_config(seg, [la_low, la_high])
    assert effective["bg_color"] == "green"


def test_resolve_multiple_kinds():
    seg = _seg(0.0, 30.0, rs={"bg_color": "blue", "floor_panels": False})
    bg_layer = Layer(kind="background", start_time_sec=0.0, end_time_sec=30.0,
                     config={"bg_color": "#000000"})
    floor_layer = Layer(kind="floor", start_time_sec=0.0, end_time_sec=30.0,
                        config={"floor_panels": True, "chevron_color": "#FFD700"})
    effective = resolve_segment_config(seg, [bg_layer, floor_layer])
    assert effective["bg_color"] == "#000000"
    assert effective["floor_panels"] is True
    assert effective["chevron_color"] == "#FFD700"


# ---------------------------------------------------------------------------
# Project helpers
# ---------------------------------------------------------------------------

def test_project_layers_by_kind():
    proj = Project(name="T")
    proj.layers.append(Layer(kind="background", start_time_sec=0.0, end_time_sec=10.0))
    proj.layers.append(Layer(kind="floor", start_time_sec=0.0, end_time_sec=10.0))
    proj.layers.append(Layer(kind="background", start_time_sec=15.0, end_time_sec=25.0))
    assert len(proj.layers_by_kind("background")) == 2
    assert len(proj.layers_by_kind("floor")) == 1


def test_project_layers_overlapping():
    proj = Project(name="T")
    la1 = Layer(kind="background", start_time_sec=0.0, end_time_sec=10.0, z_index=1)
    la2 = Layer(kind="background", start_time_sec=5.0, end_time_sec=20.0, z_index=3)
    la3 = Layer(kind="background", start_time_sec=25.0, end_time_sec=35.0, z_index=0)
    proj.layers.extend([la1, la2, la3])
    hits = proj.layers_overlapping("background", 0.0, 12.0)
    assert len(hits) == 2
    assert hits[0].z_index == 3  # sorted DESC by z_index


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

def _make_proj_with_layers():
    proj = Project(name="Persist")
    seg = _seg(0.0, 30.0)
    proj.segments.append(seg)
    auto_create_default_layers(proj, seg)
    # Add a manual layer with custom config
    proj.layers.append(Layer(
        kind="background",
        start_time_sec=10.0,
        end_time_sec=20.0,
        z_index=1,
        name="Custom BG",
        config={"bg_type": "solid", "bg_color": "#AABBCC"},
    ))
    return proj


def test_persistence_round_trip():
    proj = _make_proj_with_layers()
    store = ProjectStore()
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "test.htproj"
        store.save(proj, path)
        loaded = store.load(path)

    assert len(loaded.layers) == 3
    kinds = [la.kind for la in loaded.layers]
    assert kinds.count("background") == 2
    assert kinds.count("floor") == 1

    # Custom layer config preserved
    custom = next(la for la in loaded.layers if la.name == "Custom BG")
    assert custom.config["bg_color"] == "#AABBCC"
    assert custom.z_index == 1


def test_persistence_version_2():
    proj = _make_proj_with_layers()
    store = ProjectStore()
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "test.htproj"
        store.save(proj, path)
        data = json.loads(path.read_text())
    assert data.get("version") == 2


def test_persistence_backward_compat_no_layers_key():
    """Old project files without a 'layers' key load with layers=[]."""
    old_data = {
        "id": "x",
        "name": "old project",
        "segments": [],
        "media_items": [],
    }
    store = ProjectStore()
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "old.htproj"
        path.write_text(json.dumps(old_data))
        loaded = store.load(path)
    assert loaded.layers == []


# ---------------------------------------------------------------------------
# LAYER_KIND_COLORS
# ---------------------------------------------------------------------------

def test_layer_kind_colors_all_defined():
    for kind in ("background", "side_rails", "floor", "stickman", "countdown"):
        assert kind in LAYER_KIND_COLORS
        assert LAYER_KIND_COLORS[kind].startswith("#")
