"""One-shot SSH key bootstrap for the deploy server.

Reads ~/.ssh/id_ed25519.pub locally and appends it to the server's
~/.ssh/authorized_keys.  Runs over a single password-authenticated
SSH session via paramiko, so the password is only used once.

After this completes, `ssh user@host` and `scp` work without password
and `python build_dist.py --upload` runs end-to-end automatically.

This script is intentionally a one-off bootstrap (not part of the
normal build flow); paramiko is required only for this single run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko


HOST = "103.42.56.47"
PORT = 22
USER = "ssc"
PASSWORD = "Ssc@2025!"

PUBKEY_PATH = Path(os.environ["USERPROFILE"]) / ".ssh" / "id_ed25519.pub"


def main() -> int:
    if not PUBKEY_PATH.exists():
        print(f"ERROR: public key not found: {PUBKEY_PATH}", file=sys.stderr)
        return 1
    pubkey = PUBKEY_PATH.read_text(encoding="utf-8").strip()
    if not pubkey:
        print(f"ERROR: public key file is empty: {PUBKEY_PATH}", file=sys.stderr)
        return 1
    print(f"Local pubkey: {pubkey[:60]}…")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Connecting to {USER}@{HOST}:{PORT} (password auth)…")
    client.connect(
        hostname=HOST,
        port=PORT,
        username=USER,
        password=PASSWORD,
        allow_agent=False,
        look_for_keys=False,
        timeout=15,
    )

    # Idempotent install: ensure ~/.ssh exists with right perms, then
    # append the key only if not already present.
    remote_cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"grep -qxF '{pubkey}' ~/.ssh/authorized_keys || "
        f"echo '{pubkey}' >> ~/.ssh/authorized_keys"
    )
    print("Installing pubkey into ~/.ssh/authorized_keys…")
    stdin, stdout, stderr = client.exec_command(remote_cmd)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    if out:
        print(f"  stdout: {out.strip()}")
    if err:
        print(f"  stderr: {err.strip()}")
    if rc != 0:
        print(f"ERROR: remote command failed (exit {rc})", file=sys.stderr)
        client.close()
        return 2

    # Sanity check: count authorized_keys entries
    stdin, stdout, stderr = client.exec_command("wc -l ~/.ssh/authorized_keys")
    out = stdout.read().decode("utf-8", "replace").strip()
    print(f"  authorized_keys: {out}")

    client.close()
    print("OK — pubkey installed.  Try: ssh ssc@103.42.56.47 echo ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
