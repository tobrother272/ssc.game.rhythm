from __future__ import annotations

from pathlib import Path

from updater.self_relaunch import (
    is_running_copy,
    maybe_self_relaunch,
    needs_self_relaunch,
    schedule_running_copy_cleanup,
)


def test_needs_self_relaunch_flags() -> None:
    assert needs_self_relaunch(frozen=False, executable=Path("update.exe")) is False
    assert needs_self_relaunch(frozen=True, executable=Path("update.exe")) is True
    assert needs_self_relaunch(frozen=True, executable=Path("update_running.exe")) is False


def test_is_running_copy_flags() -> None:
    assert is_running_copy(frozen=False, executable=Path("update_running.exe")) is False
    assert is_running_copy(frozen=True, executable=Path("update_running.exe")) is True
    assert is_running_copy(frozen=True, executable=Path("update.exe")) is False


def test_maybe_self_relaunch_invokes_copy_and_spawn(tmp_path: Path) -> None:
    update_exe = tmp_path / "update.exe"
    update_exe.write_bytes(b"binary")
    calls: dict[str, object] = {}

    def _copy(src: Path, dst: Path) -> object:
        calls["copy"] = (src, dst)
        dst.write_bytes(src.read_bytes())
        return None

    def _spawn(cmd: list[str], **kwargs: object) -> object:
        calls["spawn"] = (cmd, kwargs)
        return object()

    relaunched = maybe_self_relaunch(
        ["--apply-update", "--yes"],
        frozen=True,
        executable=update_exe,
        copy_fn=_copy,
        spawn_fn=_spawn,
    )
    assert relaunched is True
    assert "copy" in calls
    assert "spawn" in calls
    copied_to = calls["copy"][1]
    assert Path(copied_to).name.lower() == "update_running.exe"
    spawn_cmd = calls["spawn"][0]
    assert Path(spawn_cmd[0]).name.lower() == "update_running.exe"


def test_schedule_running_copy_cleanup_spawns_delete_command(tmp_path: Path) -> None:
    running = tmp_path / "update_running.exe"
    running.write_bytes(b"x")

    scheduled = schedule_running_copy_cleanup(
        frozen=True,
        executable=running,
    )
    assert scheduled is True
    assert not running.exists()

