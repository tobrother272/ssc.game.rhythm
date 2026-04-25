"""JSON persistence for .htproj project files."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from studio.models import MediaItem, MediaKind, Project, RenderStatus, Segment


class ProjectStore:
    """Handles save/load operations for project metadata."""

    EXTENSION = ".htproj"

    def save(self, project: Project, path: Path) -> None:
        """Serialize and save project as JSON."""
        path = path.resolve()
        project_dir = path.parent
        payload = {
            "id": project.id,
            "name": project.name,
            "project_dir": str(project_dir),
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "main_audio_path": self._to_relative(project.main_audio_path, project_dir),
            "output_width": project.output_width,
            "output_height": project.output_height,
            "output_fps": project.output_fps,
            "media_items": [self._serialize_media(item, project_dir) for item in project.media_items],
            "segments": [self._serialize_segment(item, project_dir) for item in project.segments],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load(self, path: Path) -> Project:
        """Load project from JSON file."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        project_dir = path.parent.resolve()
        project = Project(
            id=payload.get("id", ""),
            name=payload.get("name", "Untitled Project"),
            project_dir=str(project_dir),
            created_at=payload.get("created_at", ""),
            updated_at=payload.get("updated_at", ""),
            main_audio_path=self._to_absolute(payload.get("main_audio_path"), project_dir),
            output_width=payload.get("output_width", 1920),
            output_height=payload.get("output_height", 1080),
            output_fps=payload.get("output_fps", 30),
        )
        project.media_items = [
            self._deserialize_media(item, project_dir)
            for item in payload.get("media_items", [])
        ]
        project.segments = [
            self._deserialize_segment(item, project_dir)
            for item in payload.get("segments", [])
        ]
        return project

    def _serialize_media(self, media: MediaItem, project_dir: Path) -> dict[str, Any]:
        data = asdict(media)
        data["kind"] = media.kind.value
        data["source_path"] = self._to_relative(media.source_path, project_dir)
        data["thumbnail_path"] = self._to_relative(media.thumbnail_path, project_dir)
        return data

    def _serialize_segment(self, segment: Segment, project_dir: Path) -> dict[str, Any]:
        # ``asdict`` already includes every Segment field, so any future
        # additions get persisted automatically.  We then override the
        # path-typed fields with project-relative variants for portability.
        data = asdict(segment)
        data["render_status"] = segment.render_status.value
        data["audio_path"] = self._to_relative(segment.audio_path, project_dir)
        data["video_path"] = self._to_relative(segment.video_path, project_dir)
        data["thumbnail_path"] = self._to_relative(segment.thumbnail_path, project_dir)
        return data

    def _deserialize_media(self, payload: dict[str, Any], project_dir: Path) -> MediaItem:
        return MediaItem(
            id=payload.get("id", ""),
            kind=MediaKind(payload.get("kind", MediaKind.VIDEO.value)),
            source_path=self._to_absolute(payload.get("source_path"), project_dir) or "",
            display_name=payload.get("display_name", ""),
            duration_sec=payload.get("duration_sec"),
            thumbnail_path=self._to_absolute(payload.get("thumbnail_path"), project_dir),
            imported_at=payload.get("imported_at", ""),
        )

    def _deserialize_segment(self, payload: dict[str, Any], project_dir: Path) -> Segment:
        # Accept legacy ``rendered_video_path`` key from older .htproj files
        # so projects saved before the rename keep their renders attached.
        legacy_video_path = payload.get("rendered_video_path")
        new_video_path = payload.get("video_path", legacy_video_path)
        return Segment(
            id=payload.get("id", ""),
            name=payload.get("name", "Segment"),
            start_time_sec=payload.get("start_time_sec", 0.0),
            end_time_sec=payload.get("end_time_sec", 0.0),
            audio_path=self._to_absolute(payload.get("audio_path"), project_dir) or "",
            audio_offset_sec=payload.get("audio_offset_sec", 0.0),
            audio_duration_sec=payload.get("audio_duration_sec", 0.0),
            mode=payload.get("mode", "punch"),
            render_settings=payload.get("render_settings", {}),
            video_path=self._to_absolute(new_video_path, project_dir),
            render_status=RenderStatus(payload.get("render_status", RenderStatus.IDLE.value)),
            last_rendered_at=payload.get("last_rendered_at"),
            last_render_error=payload.get("last_render_error"),
            thumbnail_path=self._to_absolute(payload.get("thumbnail_path"), project_dir),
        )

    @staticmethod
    def _to_relative(raw_path: str | None, project_dir: Path) -> str | None:
        if not raw_path:
            return raw_path
        path_obj = Path(raw_path)
        try:
            if path_obj.is_absolute():
                return str(path_obj.resolve().relative_to(project_dir.resolve()))
        except ValueError:
            return str(path_obj)
        return str(path_obj)

    @staticmethod
    def _to_absolute(raw_path: str | None, project_dir: Path) -> str | None:
        if not raw_path:
            return raw_path
        path_obj = Path(raw_path)
        if path_obj.is_absolute():
            return str(path_obj)
        return str((project_dir / path_obj).resolve())

