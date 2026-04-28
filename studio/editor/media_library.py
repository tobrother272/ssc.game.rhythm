"""Media library panel with drag/drop import."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

import cv2
from pydub import AudioSegment
from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QDrag, QIcon, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from studio.core_bridge.thumbnail_service import ThumbnailService
from studio.models import MediaItem, MediaKind, Project
from studio.widgets.drop_area import urls_to_paths

MEDIA_ID_MIME = "application/x-htstudio-media-id"


class _MediaListView(QListView):
    """Icon list view supporting drag out to timeline."""

    def startDrag(self, supported_actions: Qt.DropActions) -> None:
        index = self.currentIndex()
        media_id = index.data(Qt.ItemDataRole.UserRole)
        if not media_id:
            return
        mime = QMimeData()
        mime.setData(MEDIA_ID_MIME, str(media_id).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(supported_actions)


class MediaLibraryPanel(QWidget):
    """Panel for importing and managing media files."""

    media_selected = Signal(object)  # MediaItem | None
    media_dropped = Signal(str)  # media_id
    project_changed = Signal()

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    _VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    _AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}

    def __init__(self, thumbnail_service: ThumbnailService, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._thumbs = thumbnail_service
        self._thumbs.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._thumbs.thumbnail_failed.connect(self._on_thumbnail_failed)
        self._build_ui()

    def set_project(self, project: Project) -> None:
        """Attach project reference and refresh list model."""
        self._project = project
        if project.project_dir:
            project_dir = Path(project.project_dir) / f"{project.name}_assets"
            self._thumbs.set_project_dir(project_dir)
        self.refresh()

    def refresh(self) -> None:
        """Refresh list based on current project state."""
        self.model.clear()
        if not self._project:
            return
        show_video = self.cb_video.isChecked()
        show_audio = self.cb_audio.isChecked()
        show_image = self.cb_image.isChecked()
        # When nothing (or everything) is ticked, show all items.
        show_all = (not show_video and not show_audio and not show_image) or \
                   (show_video and show_audio and show_image)
        for media in self._project.media_items:
            if not show_all:
                if media.kind.value == "video" and not show_video:
                    continue
                if media.kind.value == "audio" and not show_audio:
                    continue
                if media.kind.value == "image" and not show_image:
                    continue
            self.model.appendRow(self._build_item(media))

    def _build_ui(self) -> None:
        self.setAcceptDrops(True)
        self.setObjectName("PanelRoot")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QWidget()
        header.setObjectName("panelHeader")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(10, 6, 10, 6)
        header_row.setSpacing(8)
        title = QLabel("Media")
        title.setObjectName("panelTitle")
        header_row.addWidget(title)
        header_row.addStretch()
        self.import_button = QPushButton("Import")
        self.import_button.clicked.connect(self._on_import_clicked)
        header_row.addWidget(self.import_button)
        self.cb_video = QCheckBox("Video")
        self.cb_audio = QCheckBox("Audio")
        self.cb_image = QCheckBox("Image")
        for cb in (self.cb_video, self.cb_audio, self.cb_image):
            cb.stateChanged.connect(self.refresh)
            header_row.addWidget(cb)
        root.addWidget(header)

        # Body
        body = QWidget()
        body.setObjectName("PanelRoot")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(8, 8, 8, 8)
        body_layout.setSpacing(6)
        root.addWidget(body, 1)

        self.view = _MediaListView()
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setIconSize(QSize(96, 96))
        self.view.setGridSize(QSize(120, 132))
        self.view.setSpacing(8)
        self.view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.view.setDragEnabled(True)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)
        self.view.clicked.connect(self._on_item_clicked)
        self.view.doubleClicked.connect(self._on_item_double_clicked)

        self.model = QStandardItemModel(self.view)
        self.view.setModel(self.model)
        body_layout.addWidget(self.view, 1)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if self._has_allowed_urls(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._has_allowed_urls(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        if not self._project:
            event.ignore()
            return
        paths = urls_to_paths(event.mimeData().urls())
        imported_any = False
        for path in paths:
            media = self._create_media_item(path)
            if media is None:
                continue
            self._project.media_items.append(media)
            self._thumbs.enqueue(media)
            imported_any = True
        if imported_any:
            self.project_changed.emit()
            self.refresh()
            event.acceptProposedAction()
            return
        event.ignore()

    def _has_allowed_urls(self, event) -> bool:
        mime = event.mimeData()
        if not mime.hasUrls():
            return False
        for path in urls_to_paths(mime.urls()):
            if self._detect_kind(path) is not None:
                return True
        return False

    def _on_import_clicked(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import media",
            "",
            "Media (*.png *.jpg *.jpeg *.webp *.bmp *.mp4 *.mov *.mkv *.avi *.webm *.mp3 *.wav *.flac *.m4a *.ogg)",
        )
        if not paths:
            return
        if not self._project:
            return
        changed = False
        for raw_path in paths:
            media = self._create_media_item(Path(raw_path))
            if media is None:
                continue
            self._project.media_items.append(media)
            self._thumbs.enqueue(media)
            changed = True
        if changed:
            self.project_changed.emit()
            self.refresh()

    def _on_item_clicked(self, index) -> None:
        if not self._project:
            return
        media_id = index.data(Qt.ItemDataRole.UserRole)
        media = self._project.get_media(str(media_id))
        self.media_selected.emit(media)

    def _on_item_double_clicked(self, index) -> None:
        media_id = index.data(Qt.ItemDataRole.UserRole)
        if media_id:
            self.media_dropped.emit(str(media_id))

    def _on_context_menu(self, point) -> None:
        index = self.view.indexAt(point)
        if not index.isValid() or not self._project:
            return
        media_id = str(index.data(Qt.ItemDataRole.UserRole))
        media = self._project.get_media(media_id)
        if media is None:
            return

        menu = QMenu(self)
        rename_action = menu.addAction("Rename")
        reveal_action = menu.addAction("Reveal in Explorer")
        remove_action = menu.addAction("Remove")
        selected = menu.exec(self.view.mapToGlobal(point))
        if selected == rename_action:
            media.display_name = f"{media.display_name}*"
            self.project_changed.emit()
            self.refresh()
        elif selected == reveal_action:
            import os

            os.startfile(str(Path(media.source_path).parent))
        elif selected == remove_action:
            self._project.media_items = [
                item for item in self._project.media_items if item.id != media_id
            ]
            self.project_changed.emit()
            self.refresh()

    def _on_thumbnail_ready(self, media_id: str, thumbnail_path: str) -> None:
        if not self._project:
            return
        media = self._project.get_media(media_id)
        if media is None:
            return
        media.thumbnail_path = thumbnail_path
        self.refresh()

    def _on_thumbnail_failed(self, _media_id: str, _error: str) -> None:
        self.refresh()

    def _create_media_item(self, path: Path) -> Optional[MediaItem]:
        kind = self._detect_kind(path)
        if kind is None:
            return None
        return MediaItem(
            id=str(uuid4()),
            kind=kind,
            source_path=str(path.resolve()),
            display_name=path.name,
            duration_sec=self._detect_duration(path, kind),
            imported_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )

    def _detect_kind(self, path: Path) -> Optional[MediaKind]:
        ext = path.suffix.lower()
        if ext in self._IMAGE_EXTS:
            return MediaKind.IMAGE
        if ext in self._VIDEO_EXTS:
            return MediaKind.VIDEO
        if ext in self._AUDIO_EXTS:
            return MediaKind.AUDIO
        return None

    def _detect_duration(self, path: Path, kind: MediaKind) -> Optional[float]:
        if kind == MediaKind.IMAGE:
            return None
        if kind == MediaKind.VIDEO:
            capture = cv2.VideoCapture(str(path))
            if not capture.isOpened():
                return None
            fps = capture.get(cv2.CAP_PROP_FPS) or 0
            frames = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            capture.release()
            if fps <= 0:
                return None
            return float(frames / fps)
        if kind == MediaKind.AUDIO:
            try:
                clip = AudioSegment.from_file(str(path))
                return len(clip) / 1000.0
            except Exception:
                return None
        return None

    def _build_item(self, media: MediaItem) -> QStandardItem:
        item = QStandardItem(media.display_name)
        item.setData(media.id, Qt.ItemDataRole.UserRole)
        if media.thumbnail_path and Path(media.thumbnail_path).exists():
            icon = QIcon(media.thumbnail_path)
        else:
            pix = QPixmap(96, 96)
            pix.fill(Qt.GlobalColor.darkGray)
            icon = QIcon(pix)
        item.setIcon(icon)
        item.setEditable(False)
        return item

