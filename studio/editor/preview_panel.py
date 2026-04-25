"""Preview player panel using Qt multimedia."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from studio.models import MediaItem, Segment


def format_ms(ms: int) -> str:
    """Format milliseconds to mm:ss string."""
    seconds = max(0, ms // 1000)
    mm, ss = divmod(seconds, 60)
    return f"{mm:02d}:{ss:02d}"


class PreviewPanel(QWidget):
    """Media preview with source selector and playback controls."""

    playhead_changed = Signal(float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._selected_media: MediaItem | None = None
        self._selected_segment: Segment | None = None
        # True if user clicked Play but source is still loading.
        self._pending_play = False
        # Position (ms) to seek to once media becomes ready; -1 = none pending.
        self._pending_seek_ms: int = -1
        # Tracks current media URL so we can reload if play() after end fails.
        self._current_url: QUrl = QUrl()
        # Becomes True once LoadedMedia/BufferedMedia fires for the current source.
        # Used to prevent intermediate Buffering transitions from re-disabling the button.
        self._media_ready = False
        # Number of times we've already retried the current URL (for stuck loads).
        self._load_retries = 0
        # Offset (project timeline seconds) corresponding to position 0 of
        # the currently-loaded media.  Raw audio = 0.  A segment-rendered
        # video = segment.start_time_sec, so the project-level red playhead
        # tracks the *project* time as the video plays, not the video's
        # internal 0..duration_sec range.
        self._playhead_offset_sec: float = 0.0
        self._build_ui()

    def set_source_media(self, media: MediaItem | None) -> None:
        """Set selected media source and load it for preview."""
        self._selected_media = media
        # Raw media items always live at project-time 0.
        self._playhead_offset_sec = 0.0
        if self.source_combo.currentData() == "media":
            self._load_active_source()

    def set_source_segment(self, segment: Segment | None) -> None:
        """Set selected segment and load the most useful source for preview.

        Priority:
        1. **Rendered video on disk** (``segment.video_path`` exists AND
           the file actually exists) — this is the user-friendly default.
           Once a segment has been rendered, clicking it should play the
           video, not the raw audio.  This holds across project reopens:
           if the saved ``.htproj`` brings back a ``video_path`` whose
           file is still in ``temps/``, we auto-load it.
        2. Raw source audio — fallback when no render exists yet.
        3. Nothing → clear.

        The source combo is synced to whatever ends up loaded, so the
        dropdown honestly reflects the player content.  The user can still
        manually switch to "Selected media" (= raw media-library item) or
        "Main timeline (stitched)" via the combo; those paths run through
        ``_load_active_source`` and override this default.
        """
        self._selected_segment = segment

        if segment is None:
            self.clear()
            return

        rendered_ready = bool(
            segment.video_path and Path(segment.video_path).exists()
        )

        if rendered_ready:
            # Sync combo so the dropdown matches what's actually playing.
            self._set_source_combo_silently("segment")
            # The rendered video starts at project-time = segment.start.
            self._playhead_offset_sec = float(segment.start_time_sec or 0.0)
            self._load_path(segment.video_path)  # type: ignore[arg-type]
            return

        if segment.audio_path:
            # Falling back to raw audio because no render exists yet.
            self._set_source_combo_silently("media")
            self._playhead_offset_sec = 0.0
            self._load_path(segment.audio_path)
            return

        self.clear()

    def _set_source_combo_silently(self, data_value: str) -> None:
        """Set the source combo to the given userData value without firing
        ``currentIndexChanged`` (which would re-trigger ``_load_active_source``
        and double-load the source we're already loading)."""
        idx = self.source_combo.findData(data_value)
        if idx < 0 or idx == self.source_combo.currentIndex():
            return
        self.source_combo.blockSignals(True)
        try:
            self.source_combo.setCurrentIndex(idx)
        finally:
            self.source_combo.blockSignals(False)

    def clear(self) -> None:
        """Clear loaded media and reset player state."""
        self._pending_play = False
        self._media_ready = False
        self._current_url = QUrl()
        self._playhead_offset_sec = 0.0
        self.player.stop()
        self.player.setSource(QUrl())
        self._show_empty("No preview source selected")
        self._set_play_button_state(playing=False)
        self.play_button.setEnabled(False)
        self.seek_slider.setRange(0, 0)
        self.time_label.setText("00:00 / 00:00")

    def _build_ui(self) -> None:
        self.setObjectName("PanelRoot")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header strip - "Player"
        header = QWidget()
        header.setObjectName("panelHeader")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(10, 6, 10, 6)
        header_row.setSpacing(8)
        title = QLabel("Player")
        title.setObjectName("panelTitle")
        header_row.addWidget(title)
        header_row.addStretch()
        self.source_combo = QComboBox()
        self.source_combo.addItem("Selected media", "media")
        self.source_combo.addItem("Selected segment (rendered)", "segment")
        self.source_combo.addItem("Main timeline (stitched)", "timeline")
        self.source_combo.setFixedWidth(200)
        self.source_combo.currentIndexChanged.connect(self._load_active_source)
        header_row.addWidget(self.source_combo)
        layout.addWidget(header)

        # Body with padding
        body = QWidget()
        body.setObjectName("PanelRoot")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 10)
        body_layout.setSpacing(8)
        layout.addWidget(body, 1)

        # Stage uses a QStackedWidget to overlay:  0=video  1=loading  2=empty
        self.stage_stack = QStackedWidget()
        self.stage_stack.setObjectName("previewStage")
        self.stage_stack.setMinimumHeight(220)

        # Page 0 — video player
        self.video_widget = QVideoWidget()
        self.stage_stack.addWidget(self.video_widget)          # index 0

        # Page 1 — loading indicator
        loading_page = QWidget()
        loading_page.setStyleSheet("background:#0a0a0a;")
        lp_layout = QVBoxLayout(loading_page)
        lp_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label = QLabel("Loading media")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.setStyleSheet(
            "color:#6b6b6b; font-size:13px; background:transparent;"
        )
        lp_layout.addWidget(self.loading_label)
        self.stage_stack.addWidget(loading_page)               # index 1

        # Page 2 — empty / no source
        empty_page = QWidget()
        empty_page.setStyleSheet("background:#0a0a0a;")
        ep_layout = QVBoxLayout(empty_page)
        ep_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label = QLabel("No preview source selected")
        self.empty_label.setObjectName("previewEmptyLabel")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ep_layout.addWidget(self.empty_label)
        self.stage_stack.addWidget(empty_page)                 # index 2

        # Page 3 — render-progress page (big "Rendering NN%" centered).
        # Implemented as a real stack page (rather than a floating child of
        # stage_stack) because: (a) QStackedLayout only manages registered
        # pages, so a free-standing child wouldn't get sized/shown reliably,
        # and (b) on Windows QVideoWidget uses a native HWND that regular
        # Qt children cannot overlay.  Switching to a dedicated page side-
        # steps both issues completely.
        render_page = QWidget()
        render_page.setObjectName("previewRenderingPage")
        render_page.setStyleSheet(
            "QWidget#previewRenderingPage { background: #0a0a0a; }"
            "QLabel#renderOverlayTitle { color:#5cc8ff; font-size:28px; "
            "font-weight:600; background:transparent; }"
            "QLabel#renderOverlaySubtitle { color:#aaaaaa; font-size:13px; "
            "background:transparent; }"
        )
        rp_layout = QVBoxLayout(render_page)
        rp_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rp_layout.setContentsMargins(20, 20, 20, 20)
        rp_layout.setSpacing(8)
        self._render_overlay_title = QLabel("Rendering 0%")
        self._render_overlay_title.setObjectName("renderOverlayTitle")
        self._render_overlay_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._render_overlay_subtitle = QLabel("")
        self._render_overlay_subtitle.setObjectName("renderOverlaySubtitle")
        self._render_overlay_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rp_layout.addStretch(1)
        rp_layout.addWidget(self._render_overlay_title)
        rp_layout.addWidget(self._render_overlay_subtitle)
        rp_layout.addStretch(1)
        self.stage_stack.addWidget(render_page)                # index 3
        self._render_page_index = 3

        self.stage_stack.setCurrentIndex(2)  # start on empty page
        body_layout.addWidget(self.stage_stack, 1)

        # Dots animation timer for loading label.
        self._loading_dots = 0
        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(450)
        self._loading_timer.timeout.connect(self._tick_loading_dots)

        # Watchdog: if a load takes too long (Qt FFmpeg stuck on MP3 probe etc.)
        # we retry once. Single-shot, started by _load_path, cancelled by
        # _on_media_status_changed when the media becomes ready / invalid.
        self._load_watchdog = QTimer(self)
        self._load_watchdog.setSingleShot(True)
        self._load_watchdog.setInterval(12000)  # 12s — enough for ~10 min MP3 probe.
        self._load_watchdog.timeout.connect(self._on_load_watchdog_fired)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.6)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.player.errorOccurred.connect(self._on_player_error)

        control_row = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self._toggle_play)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.player.stop)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self.player.setPosition)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet(
            "color:#c0c0c0;font-family:Consolas,Menlo,monospace;"
        )
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(60)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        self.full_button = QPushButton("Fullscreen")
        self.full_button.clicked.connect(self._toggle_fullscreen)

        self.play_button.setEnabled(False)

        control_row.addWidget(self.play_button)
        control_row.addWidget(self.stop_button)
        control_row.addWidget(self.seek_slider, 1)
        control_row.addWidget(self.time_label)
        vol_label = QLabel("Vol")
        vol_label.setStyleSheet("color:#8a8a8a;")
        control_row.addWidget(vol_label)
        control_row.addWidget(self.volume_slider)
        control_row.addWidget(self.full_button)
        body_layout.addLayout(control_row)

    def _load_active_source(self) -> None:
        source = self.source_combo.currentData()
        if source == "media":
            if self._selected_media is None:
                self.clear()
                return
            # Raw media plays at project-time 0.
            self._playhead_offset_sec = 0.0
            self._load_path(self._selected_media.source_path)
            return
        if source == "segment":
            if self._selected_segment is None or not self._selected_segment.video_path:
                self.clear()
                self.empty_label.setText("Chua render")
                return
            # Rendered segment video → its position 0 is project-time
            # segment.start_time_sec, so the timeline playhead lines up.
            self._playhead_offset_sec = float(
                self._selected_segment.start_time_sec or 0.0
            )
            self._load_path(self._selected_segment.video_path)
            return
        self.clear()
        self.empty_label.setText("Main timeline preview is TODO in skeleton")

    def _load_path(self, raw_path: str, *, force_reload: bool = False) -> None:
        path = Path(raw_path)
        if not path.exists():
            self.clear()
            self._show_empty("Source not found")
            return

        url = QUrl.fromLocalFile(str(path.resolve()))

        # Dedup: same URL already requested. Avoid calling setSource() again
        # because each setSource() cancels and restarts the probe — for VBR
        # MP3s this can take several seconds, making the UI feel stuck when
        # the user selects the same segment / audio twice.
        #
        # IMPORTANT: dedup is unsafe when the file content has changed under
        # the same path (e.g. after a re-render that overwrites
        # `segment_<id>.mp4` in place).  In that case ffmpeg keeps the old
        # parsed sample-tables / extradata while reading new bytes — that's
        # exactly the "Invalid NAL unit size" / "Reserved bit set" cascade
        # we hit after auto-loading a freshly rendered video.  Callers that
        # know the file just changed must pass force_reload=True to bypass.
        if url == self._current_url and not force_reload:
            if self._media_ready:
                # Source is fully loaded → just show the player and re-enable Play.
                self._show_video()
                self.play_button.setEnabled(True)
            else:
                # A load is already in-flight — leave it running, keep loading UI.
                self._show_loading()
            return

        # Genuinely new source (or forced reload of a possibly-overwritten
        # file) — fully reset and re-probe.  Detaching the source first
        # makes the backend release the old file/sample-tables so the next
        # setSource() actually re-reads the bytes from disk.
        if force_reload:
            # Caller (load_video) has already set _pending_play /
            # _pending_seek_ms for the new source.  Detaching the player
            # below fires a transient NoMedia status, whose handler would
            # otherwise clobber those flags — snapshot and restore them
            # around the detach so auto-play after render finish actually
            # works without the user clicking Play.
            saved_pending_play = self._pending_play
            saved_pending_seek_ms = self._pending_seek_ms
            self.player.stop()
            self.player.setSource(QUrl())
            self._pending_play = saved_pending_play
            self._pending_seek_ms = saved_pending_seek_ms
        else:
            self._pending_play = False
            self._pending_seek_ms = -1
        self._media_ready = False
        self._load_retries = 0
        self.play_button.setEnabled(False)
        self._set_play_button_state(playing=False)
        self._current_url = url
        self._show_loading()
        self.player.setSource(url)
        self._load_watchdog.start()

    def _toggle_play(self) -> None:
        state = self.player.playbackState()
        status = self.player.mediaStatus()
        MS = QMediaPlayer.MediaStatus
        PS = QMediaPlayer.PlaybackState

        # Case 1: currently playing -> pause.
        if state == PS.PlayingState:
            self.player.pause()
            return

        # Case 2: source still loading -> schedule auto-play.
        if status in {MS.LoadingMedia, MS.BufferingMedia, MS.StalledMedia}:
            self._pending_play = True
            self._set_play_button_state(playing=True)
            return

        # Case 3: end of media or stopped at end -> rewind then play.
        duration = self.player.duration()
        at_end = (
            status == MS.EndOfMedia
            or (duration > 0 and self.player.position() >= duration - 50)
        )
        if at_end:
            # Some backends (Windows ffmpeg) won't auto-rewind; reset position.
            self.player.setPosition(0)

        # Case 4: no media loaded but we have a cached url -> reattach source.
        if status == MS.NoMedia and not self._current_url.isEmpty():
            self._pending_play = True
            self._set_play_button_state(playing=True)
            self.player.setSource(self._current_url)
            return

        self.player.play()

    def _on_position_changed(self, value: int) -> None:
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(value)
        self.seek_slider.blockSignals(False)
        self.time_label.setText(
            f"{format_ms(value)} / {format_ms(self.player.duration())}"
        )
        # Translate media-local time → project timeline time so the timeline
        # red playhead tracks correctly even when we're playing a rendered
        # segment video that starts mid-project (offset = segment.start).
        self.playhead_changed.emit(value / 1000.0 + self._playhead_offset_sec)

    def _on_duration_changed(self, value: int) -> None:
        self.seek_slider.setRange(0, max(0, value))
        self.time_label.setText(
            f"{format_ms(self.player.position())} / {format_ms(value)}"
        )

    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        MS = QMediaPlayer.MediaStatus

        if status in {MS.LoadedMedia, MS.BufferedMedia}:
            # Source is ready — switch to video page and enable playback.
            self._load_watchdog.stop()
            self._media_ready = True
            self._load_retries = 0
            self._show_video()
            self.play_button.setEnabled(True)
            if self._pending_seek_ms >= 0:
                self.player.setPosition(self._pending_seek_ms)
                self._pending_seek_ms = -1
            if self._pending_play:
                self._pending_play = False
                self.player.play()
            return

        if status == MS.EndOfMedia:
            # File finished - keep button enabled so user can replay.
            self._load_watchdog.stop()
            self._media_ready = True
            self._pending_play = False
            self.play_button.setEnabled(True)
            self._set_play_button_state(playing=False)
            return

        if status == MS.InvalidMedia:
            self._load_watchdog.stop()
            self._media_ready = False
            self._pending_play = False
            self._set_play_button_state(playing=False)
            self.play_button.setEnabled(False)
            self._show_empty("Invalid media")
            return

        if status == MS.NoMedia:
            # NoMedia fires transiently between setSource(QUrl()) and
            # setSource(real_url) inside _load_path's force-reload path.
            # If a real URL is queued (current_url is non-empty), do NOT
            # touch the pending-play / pending-seek flags — the caller
            # (load_video) needs them to fire auto-play once LoadedMedia
            # arrives.  Otherwise this is a genuine "no source" state.
            self._media_ready = False
            self._set_play_button_state(playing=False)
            if self._current_url.isEmpty():
                self._pending_play = False
                self._pending_seek_ms = -1
                self.play_button.setEnabled(False)
            return

        # LoadingMedia / BufferingMedia / StalledMedia
        # Only disable and show loading page during the *initial* load.
        # Once _media_ready is True, don't interrupt playback view.
        if not self._media_ready:
            self.play_button.setEnabled(False)
            self._show_loading()

    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        """Keep play/pause button label in sync with the actual player state."""
        is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._set_play_button_state(playing=is_playing)
        # Ensure button stays enabled whenever we have a usable source.
        if self._media_ready or is_playing:
            self.play_button.setEnabled(True)

    def _show_loading(self) -> None:
        """Switch stage to loading page and start dot animation."""
        self._loading_dots = 0
        self.loading_label.setText("Loading media")
        self.stage_stack.setCurrentIndex(1)
        if not self._loading_timer.isActive():
            self._loading_timer.start()

    def _show_video(self) -> None:
        """Switch stage to video/audio player page."""
        self._loading_timer.stop()
        self.stage_stack.setCurrentIndex(0)

    def _show_empty(self, message: str = "No preview source selected") -> None:
        """Switch stage to empty page with given message."""
        self._loading_timer.stop()
        self.empty_label.setText(message)
        self.stage_stack.setCurrentIndex(2)

    def _tick_loading_dots(self) -> None:
        """Animate '...' on loading label."""
        self._loading_dots = (self._loading_dots + 1) % 4
        self.loading_label.setText("Loading media" + "." * self._loading_dots)

    def _on_player_error(self, error: QMediaPlayer.Error, msg: str) -> None:
        self._load_watchdog.stop()
        self._pending_play = False
        self.play_button.setEnabled(False)
        self._show_empty(f"Error: {msg}")

    def _on_load_watchdog_fired(self) -> None:
        """Triggered when a load() has been pending too long.

        Most often caused by Qt's ffmpeg backend stalling on VBR MP3 probes.
        We retry the same URL exactly once before giving up so the user isn't
        stuck on "Loading media..." indefinitely.
        """
        if self._media_ready or self._current_url.isEmpty():
            return
        if self._load_retries >= 1:
            print(
                f"[preview] watchdog: media still not ready after retry: "
                f"{self._current_url.toLocalFile()}"
            )
            self._show_empty(
                "Loading is taking longer than expected.\n"
                "Try removing and re-adding the media."
            )
            return
        self._load_retries += 1
        print(
            f"[preview] watchdog: retrying stuck load: "
            f"{self._current_url.toLocalFile()}"
        )
        url = self._current_url
        self.player.setSource(QUrl())
        self.player.setSource(url)
        self._load_watchdog.start()

    def _set_play_button_state(self, *, playing: bool) -> None:
        self.play_button.setText("Pause" if playing else "Play")

    def _on_volume_changed(self, value: int) -> None:
        self.audio_output.setVolume(value / 100.0)

    def play(self) -> None:
        """Start playback; queues if media is still loading."""
        if self._media_ready:
            self.player.play()
            self._set_play_button_state(playing=True)
        else:
            self._pending_play = True

    def load_video(self, path: str, *, playhead_offset_sec: float = 0.0) -> None:
        """Load an arbitrary video file and play it from the beginning.

        Used after a preview/render completes to show the rendered output.
        Any existing source is replaced; playback starts automatically once
        the player reports the media is ready.

        ``playhead_offset_sec`` is the project-timeline second that maps to
        position 0 of the loaded file.  For a rendered segment video this
        is ``segment.start_time_sec`` so the project timeline's red playhead
        tracks correctly while the video plays.  For arbitrary stand-alone
        videos pass 0.

        ``force_reload=True`` is passed to ``_load_path`` because the render
        pipeline overwrites ``segment_<id>.mp4`` / ``preview_<id>.mp4`` in
        place — without forcing a fresh probe, ffmpeg would keep the old
        sample-tables and produce "Invalid NAL unit size" decode errors.
        """
        self._pending_play = True
        self._pending_seek_ms = 0
        self._playhead_offset_sec = float(playhead_offset_sec)
        self._load_path(path, force_reload=True)

    # ------------------------------------------------------------------
    # Render-progress overlay API
    # ------------------------------------------------------------------
    def set_render_progress(
        self,
        pct: Optional[int],
        *,
        label: str = "Rendering",
        subtitle: str = "",
    ) -> None:
        """Show a "Rendering NN%" page on the player while a render runs.

        Switches the stage stack to the dedicated rendering page (a real
        page, not a floating overlay — see _build_ui).  Pass ``pct=None``
        or pct >= 100 to hide it (caller is expected to follow up with
        ``load_video(...)`` to show the result, or with
        ``hide_render_overlay()`` on failure).
        """
        if pct is None:
            self.hide_render_overlay()
            return
        try:
            pct_int = max(0, min(100, int(pct)))
        except (TypeError, ValueError):
            pct_int = 0
        if pct_int >= 100:
            self.hide_render_overlay()
            return
        self._render_overlay_title.setText(f"{label} {pct_int}%")
        self._render_overlay_subtitle.setText(subtitle)
        self.stage_stack.setCurrentIndex(self._render_page_index)

    def hide_render_overlay(self) -> None:
        """Leave the rendering page if it's currently shown.

        Falls back to the most appropriate page: the video page if a source
        is loaded, otherwise the empty page.  Callers who follow up with
        ``load_video(...)`` don't strictly need this — the next
        ``setSource`` will move the stack to the loading page on its own —
        but it's harmless to call and matters on render failure where no
        load follows.
        """
        if self.stage_stack.currentIndex() != self._render_page_index:
            return
        if self._media_ready and not self._current_url.isEmpty():
            self._show_video()
        else:
            self._show_empty("No preview source selected")

    def seek_to_seconds(self, time_sec: float) -> None:
        """Seek media player to the given project-timeline time (in seconds).

        Translates project time → media-local time using the current
        playhead offset.  This way a click on the project timeline at
        t = 35.0 sec lands on the right frame of a segment video whose
        position 0 corresponds to t = segment.start_time_sec.

        If the media source is still loading, the seek is queued and applied
        automatically once the player signals it is ready.
        """
        local_sec = max(0.0, time_sec - self._playhead_offset_sec)
        ms = int(local_sec * 1000)
        if self._media_ready:
            self.player.setPosition(ms)
        else:
            # Queue it — applied in _on_media_status_changed when ready.
            self._pending_seek_ms = ms

    def _toggle_fullscreen(self) -> None:
        """Toggle preview video fullscreen mode."""
        self.video_widget.setFullScreen(not self.video_widget.isFullScreen())

