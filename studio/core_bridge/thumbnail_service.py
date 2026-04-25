"""Thumbnail generation service for media and segments."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import cv2
from PIL import Image, ImageDraw
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from studio.models import MediaItem, MediaKind


class _ThumbnailWorker(QRunnable):
    """Execute thumbnail generation in background thread pool."""

    def __init__(self, service: "ThumbnailService", media: MediaItem) -> None:
        super().__init__()
        self._service = service
        self._media = media

    def run(self) -> None:
        try:
            output = self._service.generate_for_media(self._media)
            self._service.thumbnail_ready.emit(self._media.id, str(output))
        except Exception as exc:  # noqa: BLE001
            self._service.thumbnail_failed.emit(self._media.id, str(exc))


class ThumbnailService(QObject):
    """Generates thumbnails and emits completion notifications."""

    thumbnail_ready = Signal(str, str)  # media_id, thumbnail_path
    thumbnail_failed = Signal(str, str)  # media_id, error

    def __init__(self, project_dir: Path | None = None) -> None:
        super().__init__()
        self._project_dir = project_dir
        self._pool = QThreadPool.globalInstance()

    def set_project_dir(self, project_dir: Path) -> None:
        """Set output directory for generated thumbnails."""
        self._project_dir = project_dir

    def enqueue(self, media: MediaItem) -> None:
        """Queue thumbnail generation for a media item."""
        self._pool.start(_ThumbnailWorker(self, media))

    def generate_for_media(self, media: MediaItem) -> Path:
        """Generate thumbnail synchronously and return output path."""
        output_path = self._thumbnail_path(media.id)
        if media.kind == MediaKind.IMAGE:
            self._image_thumbnail(Path(media.source_path), output_path)
        elif media.kind == MediaKind.VIDEO:
            self._video_thumbnail(Path(media.source_path), output_path)
        else:
            self._audio_placeholder(output_path)
        return output_path

    def _thumbnail_path(self, media_id: str) -> Path:
        if self._project_dir is None:
            cache_dir = Path(".tmp/studio_thumbnails")
        else:
            cache_dir = self._project_dir / "thumbnails"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{media_id}.png"

    @staticmethod
    def _image_thumbnail(source: Path, output: Path) -> None:
        with Image.open(source) as image:
            image.thumbnail((256, 256))
            canvas = Image.new("RGB", (256, 256), "#0b0b14")
            offset = ((256 - image.width) // 2, (256 - image.height) // 2)
            canvas.paste(image, offset)
            canvas.save(output, format="PNG")

    @staticmethod
    def _video_thumbnail(source: Path, output: Path) -> None:
        capture = cv2.VideoCapture(str(source))
        if not capture.isOpened():
            raise RuntimeError("Cannot open video for thumbnail")
        capture.set(cv2.CAP_PROP_POS_MSEC, 500)
        ok, frame = capture.read()
        capture.release()
        if not ok or frame is None:
            raise RuntimeError("Cannot read frame from video")
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(frame)
        image.thumbnail((256, 256))
        canvas = Image.new("RGB", (256, 256), "#0b0b14")
        offset = ((256 - image.width) // 2, (256 - image.height) // 2)
        canvas.paste(image, offset)
        canvas.save(output, format="PNG")

    @staticmethod
    def _audio_placeholder(output: Path) -> None:
        canvas = Image.new("RGB", (256, 256), "#111827")
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, 180, 256, 256), fill="#1e293b")
        for i in range(24):
            x = 12 + i * 10
            height = 30 + ((i * 13) % 70)
            draw.rectangle((x, 170 - height, x + 6, 170), fill="#22d3ee")
        draw.text((95, 196), "AUDIO", fill="#f8fafc")
        canvas.save(output, format="PNG")

