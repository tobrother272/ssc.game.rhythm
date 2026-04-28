# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for rhythm_worker.exe (render/beat-detect subprocess)
#
# This binary accepts the same CLI as rhythm.py and is spawned by
# SSCStudio.exe instead of "python rhythm.py".

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

datas = []
datas += collect_data_files("librosa")
datas += collect_data_files("pydantic")

hiddenimports = [
    "src.rhythm",
    "src.stickman",
    "src.bundle_paths",
    "src.authorization",
    # Heavy scientific stack
    "numpy",
    "scipy",
    "scipy.signal",
    "scipy.fft",
    "scipy.interpolate",
    "librosa",
    "librosa.core",
    "librosa.feature",
    "librosa.onset",
    "librosa.beat",
    "soundfile",
    "audioread",
    "cv2",
    "PIL",
    "PIL.Image",
    "pydub",
    "ffmpeg",
    "requests",
    "pysrt",
    "pydantic",
    "pydantic_core",
    "trimesh",
]

hiddenimports += collect_submodules("librosa")
hiddenimports += collect_submodules("scipy")

a = Analysis(
    [str(ROOT / "src" / "rhythm_worker_entry.py")],
    pathex=[str(ROOT), str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PySide6", "matplotlib", "test"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rhythm_worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # CLI worker — keep console for stdout progress
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
    name="rhythm_worker",
)
