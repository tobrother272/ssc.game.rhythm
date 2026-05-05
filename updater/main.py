from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime

from updater import DEFAULT_MANIFEST_URL
from updater.manifest import fetch_manifest
from updater.self_relaunch import maybe_self_relaunch, schedule_running_copy_cleanup
from updater.update_logic import (
    apply_staged_update,
    diff_manifest_against_local,
    download_and_verify_files,
    ensure_disk_space_for_download,
    list_processes_in_dir,
    terminate_pid,
    updater_lock,
    wait_for_pid_exit,
)


def _default_install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def _append_runtime_log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        if getattr(sys, "frozen", False):
            log_path = Path(sys.executable).resolve().parent / "update.log"
        else:
            log_path = Path.cwd() / "update.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def _format_size(num_bytes: int) -> str:
    value = float(max(0, int(num_bytes)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def _read_updater_version() -> str:
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    try:
        text = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or "0.0.0-dev"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SSCStudio updater CLI.")
    p.add_argument(
        "--version",
        action="store_true",
        help="Print updater version and exit.",
    )
    p.add_argument(
        "--check-only",
        action="store_true",
        help="Only check/diff files; do not download or swap.",
    )
    p.add_argument(
        "--download-only",
        action="store_true",
        help="Check + download + verify into staging; do not swap.",
    )
    p.add_argument(
        "--apply-update",
        action="store_true",
        help="Check + download + verify + atomic swap + smoke test.",
    )
    p.add_argument(
        "--manifest-url",
        default=DEFAULT_MANIFEST_URL,
        help="Manifest URL (json or json.gz).",
    )
    p.add_argument(
        "--install-dir",
        default=str(_default_install_dir()),
        help="Install directory to compare against manifest.",
    )
    p.add_argument(
        "--cache-path",
        default="",
        help="Optional override for local hash cache file.",
    )
    p.add_argument(
        "--max-hash-workers",
        type=int,
        default=6,
        help="Parallel workers for local MD5 computations.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable check result JSON.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt for --download-only.",
    )
    p.add_argument(
        "--staging-dir",
        default="",
        help="Optional staging directory override (default: <install>/_update_staging).",
    )
    p.add_argument(
        "--max-download-workers",
        type=int,
        default=4,
        help="Parallel workers for download tasks.",
    )
    p.add_argument(
        "--download-retries",
        type=int,
        default=3,
        help="Retries per file for download+verify.",
    )
    p.add_argument(
        "--timeout-sec",
        type=float,
        default=30.0,
        help="Network timeout per request (seconds).",
    )
    p.add_argument(
        "--disk-margin-mb",
        type=int,
        default=50,
        help="Safety margin added to required disk space.",
    )
    p.add_argument(
        "--lock-path",
        default="",
        help="Optional lock file path override.",
    )
    p.add_argument(
        "--swap-retries",
        type=int,
        default=600,
        help=(
            "Retry count for atomic swap on PermissionError "
            "(default 600 × 100ms = 60s of patience for sibling locks/AV)."
        ),
    )
    p.add_argument(
        "--swap-retry-delay-ms",
        type=int,
        default=100,
        help="Delay between swap retries in milliseconds.",
    )
    p.add_argument(
        "--post-wait-grace-sec",
        type=float,
        default=2.0,
        help=(
            "After --wait-for-pid succeeds, sleep this long before swapping "
            "to let sibling processes drain and OS release file handles."
        ),
    )
    p.add_argument(
        "--kill-siblings",
        action="store_true",
        default=True,
        help=(
            "After waiting for the main app PID, also terminate any other "
            "processes still running from the install directory "
            "(rhythm_worker.exe, etc.). On by default."
        ),
    )
    p.add_argument(
        "--no-kill-siblings",
        dest="kill_siblings",
        action="store_false",
        help="Disable terminating sibling processes from install directory.",
    )
    p.add_argument(
        "--smoke-timeout-sec",
        type=float,
        default=20.0,
        help="Timeout for each smoke-test command.",
    )
    p.add_argument(
        "--wait-for-pid",
        type=int,
        default=0,
        help="Optional PID to wait for before swap phase.",
    )
    p.add_argument(
        "--wait-timeout-sec",
        type=float,
        default=900.0,
        help="Max wait time for --wait-for-pid before aborting.",
    )
    return p


def _resolve_common(
    args: argparse.Namespace,
) -> tuple[Path, Path | None]:
    install_dir = Path(args.install_dir).resolve()
    cache_path = Path(args.cache_path).resolve() if args.cache_path else None
    return install_dir, cache_path


def run_check(args: argparse.Namespace) -> int:
    install_dir, cache_path = _resolve_common(args)

    _append_runtime_log(f"[updater] Checking manifest: {args.manifest_url}")
    _append_runtime_log(f"[updater] Install dir      : {install_dir}")

    manifest = fetch_manifest(args.manifest_url)
    diff = diff_manifest_against_local(
        manifest,
        install_dir,
        cache_path=cache_path,
        max_workers=max(1, int(args.max_hash_workers)),
    )

    summary = {
        "engine_version": manifest.engine_version,
        "file_count_manifest": len(manifest.files),
        "to_download_count": len(diff.to_download),
        "missing_local_count": diff.missing_local,
        "mismatched_hash_count": diff.mismatched_hash,
        "unchanged_count": diff.unchanged,
        "total_download_bytes": diff.total_download_bytes,
        "total_download_human": _format_size(diff.total_download_bytes),
        "install_dir": str(install_dir),
    }

    if args.json:
        _append_runtime_log(json.dumps(summary, indent=2))
    else:
        _append_runtime_log(f"[updater] Engine version   : {manifest.engine_version}")
        _append_runtime_log(f"[updater] Manifest files   : {len(manifest.files)}")
        _append_runtime_log(f"[updater] Unchanged        : {diff.unchanged}")
        _append_runtime_log(
            f"[updater] Need download    : {len(diff.to_download)} "
            f"({_format_size(diff.total_download_bytes)})"
        )
        _append_runtime_log(
            f"[updater] Breakdown        : missing={diff.missing_local}, "
            f"hash-mismatch={diff.mismatched_hash}"
        )
        if diff.to_download:
            _append_runtime_log("[updater] First files to update:")
            for item in diff.to_download[:20]:
                _append_runtime_log(f"  - {item.path} ({_format_size(item.size)})")
            if len(diff.to_download) > 20:
                _append_runtime_log(f"  ... and {len(diff.to_download) - 20} more")
    return 0


def run_download_only(args: argparse.Namespace) -> int:
    install_dir, cache_path = _resolve_common(args)
    staging_dir = Path(args.staging_dir).resolve() if args.staging_dir else None

    lock_path = Path(args.lock_path).resolve() if args.lock_path else None
    with updater_lock(lock_path):
        print(f"[updater] Checking manifest: {args.manifest_url}")
        print(f"[updater] Install dir      : {install_dir}")
        manifest = fetch_manifest(args.manifest_url)
        diff = diff_manifest_against_local(
            manifest,
            install_dir,
            cache_path=cache_path,
            max_workers=max(1, int(args.max_hash_workers)),
        )
        if not diff.to_download:
            print("[updater] No updates needed.")
            return 0

        ok_space, free_bytes, required_bytes = ensure_disk_space_for_download(
            install_dir,
            diff.total_download_bytes,
            safety_margin_bytes=max(0, int(args.disk_margin_mb)) * 1024 * 1024,
        )
        if not ok_space:
            print(
                "[updater] ERROR: Insufficient disk space: "
                f"need={_format_size(required_bytes)}, free={_format_size(free_bytes)}",
                file=sys.stderr,
            )
            return 3

        print(
            f"[updater] Need download    : {len(diff.to_download)} "
            f"({_format_size(diff.total_download_bytes)})"
        )
        if not args.yes:
            answer = input("Download now? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("[updater] Cancelled by user.")
                return 0

        def _progress(ev: dict[str, object]) -> None:
            print(
                "[updater] Downloaded "
                f"{ev.get('completed')}/{ev.get('total')} - {ev.get('path')}"
            )

        result = download_and_verify_files(
            manifest,
            diff.to_download,
            install_dir,
            staging_dir=staging_dir,
            max_workers=max(1, int(args.max_download_workers)),
            retries=max(1, int(args.download_retries)),
            timeout_sec=max(1.0, float(args.timeout_sec)),
            progress_cb=_progress,
        )
        print(
            "[updater] Download+verify complete: "
            f"files={result.verified_count}, bytes={_format_size(result.downloaded_bytes)}"
        )
        print(f"[updater] Staging dir      : {result.staging_dir}")
        return 0


def run_apply_update(args: argparse.Namespace) -> int:
    import time

    install_dir, cache_path = _resolve_common(args)
    staging_dir = Path(args.staging_dir).resolve() if args.staging_dir else None
    lock_path = Path(args.lock_path).resolve() if args.lock_path else None
    with updater_lock(lock_path):
        _append_runtime_log(f"[updater] Checking manifest: {args.manifest_url}")
        _append_runtime_log(f"[updater] Install dir      : {install_dir}")
        manifest = fetch_manifest(args.manifest_url)
        diff = diff_manifest_against_local(
            manifest,
            install_dir,
            cache_path=cache_path,
            max_workers=max(1, int(args.max_hash_workers)),
        )
        if not diff.to_download:
            _append_runtime_log("[updater] No updates needed.")
            return 0
        ok_space, free_bytes, required_bytes = ensure_disk_space_for_download(
            install_dir,
            diff.total_download_bytes,
            safety_margin_bytes=max(0, int(args.disk_margin_mb)) * 1024 * 1024,
        )
        if not ok_space:
            _append_runtime_log(
                "[updater] ERROR: Insufficient disk space: "
                f"need={_format_size(required_bytes)}, free={_format_size(free_bytes)}"
            )
            return 3
        _append_runtime_log(
            f"[updater] Need download    : {len(diff.to_download)} "
            f"({_format_size(diff.total_download_bytes)})"
        )
        if not args.yes:
            answer = input("Proceed with apply update? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                _append_runtime_log("[updater] Cancelled by user.")
                return 0

        def _progress(ev: dict[str, object]) -> None:
            _append_runtime_log(
                "[updater] Downloaded "
                f"{ev.get('completed')}/{ev.get('total')} - {ev.get('path')}"
            )

        dl = download_and_verify_files(
            manifest,
            diff.to_download,
            install_dir,
            staging_dir=staging_dir,
            max_workers=max(1, int(args.max_download_workers)),
            retries=max(1, int(args.download_retries)),
            timeout_sec=max(1.0, float(args.timeout_sec)),
            progress_cb=_progress,
        )
        _append_runtime_log(
            "[updater] Download+verify complete: "
            f"files={dl.verified_count}, bytes={_format_size(dl.downloaded_bytes)}"
        )
        wait_pid = int(getattr(args, "wait_for_pid", 0) or 0)
        if wait_pid > 0:
            _append_runtime_log(
                f"[updater] Waiting for process {wait_pid} to exit before swap..."
            )
            ok_wait = wait_for_pid_exit(
                wait_pid,
                timeout_sec=max(1.0, float(args.wait_timeout_sec)),
                poll_sec=0.25,
            )
            if not ok_wait:
                _append_runtime_log(
                    f"[updater] ERROR: Timeout waiting for pid {wait_pid} to exit."
                )
                return 5
            _append_runtime_log(f"[updater] Process {wait_pid} has exited.")

        # Kill any sibling processes still running from install dir
        # (rhythm_worker.exe etc. that may keep _internal/* locked).
        # update_running.exe is excluded — that's us.
        if bool(getattr(args, "kill_siblings", True)):
            try:
                running = list_processes_in_dir(install_dir)
            except Exception as exc:
                _append_runtime_log(
                    f"[updater] WARN: failed to enumerate sibling processes: {exc}"
                )
                running = []
            self_pid = os.getpid()
            for pid, exe_path in running:
                if pid == self_pid:
                    continue
                exe_name = Path(exe_path).name.lower()
                if exe_name == "update_running.exe":
                    continue
                _append_runtime_log(
                    f"[updater] Terminating sibling pid={pid} ({exe_name}) "
                    f"holding files in install dir."
                )
                terminate_pid(pid, timeout_sec=5.0)

        # Grace period: even after PID exits, OS / antivirus / file
        # system filters may keep handles for a brief moment.
        grace = max(0.0, float(getattr(args, "post_wait_grace_sec", 2.0)))
        if grace > 0:
            _append_runtime_log(
                f"[updater] Grace period {grace:.1f}s before swap "
                f"(letting OS release file handles)..."
            )
            time.sleep(grace)

        _append_runtime_log("[updater] Starting atomic swap of staged files...")
        apply_result = apply_staged_update(
            install_dir,
            dl.staging_dir,
            diff.to_download,
            swap_retries=max(1, int(args.swap_retries)),
            retry_delay_sec=max(0.01, int(args.swap_retry_delay_ms) / 1000.0),
            smoke_timeout_sec=max(1.0, float(args.smoke_timeout_sec)),
            log_fn=_append_runtime_log,
        )
        if apply_result.rolled_back:
            _append_runtime_log("[updater] Update failed smoke test. Rolled back.")
            return 4
        _append_runtime_log(
            "[updater] Update applied successfully. "
            f"swapped={apply_result.swapped_count}, "
            f"backups_cleaned={apply_result.backup_count}"
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # CLI diagnostic modes should stay attached to the caller terminal
    # (no detached self-relaunch), otherwise users see "no reaction".
    is_diagnostic_mode = any(
        flag in raw_argv for flag in ("--check-only", "--download-only", "--version")
    )
    if not is_diagnostic_mode and maybe_self_relaunch(raw_argv):
        _append_runtime_log("[updater] Relaunched via update_running.exe")
        return 0

    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        if args.version:
            _append_runtime_log(_read_updater_version())
            return 0
        if args.check_only:
            return run_check(args)
        if args.download_only:
            return run_download_only(args)
        if args.apply_update:
            return run_apply_update(args)
        parser.error("Choose one mode: --check-only or --download-only or --apply-update.")
    except Exception as exc:
        _append_runtime_log(f"[updater] ERROR: {exc}")
        return 2
    finally:
        schedule_running_copy_cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

