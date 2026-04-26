"""Segment and project utility tests."""

from studio.models import Project, Segment


def test_duration_sec_non_negative() -> None:
    segment = Segment(start_time_sec=8.0, end_time_sec=5.0)
    assert segment.duration_sec == 0.0


def test_sorted_segments_returns_by_start_time() -> None:
    project = Project()
    a = Segment(name="b", start_time_sec=5.0, end_time_sec=8.0)
    b = Segment(name="a", start_time_sec=1.0, end_time_sec=2.0)
    project.segments = [a, b]
    assert [item.name for item in project.sorted_segments()] == ["a", "b"]

