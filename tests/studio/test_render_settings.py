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

