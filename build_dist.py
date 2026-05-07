"""
SSC Studio — Automated Distribution Builder
============================================

Usage
-----
    python build_dist.py [--skip-obfuscate] [--skip-ffmpeg] [--clean]
                         [--no-manifest] [--no-changelog]
                         [--upload] [--upload-method scp|rsync|auto]
                         [--engine-version X.Y.Z]

Build + auto-publish (one-liner once `.env.publish` is filled in):

    python build_dist.py --upload

What it does
------------
1. Install / upgrade required build tools (pyarmor, pyinstaller).
2. Download FFmpeg binaries into build/ffmpeg/ if needed.
3. Obfuscate source tree with PyArmor into build/obf/ (optional).
4. Build executables with PyInstaller:
   - rhythm_worker.spec
   - ssc_studio.spec
   - update.spec
5. Merge outputs into dist/SSCStudio/.
6. Generate dist/manifest.json + manifest.json.gz (optional).
7. Generate dist/changelog.md from git log (optional).
8. Optional upload to VPS via scp (default on Windows) or rsync.
   Reads creds from `.env.publish` or environment:
     SSC_UPDATE_UPLOAD_TARGET=user@host:/var/www/simple.rhythm
     SSC_UPDATE_UPLOAD_PORT=22                  (optional, default 22)
     SSC_UPDATE_SSH_KEY=C:/Users/me/.ssh/id_ed25519  (optional)
   On success the remote `latest` symlink is repointed to the new
   version directory atomically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
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
DIST_ROOT = ROOT / "dist"
BUILD_DIR = ROOT / "build"
OBF_DIR = BUILD_DIR / "obf"
FFMPEG_DIR = BUILD_DIR / "ffmpeg"
VERSION_FILE = ROOT / "VERSION"
ENV_PUBLISH_FILE = ROOT / ".env.publish"
DEFAULT_BASE_UPDATE_URL = "https://toolmgt.mksoft.io/simple.rhythm"
DEFAULT_REMOTE_ROOT_FALLBACK = "/var/www/simple.rhythm"

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


def compute_md5(file_path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    md5 = hashlib.md5()
    with file_path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest().lower()


def read_engine_version(override: str | None = None) -> str:
    if override:
        return str(override).strip()
    if VERSION_FILE.exists():
        txt = VERSION_FILE.read_text(encoding="utf-8").strip()
        if txt:
            return txt
    default_version = "1.0.0"
    VERSION_FILE.write_text(default_version + "\n", encoding="utf-8")
    print(f"  [INFO] VERSION was missing/empty. Created {VERSION_FILE} = {default_version}")
    return default_version


# ── Step 1: Install build tools ───────────────────────────────────────────────

def ensure_build_tools() -> None:
    banner("Step 1 — Installing build tools")
    # paramiko is used by the incremental upload path (default --upload).
    # Cheap to install, stdlib otherwise; pinning here keeps build envs reproducible.
    pip_install("pyarmor>=8.5", "pyinstaller>=6.0", "paramiko>=3.4")
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

    for spec_name in ["rhythm_worker.spec", "ssc_studio.spec", "update.spec"]:
        spec_src = ROOT / spec_name
        # Updater is intentionally built from raw source (not obfuscated)
        # to keep self-update logic simple and deterministic.
        if obfuscated and spec_name != "update.spec":
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

    # Copy update.exe (built as ONE-FILE binary by update.spec, so it
    # lives at dist/update.exe directly — NOT inside dist/update/).
    # See update.spec for why one-file mode is required.
    onefile_update_exe = ROOT / "dist" / "update.exe"
    legacy_update_dist = ROOT / "dist" / "update"
    legacy_update_exe = legacy_update_dist / "update.exe"
    if onefile_update_exe.exists():
        shutil.copy2(onefile_update_exe, DIST_DIR / "update.exe")
        print("  Merged: update.exe (one-file)")
    elif legacy_update_exe.exists():
        # Fallback for legacy one-folder builds.
        shutil.copy2(legacy_update_exe, DIST_DIR / "update.exe")
        print("  Merged: update.exe (legacy one-folder)")
    else:
        print("  [WARN] update.exe not found in dist/")

    # Copy FFmpeg binaries
    for binary in FFMPEG_BINARIES:
        src = FFMPEG_DIR / binary
        if src.exists():
            shutil.copy2(src, DIST_DIR / binary)
            print(f"  Copied {binary}")
        else:
            print(f"  [WARN] {binary} not found in {FFMPEG_DIR}")

    # Clean up redundant worker/update dist folders + the loose
    # one-file update.exe at dist/ root (already copied into SSCStudio/).
    shutil.rmtree(worker_dist, ignore_errors=True)
    shutil.rmtree(legacy_update_dist, ignore_errors=True)
    if onefile_update_exe.exists():
        try:
            onefile_update_exe.unlink()
        except OSError:
            pass


def generate_manifest(*, engine_version: str, base_update_url: str) -> Path:
    banner("Step 6 — Generating manifest.json")
    if not DIST_DIR.exists():
        raise RuntimeError(f"Distribution folder missing: {DIST_DIR}")

    files: list[dict[str, object]] = []
    for f in DIST_DIR.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(DIST_DIR)
        rel_parts = list(rel.parts)
        if rel_parts and rel_parts[0] == "_update_staging":
            continue
        if f.suffix.lower() in {".old", ".lock"}:
            continue
        rel_path = str(rel).replace("\\", "/")
        files.append(
            {
                "path": rel_path,
                "size": int(f.stat().st_size),
                "md5": compute_md5(f),
            }
        )
    files.sort(key=lambda x: str(x["path"]))

    manifest = {
        "engine_version": engine_version,
        "min_app_version": "1.0.0",
        "released_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "base_url": f"{base_update_url.rstrip('/')}/v{engine_version}/files/",
        "files": files,
    }
    manifest_path = DIST_ROOT / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    gz_path = DIST_ROOT / "manifest.json.gz"
    with manifest_path.open("rb") as fin, gzip.open(gz_path, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    print(f"  Generated manifest with {len(files)} files")
    print(f"  Manifest: {manifest_path}")
    print(f"  Gzip    : {gz_path}")
    return manifest_path


def generate_changelog(*, engine_version: str) -> Path:
    banner("Step 7 — Generating changelog from git log")

    try:
        last_tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=ROOT,
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        last_tag = ""

    range_spec = f"{last_tag}..HEAD" if last_tag else "HEAD"
    try:
        log_output = subprocess.check_output(
            ["git", "log", range_spec, "--pretty=format:%h %s"],
            cwd=ROOT,
            text=True,
        )
    except subprocess.CalledProcessError:
        log_output = ""

    feats: list[str] = []
    fixes: list[str] = []
    others: list[str] = []
    for line in log_output.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if "feat:" in low or "feature:" in low:
            feats.append(line)
        elif "fix:" in low or "bug:" in low:
            fixes.append(line)
        else:
            others.append(line)

    md_lines = [
        f"# Changelog — v{engine_version}",
        "",
        f"Released: {dt.datetime.utcnow().replace(microsecond=0).isoformat()}Z",
        "",
    ]
    if feats:
        md_lines.append("## Features")
        md_lines.extend(f"- {l}" for l in feats)
        md_lines.append("")
    if fixes:
        md_lines.append("## Bug fixes")
        md_lines.extend(f"- {l}" for l in fixes)
        md_lines.append("")
    if others:
        md_lines.append("## Other")
        md_lines.extend(f"- {l}" for l in others)
        md_lines.append("")
    if not feats and not fixes and not others:
        md_lines.extend(["No changes since last release.", ""])

    changelog_path = DIST_ROOT / "changelog.md"
    changelog_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(
        "  Generated changelog: "
        f"{len(feats)} features, {len(fixes)} fixes, {len(others)} other"
    )
    print(f"  Changelog: {changelog_path}")
    return changelog_path


def load_env_publish() -> bool:
    """Load .env.publish into os.environ (does not override existing values).

    Format: simple KEY=VALUE per line.  Lines starting with '#' or blank are
    ignored.  Surrounding single/double quotes on values are stripped.
    """
    if not ENV_PUBLISH_FILE.exists():
        return False
    try:
        text = ENV_PUBLISH_FILE.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
    return True


def _resolve_upload_target() -> tuple[str, str, str]:
    """Return (user, host, remote_root) parsed from SSC_UPDATE_UPLOAD_TARGET.

    Supports two formats:
      • Full: user@host:/path/on/server
      • Short: user@host          (uses DEFAULT_REMOTE_ROOT_FALLBACK)
    """
    target_root = os.environ.get("SSC_UPDATE_UPLOAD_TARGET", "").strip()
    if not target_root:
        raise RuntimeError(
            "SSC_UPDATE_UPLOAD_TARGET is not set. "
            "Add it to .env.publish, e.g. user@host:/var/www/simple.rhythm"
        )
    full_match = re.match(r"^([^@]+)@([^:]+):(.+)$", target_root)
    if full_match:
        return (
            full_match.group(1),
            full_match.group(2),
            full_match.group(3).rstrip("/") or DEFAULT_REMOTE_ROOT_FALLBACK,
        )
    short_match = re.match(r"^([^@]+)@([^:]+)$", target_root)
    if short_match:
        return (
            short_match.group(1),
            short_match.group(2),
            DEFAULT_REMOTE_ROOT_FALLBACK,
        )
    raise RuntimeError(
        f"Invalid SSC_UPDATE_UPLOAD_TARGET format: {target_root!r}\n"
        "Expected user@host or user@host:/path"
    )


def _ssh_extra_args() -> tuple[list[str], list[str]]:
    """Return (scp_args, ssh_args) for port + key options.

    scp uses -P for port, ssh uses -p.  Identity file is -i for both.
    """
    port = (os.environ.get("SSC_UPDATE_UPLOAD_PORT", "") or "22").strip()
    key = os.environ.get("SSC_UPDATE_SSH_KEY", "").strip()
    common_opts = [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    scp_args = [*common_opts, "-P", port]
    ssh_args = [*common_opts, "-p", port]
    if key:
        scp_args.extend(["-i", key])
        ssh_args.extend(["-i", key])
    return scp_args, ssh_args


# ── Incremental upload via paramiko (SFTP + remote `cp -al`) ──────────────────
# This is the default upload path because PyInstaller bundles ship hundreds
# of MB of unchanged Qt/Python libs every build.  Re-uploading them all is
# wasteful in time and bandwidth.
#
# Strategy (per release):
#   1. Fetch the previous version's manifest from `<root>/latest/manifest.json`.
#   2. Diff local manifest vs remote: classify each path as
#        - new        (path absent on server)
#        - changed    (md5 differs)
#        - unchanged  (md5 matches → no upload)
#        - removed    (path on server but not in new build)
#   3. On the server, hardlink-clone the previous /files dir into the new
#      version dir: `cp -al <prev>/files <new>/files`.  Hardlinks share
#      inodes so disk usage barely grows.
#   4. SFTP-upload only changed/new files.  Before each upload we sftp.remove
#      the existing path so the new file gets a fresh inode (does NOT clobber
#      the original previous-version file via the shared hardlink).
#   5. Remove paths that were deleted in the new build.
#   6. Upload manifest.json[.gz] + changelog.md.
#   7. Repoint `latest` symlink atomically.
#
# Result: a 200 MB build with one changed DLL transfers ~1 MB.


def _open_paramiko_client():  # type: ignore[no-untyped-def]
    """Open a paramiko SSHClient using credentials from .env.publish/env.

    Returns (client, sftp, target_tuple) where target_tuple is (user, host, remote_root).
    """
    try:
        import paramiko  # noqa: F401  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "paramiko is required for incremental upload. "
            "Run: pip install paramiko>=3.4"
        ) from exc
    import paramiko  # type: ignore

    user, host, remote_root = _resolve_upload_target()
    port = int((os.environ.get("SSC_UPDATE_UPLOAD_PORT", "") or "22").strip() or "22")
    key_path = os.environ.get("SSC_UPDATE_SSH_KEY", "").strip() or None

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"  Connecting to {user}@{host}:{port} (paramiko)…")
    client.connect(
        hostname=host,
        port=port,
        username=user,
        key_filename=key_path,
        allow_agent=True,
        look_for_keys=True,
        timeout=15,
    )
    return client, client.open_sftp(), (user, host, remote_root)


def _run_remote(client, cmd: str) -> tuple[int, str, str]:
    """Execute a shell command on the connected server."""
    _stdin, stdout, stderr = client.exec_command(cmd, get_pty=False)
    rc = stdout.channel.recv_exit_status()
    return (
        rc,
        stdout.read().decode("utf-8", "replace"),
        stderr.read().decode("utf-8", "replace"),
    )


def _sftp_mkdir_p(sftp, remote_path: str) -> None:
    """Recursive mkdir on the SFTP side (POSIX-style absolute path)."""
    if not remote_path or remote_path == "/":
        return
    parts = remote_path.strip("/").split("/")
    current = ""
    for p in parts:
        current = f"{current}/{p}" if current else f"/{p}"
        try:
            sftp.stat(current)
        except IOError:
            try:
                sftp.mkdir(current)
            except IOError:
                pass


def _read_remote_manifest(sftp, remote_root: str) -> tuple[dict[str, str], str]:
    """Return ({path: md5}, prev_files_dir_or_empty).

    Empty dict + empty string ⇒ no previous version (first ever upload).
    """
    try:
        sftp.stat(f"{remote_root}/latest")
    except IOError:
        return {}, ""
    try:
        with sftp.open(f"{remote_root}/latest/manifest.json", "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
    except (IOError, json.JSONDecodeError):
        return {}, ""
    files: dict[str, str] = {}
    for it in data.get("files", []):
        p = str(it.get("path", "")).replace("\\", "/")
        m = str(it.get("md5", "")).lower()
        if p and m and len(m) == 32:
            files[p] = m
    return files, f"{remote_root}/latest/files"


def _read_local_manifest() -> dict[str, str]:
    path = DIST_ROOT / "manifest.json"
    if not path.exists():
        raise RuntimeError(
            f"Local manifest not found: {path}\n"
            "Run build first (or re-enable manifest generation)."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for it in data.get("files", []):
        p = str(it.get("path", "")).replace("\\", "/")
        m = str(it.get("md5", "")).lower()
        if p and m:
            out[p] = m
    return out


def upload_to_vps_incremental(*, engine_version: str) -> None:
    banner("Step 8 — Uploading to VPS via SFTP (incremental)")
    client, sftp, (user, host, remote_root) = _open_paramiko_client()
    try:
        remote_version = f"{remote_root}/v{engine_version}"
        remote_files = f"{remote_version}/files"
        print(f"  Target  : {user}@{host}:{remote_version}")
        print(f"  Engine  : v{engine_version}")

        # 1. Local + previous manifest diff.
        cur_files = _read_local_manifest()
        prev_files, prev_files_dir = _read_remote_manifest(sftp, remote_root)
        if not prev_files:
            print("  No previous version on server — performing FULL upload.")
        else:
            print(
                f"  Previous version : {len(prev_files)} files at {prev_files_dir}"
            )
        print(f"  Current build    : {len(cur_files)} files")

        # Enumerate ACTUAL files on remote (regardless of what manifest says).
        # The manifest can lie if a previous upload was partial, or if files
        # were deleted out-of-band — and treating it as ground truth would
        # leave us with phantom "unchanged" files that don't actually exist.
        remote_actual: set[str] = set()
        if prev_files_dir:
            # Use sh -c to handle the case where the dir might not exist.
            enum_cmd = (
                f"if [ -d '{prev_files_dir}' ]; then "
                f"cd '{prev_files_dir}' && find . -type f 2>/dev/null "
                f"| sed 's|^\\./||'; fi"
            )
            rc, out, _ = _run_remote(client, enum_cmd)
            if rc == 0:
                remote_actual = {
                    ln.strip() for ln in out.splitlines() if ln.strip()
                }
                if len(remote_actual) != len(prev_files):
                    print(
                        f"  WARN: remote files dir has {len(remote_actual)} "
                        f"actual files but manifest claims {len(prev_files)} — "
                        "uploading whatever is missing."
                    )
                else:
                    print(f"  Verified         : {len(remote_actual)} actual files on remote")

        to_upload: list[str] = []
        unchanged = 0
        for p, m in cur_files.items():
            present_on_remote = (
                (p in remote_actual) if remote_actual else (p in prev_files)
            )
            if present_on_remote and prev_files.get(p) == m:
                unchanged += 1
            else:
                to_upload.append(p)
        # Removal candidates: anything actually on remote but not in new build.
        # Falls back to manifest diff when we couldn't enumerate.
        if remote_actual:
            to_remove = sorted(p for p in remote_actual if p not in cur_files)
        else:
            to_remove = sorted(p for p in prev_files if p not in cur_files)

        total_upload_bytes = 0
        for p in to_upload:
            local = DIST_DIR / p
            if local.exists():
                total_upload_bytes += local.stat().st_size
        print(
            f"  Diff             : upload={len(to_upload)} "
            f"({total_upload_bytes/1024/1024:.1f} MB), "
            f"remove={len(to_remove)}, unchanged={unchanged}"
        )

        # 2. Prepare remote tree.  THREE distinct cases — choose carefully:
        #    a) v<version>/files/ already exists  → in-place update, NEVER rm -rf
        #       (rm -rf would wipe the live `latest` target if same version).
        #    b) v<version>/files/ missing, prev exists → hardlink clone from prev.
        #    c) No previous at all → fresh mkdir, full upload follows.
        rc, _, err = _run_remote(client, f"mkdir -p '{remote_version}'")
        if rc != 0:
            raise RuntimeError(f"mkdir -p {remote_version} failed: {err.strip()}")

        remote_files_exists = False
        try:
            sftp.stat(remote_files)
            remote_files_exists = True
        except IOError:
            pass

        # Resolve `latest` symlink to detect same-version re-upload.
        prev_version_abs = ""
        if prev_files_dir:
            rc, out, _ = _run_remote(
                client, f"readlink -f '{remote_root}/latest' 2>/dev/null || true"
            )
            prev_version_abs = out.strip()
        same_version_as_latest = bool(
            prev_version_abs and prev_version_abs == remote_version
        )

        if remote_files_exists:
            note = (
                "same version as `latest`"
                if same_version_as_latest
                else "from previous partial upload"
            )
            print(f"  Reusing existing {remote_files} in place ({note}).")
            # Diff already accounts for which files need (re-)upload; nothing
            # else to prepare here.  Stale files outside the manifest will
            # remain — acceptable trade-off vs risking data loss.
        elif prev_files and prev_files_dir:
            print(f"  Hardlink-cloning previous /files (`cp -al`)…")
            cmd = f"cp -al '{prev_files_dir}' '{remote_files}'"
            rc, _, err = _run_remote(client, cmd)
            if rc != 0:
                print(
                    "  WARN: hardlink clone failed "
                    f"({err.strip()[:160]}); falling back to mkdir."
                )
                _run_remote(client, f"mkdir -p '{remote_files}'")
        else:
            print("  Creating fresh files dir (no previous version on server).")
            _run_remote(client, f"mkdir -p '{remote_files}'")

        # 3. Upload changed/new files.
        for idx, rel in enumerate(to_upload, 1):
            local = DIST_DIR / rel
            if not local.exists():
                print(f"  WARN: skipping missing local file {rel}")
                continue
            remote_path = f"{remote_files}/{rel}"
            _sftp_mkdir_p(sftp, remote_path.rsplit("/", 1)[0])
            # Critical: drop the hardlink BEFORE writing, so we create a
            # new inode and don't truncate the previous-version file.
            try:
                sftp.remove(remote_path)
            except IOError:
                pass
            sftp.put(str(local), remote_path)
            size_kb = local.stat().st_size / 1024
            print(f"  [{idx}/{len(to_upload)}] {rel}  ({size_kb:.0f} KB)")

        # 4. Remove deleted files.
        for rel in to_remove:
            try:
                sftp.remove(f"{remote_files}/{rel}")
                print(f"  [removed] {rel}")
            except IOError as exc:
                print(f"  WARN: could not remove {rel}: {exc}")

        # 5. Metadata files (manifest, changelog) — always overwrite.
        for name in ("manifest.json", "manifest.json.gz", "changelog.md"):
            local_meta = DIST_ROOT / name
            if not local_meta.exists():
                continue
            try:
                sftp.remove(f"{remote_version}/{name}")
            except IOError:
                pass
            sftp.put(str(local_meta), f"{remote_version}/{name}")
            print(f"  Meta: {name}")

        # 6. Atomic symlink swap.
        rc, _, err = _run_remote(
            client,
            f"ln -sfn '{remote_version}' '{remote_root}/latest'",
        )
        if rc != 0:
            raise RuntimeError(f"latest symlink update failed: {err.strip()}")
        print(f"  latest -> v{engine_version}")
        print(f"  Upload complete.  Manifest: {remote_version}/manifest.json")
    finally:
        try:
            sftp.close()
        finally:
            client.close()


def upload_to_vps_scp(*, engine_version: str) -> None:
    banner("Step 8 — Uploading to VPS via SCP (full re-upload)")
    user, host, remote_root = _resolve_upload_target()
    remote_version = f"{remote_root}/v{engine_version}"
    remote_files = f"{remote_version}/files"
    scp_args, ssh_args = _ssh_extra_args()
    target = f"{user}@{host}"

    print(f"  Target  : {target}:{remote_version}")
    print(f"  Engine  : v{engine_version}")

    # Refuse to wipe the live `latest` target.  If `latest` already points
    # at this same version, full re-upload is unsafe (rm -rf below would
    # delete the directory the running app is fetching from).
    probe = subprocess.run(
        ["ssh", *ssh_args, target,
         f"readlink -f '{remote_root}/latest' 2>/dev/null || true"],
        capture_output=True, text=True, check=False,
    )
    latest_abs = probe.stdout.strip()
    if latest_abs and latest_abs == remote_version:
        raise RuntimeError(
            f"--upload-method scp refuses to re-upload v{engine_version}: "
            f"`latest` symlink already points at it on the server. "
            f"Either bump VERSION first, or use the default "
            f"--upload-method incremental (it handles in-place safely)."
        )

    # 1. Prepare remote dir; clean files/ since we're doing a full upload
    #    (we just verified above that this is NOT the live `latest` target).
    run([
        "ssh", *ssh_args, target,
        f"mkdir -p '{remote_version}' && rm -rf '{remote_files}'",
    ])

    # 2. Upload top-level metadata files (manifest, changelog).
    metadata_sources = [
        DIST_ROOT / "manifest.json",
        DIST_ROOT / "manifest.json.gz",
        DIST_ROOT / "changelog.md",
    ]
    metadata_present = [str(p) for p in metadata_sources if p.exists()]
    if metadata_present:
        print(f"  Uploading {len(metadata_present)} metadata file(s)…")
        run([
            "scp", *scp_args,
            *metadata_present,
            f"{target}:{remote_version}/",
        ])

    # 3. Upload SSCStudio bundle as v<version>/SSCStudio/ then atomically
    #    rename to /files/.  scp does not have rsync's "trailing slash =
    #    contents" semantics, so we upload the parent dir then mv.
    print("  Uploading SSCStudio bundle (this may take a while)…")
    run([
        "scp", *scp_args, "-r",
        str(DIST_DIR),
        f"{target}:{remote_version}/",
    ])
    run([
        "ssh", *ssh_args, target,
        f"mv '{remote_version}/{DIST_DIR.name}' '{remote_files}'",
    ])

    # 4. Update `latest` symlink atomically.
    print("  Updating 'latest' symlink…")
    run([
        "ssh", *ssh_args, target,
        f"ln -sfn '{remote_version}' '{remote_root}/latest'",
    ])

    print(f"  Upload complete.  Manifest: {remote_version}/manifest.json")


def upload_to_vps_rsync(*, engine_version: str) -> None:
    """Original rsync-based upload (Linux/Mac, requires rsync in PATH)."""
    banner("Step 8 — Uploading to VPS via rsync")
    user, host, remote_root = _resolve_upload_target()
    target_version = f"{user}@{host}:{remote_root}/v{engine_version}/"

    run([
        "rsync", "-avz", "--progress", "--delete",
        str(DIST_ROOT / "manifest.json"),
        str(DIST_ROOT / "manifest.json.gz"),
        str(DIST_ROOT / "changelog.md"),
        target_version,
    ])
    run([
        "rsync", "-avz", "--progress", "--delete",
        f"{DIST_DIR}/",
        f"{target_version}files/",
    ])
    _, ssh_args = _ssh_extra_args()
    run([
        "ssh", *ssh_args, f"{user}@{host}",
        (
            f"ln -sfn '{remote_root}/v{engine_version}' "
            f"'{remote_root}/latest'"
        ),
    ])
    print("  Upload complete.")


def upload_to_vps(*, engine_version: str, method: str = "auto") -> None:
    """Dispatch upload to one of three transport modes.

    Modes:
      • incremental — paramiko + SFTP, hardlink-clones unchanged files
                      from the previous version on the server, uploads
                      ONLY changed/new files.  Default; cross-platform.
      • scp         — full upload via OpenSSH scp (no incremental).
      • rsync       — original rsync transport (Linux/Mac).

    method="auto" picks "incremental".
    """
    chosen = method.strip().lower()
    if chosen == "auto":
        chosen = "incremental"
    if chosen == "incremental":
        upload_to_vps_incremental(engine_version=engine_version)
    elif chosen == "scp":
        if shutil.which("scp") is None or shutil.which("ssh") is None:
            raise RuntimeError(
                "scp/ssh not found in PATH. Install Windows OpenSSH client "
                "(Settings → Apps → Optional features → 'OpenSSH Client')."
            )
        upload_to_vps_scp(engine_version=engine_version)
    elif chosen == "rsync":
        if shutil.which("rsync") is None:
            raise RuntimeError(
                "rsync not found in PATH. "
                "Try --upload-method incremental (default) instead."
            )
        upload_to_vps_rsync(engine_version=engine_version)
    else:
        raise RuntimeError(f"Unknown --upload-method: {method!r}")


# ── Clean ─────────────────────────────────────────────────────────────────────

def clean() -> None:
    banner("Cleaning build artifacts")
    for d in [BUILD_DIR, DIST_ROOT]:
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
    parser.add_argument(
        "--no-manifest", action="store_true",
        help="Skip manifest.json generation.",
    )
    parser.add_argument(
        "--no-changelog", action="store_true",
        help="Skip changelog.md generation.",
    )
    parser.add_argument(
        "--upload", action="store_true",
        help=(
            "Upload build artifacts to VPS after build.  Reads target "
            "from .env.publish or environment (SSC_UPDATE_UPLOAD_TARGET, "
            "SSC_UPDATE_UPLOAD_PORT, SSC_UPDATE_SSH_KEY)."
        ),
    )
    parser.add_argument(
        "--upload-method",
        choices=["auto", "incremental", "scp", "rsync"],
        default="auto",
        help=(
            "Transport for --upload.  'auto' = 'incremental' (paramiko + "
            "SFTP, hardlink-clones unchanged files server-side, uploads "
            "only the diff).  'scp' = full re-upload.  'rsync' = "
            "rsync transport (Linux/Mac only)."
        ),
    )
    parser.add_argument(
        "--engine-version", default="",
        help="Override engine version (default reads VERSION file).",
    )
    parser.add_argument(
        "--base-update-url",
        default=DEFAULT_BASE_UPDATE_URL,
        help="Base update URL used in manifest base_url field.",
    )
    args = parser.parse_args()

    # Load .env.publish (if present) BEFORE reading any upload-related
    # env vars.  Existing process env wins; .env.publish is only a
    # convenience default for dev machines.
    if load_env_publish():
        print(f"  Loaded credentials from {ENV_PUBLISH_FILE.name}")

    if args.clean:
        clean()
        return 0

    print(f"\nSSC Studio — Distribution Builder")
    print(f"Root  : {ROOT}")
    print(f"Python: {sys.executable}  ({sys.version.split()[0]})")
    print(f"Obfuscate : {'NO (--skip-obfuscate)' if args.skip_obfuscate else 'YES (PyArmor 8)'}")
    print(f"FFmpeg    : {'SKIP' if args.skip_ffmpeg else 'Auto-download'}")
    engine_version = read_engine_version(args.engine_version)
    print(f"Engine ver: {engine_version}")
    print(f"Base URL  : {args.base_update_url}")

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

    if not args.no_manifest:
        generate_manifest(
            engine_version=engine_version,
            base_update_url=args.base_update_url,
        )
    else:
        print("  Skipping manifest generation (--no-manifest).")

    if not args.no_changelog:
        generate_changelog(engine_version=engine_version)
    else:
        print("  Skipping changelog generation (--no-changelog).")

    if args.upload:
        upload_to_vps(
            engine_version=engine_version,
            method=args.upload_method,
        )

    banner("Build complete!")
    print(f"  Distribution: {DIST_DIR}")
    if not args.no_manifest:
        print(f"  Manifest    : {DIST_ROOT / 'manifest.json'}")
    if not args.no_changelog:
        print(f"  Changelog   : {DIST_ROOT / 'changelog.md'}")
    print(f"\n  To run: open dist/SSCStudio/ and double-click SSCStudio.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
