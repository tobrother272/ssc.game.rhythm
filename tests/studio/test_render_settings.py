"""Render settings parsing tests."""

from studio.models import build_settings


def test_round_trip_render_settings() -> None:
    raw = {
        "mode_list": ["line"],
        "speed": 1.25,
        "line_beats": 3,
        "line_zigzag": "horizontal",
        "stickman": True,
    }
    settings = build_settings("line", raw)
    dumped = settings.model_dump(mode="json", exclude_none=True)
    reparsed = build_settings("line", dumped).model_dump(mode="json", exclude_none=True)
    assert dumped == reparsed


def test_invalid_enum_raises_validation_error() -> None:
    try:
        build_settings("line", {"beat_source": "bad-source"})
    except Exception:
        assert True
        return
    assert False, "Expected enum validation to fail"


def test_pillar_shape_defaults() -> None:
    s = build_settings("punch", {})
    assert s.rail_pillar_count == 16
    assert s.rail_pillar_radius == 1.0
    assert s.rail_chase_mode == "time"
    assert s.rail_chase_speed_frames == 4


def test_pillar_shape_round_trip() -> None:
    raw = {
        "side_rails": True,
        "rail_shape": "pillar",
        "rail_color": "#00FFFF",
        "rail_pillar_count": 24,
        "rail_pillar_radius": 0.75,
        "rail_chase_mode": "beat",
        "rail_chase_speed_frames": 8,
    }
    s = build_settings("dance", raw)
    d = s.model_dump(mode="json")
    assert d["rail_shape"] == "pillar"
    assert d["rail_pillar_count"] == 24
    assert d["rail_pillar_radius"] == 0.75
    assert d["rail_chase_mode"] == "beat"
    assert d["rail_chase_speed_frames"] == 8


def test_pillar_count_clamped() -> None:
    from pydantic import ValidationError

    try:
        build_settings("punch", {"rail_pillar_count": 100})
        assert False, "should reject pillar_count=100"
    except ValidationError:
        pass


def test_dot_shape_defaults() -> None:
    s = build_settings("punch", {})
    assert s.rail_dot_count == 24
    assert s.rail_dot_lines == 1
    assert s.rail_dot_size_px == 6
    assert s.rail_dot_anim_mode == "audio"
    assert s.rail_dot_color_near == "#FF60FF"
    assert s.rail_dot_color_far == "#00FFFF"


def test_dot_shape_round_trip() -> None:
    raw = {
        "side_rails": True,
        "rail_shape": "dot",
        "rail_dot_count": 32,
        "rail_dot_lines": 3,
        "rail_dot_size_px": 8,
        "rail_dot_anim_mode": "twinkle",
        "rail_dot_color_near": "#FFFFFF",
        "rail_dot_color_far": "#0080FF",
    }
    s = build_settings("dance", raw)
    d = s.model_dump(mode="json")
    assert d["rail_shape"] == "dot"
    assert d["rail_dot_count"] == 32
    assert d["rail_dot_lines"] == 3
    assert d["rail_dot_anim_mode"] == "twinkle"
    assert d["rail_dot_color_near"] == "#FFFFFF"
    assert d["rail_dot_color_far"] == "#0080FF"


def test_dot_anim_mode_invalid() -> None:
    from pydantic import ValidationError

    try:
        build_settings("punch", {"rail_dot_anim_mode": "sparkle"})
        assert False, "should reject invalid anim mode"
    except ValidationError:
        pass

