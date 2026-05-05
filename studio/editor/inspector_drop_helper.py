"""Shared helpers for media drag-drop in inspector layer sections."""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtWidgets import QGroupBox

from studio.editor.media_library import MEDIA_ID_MIME

HOVER_STYLE = (
    "QGroupBox {"
    " border: 2px dashed #00AAFF;"
    " background-color: rgba(0, 170, 255, 25);"
    "}"
)
REJECT_STYLE = "QGroupBox { border: 2px solid #FF4444; }"


def get_media_from_drop(event, project) -> Optional[Tuple[object, str]]:
    """Return (MediaItem, kind) for a valid media-id drop or None."""
    if project is None:
        return None
    mime = event.mimeData()
    if mime is None or not mime.hasFormat(MEDIA_ID_MIME):
        return None
    raw = bytes(mime.data(MEDIA_ID_MIME)).decode("utf-8", errors="ignore").strip()
    if not raw:
        return None
    media = project.get_media(raw)
    if media is None:
        return None
    kind = str(getattr(media.kind, "value", media.kind)).strip().lower()
    return media, kind


def set_drop_highlight(widget: QGroupBox, on: bool) -> None:
    """Show/clear cyan hover highlight for a section."""
    if on:
        widget.setStyleSheet(HOVER_STYLE)
    else:
        widget.setStyleSheet("")


def set_drop_reject_flash(widget: QGroupBox) -> None:
    """Show red reject flash on drop target."""
    widget.setStyleSheet(REJECT_STYLE)

