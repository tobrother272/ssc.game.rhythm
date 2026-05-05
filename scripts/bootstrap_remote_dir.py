"""One-shot creation of /var/www/simple.rhythm with ssc:ssc ownership.

Uses paramiko + `sudo -S` so the password is supplied via stdin.
After this, the deploy user (ssc) can write to the path without sudo,
and `python build_dist.py --upload` runs without prompts.
"""
from __future__ import annotations

import shlex
import sys

import paramiko


HOST = "103.42.56.47"
PORT = 22
USER = "ssc"
PASSWORD = "Ssc@2025!"
REMOTE_DIR = "/var/www/simple.rhythm"


def run_sudo(client: paramiko.SSHClient, cmd: str) -> tuple[int, str, str]:
    """Run `sudo -S sh -c <cmd>`, supplying the password via stdin."""
    full = f"sudo -S -p '' sh -c {shlex.quote(cmd)}"
    stdin, stdout, stderr = client.exec_command(full, get_pty=False)
    stdin.write(PASSWORD + "\n")
    stdin.flush()
    rc = stdout.channel.recv_exit_status()
    return (
        rc,
        stdout.read().decode("utf-8", "replace"),
        stderr.read().decode("utf-8", "replace"),
    )


def main() -> int:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {USER}@{HOST}:{PORT}…")
    client.connect(
        hostname=HOST,
        port=PORT,
        username=USER,
        password=PASSWORD,
        allow_agent=True,
        look_for_keys=True,
        timeout=15,
    )

    cmd = (
        f"mkdir -p '{REMOTE_DIR}' && "
        f"chown -R {USER}:{USER} '{REMOTE_DIR}' && "
        f"chmod 755 '{REMOTE_DIR}' && "
        f"ls -ld '{REMOTE_DIR}'"
    )
    print(f"Running: sudo {cmd}")
    rc, out, err = run_sudo(client, cmd)
    if out:
        print(f"  stdout: {out.strip()}")
    if err:
        print(f"  stderr: {err.strip()}")
    client.close()
    if rc != 0:
        print(f"ERROR: sudo command failed (exit {rc})", file=sys.stderr)
        return 2
    print("OK — remote dir ready and owned by ssc.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
