from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ManifestFile:
    path: str
    size: int
    md5: str


@dataclass(frozen=True)
class Manifest:
    engine_version: str
    min_app_version: str
    released_at: str
    base_url: str
    files: tuple[ManifestFile, ...]


def _parse_file_entry(entry: Any) -> ManifestFile:
    if not isinstance(entry, dict):
        raise ValueError("Manifest file entry must be an object.")
    path = str(entry.get("path", "")).strip().replace("\\", "/")
    if not path:
        raise ValueError("Manifest file entry missing 'path'.")
    if path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"Unsafe manifest path: {path}")
    size_raw = entry.get("size")
    try:
        size = int(size_raw)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid size for manifest path: {path}") from None
    md5 = str(entry.get("md5", "")).strip().lower()
    if len(md5) != 32:
        raise ValueError(f"Invalid md5 for manifest path: {path}")
    return ManifestFile(path=path, size=max(0, size), md5=md5)


def parse_manifest_bytes(payload: bytes) -> Manifest:
    text = payload.decode("utf-8")
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("Manifest root must be an object.")

    files_raw = raw.get("files")
    if not isinstance(files_raw, list):
        raise ValueError("Manifest 'files' must be a list.")

    files = tuple(_parse_file_entry(entry) for entry in files_raw)
    return Manifest(
        engine_version=str(raw.get("engine_version", "")).strip(),
        min_app_version=str(raw.get("min_app_version", "")).strip(),
        released_at=str(raw.get("released_at", "")).strip(),
        base_url=str(raw.get("base_url", "")).strip(),
        files=files,
    )


def _maybe_gunzip(data: bytes, url: str, content_encoding: str | None) -> bytes:
    if content_encoding and "gzip" in content_encoding.lower():
        return gzip.decompress(data)
    if url.endswith(".gz"):
        try:
            return gzip.decompress(data)
        except OSError:
            return data
    return data


def fetch_manifest(
    manifest_url: str,
    *,
    timeout_sec: float = 20.0,
    retries: int = 3,
    backoff_sec: float = 1.0,
) -> Manifest:
    last_err: Exception | None = None
    attempt_count = max(1, int(retries))
    for attempt in range(1, attempt_count + 1):
        try:
            req = Request(
                manifest_url,
                headers={
                    "User-Agent": "SSCStudio-Updater/1.0",
                    "Accept-Encoding": "gzip",
                },
            )
            with urlopen(req, timeout=float(timeout_sec)) as resp:
                raw = resp.read()
                data = _maybe_gunzip(
                    raw,
                    manifest_url,
                    resp.headers.get("Content-Encoding"),
                )
                return parse_manifest_bytes(data)
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
            last_err = exc
            if attempt >= attempt_count:
                break
            time.sleep(float(backoff_sec) * (2 ** (attempt - 1)))
    if last_err is None:
        raise RuntimeError("Manifest fetch failed without error.")
    raise RuntimeError(f"Failed to fetch manifest: {last_err}") from last_err

