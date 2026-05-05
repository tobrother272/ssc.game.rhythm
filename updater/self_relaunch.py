from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


def _detached_creation_flags() -> int:
    flags = 0
    flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
    flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return flags


def needs_self_relaunch(*, frozen: bool, executable: Path) -> bool:
    if not frozen:
        return False
    return executable.name.lower() == "update.exe"


def is_running_copy(*, frozen: bool, executable: Path) -> bool:
    if not frozen:
        return False
    return executable.name.lower() == "update_running.exe"


def maybe_self_relaunch(
    argv: Sequence[str] | None = None,
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
    copy_fn: Callable[[Path, Path], object] | None = None,
    spawn_fn: Callable[..., object] | None = None,
) -> bool:
    frozen_now = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    exe = Path(sys.executable if executable is None else executable).resolve()
    if not needs_self_relaunch(frozen=frozen_now, executable=exe):
        return False

    running_copy = exe.with_name("update_running.exe")
    copier = copy_fn or shutil.copy2
    spawner = spawn_fn or subprocess.Popen
    copier(exe, running_copy)

    child_args = list(sys.argv[1:] if argv is None else argv)
    cmd = [str(running_copy), *child_args]
    spawner(
        cmd,
        cwd=str(exe.parent),
        close_fds=True,
        creationflags=_detached_creation_flags(),
    )
    return True


def schedule_running_copy_cleanup(
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
    spawn_fn: Callable[..., object] | None = None,
) -> bool:
    frozen_now = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    exe = Path(sys.executable if executable is None else executable).resolve()
    if not is_running_copy(frozen=frozen_now, executable=exe):
        return False

    # Avoid spawning visible shell windows.  Best-effort immediate cleanup:
    # if the file is still locked (common while process exits), we leave it
    # in place; next update run can overwrite it.
    try:
        exe.unlink(missing_ok=True)
        return not exe.exists()
    except OSError:
        return False

