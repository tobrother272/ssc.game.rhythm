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
    assert s.rail_pillar_highlight_count == 1
    assert s.rail_pillar_radius == 1.0
    assert s.rail_chase_mode == "time"
    assert s.rail_chase_speed_frames == 4


def test_pillar_shape_round_trip() -> None:
    raw = {
        "side_rails": True,
        "rail_shape": "pillar",
        "rail_color": "#00FFFF",
        "rail_pillar_count": 24,
        "rail_pillar_highlight_count": 5,
        "rail_pillar_radius": 0.75,
        "rail_chase_mode": "beat",
        "rail_chase_speed_frames": 8,
    }
    s = build_settings("dance", raw)
    d = s.model_dump(mode="json")
    assert d["rail_shape"] == "pillar"
    assert d["rail_pillar_count"] == 24
    assert d["rail_pillar_highlight_count"] == 5
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


def test_tube_texture_non_loop_round_trip() -> None:
    s = build_settings(
        "punch",
        {
            "side_rails": True,
            "rail_shape": "tube",
            "rail_image": "C:/tmp/rail.png",
            "rail_texture_non_loop": True,
        },
    )
    d = s.model_dump(mode="json")
    assert d["rail_shape"] == "tube"
    assert d["rail_texture_non_loop"] is True


def test_relax_extensions_round_trip() -> None:
    raw = {
        "relax_interval": 0.25,
        "relax_travel_sec": 3.5,
        "relax_wait_sec": 1.2,
        "relax_texture_low": "C:/tmp/low.png",
        "relax_texture_high": "C:/tmp/high.png",
        "relax_texture_middle": "C:/tmp/mid.png",
        "relax_hole_mask_path": "C:/tmp/mask.png",
        "relax_kind_ratio_middle": 0.6,
        "relax_countdown_enabled": False,
        "relax_countdown_color": "#FF0000",
        "relax_countdown_max_sec": 4.2,
        "relax_countdown_anim": "shake",
        "relax_countdown_audio_enabled": True,
        "relax_countdown_audio_mode": "file",
        "relax_countdown_audio_file": "C:/tmp/count.wav",
        "relax_countdown_audio_volume": 0.55,
        "relax_countdown_audio_last_mode": "file",
        "relax_countdown_audio_last_file": "C:/tmp/count_last.wav",
        "relax_countdown_x": 0.72,
        "relax_countdown_y": 0.08,
        "relax_countdown_w": 0.14,
        "relax_countdown_h": 0.20,
    }
    s = build_settings("relax", raw)
    d = s.model_dump(mode="json")
    assert d["relax_travel_sec"] == 3.5
    assert d["relax_wait_sec"] == 1.2
    assert d["relax_texture_middle"] == "C:/tmp/mid.png"
    assert d["relax_kind_ratio_middle"] == 0.6
    assert d["relax_countdown_enabled"] is False
    assert d["relax_countdown_color"] == "#FF0000"
    assert d["relax_countdown_max_sec"] == 4.2
    assert d["relax_countdown_anim"] == "shake"
    assert d["relax_countdown_audio_enabled"] is True
    assert d["relax_countdown_audio_mode"] == "file"
    assert d["relax_countdown_audio_file"] == "C:/tmp/count.wav"
    assert d["relax_countdown_audio_volume"] == 0.55
    assert d["relax_countdown_audio_last_mode"] == "file"
    assert d["relax_countdown_audio_last_file"] == "C:/tmp/count_last.wav"
    assert d["relax_countdown_x"] == 0.72
    assert d["relax_countdown_h"] == 0.20


def test_relax_defaults_backward_compatible() -> None:
    s = build_settings("relax", {"relax_interval": 0.1})
    d = s.model_dump(mode="json")
    assert d["relax_interval"] == 0.1
    assert d["relax_travel_sec"] == 3.0
    assert d["relax_wait_sec"] == 0.0
    assert d["relax_kind_ratio_middle"] == 0.33
    assert d["relax_show_low"] is True
    assert d["relax_show_high"] is True
    assert d["relax_show_middle"] is True
    assert d["relax_countdown_enabled"] is True
    assert d["relax_countdown_anim"] == "pop"
    assert d["relax_countdown_audio_enabled"] is False
    assert d["relax_countdown_audio_mode"] == "default"
    assert d["relax_countdown_audio_volume"] == 0.65
    assert d["relax_countdown_audio_last_mode"] == "default"
    assert d["relax_countdown_x"] == 0.88
    assert d["relax_countdown_h"] == 0.16


def test_background_fields_round_trip() -> None:
    s = build_settings(
        "punch",
        {
            "background_type": "video",
            "background_color": "#112233",
            "background_image": "C:/tmp/bg.png",
            "background_video": "C:/tmp/bg.mp4",
        },
    )
    d = s.model_dump(mode="json")
    assert d["background_type"] == "video"
    assert d["background_color"] == "#112233"
    assert d["background_image"] == "C:/tmp/bg.png"
    assert d["background_video"] == "C:/tmp/bg.mp4"

