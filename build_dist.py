"""
SSC Studio — Automated Distribution Builder
============================================

Usage
-----
    python build_dist.py [--skip-obfuscate] [--skip-ffmpeg] [--clean]

What it does
------------
1.  Installs / upgrades required build tools (pyarmor, pyinstaller).
2.  Downloads the static Windows FFmpeg binary (ffmpeg.exe + ffprobe.exe)
    into build/ffmpeg/ if not already present.
3.  Obfuscates the source tree with PyArmor into build/obf/.
4.  Runs PyInstaller twice:
      a) rhythm_worker.spec  → dist/rhythm_worker/  (CLI subprocess worker)
      b) ssc_studio.spec     → dist/SSCStudio/       (Qt GUI)
5.  Merges both dist trees into dist/SSCStudio/ so a single folder
    contains SSCStudio.exe + rhythm_worker.exe + ffmpeg.exe.
6.  Produces dist/SSCStudio.zip — the distributable archive.

Requirements
------------
    pip install pyarmor pyinstaller
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist" / "SSCStudio"
BUILD_DIR = ROOT / "build"
OBF_DIR = BUILD_DIR / "obf"
FFMPEG_DIR = BUILD_DIR / "ffmpeg"

# ── FFmpeg Windows static build (BtbN/FFmpeg-Builds, GPL essentials) ──────────
FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)
FFMPEG_BINARIES = ["ffmpeg.exe", "ffprobe.exe"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def banner(msg: str) -> None:
    print(f"\n{'='*70}\n  {msg}\n{'='*70}")


def run(cmd: list[str], **kw) -> None:
    """Run a command, streaming output, raise on failure."""
    print("  $", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, **kw)


def pip_install(*packages: str) -> None:
    run([sys.executable, "-m", "pip", "install", "--upgrade", *packages])


def _find_script(name: str) -> str:
    """Locate a script installed by pip (handles --user installs not on PATH)."""
    found = shutil.which(name)
    if found:
        return found
    # pip --user puts scripts here on Windows
    import sysconfig, site
    candidates = [
        Path(sysconfig.get_path("scripts")) / f"{name}.exe",
        Path(site.getusersitepackages()).parent / "Scripts" / f"{name}.exe",
        Path(sys.executable).parent / f"{name}.exe",
        Path(sys.executable).parent / "Scripts" / f"{name}.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    raise FileNotFoundError(
        f"'{name}' executable not found. Make sure it is installed and its "
        "Scripts directory is accessible."
    )


# ── Step 1: Install build tools ───────────────────────────────────────────────

def ensure_build_tools() -> None:
    banner("Step 1 — Installing build tools")
    pip_install("pyarmor>=8.5", "pyinstaller>=6.0")
    # Verify they are findable after install
    for tool in ("pyarmor", "pyinstaller"):
        try:
            path = _find_script(tool)
            print(f"  {tool}: {path}")
        except FileNotFoundError as exc:
            print(f"  [WARN] {exc}")


# ── Step 2: Download FFmpeg ────────────────────────────────────────────────────

def _download_with_progress(url: str, dest: Path) -> None:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk = 65536
        data = io.BytesIO()
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            data.write(buf)
            downloaded += len(buf)
            if total:
                pct = downloaded * 100 // total
                print(f"  Downloading… {pct}%", end="\r", flush=True)
        print()
    dest.write_bytes(data.getvalue())


def ensure_ffmpeg() -> None:
    banner("Step 2 — Bundling FFmpeg")
    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)

    missing = [b for b in FFMPEG_BINARIES
               if not (FFMPEG_DIR / b).exists()]
    if not missing:
        print("  FFmpeg binaries already present — skipping download.")
        return

    zip_path = FFMPEG_DIR / "ffmpeg_download.zip"
    print(f"  Downloading FFmpeg from:\n  {FFMPEG_URL}")
    try:
        _download_with_progress(FFMPEG_URL, zip_path)
    except URLError as exc:
        print(f"\n  [WARN] Download failed: {exc}")
        print("  Please manually place ffmpeg.exe and ffprobe.exe in:")
        print(f"  {FFMPEG_DIR}")
        return

    print("  Extracting binaries…")
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            fname = Path(member).name
            if fname in FFMPEG_BINARIES:
                data = zf.read(member)
                (FFMPEG_DIR / fname).write_bytes(data)
                print(f"  Extracted {fname} ({len(data)//1024} KB)")

    zip_path.unlink(missing_ok=True)

    still_missing = [b for b in FFMPEG_BINARIES
                     if not (FFMPEG_DIR / b).exists()]
    if still_missing:
        print(f"  [WARN] Could not find {still_missing} inside the zip.")
        print("  Check the URL or download manually.")


# ── Step 3: PyArmor obfuscation ───────────────────────────────────────────────

def obfuscate_sources() -> bool:
    """Obfuscate source tree with PyArmor.

    Returns True if obfuscation succeeded, False if it should be skipped
    (e.g. trial license exhausted — build continues without obfuscation).
    """
    banner("Step 3 — Obfuscating source code with PyArmor")

    if OBF_DIR.exists():
        shutil.rmtree(OBF_DIR)
    OBF_DIR.mkdir(parents=True)

    try:
        pyarmor = _find_script("pyarmor")
    except FileNotFoundError as exc:
        print(f"  [WARN] {exc}")
        print("  Skipping obfuscation — build will continue with unprotected source.")
        return False

    # Obfuscate studio/ and src/ packages into OBF_DIR.
    # PyArmor trial allows limited files; if limit is hit we fall back gracefully.
    failed_pkgs: list[str] = []
    for pkg in ["studio", "src"]:
        pkg_path = ROOT / pkg
        if not pkg_path.exists():
            continue
        print(f"\n  Obfuscating {pkg}/…")
        result = subprocess.run(
            [
                pyarmor, "gen",
                "--output", str(OBF_DIR),
                "--recursive",
                str(pkg_path),
            ],
            cwd=str(ROOT),
        )
        if result.returncode != 0:
            failed_pkgs.append(pkg)
            print(f"  [WARN] PyArmor failed on '{pkg}' (exit {result.returncode}).")

    if failed_pkgs:
        print(
            "\n  !! PyArmor obfuscation incomplete for: " + ", ".join(failed_pkgs) + "\n"
            "  This usually means the trial license is exhausted (>5 files per package).\n"
            "  Purchase a PyArmor license at https://pyarmor.dashingsoft.com/\n"
            "  and run:  pyarmor reg <your-license-file>\n"
            "  The build will now continue WITHOUT code protection.\n"
        )
        shutil.rmtree(OBF_DIR, ignore_errors=True)
        return False

    # Copy non-Python assets that PyArmor does not touch
    for src_file in ROOT.glob("studio/resources/**/*"):
        if src_file.is_file():
            rel = src_file.relative_to(ROOT)
            dest = OBF_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)

    print(f"\n  Obfuscated tree written to: {OBF_DIR}")
    return True


# ── Step 4 & 5: PyInstaller builds ────────────────────────────────────────────

def _patch_spec_paths(spec_src: Path, obf_root: Path) -> Path:
    """Write a temp copy of the spec with ROOT replaced by obf_root."""
    text = spec_src.read_text(encoding="utf-8")
    # Replace the source path references so PyInstaller reads obfuscated code
    obf_str = str(obf_root).replace("\\", "\\\\")
    root_str = str(ROOT).replace("\\", "\\\\")
    text = text.replace(root_str, obf_str)
    out = BUILD_DIR / spec_src.name
    out.write_text(text, encoding="utf-8")
    return out


def build_executables(obfuscated: bool) -> None:
    banner("Step 4 — Building executables with PyInstaller")

    source_root = OBF_DIR if obfuscated else ROOT

    for spec_name in ["rhythm_worker.spec", "ssc_studio.spec"]:
        spec_src = ROOT / spec_name
        if obfuscated:
            spec_path = _patch_spec_paths(spec_src, source_root)
        else:
            spec_path = spec_src

        pyinstaller = _find_script("pyinstaller")
        print(f"\n  Building from {spec_path.name}…")
        run(
            [
                pyinstaller,
                "--noconfirm",
                "--distpath", str(ROOT / "dist"),
                "--workpath", str(BUILD_DIR / "pyinstaller_work"),
                str(spec_path),
            ],
            cwd=str(ROOT),
        )


def merge_outputs() -> None:
    banner("Step 5 — Merging outputs into dist/SSCStudio/")

    worker_dist = ROOT / "dist" / "rhythm_worker"
    if not worker_dist.exists():
        print("  [WARN] rhythm_worker dist not found — skipping merge.")
        return

    if not DIST_DIR.exists():
        print("  [WARN] SSCStudio dist not found — cannot merge.")
        return

    # Copy rhythm_worker.exe (and its PyInstaller dependencies) into SSCStudio/
    for item in worker_dist.iterdir():
        dest = DIST_DIR / item.name
        if item.is_file():
            shutil.copy2(item, dest)
            print(f"  Merged: {item.name}")
        elif item.is_dir() and not dest.exists():
            shutil.copytree(item, dest)
            print(f"  Merged dir: {item.name}/")

    # Copy FFmpeg binaries
    for binary in FFMPEG_BINARIES:
        src = FFMPEG_DIR / binary
        if src.exists():
            shutil.copy2(src, DIST_DIR / binary)
            print(f"  Copied {binary}")
        else:
            print(f"  [WARN] {binary} not found in {FFMPEG_DIR}")

    # Clean up redundant worker dist folder
    shutil.rmtree(worker_dist, ignore_errors=True)


def create_zip() -> None:
    banner("Step 6 — Creating distributable zip")
    zip_path = ROOT / "dist" / "SSCStudio.zip"
    if zip_path.exists():
        zip_path.unlink()

    file_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in DIST_DIR.rglob("*"):
            if f.is_file():
                arcname = f.relative_to(DIST_DIR.parent)
                zf.write(f, arcname)
                file_count += 1

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"  Created {zip_path.name} ({file_count} files, {size_mb:.1f} MB)")


# ── Clean ─────────────────────────────────────────────────────────────────────

def clean() -> None:
    banner("Cleaning build artifacts")
    for d in [BUILD_DIR, ROOT / "dist"]:
        if d.exists():
            shutil.rmtree(d)
            print(f"  Removed {d}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build SSCStudio distributable package."
    )
    parser.add_argument(
        "--skip-obfuscate", action="store_true",
        help="Skip PyArmor obfuscation (use raw source — for testing only).",
    )
    parser.add_argument(
        "--skip-ffmpeg", action="store_true",
        help="Skip FFmpeg download (assumes build/ffmpeg/ already has the binaries).",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Remove build/ and dist/ then exit.",
    )
    args = parser.parse_args()

    if args.clean:
        clean()
        return 0

    print(f"\nSSC Studio — Distribution Builder")
    print(f"Root  : {ROOT}")
    print(f"Python: {sys.executable}  ({sys.version.split()[0]})")
    print(f"Obfuscate : {'NO (--skip-obfuscate)' if args.skip_obfuscate else 'YES (PyArmor 8)'}")
    print(f"FFmpeg    : {'SKIP' if args.skip_ffmpeg else 'Auto-download'}")

    ensure_build_tools()

    if not args.skip_ffmpeg:
        ensure_ffmpeg()

    obfuscated = False
    if not args.skip_obfuscate:
        obfuscated = obfuscate_sources()
        if not obfuscated:
            print("  Falling back to unprotected source build.\n")

    build_executables(obfuscated)
    merge_outputs()
    create_zip()

    banner("Build complete!")
    print(f"  Distribution: {DIST_DIR}")
    print(f"  Zip archive : {ROOT / 'dist' / 'SSCStudio.zip'}")
    print(f"\n  To run: open dist/SSCStudio/ and double-click SSCStudio.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
