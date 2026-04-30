"""Main studio window hosting media, preview, timeline, and inspector."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from PySide6.QtCore import QEvent, QSettings, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from studio.auth.auth_service import AuthService, AuthUser
from studio.auth.token_store import clear_token
from studio.core_bridge import (
    AudioTrimService,
    BeatDetectJob,
    BeatDetectService,
    RenderJob,
    RenderService,
    ThumbnailService,
    WaveformService,
)
from studio.editor.export_dialog import ExportDialog
from studio.editor.worker_update_dialog import WorkerUpdateDialog
from studio.editor.media_library import MediaLibraryPanel
from studio.editor.preview_panel import PreviewPanel
from studio.editor.segment_config_panel import SegmentConfigPanel
from studio.editor.timeline_panel import TimelinePanel
from studio.models import Project, RenderStatus, Segment, build_settings
from studio.persistence import ProjectStore

# Live preview is delivered by an in-process renderer (see
# ``src.live_renderer``), built on demand when the user toggles
# Preview ON.  Imported lazily inside the start-handler so the heavy
# rhythm.py + librosa + OpenCV chain doesn't load at app launch.


class _RendererWorker(QThread):
    """Build a LiveFrameRenderer off the UI thread.

    ``LiveFrameRenderer.__init__`` calls ``_analyse_audio`` which runs
    ``librosa.load``, ``librosa.stft``, and ``detect_wave_columns`` —
    ~1-3 s of CPU/IO work.  Running that on the UI thread freezes the
    window.  This worker does the heavy construction in a background
    thread and emits either ``ready`` (with the finished renderer) or
    ``failed`` (with an exception string) back to the UI thread.
    """

    ready = Signal(object)   # emits the constructed LiveFrameRenderer
    failed = Signal(str)     # emits the error message

    def __init__(self, audio_path: str, beat_times: list, mode: str, kwargs: dict, parent=None):
        super().__init__(parent)
        self._audio_path = audio_path
        self._beat_times = beat_times
        self._mode = mode
        self._kwargs = kwargs
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from src.live_renderer import LiveFrameRenderer
        try:
            renderer = LiveFrameRenderer(
                self._audio_path,
                beat_times=self._beat_times,
                mode=self._mode,
                **self._kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            if not self._cancelled:
                self.failed.emit(str(exc))
            return
        if not self._cancelled:
            self.ready.emit(renderer)
        else:
            renderer.close()



class MainWindow(QMainWindow):
    """Top-level editor shell for Human Tetris Studio."""

    signed_out = Signal()

    def __init__(
        self,
        user: AuthUser,
        auth_service: AuthService,
        parent: Optional[QMainWindow] = None,
    ) -> None:
        super().__init__(parent)
        self.user = user
        self.auth_service = auth_service
        self.was_signed_out = False
        self.project_store = ProjectStore()
        self.project_path: Optional[Path] = None
        self.project = self._new_project()

        self._settings = QSettings("human_tetris", "studio")
        self.thumbnail_service = ThumbnailService()
        # App root = repo root.  __file__ is studio/editor/main_window.py,
        # parents[2] is the project root (where studio/, src/, etc. live).
        # All rendered videos go into <app_root>/temps/, shared across
        # projects.  Persisted segment.video_path stores the absolute path.
        # In a frozen PyInstaller bundle sys.executable is SSCStudio.exe
        # and its parent is the dist/SSCStudio/ folder that also contains
        # rhythm_worker.exe.  In dev mode __file__ gives the repo root.
        if getattr(sys, "frozen", False):
            self._app_root: Path = Path(sys.executable).parent
        else:
            self._app_root = Path(__file__).resolve().parents[2]
        # Render subprocess needs the user's auth token (src.rhythm exits with
        # code 1 if --token is missing).  Pass providers (not raw strings) so
        # the latest token is fetched at job-run time, surviving re-login.
        self.render_service = RenderService(
            self._app_root,
            token_provider=self._current_auth_token,
            url_provider=self._current_auth_url,
        )
        self.waveform_service = WaveformService()
        self.waveform_service.connect_cache()
        self.audio_trim_service = AudioTrimService()
        self.audio_trim_service.ready.connect(self._on_trim_ready)
        self.audio_trim_service.failed.connect(self._on_trim_failed)

        # Beat-detection preview service — runs ``rhythm.py --detect_only``
        # so the timeline can show exactly where blocks will spawn before
        # the user commits to a full render.
        self.beat_detect_service = BeatDetectService(str(self._app_root))
        self.beat_detect_service.ready.connect(self._on_beats_ready)
        self.beat_detect_service.failed.connect(self._on_beats_failed)
        # Segment IDs whose "Auto Gen Block" was clicked while the audio
        # trim was still in flight.  Drained by ``_on_trim_ready`` (which
        # then fires the deferred detection) and ``_on_trim_failed``
        # (which clears the loading state with an error toast).  We
        # never fall back to the un-trimmed full audio because that
        # would feed rhythm.py the WRONG section of the song (only the
        # first ``duration`` seconds of the source file, not the
        # ``[start, end]`` window the segment covers) and produce ticks
        # that don't line up with the waveform the user sees.
        self._pending_auto_gen: set[str] = set()
        # Segment IDs whose audio trim is currently being produced by
        # ``AudioTrimService``.  Used by ``_request_beat_detect`` to
        # decide whether to piggy-back on an already-running trim
        # (just queue) or kick off a new ffmpeg job (queue + trim).
        # Spawning a parallel ffmpeg trim against the same output file
        # would cause Windows file-lock failures, so we keep them
        # serialised through this guard.
        self._inflight_trim_segments: set[str] = set()
        # Per-segment signature of the last trim request we issued.  Tuples of
        # ``(audio_path, round(start_sec, 3), round(end_sec, 3))`` — re-trim
        # only when this signature actually changes so render-settings edits
        # (toggling side rails, picking a colour, …) never overwrite a trim
        # file that the live-preview QMediaPlayer is currently reading.
        self._last_trim_signature: dict[str, tuple] = {}
        # Track which audio path is currently displayed to avoid redundant requests.
        self._current_waveform_path: Optional[str] = None
        # ── Export dialog (kept alive while open so user can monitor progress)
        self._export_dialog: Optional[ExportDialog] = None
        # ── Live preview mode (Preview button as a TOGGLE) ────────────
        # The preview button now drives an in-process drawing renderer
        # (:class:`src.live_renderer.LiveFrameRenderer`) instead of a
        # full ffmpeg HLS render.  When ON: the renderer produces
        # frames on demand for the segment-local audio playhead and
        # the panel's ``QLabel.setPixmap`` displays them at fps; edits
        # to mode / beats hot-reload the schedule WITHOUT re-spawning
        # any subprocess.  When OFF: renderer is closed and the panel
        # returns to idle.  Only one segment may be live-previewed at
        # a time.
        self._preview_mode_active: bool = False
        self._preview_active_segment_id: Optional[str] = None
        # The active in-process renderer (None outside preview mode).
        # Stored on the window so the edit-trigger debounce can find
        # it without round-tripping through the panel.
        self._live_preview_renderer: Optional[object] = None
        # Background thread that constructs the renderer off the UI
        # thread.  Set while loading, cleared once done or cancelled.
        self._preview_worker: Optional[_RendererWorker] = None
        # The segment that the in-flight worker is building for — used
        # to detect stale completions when the user clicks Stop before
        # the worker finishes.
        self._preview_worker_segment_id: Optional[str] = None
        # Debounce timer so a flurry of beat-tick drags ends in a
        # single hot-reload instead of rebuilding the schedule every
        # 16ms.  Hot-reload is the in-process renderer's update path
        # (~50–200 ms) so the debounce can be tight — 80 ms feels
        # instantaneous to a user dragging.
        self._preview_restart_timer = QTimer(self)
        self._preview_restart_timer.setSingleShot(True)
        self._preview_restart_timer.setInterval(80)
        self._preview_restart_timer.timeout.connect(
            self._perform_live_preview_update
        )
        # Bit-set of pending hot-reload kinds since the last update
        # actually fired — avoids redundant ``update_mode`` calls when
        # only beats changed, and vice versa.
        self._live_preview_pending_mode: bool = False
        self._live_preview_pending_beats: bool = False

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(60_000)
        self._autosave_timer.timeout.connect(self._auto_save)
        self._autosave_timer.start()

        self.setWindowTitle(f"Human Tetris Studio - {user.display_name}")
        self.resize(1500, 900)

        self._build_ui()
        self._wire_signals()
        self._set_project(self.project)
        self._restore_splitters()
        self._update_status()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_splitters()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        self._build_menu()
        self._build_toolbar()
        self._build_status_bar()

        self.outer_splitter = QSplitter()
        self.outer_splitter.setOrientation(Qt.Orientation.Vertical)
        self.top_splitter = QSplitter()
        self.top_splitter.setOrientation(Qt.Orientation.Horizontal)

        self.media_panel = MediaLibraryPanel(self.thumbnail_service)
        self.preview_panel = PreviewPanel()
        self.segment_panel = SegmentConfigPanel()
        self.timeline_panel = TimelinePanel()

        self.top_splitter.addWidget(self.media_panel)
        self.top_splitter.addWidget(self.preview_panel)
        self.top_splitter.addWidget(self.segment_panel)
        self.top_splitter.setSizes([300, 700, 360])

        self.outer_splitter.addWidget(self.top_splitter)
        self.outer_splitter.addWidget(self.timeline_panel)
        self.outer_splitter.setSizes([550, 280])

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.outer_splitter)
        self.setCentralWidget(container)
        self._init_timeline_render_overlay()

    def _init_timeline_render_overlay(self) -> None:
        """Create a blocking overlay shown on top of the timeline while rendering."""
        self._timeline_render_overlay = QFrame(self.timeline_panel)
        self._timeline_render_overlay.setObjectName("timelineRenderOverlay")
        self._timeline_render_overlay.setStyleSheet(
            "#timelineRenderOverlay {"
            " background-color: rgba(8, 8, 12, 190);"
            " border: 1px solid rgba(255, 255, 255, 45);"
            " border-radius: 6px;"
            "}"
        )
        box = QVBoxLayout(self._timeline_render_overlay)
        box.setContentsMargins(24, 16, 24, 16)
        box.setSpacing(8)
        box.addStretch(1)

        self._timeline_render_title = QLabel("Rendering…")
        self._timeline_render_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._timeline_render_title.setStyleSheet(
            "color: #F4F6FF; font-size: 16px; font-weight: 700;"
        )
        box.addWidget(self._timeline_render_title)

        self._timeline_render_message = QLabel("Please wait until current segment finishes.")
        self._timeline_render_message.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._timeline_render_message.setStyleSheet(
            "color: #D6DBF5; font-size: 12px;"
        )
        box.addWidget(self._timeline_render_message)

        self._timeline_render_progress = QProgressBar()
        self._timeline_render_progress.setRange(0, 100)
        self._timeline_render_progress.setValue(0)
        self._timeline_render_progress.setFormat("%p%")
        self._timeline_render_progress.setTextVisible(True)
        self._timeline_render_progress.setFixedHeight(18)
        box.addWidget(self._timeline_render_progress)
        box.addStretch(2)

        self._timeline_render_overlay.hide()
        self.timeline_panel.installEventFilter(self)
        self._resize_timeline_render_overlay()

    def _resize_timeline_render_overlay(self) -> None:
        if not hasattr(self, "_timeline_render_overlay"):
            return
        margin = 10
        rect = self.timeline_panel.rect().adjusted(margin, margin, -margin, -margin)
        self._timeline_render_overlay.setGeometry(rect)

    def _set_timeline_render_overlay(
        self,
        *,
        visible: bool,
        title: str = "Rendering…",
        message: str = "Please wait until current segment finishes.",
        progress: Optional[int] = None,
    ) -> None:
        if not hasattr(self, "_timeline_render_overlay"):
            return
        self._timeline_render_title.setText(title)
        self._timeline_render_message.setText(message)
        if progress is not None:
            self._timeline_render_progress.setValue(max(0, min(100, int(progress))))
        if visible:
            self._resize_timeline_render_overlay()
            self._timeline_render_overlay.raise_()
            self._timeline_render_overlay.show()
        else:
            self._timeline_render_overlay.hide()

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # type: ignore[override]
        if watched is self.timeline_panel and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.Show,
        }:
            self._resize_timeline_render_overlay()
        return super().eventFilter(watched, event)

    def _build_menu(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("File")
        file_menu.addAction("New", self._on_new_project, "Ctrl+N")
        file_menu.addAction("Open", self._on_open_project, "Ctrl+O")

        # Reopen Recent — populated lazily on aboutToShow so it always reflects
        # the current QSettings state (other windows may have updated it).
        self.recent_menu = QMenu("Reopen Recent Project", self)
        self.recent_menu.aboutToShow.connect(self._populate_recent_menu)
        file_menu.addMenu(self.recent_menu)

        file_menu.addAction("Save", self._on_save_project, "Ctrl+S")
        file_menu.addAction("Save As", self._on_save_as_project, "Ctrl+Shift+S")
        file_menu.addSeparator()
        file_menu.addAction("Sign out", self._on_sign_out)
        file_menu.addAction("Exit", self.close)

        render_menu = menu.addMenu("Render")
        self._act_render_selected = render_menu.addAction(
            "Render Selected Segment", self._render_selected_segment
        )
        self._act_render_all = render_menu.addAction(
            "Render All", self._render_all_segments
        )

        menu.addMenu("Edit")

        help_menu = menu.addMenu("Help")
        help_menu.addAction("Update Worker…", self._on_update_worker_clicked)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        self.addToolBar(toolbar)
        toolbar.addAction("New", self._on_new_project)
        toolbar.addAction("Open", self._on_open_project)
        toolbar.addAction("Save", self._on_save_project)
        toolbar.addAction("Save As", self._on_save_as_project)
        toolbar.addSeparator()
        self._tb_act_render_selected = toolbar.addAction(
            "Render Selected", self._render_selected_segment
        )
        self._tb_act_render_all = toolbar.addAction(
            "Render All", self._render_all_segments
        )

        # Right-side spacer + Export accent button
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().Policy.Expanding,
                             spacer.sizePolicy().Policy.Preferred)
        toolbar.addWidget(spacer)

        self.export_button = QPushButton("Export")
        self.export_button.setObjectName("accentButton")
        self.export_button.setToolTip("Configure and export all segments to a single video file")
        self.export_button.clicked.connect(self._on_export_button_clicked)
        toolbar.addWidget(self.export_button)

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.statusBar().showMessage("Ready")

    def _wire_signals(self) -> None:
        self.media_panel.media_selected.connect(self.preview_panel.set_source_media)
        self.media_panel.project_changed.connect(self._on_project_changed)
        self.timeline_panel.create_segment_requested.connect(self._on_create_segment_requested)
        self.timeline_panel.segment_selected.connect(self._on_segment_selected)
        self.timeline_panel.segment_changed.connect(self._on_segment_changed_by_timeline)
        self.timeline_panel.playhead_seek_requested.connect(
            self.preview_panel.seek_to_seconds
        )
        self.timeline_panel.segment_split.connect(self._on_segment_split)
        self.timeline_panel.segment_joined.connect(self._on_segment_joined)
        self.timeline_panel.segment_delete_requested.connect(
            self._on_segment_delete_requested
        )
        self.timeline_panel.auto_gen_block_requested.connect(
            self._on_auto_gen_block_requested
        )
        self.timeline_panel.beat_events_edited.connect(
            self._on_beat_events_edited
        )
        self.timeline_panel.beat_threshold_changed.connect(
            self._on_beat_threshold_changed
        )
        self.timeline_panel.segment_duplicated.connect(
            self._on_segment_duplicated
        )
        self.timeline_panel.segment_moved.connect(self._on_segment_moved)

        # Undo / redo — delegate to the timeline panel's undo stack.
        undo_sc = QShortcut(QKeySequence.StandardKey.Undo, self)
        undo_sc.activated.connect(self.timeline_panel.undo_stack.undo)
        redo_sc = QShortcut(QKeySequence.StandardKey.Redo, self)
        redo_sc.activated.connect(self.timeline_panel.undo_stack.redo)
        self.preview_panel.playhead_changed.connect(self.timeline_panel.set_playhead)
        self.preview_panel.stickman_location_changed.connect(
            self._on_stickman_location_edited
        )
        self.preview_panel.floor_wall_committed.connect(
            self._on_floor_wall_committed
        )
        # The panel may auto-stop live-preview when its source is
        # forcibly replaced (e.g. user clicked another segment) — keep
        # our toggle flags + segment-panel button in sync via this
        # signal so the state machine stays consistent.
        self.preview_panel.live_preview_stopped.connect(
            self._on_live_preview_panel_stopped
        )
        self.segment_panel.segment_changed.connect(self._on_segment_changed_by_form)
        self.segment_panel.render_requested.connect(self._on_render_segment_requested)
        self.segment_panel.preview_requested.connect(self._on_preview_segment_requested)

        self.render_service.progress.connect(self._on_render_progress)
        self.render_service.finished.connect(self._on_render_finished)
        self.render_service.failed.connect(self._on_render_failed)
        self.render_service.trimmed.connect(self._on_trim_ready)

        self.waveform_service.ready.connect(self._on_waveform_ready)
        self.waveform_service.failed.connect(self._on_waveform_failed)

    def _set_project(self, project: Project) -> None:
        # If the user opens / creates a new project while a preview is
        # running, the previous segment_id is meaningless in the new
        # project — kill the daemon, wipe its buffered .ts files, and
        # reset the toggle so the Preview button starts clean.
        if getattr(self, "_preview_mode_active", False):
            self._stop_preview_mode()
        self.project = project
        # Reset trim cache when swapping projects so signatures from the
        # previous project don't suppress legitimate trims in the new one.
        self._last_trim_signature.clear()
        self._inflight_trim_segments.clear()
        # Pre-populate cache for segments that already have a trim on disk
        # so a no-op form edit right after open doesn't re-trim.
        for seg in project.segments:
            if (
                seg.audio_path
                and seg.trimmed_audio_path
                and Path(seg.trimmed_audio_path).exists()
                and seg.duration_sec > 0
            ):
                start = self._audio_offset(seg)
                end = start + (
                    seg.audio_duration_sec
                    if seg.audio_duration_sec > 0
                    else seg.duration_sec
                )
                self._last_trim_signature[seg.id] = (
                    str(seg.audio_path),
                    round(float(start), 3),
                    round(float(end), 3),
                )
        self.media_panel.set_project(project)
        self.timeline_panel.set_project(project)
        self.timeline_panel.clear_beat_events()
        # Re-paint the timeline strip from each segment's persisted
        # ``beat_events`` so the user sees the same ticks they last
        # generated, immediately after opening the project — no need
        # to click Auto Gen Block again just to re-display them.  The
        # detection itself is NOT re-run; we trust the saved JSON.
        for seg in project.segments:
            if seg.beat_events:
                self.timeline_panel.set_beat_events(
                    seg.id, list(seg.beat_events)
                )
        self.segment_panel.set_project(project)
        self.segment_panel.set_segment(None)
        self._current_waveform_path = None
        # Restore waveform from the first audio MediaItem with cached data.
        # Prefer RMS (matches game render); fall back to nothing — old peak
        # data alone is no longer drawn so re-import will recompute RMS.
        restored = False
        for media in project.media_items:
            if media.kind.value != "audio":
                continue
            if not media.waveform_rms:
                continue
            self._current_waveform_path = media.source_path
            self.timeline_panel.set_waveform(
                media.source_path,
                media.waveform_rms,
                media.waveform_duration_sec,
                rms_per_sec=media.waveform_rms_per_sec,
            )
            restored = True
            break
        if not restored:
            self.timeline_panel.clear_waveform()
            # If audio exists but RMS was never computed (legacy project),
            # request extraction now so the waveform appears on next paint.
            for media in project.media_items:
                if media.kind.value == "audio" and not media.waveform_rms:
                    self.timeline_panel.set_waveform_loading()
                    self._current_waveform_path = media.source_path
                    self.waveform_service.request(media.source_path)
                    break
        self._update_status()

    def _new_project(self) -> Project:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return Project(
            id=str(uuid4()),
            name="Untitled Project",
            project_dir=str(Path.cwd()),
            created_at=now,
            updated_at=now,
        )

    def _on_new_project(self) -> None:
        self._set_project(self._new_project())
        self.project_path = None
        self.statusBar().showMessage("Created new project", 3000)

    def _on_open_project(self) -> None:
        raw_path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", "", "Human Tetris Project (*.htproj)"
        )
        if not raw_path:
            return
        self._open_project_path(Path(raw_path))

    def _open_project_path(self, path: Path) -> None:
        """Open the given .htproj path, with friendly error handling.

        Used by both the Open dialog and the Reopen Recent submenu.
        """
        if not path.exists():
            QMessageBox.warning(
                self,
                "Project not found",
                f"The project file no longer exists:\n{path}",
            )
            self._remove_recent_project(path)
            return
        try:
            project = self.project_store.load(path)
        except Exception as exc:  # noqa: BLE001 - surface load failures to user
            QMessageBox.critical(
                self,
                "Failed to open project",
                f"Could not open {path.name}:\n{exc}",
            )
            return
        self.project_path = path
        self._set_project(project)
        self._add_recent_project(path)
        self.statusBar().showMessage(f"Opened {path.name}", 3000)

    def _on_save_project(self) -> None:
        if self.project_path is None:
            self._on_save_as_project()
            return
        self._save_to_path(self.project_path)

    def _on_save_as_project(self) -> None:
        raw_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Project As",
            str(Path(self.project.project_dir) / f"{self.project.name}.htproj"),
            "Human Tetris Project (*.htproj)",
        )
        if not raw_path:
            return
        path = Path(raw_path)
        if path.suffix.lower() != ".htproj":
            path = path.with_suffix(".htproj")
        self.project_path = path
        self.project.project_dir = str(path.parent.resolve())
        self._save_to_path(path)

    def _save_to_path(self, path: Path) -> None:
        self.project.updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.project_store.save(self.project, path)
        self._add_recent_project(path)
        self.statusBar().showMessage(f"Saved {path.name}", 3000)
        self._update_status()

    def _auto_save(self) -> None:
        if self.project_path is None:
            return
        self._save_to_path(self.project_path)

    # ------------------------------------------------------------------
    # Recent projects (File -> Reopen Recent Project)
    # ------------------------------------------------------------------
    RECENT_PROJECTS_KEY = "recent_projects"
    RECENT_PROJECTS_MAX = 10

    def _recent_projects(self) -> list[str]:
        """Return absolute paths of recently opened/saved projects, newest first."""
        raw = self._settings.value(self.RECENT_PROJECTS_KEY, [])
        # QSettings on Windows stores single-element lists as a bare string,
        # so normalize to a list of strings.
        if isinstance(raw, str):
            return [raw] if raw else []
        if not isinstance(raw, list):
            return []
        return [str(p) for p in raw if p]

    def _save_recent_projects(self, items: list[str]) -> None:
        self._settings.setValue(self.RECENT_PROJECTS_KEY, items)

    def _add_recent_project(self, path: Path) -> None:
        """Move `path` to the front of the recent list, dedup, cap at MAX."""
        try:
            absolute = str(path.resolve())
        except OSError:
            absolute = str(path)
        items = [p for p in self._recent_projects() if p != absolute]
        items.insert(0, absolute)
        del items[self.RECENT_PROJECTS_MAX:]
        self._save_recent_projects(items)

    def _remove_recent_project(self, path: Path) -> None:
        """Drop a single entry from the recent list (e.g. file deleted)."""
        try:
            absolute = str(path.resolve())
        except OSError:
            absolute = str(path)
        items = [p for p in self._recent_projects() if p != absolute and p != str(path)]
        self._save_recent_projects(items)

    def _clear_recent_projects(self) -> None:
        self._save_recent_projects([])
        self.statusBar().showMessage("Cleared recent projects", 2000)

    def _populate_recent_menu(self) -> None:
        """Rebuild the Reopen Recent submenu from QSettings on every show."""
        self.recent_menu.clear()
        items = self._recent_projects()
        if not items:
            empty = self.recent_menu.addAction("(No recent projects)")
            empty.setEnabled(False)
            return
        for index, raw in enumerate(items, start=1):
            path = Path(raw)
            # Show "1. project_name  —  C:/dir/parent" so users can disambiguate.
            try:
                parent = path.parent
                label = f"{index}. {path.stem}  —  {parent}"
            except Exception:  # noqa: BLE001 - never let a bad entry block the menu
                label = f"{index}. {raw}"
            action = self.recent_menu.addAction(label)
            if not path.exists():
                action.setEnabled(False)
                action.setText(label + "  (missing)")
            # Bind the path explicitly so the closure captures this iteration.
            action.triggered.connect(
                lambda _checked=False, p=path: self._open_project_path(p)
            )
        self.recent_menu.addSeparator()
        clear_action = self.recent_menu.addAction("Clear recent projects")
        clear_action.triggered.connect(self._clear_recent_projects)

    def _current_auth_token(self) -> Optional[str]:
        """Return the latest auth token for the render subprocess.

        Tries the in-memory ``AuthUser.token`` first, then falls back to the
        keychain so a freshly-launched studio that auto-restored a session
        still has a token before the user navigates anywhere.
        """
        token = getattr(self.user, "token", None) if self.user else None
        if token:
            return token
        try:
            from studio.auth.token_store import load_token
            return load_token()
        except Exception:  # noqa: BLE001 - keyring may be unavailable
            return None

    def _current_auth_url(self) -> Optional[str]:
        """Return the backend host (without scheme) for src.rhythm --url."""
        try:
            from studio.auth.api_client import AuthApiClient
            base = AuthApiClient.DEFAULT_BASE_URL
            return base.replace("https://", "").replace("http://", "").rstrip("/")
        except Exception:  # noqa: BLE001
            return None

    def _app_temps_dir(self) -> Path:
        """Return ``<app_root>/temps`` and ensure the directory exists.

        All rendered MP4s (Render and Preview) are written here.  Living in
        the app root (rather than per-project) means the directory is stable
        across project moves/saves; segments persist absolute paths that
        survive because ``temps/`` is never relocated by user actions.
        """
        path = self._app_root / "temps"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _on_sign_out(self) -> None:
        answer = QMessageBox.question(
            self,
            "Sign out",
            f"Dang xuat khoi {self.user.username}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.auth_service.logout(self.user)
        clear_token()
        self.was_signed_out = True
        self.signed_out.emit()
        self.close()

    def _on_project_changed(self) -> None:
        self.project.updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.timeline_panel.refresh()
        self._update_status()

    def _request_waveform_for(self, audio_path: Optional[str]) -> None:
        """Show waveform for ``audio_path`` (cached MediaItem RMS when available).

        A missing path does **not** clear the strip. Deselecting a segment
        (or a harmless click that does not retarget a segment) should leave
        the last RMS draw intact so the fill/outline never vanish. Callers
        that truly need an empty track (e.g. segment with no audio, failed
        extraction) use :meth:`TimelinePanel.clear_waveform` explicitly.
        """
        if not audio_path:
            return
        if audio_path == self._current_waveform_path:
            return
        self._current_waveform_path = audio_path

        # Check if the MediaItem already has cached RMS (persist across restart).
        media = self._find_media_by_path(audio_path)
        if media and media.waveform_rms:
            self.timeline_panel.set_waveform(
                audio_path,
                media.waveform_rms,
                media.waveform_duration_sec,
                rms_per_sec=media.waveform_rms_per_sec,
            )
            return

        # No cache — extract in background.
        self.timeline_panel.set_waveform_loading()
        self.statusBar().showMessage("Extracting waveform...", 2000)
        self.waveform_service.request(audio_path)

    def _find_media_by_path(self, source_path: str):
        """Return MediaItem whose source_path matches, or None."""
        for m in self.project.media_items:
            if m.source_path == source_path:
                return m
        return None

    def _on_waveform_ready(
        self,
        audio_path: str,
        peaks: object,
        rms: object,
        duration_sec: float,
    ) -> None:
        if audio_path != self._current_waveform_path:
            return
        peaks_list = list(peaks) if peaks else []
        rms_list   = list(rms)   if rms   else []
        # Persist into the MediaItem so reopening the project skips extraction.
        media = self._find_media_by_path(audio_path)
        if media:
            media.waveform_peaks = peaks_list
            media.waveform_peaks_per_sec = self.waveform_service.PEAKS_PER_SEC
            media.waveform_rms = rms_list
            media.waveform_rms_per_sec = self.waveform_service.RMS_PER_SEC
            media.waveform_duration_sec = duration_sec
        self.timeline_panel.set_waveform(
            audio_path,
            rms_list,
            duration_sec,
            rms_per_sec=self.waveform_service.RMS_PER_SEC,
        )
        self.statusBar().showMessage(
            f"Waveform loaded ({duration_sec:.1f}s)", 3000
        )

    def _on_waveform_failed(self, audio_path: str, message: str) -> None:
        if audio_path != self._current_waveform_path:
            return
        self.timeline_panel.clear_waveform()
        self.statusBar().showMessage(f"Waveform failed: {message}", 5000)

    # ------------------------------------------------------------------
    # Audio trim — pre-cut segment audio to <project_dir>/temps/
    # ------------------------------------------------------------------
    def _project_temps_dir(self) -> Path:
        """Return <project_dir>/temps/, creating it if needed.

        Falls back to <app_root>/temps/ when the project has never been
        saved (no project_dir is set yet).
        """
        proj_dir = getattr(self.project, "project_dir", None)
        base = Path(proj_dir) if proj_dir else self._app_root
        path = base / "temps"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _audio_offset(segment: "Segment") -> float:
        """Return the segment's audio-file start offset.

        ``audio_offset_sec is None`` means the field was never explicitly set
        (legacy segment).  Fall back to ``start_time_sec`` which was the
        implicit audio offset in all pre-existing code.  An explicit 0.0 is
        valid and must not be treated as "unset".
        """
        if segment.audio_offset_sec is not None:
            return segment.audio_offset_sec
        return segment.start_time_sec

    def _request_audio_trim(self, segment: "Segment") -> None:
        """Queue a background FFmpeg trim of *segment*'s audio window.

        Skipped silently if the segment has no audio source or zero duration.
        Output file keeps the same extension as the source so the codec/
        container is never changed (MP3 → MP3, AAC → AAC, etc.).
        Saved to the same <app_root>/temps/ folder as rendered videos so
        everything is co-located.

        Marks the segment as having a trim in-flight so the beat-detect
        path can piggy-back on this job instead of spawning a parallel
        ffmpeg that would race on the same output file.
        """
        if not segment.audio_path or not Path(segment.audio_path).exists():
            return
        if segment.duration_sec <= 0:
            return
        src_ext = Path(segment.audio_path).suffix or ".mp3"
        out_path = self._app_temps_dir() / f"audio_{segment.id}{src_ext}"
        audio_start = self._audio_offset(segment)
        audio_end = audio_start + (
            segment.audio_duration_sec
            if segment.audio_duration_sec > 0
            else segment.duration_sec
        )
        # Skip when the audio window for this segment hasn't changed since
        # the last successful trim AND the trimmed file still exists on
        # disk.  Render-settings edits (side rails, colours, …) emit
        # ``segment_changed`` but do not affect the audio window — re-running
        # ffmpeg here would race with the QMediaPlayer that is reading the
        # same trim file during live preview and could leave a corrupted
        # file on disk (or silently skip on Windows because of the file
        # lock).  Cache invalidates automatically when start/end/source
        # changes, so a real audio-window edit still re-trims.
        sig = (
            str(segment.audio_path),
            round(float(audio_start), 3),
            round(float(audio_end), 3),
        )
        cached_sig = self._last_trim_signature.get(segment.id)
        trim_exists = bool(
            segment.trimmed_audio_path
            and Path(segment.trimmed_audio_path).exists()
        )
        if cached_sig == sig and trim_exists and segment.id not in self._inflight_trim_segments:
            return
        # Real audio-window change while live preview is reading the trim
        # file — drop preview first so QMediaPlayer releases the file
        # handle (Windows) before ffmpeg overwrites it.  The user can
        # re-toggle Preview once the new trim lands.
        if (
            self._preview_mode_active
            and self._preview_active_segment_id == segment.id
        ):
            self._stop_preview_mode()
            self.statusBar().showMessage(
                "Preview stopped — segment audio window changed; "
                "click Preview again once retrim finishes.",
                4000,
            )
        self._last_trim_signature[segment.id] = sig
        self._inflight_trim_segments.add(segment.id)
        self.audio_trim_service.trim(
            segment_id=segment.id,
            audio_path=segment.audio_path,
            start_sec=audio_start,
            end_sec=audio_end,
            out_path=out_path,
        )

    def _on_trim_ready(self, segment_id: str, trimmed_path: str) -> None:
        """Store the trimmed WAV path on the segment and update the UI."""
        self._inflight_trim_segments.discard(segment_id)
        segment = self.project.get_segment(segment_id)
        if segment is None:
            return
        segment.trimmed_audio_path = trimmed_path
        # Refresh the inspector so the read-only field updates.
        if self.segment_panel.current_segment and \
                self.segment_panel.current_segment.id == segment_id:
            self.segment_panel.set_segment(segment)
        # If this segment is currently loaded in the player (e.g. after split/
        # join/dup the trim runs in background while raw audio plays), reload
        # the player now so it uses the correctly-trimmed file with proper duration.
        selected = getattr(self.timeline_panel, "_selected_segment_id", None)
        if selected == segment_id and not self._preview_mode_active:
            self.preview_panel.set_source_segment(segment)
        self.statusBar().showMessage(
            f"Trimmed audio saved: {Path(trimmed_path).name}", 3000
        )
        # If the user clicked "Auto Gen Block" while this trim was still
        # running, dispatch the deferred detection now that the correct
        # audio chunk is on disk.  ``BeatDetectJob.cache_key`` keys on
        # the file's (size, mtime_ns) so a re-trim that overwrites the
        # same path will not return stale events.
        if segment_id in self._pending_auto_gen:
            self._pending_auto_gen.discard(segment_id)
            self._request_beat_detect(segment)

    def _on_trim_failed(self, segment_id: str, message: str) -> None:
        self._inflight_trim_segments.discard(segment_id)
        # Drop the cached signature so the next form change retries the
        # trim instead of silently skipping with stale params.
        self._last_trim_signature.pop(segment_id, None)
        self.statusBar().showMessage(f"Audio trim failed: {message}", 5000)
        print(f"[AudioTrim] segment={segment_id} error={message}", flush=True)
        # Drop any deferred Auto-Gen request — without a valid trim we
        # have nothing safe to feed rhythm.py.  Clear the loading state
        # so the timeline strip stops spinning forever.
        if segment_id in self._pending_auto_gen:
            self._pending_auto_gen.discard(segment_id)
            self.timeline_panel.clear_beat_events(segment_id)
            self.statusBar().showMessage(
                f"Auto Gen Block aborted: trim failed ({message})", 6000
            )

    # ── Beat-detection preview ──────────────────────────────────────────
    def _request_beat_detect(self, segment: Segment) -> None:
        """Schedule a ``rhythm.py --detect_only`` run for *segment*.

        Detection ONLY runs against the pre-trimmed audio (the WAV/MP3
        produced by :class:`AudioTrimService` covering exactly the
        segment's [start, end] window).  When the trim isn't on disk
        yet the request is queued in ``self._pending_auto_gen`` and
        dispatched from ``_on_trim_ready`` once ffmpeg finishes —
        we never fall back to the full audio file because that would
        analyse the song's leading ``duration`` seconds (not the
        segment's actual time window) and produce ticks that don't
        align with the waveform shown on the timeline strip.
        """
        if not segment.audio_path:
            return
        if segment.duration_sec <= 0:
            return

        trimmed = segment.trimmed_audio_path
        if not trimmed or not Path(trimmed).exists():
            # Queue this request and ensure a trim is in flight.  When
            # one is already running we piggy-back on it (re-issuing
            # would race on the same output file and trip Windows file
            # locking inside ffmpeg) — ``_on_trim_ready`` will drain
            # ``_pending_auto_gen`` once the existing job lands.
            self._pending_auto_gen.add(segment.id)
            self.timeline_panel.set_beat_events_loading(segment.id)
            self.statusBar().showMessage(
                f"Auto Gen Block: waiting for audio trim of "
                f"'{segment.name}'…",
                4000,
            )
            if segment.id not in self._inflight_trim_segments:
                self._request_audio_trim(segment)
            return

        rs = segment.render_settings or {}
        # ``rhythm.py --detect_only`` uses ``_parse_modes()`` which only
        # understands single-mode names ("punch","dance","line","relax") or
        # comma-combined specs ("punch,dance").  The UI's "combo" meta-mode
        # is not a valid CLI value and raises ValueError.  For beat detection
        # purposes combo = punch + dance cycling, so always use "punch,dance".
        raw_mode = str(segment.mode or "punch")
        detect_mode = "punch,dance" if raw_mode == "combo" else raw_mode
        job = BeatDetectJob(
            segment_id=segment.id,
            audio_path=trimmed,
            duration_sec=float(segment.duration_sec),
            mode=detect_mode,
            beat_source=str(rs.get("beat_source", "onset")),
            beat_sens=float(rs.get("beat_sens", 0.7)),
            beat_min_gap=int(rs.get("beat_min_gap", 4)),
            density=float(rs.get("density", 0.5)),
            speed=float(rs.get("speed", 0.8)),
            fps=int(self.project.output_fps or 30),
            beat_height_threshold=float(getattr(
                segment, "beat_height_threshold", 0.0
            ) or 0.0),
        )
        self.timeline_panel.set_beat_events_loading(segment.id)
        self.statusBar().showMessage(
            f"Detecting beats for {segment.name}…", 2000
        )
        self.beat_detect_service.detect(job)

    def _on_beats_ready(self, segment_id: str, events: object) -> None:
        evs = list(events) if events else []
        self.timeline_panel.set_beat_events(segment_id, evs)
        seg = self.project.get_segment(segment_id)
        seg_name = seg.name if seg else segment_id[:8]
        # Persist on the segment so the ticks survive a project re-open
        # — the next session shows them on the timeline strip without
        # the user needing to click Auto Gen Block again.  Tuples here
        # match the in-memory shape used by BeatDetectService and the
        # timeline draw helpers; the project store rewrites them as
        # JSON arrays on save and rehydrates back to tuples on load.
        if seg is not None:
            # Events from BeatDetectService are 3-tuples
            # ``(t, kind, height)``; legacy code paths that only emit
            # 2-tuples are supported by treating missing height as 1.0
            # so they survive the round-trip without being filtered
            # out by the threshold slider.
            normalised: list[tuple[float, str, float]] = []
            for ev in evs:
                if len(ev) >= 3:
                    normalised.append(
                        (float(ev[0]), str(ev[1]),
                         max(0.0, min(1.0, float(ev[2]))))
                    )
                elif len(ev) == 2:
                    normalised.append((float(ev[0]), str(ev[1]), 1.0))
            seg.beat_events = normalised
            self._on_project_changed()
        self.statusBar().showMessage(
            f"Beats ready for {seg_name}: {len(evs)} block(s)", 3000
        )

    def _on_beat_events_edited(self, segment_id: str) -> None:
        """Sync timeline-panel edits back to the segment + autosave.

        The panel owns the live ``_beat_events`` dict during a session;
        this handler copies the latest list for ``segment_id`` into
        :attr:`Segment.beat_events` and marks the project dirty so the
        autosave timer flushes the edit to disk. Without this, every
        drag / delete / kind-change would be lost on reload.

        We deliberately do **not** call :meth:`_on_project_changed`
        here, because the timeline panel already manages its own
        scene rebuild for beat edits (see
        :meth:`TimelinePanel._commit_beat_edit`) and the threshold
        line release path explicitly does NOT want a refresh (the
        line + waveform are still valid as drawn).  An extra
        ``refresh()`` from this handler would call ``scene.clear()``
        a second time, wiping the line/waveform/ticks and rebuilding
        them — visually identical to the elements being "hidden by my
        click" the user reported.  Persistence is still kept: we
        update ``project.updated_at`` and mark dirty via
        :meth:`_update_status` directly.
        """
        if not segment_id:
            return
        seg = self.project.get_segment(segment_id)
        if seg is None:
            return
        events = self.timeline_panel.get_beat_events(segment_id)
        normalised: list[tuple[float, str, float]] = []
        for ev in events:
            if len(ev) >= 3:
                normalised.append(
                    (float(ev[0]), str(ev[1]),
                     max(0.0, min(1.0, float(ev[2]))))
                )
            elif len(ev) == 2:
                normalised.append((float(ev[0]), str(ev[1]), 1.0))
        seg.beat_events = normalised
        self.project.updated_at = datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat()
        self._update_status()
        self.statusBar().showMessage(
            f"Beats edited for {seg.name}: {len(events)} block(s)",
            2000,
        )

        # ── Live preview hot-reload ─────────────────────────────────
        # When live-preview is ON for THIS segment, the renderer's
        # schedule needs to absorb the new tick array.  We use the
        # beats-only path because beat-tick drags don't change the
        # mode / scene / HUDs; rebuilding those would only add
        # latency (50-200 ms each) without changing anything the
        # user can see.
        self._request_preview_beats_only(segment_id)

    def _on_stickman_location_edited(
        self, segment_id: str, location: dict
    ) -> None:
        """Persist a player-overlay drag/resize back onto the segment.

        ``location`` is ``{"x", "y", "w", "h"}`` fractions (0..1) of
        the rendered frame, exactly the shape stored on
        :attr:`Segment.stickman_location` and consumed by
        :meth:`RenderService._run_job` when building the
        ``--stick_x0/y0/w/h`` CLI flags.  We round to 4 decimals so
        the JSON file stays readable / diff-friendly without losing
        sub-pixel precision at the project's max resolution.
        """
        if not segment_id:
            return
        seg = self.project.get_segment(segment_id)
        if seg is None:
            return
        try:
            seg.stickman_location = {
                "x": round(float(location.get("x", 0.0)), 4),
                "y": round(float(location.get("y", 0.0)), 4),
                "w": round(float(location.get("w", 0.0)), 4),
                "h": round(float(location.get("h", 0.0)), 4),
            }
        except (TypeError, ValueError):
            return
        self._on_project_changed()
        # Hot-reload live preview so the dragged box lands on the
        # rendered frame within the next ~80 ms debounce tick — same
        # treatment as a mode/density edit.  Without this the user has
        # to toggle Preview off + on for the new placement to show up,
        # defeating the point of the drag-edit overlay.
        self._request_preview_restart(seg.id)
        self.statusBar().showMessage(
            f"Stickman box updated for {seg.name}: "
            f"x={seg.stickman_location['x']*100:.1f}% "
            f"y={seg.stickman_location['y']*100:.1f}% "
            f"w={seg.stickman_location['w']*100:.1f}% "
            f"h={seg.stickman_location['h']*100:.1f}%",
            2500,
        )

    def _on_floor_wall_committed(
        self,
        hit_frac: float,
        horizon_frac: float,
        near_spread: float,
        far_spread: float,
        wall_floor_gap_frac: float = 0.0,
    ) -> None:
        """Persist floor/wall drag result into the active segment's render_settings."""
        seg_id = self._preview_active_segment_id
        if not seg_id:
            return
        seg = self.project.get_segment(seg_id)
        if seg is None:
            return
        seg.render_settings["floor_hit_frac"]       = round(hit_frac,            4)
        seg.render_settings["horizon_frac"]         = round(horizon_frac,        4)
        seg.render_settings["floor_spread_frac"]    = round(near_spread,         4)
        seg.render_settings["far_spread_frac"]      = round(far_spread,          4)
        seg.render_settings["wall_floor_gap_frac"]  = round(wall_floor_gap_frac, 4)
        self._on_project_changed()
        self.statusBar().showMessage(
            f"Camera adjusted — floor:{hit_frac*100:.1f}%  "
            f"horizon:{horizon_frac*100:.1f}%  "
            f"near:{near_spread*100:.1f}%  far:{far_spread*100:.1f}%  "
            f"gap:{wall_floor_gap_frac*100:.1f}%",
            2500,
        )

    def _on_beats_failed(self, segment_id: str, message: str) -> None:
        # Don't wipe the segment's saved events — a transient ffmpeg /
        # subprocess blip shouldn't cost the user their last good
        # detection.  We DO restore the strip from the saved list so
        # the spinner/loading state is replaced by something readable
        # (or cleared entirely if the segment never had a successful
        # run on file).
        seg = self.project.get_segment(segment_id)
        if seg is not None and seg.beat_events:
            self.timeline_panel.set_beat_events(
                segment_id, list(seg.beat_events)
            )
        else:
            self.timeline_panel.clear_beat_events(segment_id)
        self.statusBar().showMessage(
            f"Beat detection failed: {message}", 5000
        )
        print(f"[BeatDetect] segment={segment_id} error={message}",
              flush=True)

    def _on_auto_gen_block_requested(self, segment_id: str) -> None:
        """Manual handler for the timeline's "Auto Gen Block" button.

        Beat-detect runs ONLY through this entry-point now (no auto-fire
        on selection / drag / form-change), so the user explicitly opts
        in to spawning the rhythm.py subprocess.  The subsequent
        ``ready`` / ``failed`` signals from :class:`BeatDetectService`
        flow into ``_on_beats_ready`` / ``_on_beats_failed`` exactly as
        before, painting the timeline strip when results land.
        """
        if not segment_id:
            return
        segment = self.project.get_segment(segment_id)
        if segment is None:
            self.statusBar().showMessage(
                "Auto Gen Block: select a segment first", 3000
            )
            return
        self._request_beat_detect(segment)

    def _on_beat_threshold_changed(
        self, segment_id: str, threshold: float
    ) -> None:
        """Persist the new threshold without re-running detection.

        Per explicit user request: the red threshold line is now a
        passive *configuration* knob — only the toolbar's
        *Auto Gen Block* and *Gen by Chart* buttons (re)generate
        beat sticks.  Dragging the line just stores the value on
        the segment so the next gen run picks it up.

        The actual write to :pyattr:`Segment.beat_height_threshold`
        happens live inside the panel's
        ``_on_beat_threshold_dragged`` slot; the dirty / autosave
        flush is taken care of by the sibling
        ``beat_events_edited`` emit on drag release (see
        :meth:`_on_beat_events_edited`).  This handler therefore
        only needs to surface a brief status hint so the user
        knows the value was captured but no detection ran.
        """
        if not segment_id:
            return
        segment = self.project.get_segment(segment_id)
        if segment is None:
            return
        thr = max(0.0, min(1.0, float(threshold or 0.0)))
        self.statusBar().showMessage(
            f"Threshold for {segment.name}: {thr:.2f} "
            f"— click Auto Gen / Gen by Chart to apply",
            3000,
        )

    def _on_create_segment_requested(self, media_id: str, start_time: float) -> None:
        media = self.project.get_media(media_id)
        if media is None:
            return
        duration = media.duration_sec if media.duration_sec else 8.0

        # Placement rule:
        #   - No segments yet  → start at 0.0
        #   - Segments exist   → start right after the last segment's end time
        #     (ignore the drop-position; keeps timeline gap-free).
        if not self.project.segments:
            start_time = 0.0
        else:
            start_time = max(
                seg.end_time_sec for seg in self.project.segments
            )
        start_time = round(start_time * 10) / 10.0

        segment = Segment(
            name=f"Segment {len(self.project.segments) + 1}",
            start_time_sec=start_time,
            end_time_sec=start_time + duration,
            audio_path=media.source_path if media.kind.value == "audio" else "",
            audio_offset_sec=start_time,   # explicit: matches initial timeline position
            audio_duration_sec=duration,
            mode="punch",
            render_settings=build_settings("punch", {}).model_dump(mode="json", exclude_none=True),
        )
        self.project.segments.append(segment)
        self.timeline_panel.refresh()
        self.segment_panel.set_segment(segment)
        self.preview_panel.set_source_segment(segment)
        # Extract waveform now that an audio media has been dropped onto timeline.
        if segment.audio_path:
            self._request_waveform_for(segment.audio_path)
        # Trim the audio clip for this segment so the WAV is ready before
        # the user hits Render / Preview.
        self._request_audio_trim(segment)
        self._on_project_changed()

    def _on_segment_selected(self, segment: Segment | None) -> None:
        self.segment_panel.set_segment(segment)
        self.preview_panel.set_source_segment(segment)
        # Load waveform for this segment’s audio, or show empty for a segment
        # that has no file.  Deselect (``segment is None``) keeps the last
        # painted RMS so clicks on the timeline chrome never blank the strip.
        if segment is not None and segment.audio_path:
            self._request_waveform_for(segment.audio_path)
        elif segment is not None and not segment.audio_path:
            self._current_waveform_path = None
            self.timeline_panel.clear_waveform()
        # else: deselect — leave :attr:`_current_waveform_path` + strip as-is
        if segment is not None:
            start = segment.start_time_sec
            self.timeline_panel.set_playhead(start)
            self.preview_panel.seek_to_seconds(start)

    def _on_segment_changed_by_timeline(self, _segment_id: str) -> None:
        current = self.segment_panel.current_segment
        if current is not None:
            self.segment_panel.set_segment(current)
            # Re-trim after a timeline drag/move so the [start, end] window
            # of the segment's audio matches the new position.  Beat-detect
            # is NOT triggered here — the user runs it manually via the
            # "Auto Gen Block" button when they're ready to preview blocks.
            self._request_audio_trim(current)
            # Audio window changes invalidate the renderer's audio
            # buffers — librosa already crunched the OLD trim.  Rather
            # than silently render mismatched visuals, drop out of
            # preview so the user knows to re-toggle once the new
            # trim lands.
            if (
                self._preview_mode_active
                and self._preview_active_segment_id == current.id
            ):
                self._stop_preview_mode()
                self.statusBar().showMessage(
                    "Preview stopped — segment range changed; "
                    "click Preview again to reload.",
                    4000,
                )
        self._sync_preview_button_state()
        self._on_project_changed()

    def _sync_preview_button_state(self) -> None:
        """Reflect the live-preview toggle on whatever segment the
        config panel is currently showing.

        The Preview button lives on the segment panel and only knows
        about the segment it's displaying — so when the user clicks
        a DIFFERENT segment in the timeline, we need to re-paint the
        button (checked iff the displayed segment is the one being
        previewed).
        """
        current = self.segment_panel.current_segment
        active = (
            self._preview_mode_active
            and current is not None
            and current.id == self._preview_active_segment_id
        )
        self.segment_panel.set_preview_active(bool(active))

    def _on_segment_split(self, original_id: str, new_id: str) -> None:
        """Handle timeline split: trim both halves and mark project dirty."""
        new_segment = self.project.get_segment(new_id)
        orig_segment = self.project.get_segment(original_id)
        self.segment_panel.set_project(self.project)
        if new_segment is not None:
            self.segment_panel.set_segment(new_segment)
        # Immediately trim both halves in background so trimmed_audio_path is
        # ready for render/preview without waiting for the user to click anything.
        if orig_segment is not None:
            self._request_audio_trim(orig_segment)
        if new_segment is not None:
            self._request_audio_trim(new_segment)
        # Splitting changes the segment's audio window — the renderer
        # was loaded with the pre-split clip and would now be
        # rendering against the wrong portion.  Drop out of preview;
        # the user re-toggles on the half they want to inspect.
        if self._preview_mode_active and self._preview_active_segment_id in (
            original_id,
            new_id,
        ):
            self._stop_preview_mode()
            self.statusBar().showMessage(
                "Preview stopped — segment was split; "
                "click Preview again on the desired half.",
                4000,
            )
        self.statusBar().showMessage("Trimming split audio in background…", 3000)
        self._sync_preview_button_state()
        self._on_project_changed()

    def _on_segment_joined(self, kept_id: str, removed_id: str) -> None:
        """Handle timeline join: re-trim merged audio and mark project dirty."""
        kept_segment = self.project.get_segment(kept_id)
        self.segment_panel.set_project(self.project)
        if kept_segment is not None:
            self.segment_panel.set_segment(kept_segment)
        # Immediately re-trim the merged segment's audio in background.
        # _do_join already updated audio_offset_sec / audio_duration_sec and
        # cleared trimmed_audio_path; this extracts the full joined window.
        if kept_segment is not None:
            self._request_audio_trim(kept_segment)
        # If preview was running on either half, stop it — the audio window
        # has grown and the renderer would reference a stale trimmed clip.
        if self._preview_mode_active and self._preview_active_segment_id in (
            kept_id,
            removed_id,
        ):
            self._stop_preview_mode()
            self.statusBar().showMessage(
                "Preview stopped — segments were joined; "
                "click Preview again to preview the merged segment.",
                4000,
            )
        # Clear any beat-event cache for the removed segment.
        if hasattr(self.timeline_panel, "_beat_events"):
            self.timeline_panel._beat_events.pop(removed_id, None)
        self.statusBar().showMessage("Trimming joined audio in background…", 3000)
        self._sync_preview_button_state()
        self._on_project_changed()

    def _on_segment_duplicated(self, new_segment_id: str) -> None:
        """Handle timeline Ctrl+D duplicate — copy audio/video and mark dirty."""
        import shutil

        new_seg = self.project.get_segment(new_segment_id)
        self.segment_panel.set_project(self.project)
        if new_seg is not None:
            self.segment_panel.set_segment(new_seg)

            # Copy trimmed audio so the duplicate has its own independent file.
            src_audio = new_seg.trimmed_audio_path
            if src_audio and Path(src_audio).exists():
                src_ext = Path(src_audio).suffix or ".mp3"
                dst_audio = self._app_temps_dir() / f"audio_{new_segment_id}{src_ext}"
                try:
                    shutil.copy2(src_audio, dst_audio)
                    new_seg.trimmed_audio_path = str(dst_audio)
                except OSError:
                    new_seg.trimmed_audio_path = None
                    self._request_audio_trim(new_seg)
            else:
                # No pre-trimmed file yet — schedule a fresh trim
                new_seg.trimmed_audio_path = None
                self._request_audio_trim(new_seg)

            # video_path: keep the same reference (the duplicate starts IDLE
            # and will produce its own render; the original's video is read-only
            # from its perspective until it re-renders).

        self._sync_preview_button_state()
        self._on_project_changed()
        self.statusBar().showMessage("Segment duplicated (audio copied)", 2000)

    def _on_segment_moved(
        self, segment_id: str, new_start: float, new_end: float
    ) -> None:
        """Handle timeline segment drag — mark project dirty."""
        self._on_project_changed()

    def _on_segment_delete_requested(self, segment_id: str) -> None:
        """Drop a segment from the project after user-confirmed delete.

        Cleans up every cache that referenced this segment so a future
        re-create with the same id (very unlikely but possible) starts
        from a clean slate:

        - Stop live preview if it was driving this segment (the
          renderer holds onto the trimmed audio buffer; keeping it
          alive after the segment is gone would render against a
          stale window).
        - Clear the segment-config inspector if it was showing this
          segment.
        - Remove the timeline panel's beat-event cache for this id.
        - Remove the segment from ``project.segments``.
        - Mark project dirty so autosave persists the deletion.
        """
        import copy as _copy
        seg = self.project.get_segment(segment_id)
        if seg is None:
            return
        seg_name = seg.name
        # Snapshot for undo before any mutation
        seg_snapshot = _copy.deepcopy(seg)
        beat_snapshot = list(
            self.timeline_panel._beat_events.get(segment_id, [])
        )

        if (
            self._preview_mode_active
            and self._preview_active_segment_id == segment_id
        ):
            self._stop_preview_mode()
        current = self.segment_panel.current_segment
        if current is not None and current.id == segment_id:
            self.segment_panel.set_segment(None)
        self.timeline_panel.clear_beat_events(segment_id)
        self._last_trim_signature.pop(segment_id, None)
        self._inflight_trim_segments.discard(segment_id)
        self.project.segments = [
            s for s in self.project.segments if s.id != segment_id
        ]
        self.timeline_panel.refresh()
        self._sync_preview_button_state()
        self._on_project_changed()
        self.statusBar().showMessage(
            f"Segment '{seg_name}' deleted — Ctrl+Z to undo", 3000
        )

        # Push undo command to the timeline panel's undo stack.
        panel = self.timeline_panel
        main_win = self

        def _undo_delete() -> None:
            if panel._project is None:
                return
            panel._project.segments.append(_copy.deepcopy(seg_snapshot))
            panel._beat_events[seg_snapshot.id] = list(beat_snapshot)
            panel._selected_segment_id = seg_snapshot.id
            panel.refresh()
            restored = panel._project.get_segment(seg_snapshot.id)
            if restored is not None:
                panel.segment_selected.emit(restored)
            main_win._sync_preview_button_state()
            main_win._on_project_changed()

        def _redo_delete() -> None:
            if panel._project is None:
                return
            main_win_cur = main_win.segment_panel.current_segment
            if main_win_cur is not None and main_win_cur.id == seg_snapshot.id:
                main_win.segment_panel.set_segment(None)
            panel._project.segments = [
                s for s in panel._project.segments if s.id != seg_snapshot.id
            ]
            panel._beat_events.pop(seg_snapshot.id, None)
            if panel._selected_segment_id == seg_snapshot.id:
                panel._selected_segment_id = None
                panel.segment_selected.emit(None)
            panel.refresh()
            main_win._sync_preview_button_state()
            main_win._on_project_changed()

        from studio.editor.timeline_panel import _Cmd
        panel.undo_stack.push(_Cmd("Delete Segment", _undo_delete, _redo_delete))

    def _on_segment_changed_by_form(self, _segment_id: str) -> None:
        self.timeline_panel.refresh()
        current = self.segment_panel.current_segment
        audio = current.audio_path if current and current.audio_path else None
        if audio:
            self._request_waveform_for(audio)
        else:
            self._current_waveform_path = None
            self.timeline_panel.clear_waveform()
        # Re-trim: the user may have changed start/end or the audio source.
        # Beat-detect is NOT triggered here either — the user iterates on
        # mode/sens/density/… without paying for a subprocess each tweak,
        # then clicks "Auto Gen Block" when they want to preview the
        # resulting block layout on the timeline.
        if current:
            self._request_audio_trim(current)
        # Re-sync the preview panel's stickman toggle in case the
        # form just flipped ``render_settings.stickman`` — without
        # this the toolbar button can stay enabled / disabled with
        # stale state until the user re-selects the segment.
        if hasattr(self, "preview_panel"):
            self.preview_panel._refresh_stickman_button_state()
        # Live preview reflects this segment's mode/density/threshold,
        # so any form change while preview mode is ON should restart
        # the render from the current cursor with the new params.
        # Debounced so rapid spinner-drags don't churn ffmpeg.
        if current is not None:
            self._request_preview_restart(current.id)
        self._update_status()

    def _render_selected_segment(self) -> None:
        current = self.segment_panel.current_segment
        if current is None:
            return
        self._enqueue_segment(current)

    def _render_all_segments(self) -> None:
        for segment in self.project.sorted_segments():
            self._enqueue_segment(segment)

    # ── Export flow ──────────────────────────────────────────────────────────

    def _on_update_worker_clicked(self) -> None:
        """Open the rhythm_worker.exe download dialog."""
        dlg = WorkerUpdateDialog(parent=self)
        dlg.exec()

    def _on_export_button_clicked(self) -> None:
        """Show the detailed Export dialog (stays open; user closes manually)."""
        if not self.project.segments:
            QMessageBox.information(self, "Export", "No segments to export.")
            return

        # Re-use an existing open dialog if one is already showing.
        if getattr(self, "_export_dialog", None) is not None:
            dlg = self._export_dialog
            dlg.raise_()
            dlg.activateWindow()
            return

        dlg = ExportDialog(
            self,
            project=self.project,
            app_root=self._app_root,
            temps_dir=self._app_temps_dir(),
            token_provider=self._current_auth_token,
            url_provider=self._current_auth_url,
        )
        # When the dialog renders a segment, update video_path and persist.
        dlg.segment_rendered.connect(self._on_export_segment_rendered)
        dlg.destroyed.connect(self._on_export_dialog_closed)
        self._export_dialog = dlg

        # Set a reasonable default output path
        if self.project_path:
            default_out = str(Path(self.project_path).with_suffix(".mp4"))
        else:
            default_out = str(self._app_temps_dir() / "export.mp4")
        dlg._path_edit.setText(default_out)

        dlg.show()

    def _on_export_segment_rendered(self, segment_id: str, output_path: str) -> None:
        """Called by ExportDialog when a segment finishes rendering."""
        # Delegate to the normal render-finished handler so video_path is
        # persisted, the timeline refreshes, and autosave fires.
        self._on_render_finished(segment_id, output_path)

    def _on_export_dialog_closed(self) -> None:
        self._export_dialog = None  # type: ignore[assignment]

    def _on_preview_segment_requested(self, segment_id: str) -> None:
        """Toggle live preview mode for the segment.

        OFF → ON  : build an in-process :class:`LiveFrameRenderer` for
        the segment and hand it to :class:`PreviewPanel` so the player
        starts drawing frames against the segment's audio playhead.
        First frame appears in ~1.5–2 s (audio analysis + scene init);
        every subsequent edit hot-reloads in <200 ms.

        ON  → OFF : tear down the renderer and stop audio playback.

        While the toggle is ON, edits to mode / beats fire
        ``_request_preview_restart`` which debounces and routes the
        change to ``preview_panel.update_live_*`` — no subprocess
        spawn, no HLS, no .ts files.
        """
        # If a worker is still loading (mode not yet active), cancel it.
        # Clicking the same segment's button a second time = cancel load.
        if self._preview_worker is not None:
            same = (self._preview_worker_segment_id == segment_id)
            self._cancel_preview_worker()
            self.statusBar().showMessage("Preview loading cancelled.", 2000)
            self.segment_panel.set_preview_active(False)
            if same:
                return
            # Different segment — fall through to start a new load.

        if self._preview_mode_active:
            already = (self._preview_active_segment_id == segment_id)
            self._stop_preview_mode()
            if already:
                return
            # Else fall through to start a preview for the new segment.

        segment = self.project.get_segment(segment_id)
        if segment is None:
            return
        if not segment.audio_path:
            self.statusBar().showMessage("Segment has no audio source", 3000)
            self.segment_panel.set_preview_active(False)
            return

        self._start_live_preview(segment)

    def _stop_preview_mode(self) -> None:
        """Tear down the in-process live preview + reset toggle state.

        Idempotent.  Cancels the pending edit-debounce, drops the
        renderer (frees its 30 – 100 MB of NumPy buffers), tells the
        panel to stop drawing and revert to its idle page, and
        un-checks the segment-panel's Preview button.

        Note: ``preview_panel.stop_live_preview`` emits
        ``live_preview_stopped`` which we also handle in
        :meth:`_on_live_preview_panel_stopped`.  That handler is
        intentionally idempotent so the round-trip from this method
        through the signal path doesn't double-message the user via
        the status bar.
        """
        seg_id = self._preview_active_segment_id
        # Cancel any in-flight background renderer build.
        self._cancel_preview_worker()
        # Cancel any debounced hot-reload that hasn't fired yet — its
        # target renderer is about to be closed.
        self._preview_restart_timer.stop()
        self._live_preview_pending_mode = False
        self._live_preview_pending_beats = False
        # Tell the panel to stop the frame timer + close the renderer.
        try:
            self.preview_panel.stop_live_preview()
        except Exception:  # noqa: BLE001
            pass
        # Drop our own reference to the renderer (panel holds the
        # canonical one but mirroring it here lets edit handlers
        # short-circuit before bothering the panel).
        self._live_preview_renderer = None
        self._preview_mode_active = False
        self._preview_active_segment_id = None
        if seg_id is not None:
            self.statusBar().showMessage("Preview stopped.", 2500)
        self.segment_panel.set_preview_active(False)
        self._update_status()

    def _on_live_preview_panel_stopped(self) -> None:
        """Mirror panel-initiated live-preview stops onto our state.

        Reachable when the panel auto-stops because a different source
        was loaded (timeline click on another segment, source-combo
        change, post-render auto-load…).  Our explicit
        :meth:`_stop_preview_mode` also routes through this slot via
        ``stop_live_preview``'s signal — that's why the body is
        idempotent.
        """
        if not self._preview_mode_active:
            return
        self._preview_restart_timer.stop()
        self._live_preview_pending_mode = False
        self._live_preview_pending_beats = False
        self._live_preview_renderer = None
        self._preview_mode_active = False
        self._preview_active_segment_id = None
        self.segment_panel.set_preview_active(False)
        self._update_status()

    def _start_live_preview(self, segment: Segment) -> None:
        """Kick off background construction of a LiveFrameRenderer.

        ``LiveFrameRenderer.__init__`` runs ``librosa.load`` + FFT +
        wave-column detection (~1-3 s).  We offload that work to
        :class:`_RendererWorker` so the UI stays responsive.  A
        "Loading…" message appears on the status bar; the renderer is
        wired to the panel in :meth:`_on_renderer_ready` once the
        thread finishes.
        """
        # Cancel any in-flight worker from a previous click.
        self._cancel_preview_worker()

        # Pick the audio file: pre-trimmed WAV ▸ raw audio_path.
        audio_path = ""
        if (
            getattr(segment, "trimmed_audio_path", None)
            and Path(segment.trimmed_audio_path).exists()
        ):
            audio_path = str(segment.trimmed_audio_path)
        elif segment.audio_path:
            audio_path = str(segment.audio_path)
        if not audio_path:
            self.statusBar().showMessage(
                "Segment has no audio source — cannot preview.", 4000
            )
            return

        # Beat array: ``segment.beat_events`` is a list of
        # ``(t_local, kind, height)`` tuples; the renderer only needs
        # the t_local floats.  Tolerate scalar entries from older
        # project files.
        beat_times: list[float] = []
        for ev in (segment.beat_events or []):
            try:
                t = (
                    float(ev[0])
                    if isinstance(ev, (tuple, list))
                    else float(ev)
                )
            except (TypeError, ValueError, IndexError):
                continue
            beat_times.append(max(0.0, t))

        kwargs = self._live_renderer_kwargs(segment)

        # For combo mode, ``segment.mode`` is the literal string ``"combo"``
        # which ``_parse_modes`` doesn't accept (only the individual sub-modes
        # punch/dance/line/relax are valid).  Convert to a comma-joined spec
        # from ``mode_list`` in render_settings so the renderer receives e.g.
        # ``"punch,dance"`` and activates proper combo cycling.
        rs = segment.render_settings or {}
        if segment.mode == "combo":
            mode_list = rs.get("mode_list") or ["punch", "dance"]
            mode_str = ",".join(str(m) for m in mode_list)
        else:
            mode_str = segment.mode or "punch"

        worker = _RendererWorker(
            audio_path, beat_times, mode_str, kwargs, parent=self
        )
        worker.ready.connect(
            lambda renderer, seg=segment, ap=audio_path: self._on_renderer_ready(renderer, seg, ap)
        )
        worker.failed.connect(self._on_renderer_failed)
        worker.finished.connect(worker.deleteLater)

        self._preview_worker = worker
        self._preview_worker_segment_id = segment.id

        self.statusBar().showMessage(
            f"Loading preview for '{segment.name}'…", 0
        )
        self.segment_panel.set_preview_loading(True)
        worker.start()

    def _cancel_preview_worker(self) -> None:
        """Cancel and discard any in-flight renderer worker."""
        if self._preview_worker is not None:
            self._preview_worker.cancel()
            self._preview_worker.quit()
            self._preview_worker = None
            self._preview_worker_segment_id = None
            self.segment_panel.set_preview_loading(False)

    def _on_renderer_ready(self, renderer: object, segment: Segment, audio_path: str) -> None:
        """Called on the UI thread when the background worker finishes."""
        self._preview_worker = None
        self._preview_worker_segment_id = None
        self.segment_panel.set_preview_loading(False)

        # User may have clicked Stop while we were loading.
        if self._preview_mode_active:
            # Already switched to a different segment.
            return

        self._preview_mode_active = True
        self._preview_active_segment_id = segment.id
        self._live_preview_renderer = renderer

        seg_start = float(segment.start_time_sec or 0.0)
        try:
            self.preview_panel.start_live_preview(
                renderer,
                audio_path,
                start_local_sec=0.0,
                project_offset_sec=seg_start,
            )
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(
                f"Preview panel failed to attach: {exc}", 6000
            )
            print(f"[preview] panel.start_live_preview error: {exc!r}", flush=True)
            renderer.close()
            self._live_preview_renderer = None
            self._preview_mode_active = False
            self._preview_active_segment_id = None
            self.segment_panel.set_preview_active(False)
            return

        self.segment_panel.set_preview_active(True)
        self.statusBar().showMessage(
            f"Preview live: {segment.name} — edit to hot-reload.", 4000
        )
        self._update_status()

    def _on_renderer_failed(self, error_msg: str) -> None:
        """Called on the UI thread when the background worker raises."""
        self._preview_worker = None
        self._preview_worker_segment_id = None
        self.segment_panel.set_preview_loading(False)
        self.statusBar().showMessage(
            f"Preview failed to initialise: {error_msg}", 6000
        )
        print(f"[preview] LiveFrameRenderer init error: {error_msg!r}", flush=True)
        self.segment_panel.set_preview_active(False)

    def _live_renderer_kwargs(self, segment: Segment) -> dict:
        """Translate a Segment's render_settings into LiveFrameRenderer kwargs.

        Centralised so both initial construction and any future
        "rebuild renderer with new settings" path use the exact same
        mapping.  Anything missing from the segment's
        ``render_settings`` dict falls back to the live-renderer's
        constructor default.
        """
        # Late import: ``_parse_color`` lives next to ``rhythm.py`` and
        # we already added ``src/`` to ``sys.path`` from
        # ``live_renderer.py``'s import block, so it's resolvable here.
        try:
            from rhythm import _parse_color
        except ImportError:
            # Fallback: keep colour overrides off rather than crashing.
            def _parse_color(_: str) -> None:  # type: ignore[no-redef]
                return None

        rs = segment.render_settings or {}

        def _get(key: str, default):
            val = rs.get(key, default)
            if val is None:
                return default
            return val

        kwargs: dict = {
            "fps": 24,
            "width": 1280,
            "height": 720,
            "bloom": False,  # always off for live preview (8–15 ms / frame)
            "show_stickman": bool(_get("stickman", True)),
            "show_floor_panels": bool(_get("floor_panels", True)),
            "floor_panel_color": _get("floor_panel_color", None) or "",
            "floor_panel_opacity": float(_get("floor_panel_opacity", 1.0) or 1.0),
            "floor_panel_blink": bool(_get("floor_panel_blink", False)),
            "floor_panel_image": _get("floor_panel_image", None) or "",
            "floor_full_static_image": bool(_get("floor_full_static_image", False)),
            "floor_layout":          str(_get("floor_layout", "auto")),
            "floor_bg_color":        _get("floor_bg_color", None) or "",
            "floor_bg_opacity":      float(_get("floor_bg_opacity", 1.0) or 1.0),
            "chevron_color":         str(_get("chevron_color", "#FFD700")),
            "chevron_scroll":        bool(_get("chevron_scroll", True)),
            "chevron_blink":         bool(_get("chevron_blink", False)),
            "chevron_width_frac":    float(_get("chevron_width_frac", 0.45) or 0.45),
            "chevron_count":         int(_get("chevron_count", 6) or 6),
            "show_side_rails":       bool(_get("side_rails", False)),
            "rail_color":            str(_get("rail_color", "#FF60FF")),
            "rail_shape":            str(_get("rail_shape", "chunky")),
            "rail_height":           float(_get("rail_height", 0.14)),
            "rail_offset_x":         float(_get("rail_offset_x", 0.08)),
            "rail_image":            _get("rail_image", None) or "",
            "rail_texture_non_loop": bool(_get("rail_texture_non_loop", False)),
            "rail_pulse":            str(_get("rail_pulse", "beat")),
            "rail_pulse_intensity":  float(_get("rail_pulse_intensity", 0.6)),
            "rail_chevron_depth":    float(_get("rail_chevron_depth", 1.0) or 1.0),
            "rail_chevron_density":  int(_get("rail_chevron_density", 6) or 6),
            "rail_pillar_count":     int(_get("rail_pillar_count", 16) or 16),
            "rail_pillar_radius":    float(_get("rail_pillar_radius", 1.0) or 1.0),
            "rail_chase_mode":       str(_get("rail_chase_mode", "time") or "time"),
            "rail_chase_speed_frames": int(_get("rail_chase_speed_frames", 4) or 4),
            "rail_dot_count":        int(_get("rail_dot_count", 24) or 24),
            "rail_dot_lines":        int(_get("rail_dot_lines", 1) or 1),
            "rail_dot_size_px":      int(_get("rail_dot_size_px", 6) or 6),
            "rail_dot_anim_mode":    str(_get("rail_dot_anim_mode", "audio") or "audio"),
            "rail_dot_color_near":   str(_get("rail_dot_color_near", "#FF60FF") or "#FF60FF"),
            "rail_dot_color_far":    str(_get("rail_dot_color_far", "#00FFFF") or "#00FFFF"),
            "floor_hit_frac":       _get("floor_hit_frac", None),
            "horizon_frac":         _get("horizon_frac", None),
            "floor_spread_frac":    _get("floor_spread_frac", None),
            "far_spread_frac":      _get("far_spread_frac", None),
            "wall_floor_gap_frac":  _get("wall_floor_gap_frac", None),
            "max_per_lane": int(_get("max_per_lane", 2)),
            "block_speed": float(_get("speed", 0.8)),
            "beat_min_gap": int(_get("beat_min_gap", 4)),
            "line_beats": int(_get("line_beats", 2)),
            "line_zigzag": str(_get("line_zigzag", "vertical") or "vertical"),
            "dance_pair_cycle": int(_get("dance_pair_cycle", 4)),
            "punch_pair_cycle": int(_get("punch_pair_cycle", 4)),
        }

        # Cube + panel colours: settings store hex / "R,G,B" strings,
        # ``_parse_color`` returns BGR tuples ready for OpenCV.
        try:
            kwargs["cube_color_left"] = _parse_color(rs.get("cube_color_left"))
            kwargs["cube_color_right"] = _parse_color(rs.get("cube_color_right"))
            kwargs["panel_neon_color"] = _parse_color(rs.get("panel_neon_color"))
        except ValueError:
            # Malformed colour string in settings — silently skip the
            # override and let the renderer use its defaults.
            kwargs.setdefault("cube_color_left", None)
            kwargs.setdefault("cube_color_right", None)
            kwargs.setdefault("panel_neon_color", None)

        # Lane filter: settings list is 1-based; renderer expects a
        # 0-based set, or ``None`` for "all enabled".
        lanes = rs.get("lanes")
        if isinstance(lanes, list) and lanes:
            try:
                lane_set = {max(0, int(l) - 1) for l in lanes}
            except (TypeError, ValueError):
                lane_set = None
            kwargs["lane_filter"] = lane_set
        else:
            kwargs["lane_filter"] = None

        # Stickman draw-box: segment stores ``{x, y, w, h}`` fractions
        # (0..1).  Convert to pixel rect against the renderer's chosen
        # 1280×720 canvas; passing ``None`` keeps the StickmanHUD
        # default.
        if kwargs["show_stickman"]:
            box = self._segment_stickman_box_pixels(
                segment, kwargs["width"], kwargs["height"]
            )
            if box is not None:
                kwargs["stickman_box"] = box

        return kwargs

    def _segment_stickman_box_pixels(
        self,
        segment: Segment,
        width: int = 1280,
        height: int = 720,
    ) -> Optional[tuple[int, int, int, int]]:
        """Resolve a segment's stickman fractions to a pixel ``(x,y,w,h)``.

        Mirrors the math used at renderer construction so a hot-reload
        triggered by a stickman drag/resize lands on EXACTLY the same
        box the user sees in the player overlay.  Returns ``None`` when
        the segment has no ``stickman_location`` dict or any field
        fails to coerce — callers pass ``None`` straight through to
        :class:`LiveFrameRenderer` which then falls back to the
        :class:`StickmanHUD` default left-column box.
        """
        loc = getattr(segment, "stickman_location", None)
        if not isinstance(loc, dict):
            return None
        try:
            return (
                int(float(loc.get("x", 0.0)) * width),
                int(float(loc.get("y", 0.0)) * height),
                int(float(loc.get("w", 0.0)) * width),
                int(float(loc.get("h", 0.0)) * height),
            )
        except (TypeError, ValueError):
            return None

    def _request_preview_restart(self, segment_id: str) -> None:
        """Schedule a debounced live-preview hot-reload.

        Called from any handler that mutates state the renderer's
        schedule depends on — beat ticks, mode, density, density-gap,
        line zigzag, lane mask…  Multiple consecutive edits within
        ~80ms coalesce into a single ``update_beats`` /
        ``update_mode`` call.

        Distinct from the OLD HLS-restart flow with the same name —
        this no longer cancels any subprocess; it just queues a
        renderer.update_*() call.  Kept under the same name so all
        the existing callers in the form / timeline edit handlers
        keep working without churn.
        """
        if not self._preview_mode_active:
            return
        if segment_id != self._preview_active_segment_id:
            return
        # Default to "everything changed" — cheap because we only fire
        # ``update_mode`` once and ``update_beats`` once after the
        # debounce; no per-edit subprocess work to amortise.
        self._live_preview_pending_mode = True
        self._live_preview_pending_beats = True
        self._preview_restart_timer.start()

    def _request_preview_beats_only(self, segment_id: str) -> None:
        """Variant of ``_request_preview_restart`` for pure-beat edits.

        Used by :meth:`_on_beat_events_edited` so a beat-tick drag
        doesn't also rebuild the cam/tunnel/HUD scene — that would
        be wasteful (mode hasn't changed) and adds a few hundred ms
        of latency on top of the schedule rebuild.
        """
        if not self._preview_mode_active:
            return
        if segment_id != self._preview_active_segment_id:
            return
        self._live_preview_pending_beats = True
        self._preview_restart_timer.start()

    def _perform_live_preview_update(self) -> None:
        """Apply the queued mode/beat hot-reload to the live renderer."""
        if not self._preview_mode_active:
            return
        seg_id = self._preview_active_segment_id
        if seg_id is None:
            return
        segment = self.project.get_segment(seg_id)
        if segment is None:
            self._stop_preview_mode()
            return

        do_mode = self._live_preview_pending_mode
        do_beats = self._live_preview_pending_beats
        # Reset the dirty flags BEFORE issuing the calls so an edit
        # that lands during the update is still picked up by the
        # next debounce tick instead of being lost when we clear at
        # the bottom.
        self._live_preview_pending_mode = False
        self._live_preview_pending_beats = False

        # Mode update goes first because update_mode rebuilds the
        # scene (cam / tunnel / game), which means the schedule has
        # to be re-derived from the current beat array anyway — so we
        # follow with update_beats whenever the user has re-edited
        # the array since the renderer was constructed.
        #
        # Decor params (stickman visibility / box, floor panels) are
        # bundled into this call so a single ``_build_scene`` rebuild
        # picks up everything the user just edited in the segment-
        # config form (mode + Sticky Man + Floor panels) or via the
        # player's stickman drag overlay.  Passing them on every mode
        # update — even when only the mode list changed — is harmless:
        # the renderer just rebinds attrs to their existing values.
        if do_mode:
            rs = segment.render_settings or {}
            show_stickman = bool(rs.get("stickman", True))
            show_floor_panels = bool(rs.get("floor_panels", True))
            # Use "" (not None) so update_mode's "if x is not None" guard
            # fires even when the user has cleared the value — "" or None → None.
            floor_panel_color = rs.get("floor_panel_color") or ""
            floor_panel_opacity = float(rs.get("floor_panel_opacity", 1.0) or 1.0)
            floor_panel_blink = bool(rs.get("floor_panel_blink", False))
            floor_panel_image = rs.get("floor_panel_image") or ""
            floor_full_static_image = bool(rs.get("floor_full_static_image", False))
            floor_layout      = str(rs.get("floor_layout", "auto"))
            floor_bg_color    = rs.get("floor_bg_color") or ""
            floor_bg_opacity  = float(rs.get("floor_bg_opacity", 1.0) or 1.0)
            chevron_color     = str(rs.get("chevron_color", "#FFD700"))
            chevron_scroll    = bool(rs.get("chevron_scroll", True))
            chevron_blink     = bool(rs.get("chevron_blink", False))
            chevron_width_frac = float(rs.get("chevron_width_frac", 0.45) or 0.45)
            chevron_count     = int(rs.get("chevron_count", 6) or 6)
            show_side_rails      = bool(rs.get("side_rails", False))
            rail_color           = str(rs.get("rail_color", "#FF60FF"))
            rail_shape           = str(rs.get("rail_shape", "chunky"))
            rail_height          = float(rs.get("rail_height", 0.14) or 0.14)
            rail_offset_x        = float(rs.get("rail_offset_x", 0.08) or 0.08)
            rail_image           = rs.get("rail_image") or ""
            rail_texture_non_loop = bool(rs.get("rail_texture_non_loop", False))
            rail_pulse           = str(rs.get("rail_pulse", "beat"))
            rail_pulse_intensity = float(rs.get("rail_pulse_intensity", 0.6) or 0.6)
            rail_chevron_depth   = float(rs.get("rail_chevron_depth", 1.0) or 1.0)
            rail_chevron_density = int(rs.get("rail_chevron_density", 6) or 6)
            rail_pillar_count = int(rs.get("rail_pillar_count", 16) or 16)
            rail_pillar_radius = float(rs.get("rail_pillar_radius", 1.0) or 1.0)
            rail_chase_mode = str(rs.get("rail_chase_mode", "time") or "time")
            rail_chase_speed_frames = int(rs.get("rail_chase_speed_frames", 4) or 4)
            rail_dot_count = int(rs.get("rail_dot_count", 24) or 24)
            rail_dot_lines = int(rs.get("rail_dot_lines", 1) or 1)
            rail_dot_size_px = int(rs.get("rail_dot_size_px", 6) or 6)
            rail_dot_anim_mode = str(rs.get("rail_dot_anim_mode", "audio") or "audio")
            rail_dot_color_near = str(rs.get("rail_dot_color_near", "#FF60FF") or "#FF60FF")
            rail_dot_color_far = str(rs.get("rail_dot_color_far", "#00FFFF") or "#00FFFF")
            max_per_lane = max(1, int(rs.get("max_per_lane", 2) or 2))
            stickman_box = (
                self._segment_stickman_box_pixels(segment)
                if show_stickman else None
            )
            # Same combo→mode_list conversion as in _start_live_preview.
            if segment.mode == "combo":
                mode_list = rs.get("mode_list") or ["punch", "dance"]
                mode_str = ",".join(str(m) for m in mode_list)
            else:
                mode_str = segment.mode or "punch"
            try:
                self.preview_panel.update_live_mode(
                    mode_str,
                    show_stickman=show_stickman,
                    stickman_box=stickman_box,
                    show_floor_panels=show_floor_panels,
                    floor_panel_color=floor_panel_color,
                    floor_panel_opacity=floor_panel_opacity,
                    floor_panel_blink=floor_panel_blink,
                    floor_panel_image=floor_panel_image,
                    floor_full_static_image=floor_full_static_image,
                    floor_layout=floor_layout,
                    floor_bg_color=floor_bg_color,
                    floor_bg_opacity=floor_bg_opacity,
                    chevron_color=chevron_color,
                    chevron_scroll=chevron_scroll,
                    chevron_blink=chevron_blink,
                    chevron_width_frac=chevron_width_frac,
                    chevron_count=chevron_count,
                    show_side_rails=show_side_rails,
                    rail_color=rail_color,
                    rail_shape=rail_shape,
                    rail_height=rail_height,
                    rail_offset_x=rail_offset_x,
                    rail_image=rail_image,
                    rail_texture_non_loop=rail_texture_non_loop,
                    rail_pulse=rail_pulse,
                    rail_pulse_intensity=rail_pulse_intensity,
                    rail_chevron_depth=rail_chevron_depth,
                    rail_chevron_density=rail_chevron_density,
                    rail_pillar_count=rail_pillar_count,
                    rail_pillar_radius=rail_pillar_radius,
                    rail_chase_mode=rail_chase_mode,
                    rail_chase_speed_frames=rail_chase_speed_frames,
                    rail_dot_count=rail_dot_count,
                    rail_dot_lines=rail_dot_lines,
                    rail_dot_size_px=rail_dot_size_px,
                    rail_dot_anim_mode=rail_dot_anim_mode,
                    rail_dot_color_near=rail_dot_color_near,
                    rail_dot_color_far=rail_dot_color_far,
                    max_per_lane=max_per_lane,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[preview] update_mode error: {exc!r}", flush=True)
        if do_beats:
            beat_times: list[float] = []
            for ev in (segment.beat_events or []):
                try:
                    t = (
                        float(ev[0])
                        if isinstance(ev, (tuple, list))
                        else float(ev)
                    )
                except (TypeError, ValueError, IndexError):
                    continue
                beat_times.append(max(0.0, t))
            try:
                self.preview_panel.update_live_beats(beat_times)
            except Exception as exc:  # noqa: BLE001
                print(f"[preview] update_beats error: {exc!r}", flush=True)

    def _on_render_segment_requested(self, segment_id: str) -> None:
        segment = self.project.get_segment(segment_id)
        if segment is None:
            return
        self._enqueue_segment(segment)

    def _enqueue_segment(self, segment: Segment) -> None:
        if not segment.audio_path:
            self.statusBar().showMessage("Segment has no audio source", 3000)
            return
        # Renders go into <app_root>/temps/, NOT into the project directory.
        # That way the rendered file survives moving/copying the .htproj
        # project folder, and segment.video_path stays valid because we
        # persist its absolute path.
        output_path = self._app_temps_dir() / f"segment_{segment.id}.mp4"
        segment.render_status = RenderStatus.QUEUED
        segment.last_render_progress = 0
        job = self.render_service.build_job(
            segment,
            output_path,
            output_width=self.project.output_width,
            output_height=self.project.output_height,
            output_fps=self.project.output_fps,
            project_temps_dir=str(self._app_temps_dir()),
        )
        self.render_service.enqueue(job)
        # Show "Rendering 0%" overlay over the player so the user gets clear
        # in-app feedback while the subprocess starts up.
        self.preview_panel.set_render_progress(
            0,
            label="Rendering",
            subtitle=f"{segment.name} — preparing…",
        )
        self._set_timeline_render_overlay(
            visible=True,
            title=f"Rendering: {segment.name}",
            message="Timeline is locked until current segment render finishes.",
            progress=0,
        )
        self.statusBar().showMessage(
            f"Rendering '{segment.name}' ({segment.start_time_sec:.1f}s"
            f"–{segment.end_time_sec:.1f}s)…", 3000
        )
        self._update_status()
        self.timeline_panel.refresh()
        self.segment_panel.set_segment(segment)
        self._refresh_render_controls()

    def _on_render_progress(self, segment_id: str, progress: int) -> None:
        segment = self.project.get_segment(segment_id)
        if segment is None:
            return
        segment.render_status = RenderStatus.RENDERING
        segment.last_render_progress = max(0, min(100, int(progress)))
        self.statusBar().showMessage(
            f"Rendering {segment.name}: {progress}%", 1500
        )
        self.preview_panel.set_render_progress(
            progress,
            label="Rendering",
            subtitle=segment.name,
        )
        self._set_timeline_render_overlay(
            visible=True,
            title=f"Rendering: {segment.name}",
            message=f"Please wait… {progress}%",
            progress=progress,
        )
        # Lightweight status-label-only refresh — never rebuilds form widgets,
        # so any spinbox the user might be editing during a long render is
        # preserved.  No-op when the user has selected a different segment.
        self.segment_panel.refresh_status_only(segment)
        self._refresh_render_controls()
        self._update_status()

    def _on_render_finished(self, segment_id: str, output_path: str) -> None:
        segment = self.project.get_segment(segment_id)
        if segment is None:
            return

        segment.render_status = RenderStatus.DONE
        segment.last_render_error = None
        segment.last_render_progress = 100
        segment.last_rendered_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        self.preview_panel.hide_render_overlay()
        self._set_timeline_render_overlay(visible=False)

        # The rendered file's local time 0 corresponds to project time
        # ``segment.start_time_sec`` — pass it so the timeline's red
        # playhead tracks the project timeline (not 0..video_duration).
        offset = float(segment.start_time_sec or 0.0)
        # Persist the absolute path so the segment knows where its
        # rendered MP4 lives even after the project file moves.
        segment.video_path = str(Path(output_path).resolve())
        self.statusBar().showMessage(
            f"Render done: {segment.name} — playing…", 5000
        )
        # Auto-load + play the rendered video.  ``load_video`` queues
        # playback so it kicks in as soon as the player reports the
        # media is ready.
        self.preview_panel.load_video(output_path, playhead_offset_sec=offset)
        self.timeline_panel.set_playhead(segment.start_time_sec)
        # Persist render metadata (video_path, status, timestamp) to
        # disk immediately so a crash / accidental close doesn't lose
        # the freshly rendered association.
        self._auto_save_after_render(segment.name)

        self.segment_panel.set_segment(segment)
        self.timeline_panel.refresh()
        self._refresh_render_controls()
        self._update_status()

    def _auto_save_after_render(self, segment_name: str) -> None:
        """Persist the project right after a successful render.

        - If the project already has a path, save silently and tell the
          status bar.
        - If the project has never been saved, leave it alone (don't pop
          a dialog mid-render-flow) and warn so the user knows to save.
        """
        if self.project_path is None:
            self.statusBar().showMessage(
                f"Rendered {segment_name}. Project not saved yet — use Ctrl+S to keep the render.",
                8000,
            )
            return
        try:
            self._save_to_path(self.project_path)
            self.statusBar().showMessage(
                f"Rendered {segment_name} — project auto-saved.", 5000
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.statusBar().showMessage(
                f"Rendered {segment_name}, but auto-save failed: {exc}",
                8000,
            )

    def _on_render_failed(self, segment_id: str, message: str) -> None:
        segment = self.project.get_segment(segment_id)
        if segment is None:
            return
        segment.render_status = RenderStatus.ERROR
        segment.last_render_error = message
        segment.last_render_progress = 0
        self.preview_panel.hide_render_overlay()
        self._set_timeline_render_overlay(visible=False)
        self.statusBar().showMessage(f"Render failed: {segment.name}", 5000)
        self.segment_panel.set_segment(segment)
        self._sync_preview_button_state()
        self._refresh_render_controls()
        self._update_status()

    def _refresh_render_controls(self) -> None:
        """Disable render actions/buttons while queue is active."""
        busy = any(
            seg.render_status in {RenderStatus.QUEUED, RenderStatus.RENDERING}
            for seg in self.project.segments
        )
        enabled = not busy
        for act_name in (
            "_act_render_selected",
            "_act_render_all",
            "_tb_act_render_selected",
            "_tb_act_render_all",
        ):
            act = getattr(self, act_name, None)
            if act is not None:
                act.setEnabled(enabled)

        current = self.segment_panel.current_segment
        can_render_current = (
            enabled and current is not None and bool(current.audio_path)
        )
        self.segment_panel.render_button.setEnabled(can_render_current)
        if enabled:
            self._set_timeline_render_overlay(visible=False)

    def _update_status(self) -> None:
        self._refresh_render_controls()
        queue_count = sum(
            1
            for segment in self.project.segments
            if segment.render_status in {RenderStatus.QUEUED, RenderStatus.RENDERING}
        )
        current_time = datetime.now().strftime("%H:%M:%S")
        self.statusBar().showMessage(
            f"Project: {self.project.name} | Time: {current_time} | Queue: {queue_count}"
        )

    def _save_splitters(self) -> None:
        self._settings.setValue("studio/top_splitter", self.top_splitter.saveState())
        self._settings.setValue("studio/outer_splitter", self.outer_splitter.saveState())

    def _restore_splitters(self) -> None:
        top = self._settings.value("studio/top_splitter")
        outer = self._settings.value("studio/outer_splitter")
        if top:
            self.top_splitter.restoreState(top)
        if outer:
            self.outer_splitter.restoreState(outer)

