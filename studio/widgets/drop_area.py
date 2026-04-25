"""Generic drop helper utilities."""

from __future__ import annotations

from pathlib import Path


def urls_to_paths(urls: list) -> list[Path]:
    """Convert Qt urls to local file paths."""
    paths: list[Path] = []
    for url in urls:
        if url.isLocalFile():
            paths.append(Path(url.toLocalFile()))
    return paths

