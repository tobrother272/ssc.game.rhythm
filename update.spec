# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for update.exe (standalone updater)
#
# IMPORTANT: This MUST be built as a one-file binary (single self-extracting
# update.exe) — NOT one-folder.  Reason: when packaged in one-folder mode,
# update.exe shares the host application's `_internal/` directory and
# therefore holds an open handle on `_internal/base_library.zip` while
# running.  That makes it impossible to swap that file (WinError 32) even
# after SSCStudio.exe exits, because update.exe itself is the locker.
#
# In one-file mode, PyInstaller extracts dependencies to a private temp
# directory (%TEMP%/_MEIxxxxx) at startup, so the updater never touches
# the host app's `_internal/` folder at all.

from pathlib import Path

ROOT = Path(SPECPATH)

# Updater is intentionally a pure-stdlib CLI tool: no PySide6, no cv2, etc.
# Keeping it minimal makes the one-file binary small (~5MB) and fast to
# extract on every run.
datas = []

hiddenimports = [
    "updater.main",
    "updater.manifest",
    "updater.update_logic",
    "updater.self_relaunch",
]

a = Analysis(
    [str(ROOT / "updater" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "cv2",
        "numpy",
        "scipy",
        "librosa",
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtMultimedia",
        "shiboken6",
        "PIL",
        "pydub",
        "matplotlib",
        "tkinter",
        "test",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# One-file mode: bundle scripts + binaries + datas all into a single exe.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="update",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # Keep console enabled so diagnostic CLI modes
    # (--check-only/--download-only/--version) print output in terminal.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
