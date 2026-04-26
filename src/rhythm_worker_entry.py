"""Entry point for rhythm_worker.exe (PyInstaller sub-process binary).

This thin wrapper puts src/ on sys.path (matching the dev "python rhythm.py"
behaviour where the script's directory is automatically on sys.path) and then
delegates to rhythm.py's __main__ block.

The worker is built as a separate --console executable so that the GUI
(SSCStudio.exe) can spawn it the same way beat_detect_service and
render_service spawn "python rhythm.py" in development.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path


def main() -> None:
    # In the frozen bundle, _MEIPASS contains the extracted package tree.
    # src/ lives directly inside _MEIPASS; put it on sys.path so that the
    # sibling imports inside rhythm.py (from stickman import ...) resolve.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        src_dir = meipass / "src"
        if src_dir.exists() and str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        # Also add meipass root so top-level packages (studio, src) resolve.
        if str(meipass) not in sys.path:
            sys.path.insert(0, str(meipass))

    # Set the ffmpeg binary path env var so rhythm.py's subprocess calls pick
    # up the bundled binary automatically (ffmpeg-python respects FFMPEG_BINARY).
    bundle_dir = Path(sys.executable).parent
    ffmpeg_bin = bundle_dir / "ffmpeg.exe"
    if ffmpeg_bin.exists():
        os.environ.setdefault("FFMPEG_BINARY", str(ffmpeg_bin))
        # Also prepend to PATH so plain `ffmpeg` subprocess calls find it.
        os.environ["PATH"] = str(bundle_dir) + os.pathsep + os.environ.get("PATH", "")

    # Import and run rhythm as __main__
    import runpy
    runpy.run_module("src.rhythm", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
