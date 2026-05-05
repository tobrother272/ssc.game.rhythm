from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from updater.manifest import Manifest, ManifestFile


@dataclass(frozen=True)
class DiffResult:
    to_download: tuple[ManifestFile, ...]
    missing_local: int
    mismatched_hash: int
    unchanged: int
    total_download_bytes: int


@dataclass(frozen=True)
class DownloadResult:
    downloaded_count: int
    verified_count: int
    downloaded_bytes: int
    staging_dir: Path


@dataclass(frozen=True)
class ApplyResult:
    swapped_count: int
    rolled_back: bool
    smoke_ok: bool
    backup_count: int


class HashCache:
    def __init__(self, cache_path: Path) -> None:
        self._cache_path = cache_path
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = False

    def load(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(raw, dict):
            entries = raw.get("entries")
            if isinstance(entries, dict):
                self._entries = entries

    def get(self, key: str, *, mtime_ns: int, size: int) -> str | None:
        item = self._entries.get(key)
        if not isinstance(item, dict):
            return None
        if item.get("mtime_ns") != mtime_ns or item.get("size") != size:
            return None
        md5 = item.get("md5")
        return md5 if isinstance(md5, str) and len(md5) == 32 else None

    def put(self, key: str, *, mtime_ns: int, size: int, md5: str) -> None:
        self._entries[key] = {"mtime_ns": mtime_ns, "size": size, "md5": md5}
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": self._entries}
        self._cache_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self._dirty = False


def _safe_local_path(install_dir: Path, rel_path: str) -> Path:
    local = (install_dir / rel_path).resolve()
    install_root = install_dir.resolve()
    if install_root not in local.parents and local != install_root:
        raise ValueError(f"Unsafe relative path in manifest: {rel_path}")
    return local


def compute_md5(file_path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    md5 = hashlib.md5()
    with file_path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest().lower()


def _md5_with_cache(rel_path: str, file_path: Path, cache: HashCache) -> str:
    st = file_path.stat()
    cached = cache.get(rel_path, mtime_ns=st.st_mtime_ns, size=st.st_size)
    if cached:
        return cached
    md5 = compute_md5(file_path)
    cache.put(rel_path, mtime_ns=st.st_mtime_ns, size=st.st_size, md5=md5)
    return md5


def diff_manifest_against_local(
    manifest: Manifest,
    install_dir: Path,
    *,
    cache_path: Path | None = None,
    max_workers: int = 6,
) -> DiffResult:
    install_dir = install_dir.resolve()
    cache = HashCache(cache_path or (install_dir / "local_hashes.json"))
    cache.load()

    to_download: list[ManifestFile] = []
    existing_to_hash: list[tuple[ManifestFile, Path]] = []
    missing_local = 0

    for item in manifest.files:
        try:
            local_path = _safe_local_path(install_dir, item.path)
        except ValueError:
            to_download.append(item)
            missing_local += 1
            continue
        if not local_path.exists():
            to_download.append(item)
            missing_local += 1
            continue
        existing_to_hash.append((item, local_path))

    mismatched_hash = 0
    unchanged = 0
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as ex:
        future_map = {
            ex.submit(_md5_with_cache, item.path, local_path, cache): item
            for item, local_path in existing_to_hash
        }
        for fut in as_completed(future_map):
            item = future_map[fut]
            try:
                md5 = fut.result()
            except OSError:
                to_download.append(item)
                mismatched_hash += 1
                continue
            if md5 == item.md5:
                unchanged += 1
            else:
                to_download.append(item)
                mismatched_hash += 1

    cache.save()
    total_download_bytes = sum(max(0, int(it.size)) for it in to_download)
    return DiffResult(
        to_download=tuple(to_download),
        missing_local=missing_local,
        mismatched_hash=mismatched_hash,
        unchanged=unchanged,
        total_download_bytes=total_download_bytes,
    )


def ensure_disk_space_for_download(
    install_dir: Path,
    total_download_bytes: int,
    *,
    safety_margin_bytes: int = 50 * 1024 * 1024,
) -> tuple[bool, int, int]:
    usage = shutil.disk_usage(str(install_dir))
    required = max(0, int(total_download_bytes)) + max(0, int(safety_margin_bytes))
    free = int(usage.free)
    return free >= required, free, required


def _download_file_with_resume(
    url: str,
    dest_file: Path,
    *,
    timeout_sec: float = 30.0,
) -> int:
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    existing = dest_file.stat().st_size if dest_file.exists() else 0
    headers = {
        "User-Agent": "SSCStudio-Updater/1.0",
        "Accept-Encoding": "identity",
    }
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=float(timeout_sec)) as resp:
            status_raw = getattr(resp, "status", None)
            if status_raw is None:
                status_raw = resp.getcode()
            status_code = int(status_raw) if status_raw is not None else 200
            append = existing > 0 and status_code == 206
            mode = "ab" if append else "wb"
            downloaded = 0
            with dest_file.open(mode) as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
        return downloaded
    except HTTPError as exc:
        # Resume offset is no longer valid (stale .part after server file changed).
        # Let caller re-verify current file and trigger clean retry if needed.
        if int(getattr(exc, "code", 0)) == 416 and existing > 0:
            return 0
        raise


def _download_and_verify_one(
    item: ManifestFile,
    manifest: Manifest,
    staging_dir: Path,
    *,
    retries: int,
    timeout_sec: float,
) -> tuple[str, int]:
    dest_file = staging_dir / item.path
    url = urljoin(manifest.base_url, item.path)
    attempts = max(1, int(retries))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _download_file_with_resume(url, dest_file, timeout_sec=timeout_sec)
            if item.size > 0:
                actual_size = dest_file.stat().st_size
                if int(actual_size) != int(item.size):
                    raise ValueError(
                        f"Size mismatch for {item.path}: expected={item.size}, got={actual_size}"
                    )
            actual_md5 = compute_md5(dest_file)
            if actual_md5 != item.md5:
                raise ValueError(
                    f"MD5 mismatch for {item.path}: expected={item.md5}, got={actual_md5}"
                )
            return item.path, int(dest_file.stat().st_size)
        except (URLError, OSError, ValueError) as exc:
            last_error = exc
            # For hash/size mismatch start the next retry from clean file.
            if isinstance(exc, ValueError):
                dest_file.unlink(missing_ok=True)
            if attempt >= attempts:
                break
            time.sleep(0.5 * (2 ** (attempt - 1)))
    if last_error is None:
        raise RuntimeError(f"Unknown download failure for {item.path}")
    raise RuntimeError(f"Download failed for {item.path}: {last_error}") from last_error


def download_and_verify_files(
    manifest: Manifest,
    to_download: tuple[ManifestFile, ...],
    install_dir: Path,
    *,
    staging_dir: Path | None = None,
    max_workers: int = 4,
    retries: int = 3,
    timeout_sec: float = 30.0,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> DownloadResult:
    target_staging = (staging_dir or (install_dir / "_update_staging")).resolve()
    target_staging.mkdir(parents=True, exist_ok=True)
    if not to_download:
        return DownloadResult(
            downloaded_count=0,
            verified_count=0,
            downloaded_bytes=0,
            staging_dir=target_staging,
        )

    total_downloaded_bytes = 0
    completed = 0
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as ex:
        futures = [
            ex.submit(
                _download_and_verify_one,
                item,
                manifest,
                target_staging,
                retries=max(1, int(retries)),
                timeout_sec=float(timeout_sec),
            )
            for item in to_download
        ]
        for fut in as_completed(futures):
            path, bytes_count = fut.result()
            with lock:
                completed += 1
                total_downloaded_bytes += int(bytes_count)
                if progress_cb is not None:
                    progress_cb(
                        {
                            "completed": completed,
                            "total": len(to_download),
                            "path": path,
                            "downloaded_bytes": total_downloaded_bytes,
                        }
                    )
    return DownloadResult(
        downloaded_count=len(to_download),
        verified_count=len(to_download),
        downloaded_bytes=total_downloaded_bytes,
        staging_dir=target_staging,
    )


def _backup_path_for(target: Path) -> Path:
    return target.with_name(target.name + ".old")


def _is_pid_live(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but not signalable by current user.
        return True
    except OSError:
        return False


def wait_for_pid_exit(
    pid: int,
    *,
    timeout_sec: float = 900.0,
    poll_sec: float = 0.25,
) -> bool:
    """Wait until process *pid* exits (or timeout).

    Returns True when pid is no longer alive, False on timeout.
    """
    pid_i = int(pid)
    if pid_i <= 0:
        return True
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while _is_pid_live(pid_i):
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.05, float(poll_sec)))
    return True


def list_processes_in_dir(install_dir: Path) -> list[tuple[int, str]]:
    """Enumerate processes whose executable lives under *install_dir*.

    Windows-only; returns [] on non-Windows or on enumeration failure.
    Uses pure stdlib (ctypes + psapi) — no external deps.
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []

    install_root = str(install_dir.resolve()).lower().rstrip("\\")
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    EnumProcesses = psapi.EnumProcesses
    EnumProcesses.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    EnumProcesses.restype = wintypes.BOOL

    OpenProcess = kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    QueryFullProcessImageNameW = kernel32.QueryFullProcessImageNameW
    QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    QueryFullProcessImageNameW.restype = wintypes.BOOL

    arr_size = 4096
    pids = (wintypes.DWORD * arr_size)()
    needed = wintypes.DWORD(0)
    if not EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    count = needed.value // ctypes.sizeof(wintypes.DWORD)

    results: list[tuple[int, str]] = []
    for i in range(min(count, arr_size)):
        pid = int(pids[i])
        if pid <= 4:
            continue
        h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            buf_len = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(buf_len.value)
            if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(buf_len)):
                exe_path = buf.value
                if exe_path and exe_path.lower().startswith(install_root):
                    results.append((pid, exe_path))
        finally:
            CloseHandle(h)
    return results


def terminate_pid(pid: int, *, timeout_sec: float = 5.0) -> bool:
    """Best-effort terminate of *pid*. Windows-only; returns True on success."""
    if os.name != "nt" or int(pid) <= 0:
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/F"],
            capture_output=True,
            timeout=float(timeout_sec),
        )
    except Exception:
        return False
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    while _is_pid_live(int(pid)):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


@contextmanager
def updater_lock(lock_path: Path | None = None):
    path = (lock_path or (Path(tempfile.gettempdir()) / "ssc_updater.lock")).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stale = True
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            stale = not _is_pid_live(int(meta.get("pid", -1)))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            stale = True
        if stale:
            path.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"Update lock already active: {path}")
    payload = {"pid": os.getpid(), "created_at": time.time()}
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def atomic_swap_file(
    target: Path,
    staged_file: Path,
    *,
    retries: int = 600,
    retry_delay_sec: float = 0.1,
    log_fn: Callable[[str], None] | None = None,
) -> Path | None:
    if not staged_file.exists():
        raise FileNotFoundError(f"Staged file missing: {staged_file}")
    backup = _backup_path_for(target)
    attempts = max(1, int(retries))
    last_err: Exception | None = None
    started_at = time.monotonic()
    for attempt in range(1, attempts + 1):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            backup.unlink(missing_ok=True)
            if target.exists():
                os.replace(target, backup)
            os.replace(staged_file, target)
            if log_fn and attempt > 1:
                elapsed = time.monotonic() - started_at
                log_fn(
                    f"[updater] Swap succeeded for {target.name} "
                    f"after {attempt} attempts ({elapsed:.1f}s)"
                )
            return backup if backup.exists() else None
        except PermissionError as exc:
            last_err = exc
            if attempt >= attempts:
                break
            # Log every 10 attempts (1s) so update.log shows progress
            # without flooding it with hundreds of lines.
            if log_fn and attempt % 10 == 1 and attempt > 1:
                elapsed = time.monotonic() - started_at
                log_fn(
                    f"[updater] Swap retry {attempt}/{attempts} for "
                    f"{target.name} (locked, waited {elapsed:.1f}s): {exc}"
                )
            time.sleep(float(retry_delay_sec))
        except OSError as exc:
            last_err = exc
            break
    if last_err is None:
        raise RuntimeError(f"Atomic swap failed: {target}")
    raise RuntimeError(f"Atomic swap failed for {target}: {last_err}") from last_err


def rollback_swapped_files(backup_map: list[tuple[Path, Path]]) -> None:
    for target, backup in reversed(backup_map):
        if not backup.exists():
            continue
        target.unlink(missing_ok=True)
        os.replace(backup, target)


def cleanup_backup_files(backup_map: list[tuple[Path, Path]]) -> None:
    for _target, backup in backup_map:
        backup.unlink(missing_ok=True)


def run_smoke_tests(
    install_dir: Path,
    *,
    timeout_sec: float = 20.0,
    commands: list[list[str]] | None = None,
) -> tuple[bool, list[str]]:
    """Smoke-test the post-swap install.

    The default check is intentionally MINIMAL: only verify that
    ``update.exe --version`` runs to completion.  This proves the
    swap produced an executable binary that loads its bundled stdlib
    (i.e. files were not corrupted/truncated) without depending on
    the host machine's runtime environment (CUDA, cuDNN, codecs…).

    Heavier verifications like ``rhythm_worker.exe --help`` are
    intentionally NOT in the default set, because those binaries
    eagerly import optional GPU/codec libraries; a missing CUDA
    path would otherwise cause the updater to roll back a perfectly
    good update — a false positive that lost the user fixes for
    reasons unrelated to the swap itself.

    Callers may pass an explicit *commands* list to opt into stricter
    smoke checks.
    """
    default_commands = [
        ["update.exe", "--version"],
    ]
    checks = commands if commands is not None else default_commands
    messages: list[str] = []
    for cmd in checks:
        if not cmd:
            continue
        argv = list(cmd)
        first = argv[0]
        candidate = Path(first)
        if not candidate.is_absolute():
            maybe_local = install_dir / first
            if maybe_local.exists():
                argv[0] = str(maybe_local)
            elif commands is None:
                # For default checks, missing binary is a hard failure.
                messages.append(f"missing binary: {first}")
                return False, messages
        try:
            proc = subprocess.run(
                argv,
                cwd=str(install_dir),
                capture_output=True,
                text=True,
                timeout=float(timeout_sec),
            )
        except Exception as exc:
            messages.append(f"{' '.join(cmd)} -> exception: {exc}")
            return False, messages
        if proc.returncode != 0:
            messages.append(
                f"{' '.join(cmd)} -> exit={proc.returncode}; "
                f"stderr={proc.stderr.strip()[:240]}"
            )
            return False, messages
        messages.append(f"{' '.join(cmd)} -> ok")
    return True, messages


def apply_staged_update(
    install_dir: Path,
    staging_dir: Path,
    files: tuple[ManifestFile, ...],
    *,
    swap_retries: int = 600,
    retry_delay_sec: float = 0.1,
    smoke_timeout_sec: float = 20.0,
    smoke_commands: list[list[str]] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> ApplyResult:
    backup_map: list[tuple[Path, Path]] = []
    install_dir = install_dir.resolve()
    staging_dir = staging_dir.resolve()
    try:
        for item in files:
            target = _safe_local_path(install_dir, item.path)
            staged_file = _safe_local_path(staging_dir, item.path)
            backup = atomic_swap_file(
                target,
                staged_file,
                retries=swap_retries,
                retry_delay_sec=retry_delay_sec,
                log_fn=log_fn,
            )
            if backup is not None:
                backup_map.append((target, backup))
        smoke_ok, smoke_msgs = run_smoke_tests(
            install_dir,
            timeout_sec=smoke_timeout_sec,
            commands=smoke_commands,
        )
        if log_fn:
            for m in smoke_msgs:
                log_fn(f"[updater] smoke: {m}")
        if not smoke_ok:
            rollback_swapped_files(backup_map)
            return ApplyResult(
                swapped_count=len(files),
                rolled_back=True,
                smoke_ok=False,
                backup_count=len(backup_map),
            )
        cleanup_backup_files(backup_map)
        shutil.rmtree(staging_dir, ignore_errors=True)
        return ApplyResult(
            swapped_count=len(files),
            rolled_back=False,
            smoke_ok=True,
            backup_count=len(backup_map),
        )
    except Exception:
        rollback_swapped_files(backup_map)
        raise

