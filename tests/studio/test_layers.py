"""Tests for multi-track timeline layer system — Phase 1 + cleanup spec."""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from studio.models import (
    Layer,
    Project,
    auto_create_default_layers,
    migrate_render_settings_to_layers,
    resolve_segment_config,
)
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

def test_auto_create_creates_background_floor_stickman():
    proj, seg = _proj_with_seg(0.0, 30.0)
    auto_create_default_layers(proj, seg)
    kinds = {la.kind for la in proj.layers}
    assert "background" in kinds
    assert "floor" in kinds
    assert "stickman" in kinds
    assert len(proj.layers) == 3


def test_auto_create_covers_segment_range():
    proj, seg = _proj_with_seg(5.0, 25.0)
    auto_create_default_layers(proj, seg)
    for la in proj.layers:
        assert la.start_time_sec == 5.0
        assert la.end_time_sec == 25.0


def test_auto_create_stickman_default_config():
    proj, seg = _proj_with_seg(0.0, 30.0)
    auto_create_default_layers(proj, seg)
    stick = next(la for la in proj.layers if la.kind == "stickman")
    assert stick.config["stickman"] is True
    assert "stickman_location" in stick.config


def test_auto_create_still_creates_for_overlap_if_range_differs():
    """Each new segment gets its own defaults even with overlapping globals."""
    proj = Project(name="T")
    seg1 = _seg(0.0, 10.0)
    proj.segments.append(seg1)
    auto_create_default_layers(proj, seg1)
    assert len(proj.layers) == 3

    # Extend all layers to cover full project
    for la in proj.layers:
        la.end_time_sec = 30.0

    # Second segment overlaps the extended layers but still gets its own
    # exact-range defaults.
    seg2 = _seg(10.0, 20.0)
    proj.segments.append(seg2)
    auto_create_default_layers(proj, seg2)
    assert len(proj.layers) == 6


def test_auto_create_no_skip_if_adjacent_no_overlap():
    """Adjacent (non-overlapping) segment → new layers created."""
    proj = Project(name="T")
    seg1 = _seg(0.0, 10.0)
    proj.segments.append(seg1)
    auto_create_default_layers(proj, seg1)
    assert len(proj.layers) == 3

    seg2 = _seg(10.0, 20.0)
    proj.segments.append(seg2)
    auto_create_default_layers(proj, seg2)
    assert len(proj.layers) == 6  # 3 more layers for seg2


# ---------------------------------------------------------------------------
# resolve_segment_config
# ---------------------------------------------------------------------------

def test_resolve_no_layers_strips_visual_fields():
    # Visual fields in render_settings are stripped when there is no covering
    # layer — "layer absence = feature off".  Non-visual fields survive.
    seg = _seg(0.0, 30.0, rs={"bg_color": "blue", "floor_panels": False, "mode": "punch"})
    effective = resolve_segment_config(seg, [])
    assert "bg_color" not in effective       # visual → stripped
    assert "floor_panels" not in effective   # visual → stripped
    assert effective.get("mode") == "punch"  # non-visual → kept


def test_resolve_layer_overrides_render_settings():
    seg = _seg(0.0, 30.0, rs={"bg_color": "blue"})
    la = Layer(kind="background", start_time_sec=0.0, end_time_sec=30.0,
               config={"bg_color": "#FF0000"})
    effective = resolve_segment_config(seg, [la])
    assert effective["bg_color"] == "#FF0000"


def test_resolve_non_overlapping_layer_ignored():
    # Non-overlapping layer is ignored; visual field from render_settings is
    # also stripped → result has no bg_color at all.
    seg = _seg(20.0, 30.0, rs={"bg_color": "blue"})
    la = Layer(kind="background", start_time_sec=0.0, end_time_sec=15.0,
               config={"bg_color": "#FF0000"})
    effective = resolve_segment_config(seg, [la])
    assert "bg_color" not in effective  # neither source applies


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


