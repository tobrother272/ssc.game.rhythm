from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from updater.manifest import Manifest, ManifestFile, parse_manifest_bytes
from updater.update_logic import (
    apply_staged_update,
    diff_manifest_against_local,
    download_and_verify_files,
    ensure_disk_space_for_download,
    updater_lock,
)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_parse_manifest_bytes_ok() -> None:
    payload = {
        "engine_version": "1.2.3",
        "min_app_version": "1.0.0",
        "released_at": "2026-05-15T10:00:00Z",
        "base_url": "https://toolmgt.mksoft.io/simple.rhythm/v1.2.3/files/",
        "files": [
            {"path": "SSCStudio.exe", "size": 12, "md5": "d41d8cd98f00b204e9800998ecf8427e"},
        ],
    }
    manifest = parse_manifest_bytes(json.dumps(payload).encode("utf-8"))
    assert manifest.engine_version == "1.2.3"
    assert len(manifest.files) == 1
    assert manifest.files[0].path == "SSCStudio.exe"


def test_diff_manifest_against_local_detects_missing_and_mismatch(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir(parents=True)

    keep_path = install_dir / "keep.txt"
    bad_path = install_dir / "bad.txt"
    _write(keep_path, b"same")
    _write(bad_path, b"old")

    manifest = Manifest(
        engine_version="1.0.0",
        min_app_version="1.0.0",
        released_at="now",
        base_url="https://toolmgt.mksoft.io/simple.rhythm/latest/files/",
        files=(
            ManifestFile(
                path="keep.txt",
                size=4,
                md5="51037a4a37730f52c8732586d3aaa316",  # md5("same")
            ),
            ManifestFile(
                path="bad.txt",
                size=3,
                md5="22af645d1859cb5ca6da0c484f1f37ea",  # md5("new")
            ),
            ManifestFile(
                path="missing.txt",
                size=7,
                md5="3d801aa532c1cec3ee82d87a99fdf63f",
            ),
        ),
    )

    result = diff_manifest_against_local(
        manifest,
        install_dir,
        cache_path=install_dir / "local_hashes.json",
        max_workers=2,
    )
    assert result.unchanged == 1
    assert result.missing_local == 1
    assert result.mismatched_hash == 1
    assert len(result.to_download) == 2
    assert {f.path for f in result.to_download} == {"bad.txt", "missing.txt"}


def test_ensure_disk_space_for_download_math(tmp_path: Path) -> None:
    ok, free, required = ensure_disk_space_for_download(
        tmp_path,
        total_download_bytes=10 * 1024 * 1024,
        safety_margin_bytes=5 * 1024 * 1024,
    )
    assert free >= 0
    assert required == 15 * 1024 * 1024
    assert isinstance(ok, bool)


def test_download_and_verify_files_from_file_url(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    install_dir = tmp_path / "install"
    source_dir.mkdir(parents=True)
    install_dir.mkdir(parents=True)

    worker_data = b"worker-binary-content"
    ffmpeg_data = b"ffmpeg-binary-content"
    _write(source_dir / "rhythm_worker.exe", worker_data)
    _write(source_dir / "ffmpeg.exe", ffmpeg_data)

    manifest = Manifest(
        engine_version="1.2.3",
        min_app_version="1.0.0",
        released_at="now",
        base_url=f"{source_dir.as_uri()}/",
        files=(
            ManifestFile(
                path="rhythm_worker.exe",
                size=len(worker_data),
                md5=hashlib.md5(worker_data).hexdigest(),
            ),
            ManifestFile(
                path="ffmpeg.exe",
                size=len(ffmpeg_data),
                md5=hashlib.md5(ffmpeg_data).hexdigest(),
            ),
        ),
    )
    to_download = manifest.files
    result = download_and_verify_files(
        manifest,
        to_download,
        install_dir,
        max_workers=2,
        retries=2,
        timeout_sec=5.0,
    )
    assert result.downloaded_count == 2
    assert result.verified_count == 2
    assert (result.staging_dir / "rhythm_worker.exe").read_bytes() == worker_data
    assert (result.staging_dir / "ffmpeg.exe").read_bytes() == ffmpeg_data


def test_apply_staged_update_success(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    staging_dir = install_dir / "_update_staging"
    install_dir.mkdir(parents=True)
    old_worker = b"old-worker"
    new_worker = b"new-worker"
    old_ff = b"old-ffmpeg"
    new_ff = b"new-ffmpeg"
    _write(install_dir / "rhythm_worker.exe", old_worker)
    _write(install_dir / "ffmpeg.exe", old_ff)
    _write(staging_dir / "rhythm_worker.exe", new_worker)
    _write(staging_dir / "ffmpeg.exe", new_ff)
    files = (
        ManifestFile(path="rhythm_worker.exe", size=len(new_worker), md5=hashlib.md5(new_worker).hexdigest()),
        ManifestFile(path="ffmpeg.exe", size=len(new_ff), md5=hashlib.md5(new_ff).hexdigest()),
    )
    result = apply_staged_update(
        install_dir,
        staging_dir,
        files,
        smoke_commands=[[sys.executable, "-c", "import sys; sys.exit(0)"]],
    )
    assert result.smoke_ok is True
    assert result.rolled_back is False
    assert (install_dir / "rhythm_worker.exe").read_bytes() == new_worker
    assert (install_dir / "ffmpeg.exe").read_bytes() == new_ff
    assert not (install_dir / "rhythm_worker.exe.old").exists()
    assert not (install_dir / "ffmpeg.exe.old").exists()
    assert not staging_dir.exists()


def test_apply_staged_update_rolls_back_on_smoke_fail(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    staging_dir = install_dir / "_update_staging"
    install_dir.mkdir(parents=True)
    old_worker = b"old-worker"
    new_worker = b"new-worker"
    _write(install_dir / "rhythm_worker.exe", old_worker)
    _write(staging_dir / "rhythm_worker.exe", new_worker)
    files = (
        ManifestFile(path="rhythm_worker.exe", size=len(new_worker), md5=hashlib.md5(new_worker).hexdigest()),
    )
    result = apply_staged_update(
        install_dir,
        staging_dir,
        files,
        smoke_commands=[[sys.executable, "-c", "import sys; sys.exit(1)"]],
    )
    assert result.smoke_ok is False
    assert result.rolled_back is True
    assert (install_dir / "rhythm_worker.exe").read_bytes() == old_worker


def test_updater_lock_blocks_concurrent_attempt(tmp_path: Path) -> None:
    lock_file = tmp_path / "u.lock"
    with updater_lock(lock_file):
        with pytest.raises(RuntimeError):
            with updater_lock(lock_file):
                pass

