# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for SSCStudio.exe (main GUI)
#
# Build with:
#   pyinstaller ssc_studio.spec
# Or use build_dist.py which handles everything automatically.

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# ── Data files ────────────────────────────────────────────────────────────────
datas = [
    # QSS stylesheet
    (str(ROOT / "studio" / "resources" / "styles.qss"), "studio/resources"),
]

# Include all .qss / .png / .svg inside studio/resources if they exist
resources_dir = ROOT / "studio" / "resources"
if resources_dir.exists():
    for f in resources_dir.rglob("*"):
        if f.is_file():
            rel = str(f.parent.relative_to(ROOT))
            if (str(f), rel) not in datas:
                datas.append((str(f), rel))

# Pydantic v2 uses compiled validators — include them
datas += collect_data_files("pydantic")
datas += collect_data_files("PySide6")

# ── Hidden imports ─────────────────────────────────────────────────────────────
hiddenimports = [
    # Studio modules
    "studio.app",
    "studio.auth.auth_service",
    "studio.auth.login_window",
    "studio.editor.main_window",
    "studio.core_bridge",
    "studio.core_bridge.beat_detect_service",
    "studio.core_bridge.render_service",
    "studio.core_bridge.audio_trim_service",
    "studio.models",
    "studio.models.segment",
    # src helpers used by studio
    "src.bundle_paths",
    "src.live_renderer",
    # Third-party that PyInstaller may miss
    "pydantic",
    "pydantic.v1",
    "pydantic_core",
    "keyring",
    "keyring.backends",
    "keyring.backends.Windows",
    "keyring.backends.fail",
    "httpx",
    "cv2",
    "PIL",
    "PIL.Image",
    "pydub",
    "numpy",
    "scipy",
    "scipy.signal",
    "scipy.interpolate",
    "librosa",
    "soundfile",
    "audioread",
    "srt",
    "pysrt",
    "requests",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
]

hiddenimports += collect_submodules("keyring")
hiddenimports += collect_submodules("pydantic")
hiddenimports += collect_submodules("librosa")

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    [str(ROOT / "studio" / "app.py")],
    pathex=[str(ROOT), str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "test", "unittest"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SSCStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SSCStudio",
)