def test_resolve_background_image_keeps_default_color_type_safe():
    """Image/video backgrounds may leave bg_color=None; this must not
    force background_color=None (string field in render settings)."""
    seg = _seg(0.0, 10.0, rs={})
    bg_layer = Layer(
        kind="background",
        start_time_sec=0.0,
        end_time_sec=10.0,
        config={
            "bg_type": "image",
            "bg_color": None,
            "bg_image": "C:/tmp/bg.jpg",
        },
    )
    effective = resolve_segment_config(seg, [bg_layer])
    assert effective["bg_type"] == "image"
    assert effective["background_type"] == "image"
    assert effective["bg_image"] == "C:/tmp/bg.jpg"
    assert effective["background_image"] == "C:/tmp/bg.jpg"
    # Must stay absent so build_settings() uses default "#000000".
    assert "background_color" not in effective


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

    # 3 auto (background + floor + stickman) + 1 custom = 4
    assert len(loaded.layers) == 4
    kinds = [la.kind for la in loaded.layers]
    assert kinds.count("background") == 2
    assert kinds.count("floor") == 1
    assert kinds.count("stickman") == 1

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
    for kind in ("background", "side_rails", "floor", "stickman", "countdown", "start_gate"):
        assert kind in LAYER_KIND_COLORS
        assert LAYER_KIND_COLORS[kind].startswith("#")


# ---------------------------------------------------------------------------
# Migration — migrate_render_settings_to_layers
# ---------------------------------------------------------------------------

def test_migration_extracts_rail_color():
    proj = Project(name="T")
    seg = _seg(0.0, 30.0, rs={"rail_color": "#FF0000", "side_rails": True})
    proj.segments.append(seg)
    migrate_render_settings_to_layers(proj)
    rail_layers = [la for la in proj.layers if la.kind == "side_rails"]
    assert len(rail_layers) == 1
    assert rail_layers[0].config["rail_color"] == "#FF0000"


def test_migration_extracts_stickman():
    proj = Project(name="T")
    seg = _seg(0.0, 30.0, rs={"stickman": True, "stickman_location": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.5}})
    proj.segments.append(seg)
    migrate_render_settings_to_layers(proj)
    stick_layers = [la for la in proj.layers if la.kind == "stickman"]
    assert len(stick_layers) == 1
    assert stick_layers[0].config["stickman"] is True


def test_migration_extracts_countdown():
    proj = Project(name="T")
    seg = _seg(0.0, 30.0, rs={"relax_countdown_enabled": True, "relax_countdown_color": "#FFFF00"})
    proj.segments.append(seg)
    migrate_render_settings_to_layers(proj)
    cd_layers = [la for la in proj.layers if la.kind == "countdown"]
    assert len(cd_layers) == 1
    assert cd_layers[0].config["relax_countdown_enabled"] is True


def test_migration_idempotent():
    """Running migration twice must not duplicate layers."""
    proj = Project(name="T")
    seg = _seg(0.0, 30.0, rs={"side_rails": True, "rail_color": "#0000FF"})
    proj.segments.append(seg)
    migrate_render_settings_to_layers(proj)
    count_after_first = len(proj.layers)
    migrate_render_settings_to_layers(proj)
    assert len(proj.layers) == count_after_first


def test_migration_skips_kind_if_layer_exists():
    """Migration skips a kind if a layer of that kind already overlaps."""
    proj = Project(name="T")
    seg = _seg(0.0, 30.0, rs={"stickman": True})
    proj.segments.append(seg)
    # Pre-create a stickman layer
    proj.layers.append(Layer(kind="stickman", start_time_sec=0.0, end_time_sec=30.0, config={}))
    migrate_render_settings_to_layers(proj)
    assert len([la for la in proj.layers if la.kind == "stickman"]) == 1


def test_migration_no_fields_no_layer():
    """Segment with no visual fields → no layers created."""
    proj = Project(name="T")
    seg = _seg(0.0, 30.0, rs={"beat_sens": 0.65, "density": 0.5})
    proj.segments.append(seg)
    migrate_render_settings_to_layers(proj)
    assert proj.layers == []


def test_migration_on_load_old_project():
    """ProjectStore.load auto-migrates old project files on load."""
    old_data = {
        "id": "x",
        "name": "old project",
        "segments": [
            {
                "id": "s1",
                "name": "Seg 1",
                "start_time_sec": 0.0,
                "end_time_sec": 30.0,
                "mode": "punch",
                "render_settings": {"stickman": True, "floor_panels": True},
                "audio_path": None,
            }
        ],
        "media_items": [],
    }
    store = ProjectStore()
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "old.htproj"
        path.write_text(json.dumps(old_data))
        loaded = store.load(path)
    kinds = {la.kind for la in loaded.layers}
    assert "stickman" in kinds
    assert "floor" in kinds


def test_auto_create_does_not_include_side_rails_countdown_start_gate():
    """Side rails, countdown, and start gate are NOT auto-created."""
    proj, seg = _proj_with_seg(0.0, 30.0)
    auto_create_default_layers(proj, seg)
    kinds = {la.kind for la in proj.layers}
    assert "side_rails" not in kinds
    assert "countdown" not in kinds
    assert "start_gate" not in kinds
