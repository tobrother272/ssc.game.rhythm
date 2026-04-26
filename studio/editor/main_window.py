"""Main studio window hosting media, preview, timeline, and inspector."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
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
from studio.editor.media_library import MediaLibraryPanel
from studio.editor.preview_panel import PreviewPanel
from studio.editor.segment_config_panel import SegmentConfigPanel
from studio.editor.timeline_panel import TimelinePanel
from studio.models import Project, RenderStatus, Segment, build_settings
from studio.persistence import ProjectStore


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
        self._app_root: Path = Path(__file__).resolve().parents[2]
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
        # Track which audio path is currently displayed to avoid redundant requests.
        self._current_waveform_path: Optional[str] = None
        # Segment IDs whose render was triggered by the Preview button;
        # when their job finishes the rendered video is auto-played.
        self._preview_segment_ids: set[str] = set()

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(60_000)
        self._autosave_timer.timeout.connect(self._auto_save)
        self._autosave_timer.start()

        self._segment_sync_timer = QTimer(self)
        self._segment_sync_timer.setInterval(300)
        self._segment_sync_timer.timeout.connect(self._sync_timeline_positions)
        self._segment_sync_timer.start()

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
        render_menu.addAction("Render Selected Segment", self._render_selected_segment)
        render_menu.addAction("Render All", self._render_all_segments)

        menu.addMenu("Edit")
        menu.addMenu("Help")

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
        toolbar.addAction("Render Selected", self._render_selected_segment)
        toolbar.addAction("Render All", self._render_all_segments)

        # Right-side spacer + Export accent button
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().Policy.Expanding,
                             spacer.sizePolicy().Policy.Preferred)
        toolbar.addWidget(spacer)

        self.export_button = QPushButton("Export")
        self.export_button.setObjectName("accentButton")
        self.export_button.setToolTip("Render all segments and export")
        self.export_button.clicked.connect(self._render_all_segments)
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
        self.timeline_panel.auto_gen_block_requested.connect(
            self._on_auto_gen_block_requested
        )
        self.timeline_panel.beat_events_edited.connect(
            self._on_beat_events_edited
        )
        self.preview_panel.playhead_changed.connect(self.timeline_panel.set_playhead)
        self.preview_panel.playback_state_changed.connect(
            self._on_preview_playback_state_changed
        )
        self.preview_panel.stickman_location_changed.connect(
            self._on_stickman_location_edited
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
        self.project = project
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

    def _request_audio_trim(self, segment: Segment) -> None:
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
        self._inflight_trim_segments.add(segment.id)
        self.audio_trim_service.trim(
            segment_id=segment.id,
            audio_path=segment.audio_path,
            start_sec=segment.start_time_sec,
            end_sec=segment.end_time_sec,
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
        self.statusBar().showMessage(
            f"Stickman box updated for {seg.name}: "
            f"x={seg.stickman_location['x']*100:.1f}% "
            f"y={seg.stickman_location['y']*100:.1f}% "
            f"w={seg.stickman_location['w']*100:.1f}% "
            f"h={seg.stickman_location['h']*100:.1f}%",
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

    def _on_preview_playback_state_changed(self, state_value: int) -> None:
        """Allow timeline scrubbing only while preview is Playing / Paused.

        The user explicitly asked that the red playhead stop following
        mouse clicks whenever the preview video is in StoppedState — a
        click on the timeline (or even the playhead itself) while the
        media is parked must NOT lurch playback to a new position.
        StoppedState's enum value is 0 (matches PySide6's
        ``QMediaPlayer.PlaybackState.StoppedState``); anything else is
        Playing or Paused, both of which keep scrubbing live so the
        user can still seek mid-playback or while paused.

        Compares against ``StoppedState.value`` rather than wrapping
        the enum in ``int(...)`` because PySide6's ``PlaybackState``
        is not an ``IntEnum`` in every build (CPython 3.13 + Qt 6.7+
        raises ``TypeError`` on direct ``int(state)`` conversion).
        """
        from PySide6.QtMultimedia import QMediaPlayer

        is_stopped = state_value == int(
            QMediaPlayer.PlaybackState.StoppedState.value
        )
        self.timeline_panel.set_scrub_enabled(not is_stopped)

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
        self._on_project_changed()

    def _on_segment_split(self, original_id: str, new_id: str) -> None:
        """Handle timeline split: update inspector and mark project dirty."""
        new_segment = self.project.get_segment(new_id)
        self.segment_panel.set_project(self.project)
        if new_segment is not None:
            self.segment_panel.set_segment(new_segment)
        self._on_project_changed()

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
        self._update_status()

    def _sync_timeline_positions(self) -> None:
        self.timeline_panel.sync_segment_positions()

    def _render_selected_segment(self) -> None:
        current = self.segment_panel.current_segment
        if current is None:
            return
        self._enqueue_segment(current)

    def _render_all_segments(self) -> None:
        for segment in self.project.sorted_segments():
            self._enqueue_segment(segment)

    def _on_preview_segment_requested(self, segment_id: str) -> None:
        """Render the segment (trimmed audio + current settings) and auto-play.

        A dedicated preview render job is enqueued.  The output is written to
        ``<app_root>/temps/preview_<segment_id>.mp4`` so it doesn't clobber
        the full-quality render at ``temps/segment_<segment_id>.mp4``.
        When the render finishes, ``_on_render_finished`` detects the
        preview flag and loads the video into the preview panel.
        """
        segment = self.project.get_segment(segment_id)
        if segment is None:
            return
        if not segment.audio_path:
            self.statusBar().showMessage("Segment has no audio source", 3000)
            return
        # Use a separate output path so preview doesn't clobber the real render.
        output_path = self._app_temps_dir() / f"preview_{segment.id}.mp4"
        dur = segment.duration_sec if segment.duration_sec and segment.duration_sec > 0 else None
        # Forward saved beat events so the preview matches the timeline
        # ticks exactly — same array path the full Render uses below.
        beat_times: list[float] = []
        for ev in (segment.beat_events or []):
            try:
                t = float(ev[0]) if isinstance(ev, (tuple, list)) else float(ev)
            except (TypeError, ValueError, IndexError):
                continue
            beat_times.append(t)
        job = RenderJob(
            segment_id=segment.id,
            mode=segment.mode,
            audio_path=segment.audio_path,
            output_path=str(output_path),
            render_settings=segment.render_settings or {},
            start_time_sec=segment.start_time_sec,
            duration_sec=dur,
            is_preview=True,
            output_width=self.project.output_width,
            output_height=self.project.output_height,
            output_fps=self.project.output_fps,
            trimmed_audio_path=segment.trimmed_audio_path,
            project_temps_dir=str(self._app_temps_dir()),
            beat_times=beat_times,
        )
        self._preview_segment_ids.add(segment.id)
        segment.render_status = RenderStatus.QUEUED
        segment.last_render_progress = 0
        self.render_service.enqueue(job)
        self.statusBar().showMessage(
            f"Preview rendering '{segment.name}' ({segment.start_time_sec:.1f}s"
            f"–{segment.end_time_sec:.1f}s)…", 3000
        )
        # Show "Preview 0%" overlay over the player so the user has a clear
        # visual cue that something is happening (matches the Render flow).
        self.preview_panel.set_render_progress(
            0,
            label="Preview",
            subtitle=f"{segment.name} — preparing…",
        )
        self.segment_panel.set_segment(segment)
        self._update_status()

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
        self.statusBar().showMessage(
            f"Rendering '{segment.name}' ({segment.start_time_sec:.1f}s"
            f"–{segment.end_time_sec:.1f}s)…", 3000
        )
        self._update_status()
        self.timeline_panel.refresh()
        self.segment_panel.set_segment(segment)

    def _on_render_progress(self, segment_id: str, progress: int) -> None:
        segment = self.project.get_segment(segment_id)
        if segment is None:
            return
        segment.render_status = RenderStatus.RENDERING
        segment.last_render_progress = max(0, min(100, int(progress)))
        is_preview = segment_id in self._preview_segment_ids
        label = "Preview" if is_preview else "Rendering"
        self.statusBar().showMessage(
            f"{label} {segment.name}: {progress}%", 1500
        )
        # Big overlay percentage centered on the player.
        self.preview_panel.set_render_progress(
            progress,
            label=label,
            subtitle=segment.name,
        )
        # Lightweight status-label-only refresh — never rebuilds form widgets,
        # so any spinbox the user might be editing during a long render is
        # preserved.  No-op when the user has selected a different segment.
        self.segment_panel.refresh_status_only(segment)
        self._update_status()

    def _on_render_finished(self, segment_id: str, output_path: str) -> None:
        segment = self.project.get_segment(segment_id)
        if segment is None:
            return
        is_preview = segment_id in self._preview_segment_ids
        self._preview_segment_ids.discard(segment_id)

        segment.render_status = RenderStatus.DONE
        segment.last_render_error = None
        segment.last_render_progress = 100
        segment.last_rendered_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        # Hide the rendering overlay; we're about to load the result.
        self.preview_panel.hide_render_overlay()

        # The rendered file's local time 0 corresponds to project time
        # ``segment.start_time_sec`` — pass it so the timeline's red
        # playhead tracks the project timeline (not 0..video_duration).
        offset = float(segment.start_time_sec or 0.0)
        if is_preview:
            # Preview renders don't overwrite the segment's official
            # video_path; they just show the result immediately in the
            # preview panel.
            self.statusBar().showMessage(
                f"Preview ready: {segment.name} — playing…", 5000
            )
            self.preview_panel.load_video(output_path, playhead_offset_sec=offset)
            self.timeline_panel.set_playhead(segment.start_time_sec)
        else:
            # Persist the absolute path so the segment knows where its
            # rendered MP4 lives even after the project file moves.
            segment.video_path = str(Path(output_path).resolve())
            self.statusBar().showMessage(
                f"Render done: {segment.name} — playing…", 5000
            )
            # Auto-load + play the rendered video, mirroring the Preview UX.
            # ``load_video`` queues playback so it kicks in as soon as the
            # player reports the media is ready.
            self.preview_panel.load_video(output_path, playhead_offset_sec=offset)
            self.timeline_panel.set_playhead(segment.start_time_sec)
            # Persist render metadata (video_path, status, timestamp) to
            # disk immediately so a crash / accidental close doesn't lose
            # the freshly rendered association.  Skipped for previews
            # since they don't mutate any saved state.
            self._auto_save_after_render(segment.name)

        self.segment_panel.set_segment(segment)
        self.timeline_panel.refresh()
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
        self._preview_segment_ids.discard(segment_id)
        segment.render_status = RenderStatus.ERROR
        segment.last_render_error = message
        segment.last_render_progress = 0
        self.preview_panel.hide_render_overlay()
        self.statusBar().showMessage(f"Render failed: {segment.name}", 5000)
        self.segment_panel.set_segment(segment)
        self._update_status()

    def _update_status(self) -> None:
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

