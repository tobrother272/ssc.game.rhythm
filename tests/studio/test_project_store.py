"""Project persistence round-trip tests."""

from pathlib import Path

from studio.models import MediaItem, MediaKind, Project, Segment
from studio.persistence import ProjectStore


def test_save_load_round_trip(tmp_path: Path) -> None:
    project = Project(
        name="Demo",
        project_dir=str(tmp_path),
        media_items=[
            MediaItem(kind=MediaKind.AUDIO, source_path=str(tmp_path / "a.mp3"), display_name="a"),
            MediaItem(kind=MediaKind.VIDEO, source_path=str(tmp_path / "v.mp4"), display_name="v"),
            MediaItem(kind=MediaKind.IMAGE, source_path=str(tmp_path / "i.png"), display_name="i"),
        ],
        segments=[
            Segment(name="S1", start_time_sec=0.0, end_time_sec=5.0, audio_path=str(tmp_path / "a.mp3")),
            Segment(name="S2", start_time_sec=7.0, end_time_sec=12.0, audio_path=str(tmp_path / "a.mp3")),
        ],
    )
    path = tmp_path / "Demo.htproj"
    store = ProjectStore()
    store.save(project, path)

    loaded = store.load(path)
    assert loaded.name == project.name
    assert len(loaded.media_items) == 3
    assert len(loaded.segments) == 2
    assert loaded.segments[0].name == "S1"
    assert loaded.output_fps == 30

