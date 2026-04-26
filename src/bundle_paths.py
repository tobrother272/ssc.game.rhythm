"""Bundle-aware path helpers for PyInstaller packaging.

When the app runs as a frozen PyInstaller bundle:
  - sys.frozen == True
  - sys._MEIPASS points to the extracted temp directory (onefile) or
    the application directory (onedir)
  - ffmpeg.exe and rhythm_worker.exe live next to the main executable

In development (unfrozen), behaviour is identical to the old hard-coded
shutil.which / sys.executable / repo_root logic.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _bundle_dir() -> Path | None:
    """Return the directory containing bundled binaries, or None if not frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return None


def find_ffmpeg() -> str:
    """Return the absolute path to the ffmpeg binary.

    Search order:
      1. Bundled ffmpeg.exe sitting next to the .exe (frozen only).
      2. PATH (shutil.which).
      3. Common Windows install locations.
    """
    bundle = _bundle_dir()
    if bundle is not None:
        candidate = bundle / "ffmpeg.exe"
        if candidate.exists():
            return str(candidate)

    found = shutil.which("ffmpeg")
    if found:
        return found

    win_candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ]
    for c in win_candidates:
        if Path(c).exists():
            return c

    raise FileNotFoundError(
        "ffmpeg not found. Install ffmpeg and make sure it is on PATH."
    )


def find_ffprobe() -> str:
    """Return the absolute path to the ffprobe binary."""
    bundle = _bundle_dir()
    if bundle is not None:
        candidate = bundle / "ffprobe.exe"
        if candidate.exists():
            return str(candidate)

    found = shutil.which("ffprobe")
    if found:
        return found

    win_candidates = [
        r"C:\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffprobe.exe",
    ]
    for c in win_candidates:
        if Path(c).exists():
            return c

    raise FileNotFoundError("ffprobe not found.")


def get_rhythm_command(repo_root: Path) -> list[str]:
    """Return the argv prefix + script path for spawning rhythm.py.

    In a frozen bundle the rhythm_worker.exe handles everything and no
    Python interpreter flag is needed.  In development the current
    interpreter is used directly.

    Usage::
        cmd = get_rhythm_command(repo_root) + ["-i", audio, "-o", output, ...]
    """
    bundle = _bundle_dir()
    if bundle is not None:
        worker = bundle / "rhythm_worker.exe"
        if worker.exists():
            return [str(worker)]
        # Fallback: should not happen in a correct build, but surface clearly.
        raise FileNotFoundError(
            f"rhythm_worker.exe not found next to {sys.executable}. "
            "The distribution may be corrupted."
        )

    rhythm_script = repo_root / "src" / "rhythm.py"
    return [sys.executable, "-u", str(rhythm_script)]
