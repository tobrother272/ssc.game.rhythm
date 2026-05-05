# Auto-Update Engine Spec

## Mục tiêu

Triển khai hệ thống auto-update cho SSCStudio: user click **Help → Update Engine**, app tự động tải và áp dụng các file thay đổi từ VPS host về thư mục cài đặt local.

## Quyết định đã chốt (recap)

| # | Aspect | Quyết định |
|---|---|---|
| 1 | **Update scope** | Toàn bộ: SSCStudio.exe + rhythm_worker.exe + ffmpeg + _internal/ DLLs |
| 2 | **Strategy** | **B — Bootstrap updater riêng** (`update.exe`) vì cần update cả GUI |
| 3 | **Granularity** | File-level (mỗi file một entry trong manifest) |
| 4 | **Host** | VPS riêng (HTTP server) |
| 5 | **Hash** | MD5 (đủ cho integrity check) |
| 6 | **UI mode** | Async background — `update.exe` standalone process |
| 7 | **Install location** | User dir = thư mục hiện tại của SSCStudio.exe |
| 8 | **Changelog UI** | KHÔNG hiện trên UI |
| 9 | **Changelog gen** | **Auto-generate** khi chạy build script |

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  VPS Server                                               │
│                                                           │
│  https://your-vps.com/engine/                            │
│    ├── manifest.json          (file + MD5 list)          │
│    ├── changelog.md           (auto-gen từ git log)      │
│    └── files/                                            │
│        ├── SSCStudio.exe                                  │
│        ├── update.exe                                     │
│        ├── rhythm_worker.exe                              │
│        ├── ffmpeg.exe                                     │
│        ├── ffprobe.exe                                    │
│        └── _internal/                                     │
│            ├── python313.dll                              │
│            ├── PySide6/QtCore.dll                         │
│            └── ... (tất cả files)                        │
└──────────────────────────────────────────────────────────┘
                         ↑
                         │ HTTP GET
                         │
┌──────────────────────────────────────────────────────────┐
│  User machine — dist/SSCStudio/                          │
│                                                           │
│  ┌──────────────────────┐                                │
│  │  SSCStudio.exe (GUI) │                                │
│  │                       │                                │
│  │  Help → Update Engine │                                │
│  │      ↓                │                                │
│  │  Spawn update.exe     │ ──────────┐                   │
│  │      ↓                │           │                   │
│  │  Continue working     │           │                   │
│  └──────────────────────┘            │                   │
│                                       ↓                   │
│  ┌──────────────────────────────────────────────┐        │
│  │  update.exe (standalone updater)              │        │
│  │                                                │        │
│  │  1. GET manifest.json                         │        │
│  │  2. Compute local MD5s                        │        │
│  │  3. Diff → list files to download             │        │
│  │  4. Show progress UI (Qt small dialog)        │        │
│  │  5. Download to _update_staging/              │        │
│  │  6. Verify MD5 from manifest                  │        │
│  │  7. Wait for SSCStudio.exe to exit            │        │
│  │  8. Atomic swap (rename .old → move new)      │        │
│  │  9. Smoke test (launch worker --version)      │        │
│  │ 10. Cleanup .old files                        │        │
│  │ 11. Optional: relaunch SSCStudio.exe          │        │
│  └──────────────────────────────────────────────┘        │
│                                                           │
│  _update_staging/    (temp dir, deleted after swap)      │
│  *.old               (backup files, deleted after swap)  │
└──────────────────────────────────────────────────────────┘
```

---

## Manifest format

**File:** `manifest.json` (gzip-compressed: `manifest.json.gz` để giảm size)

```json
{
  "engine_version": "1.2.3",
  "min_app_version": "1.0.0",
  "released_at": "2026-05-15T10:00:00Z",
  "base_url": "https://your-vps.com/engine/v1.2.3/files/",
  "files": [
    {
      "path": "SSCStudio.exe",
      "size": 12345678,
      "md5": "abc123def456..."
    },
    {
      "path": "update.exe",
      "size": 5000000,
      "md5": "fed987..."
    },
    {
      "path": "rhythm_worker.exe",
      "size": 90000000,
      "md5": "789abc..."
    },
    {
      "path": "ffmpeg.exe",
      "size": 80000000,
      "md5": "111aaa..."
    },
    {
      "path": "_internal/python313.dll",
      "size": 5500000,
      "md5": "222bbb..."
    },
    {
      "path": "_internal/PySide6/QtCore.pyd",
      "size": 8000000,
      "md5": "333ccc..."
    }
    // ... thousands more for _internal/
  ]
}
```

**Quy ước:**
- `path` = relative từ `dist/SSCStudio/` root, dùng forward slash `/`
- `size` = bytes (để pre-check disk space)
- `md5` = lowercase hex
- `base_url` = prefix cho file URL: full URL = `base_url + path`

**Versioned URLs**: dùng `/engine/v{version}/files/` để mỗi version có folder riêng → cho phép rollback bằng cách đổi manifest URL về version cũ.

---

## Self-update problem cho `update.exe`

`update.exe` cũng nằm trong manifest và có thể cần update. Nhưng **không thể tự ghi đè khi đang chạy**.

**Giải pháp**: copy-and-relaunch pattern.

```
1. update.exe khởi động.
2. Detect: đang chạy từ "update.exe" hay "update_running.exe"?
3. Nếu là "update.exe" gốc:
   - Copy chính mình thành "update_running.exe"
   - Spawn "update_running.exe" + exit ngay
4. "update_running.exe" làm toàn bộ update logic.
5. Khi swap files, có thể swap luôn "update.exe" gốc (vì không bị lock).
6. Sau cleanup, "update_running.exe" có thể tự xóa (qua batch file delay) hoặc để lần sau.
```

Đơn giản hơn: chỉ cần "update.exe" có path khác với file cần swap. Detect via argv[0].

---

## Update.exe lifecycle

### Khởi động

```
1. Parse args: 
   --check-only         (chỉ kiểm tra, không tải)
   --silent             (không show UI, dùng cho scheduled check)
   --auto-restart       (relaunch SSCStudio sau update)

2. Detect mode:
   - Self-copy nếu chạy từ "update.exe" gốc
   - Continue nếu chạy từ "update_running.exe"

3. Lock file check (prevent concurrent update):
   - Tạo "update.lock" trong %TEMP%
   - Nếu lock đã tồn tại + process còn live → exit với error message
```

### Check phase

```
4. Show UI: "Checking for updates..."
5. HTTP GET manifest.json (with retry: 3 attempts, exponential backoff)
6. Parse manifest, validate schema
7. Show: "Engine version available: 1.2.3"

8. Compute local MD5s (parallel, threadpool):
   - For each file in manifest:
     - Local exists? → compute MD5
     - Local NOT exists? → mark for download
   
9. Diff:
   - mismatch → mark for download
   - match → skip
   - extra files local (not in manifest) → optionally remove (V2 feature)

10. Show summary: "X files to update, Y MB total. Download?"
```

### Download phase

```
11. User confirm → start download.

12. Pre-check disk space: free >= total_download_size + safety_margin (50MB).
   If insufficient → abort with clear error.

13. Create _update_staging/ in install dir.

14. Download each file (parallel, max 4 concurrent):
    - URL = manifest.base_url + file.path
    - Save to _update_staging/file.path
    - HTTP Range support nếu resume
    - Show progress: per-file + overall
    - Show speed (MB/s) + ETA

15. Verify each downloaded file:
    - Compute MD5
    - Compare with manifest
    - Mismatch → re-download (max 3 retries) → if still fail, abort
```

### Swap phase

```
16. Wait for SSCStudio.exe to exit:
    - Poll PID via psutil or tasklist
    - Show: "Waiting for SSCStudio to close..."
    - User can close SSCStudio manually
    - OR offer button "Close SSCStudio now" (sends WM_CLOSE)

17. Atomic swap loop:
    For each file in download list:
       a. If target exists: rename(target, target + ".old")
       b. Move staging/file → target
       c. Track .old files for rollback

18. Smoke test:
    - Launch rhythm_worker.exe --help (check exit code 0)
    - Launch update.exe --version (verify update.exe healthy)

19. If smoke test pass:
    - Delete all .old files
    - Delete _update_staging/
    - Show: "Update complete!"

20. If smoke test fail:
    - Rollback: rename(target + ".old", target) for all swapped files
    - Show: "Update failed, rolled back to previous version."
    - Save log to update_failure.log

21. Optional: if --auto-restart, spawn SSCStudio.exe and exit.
22. Release lock file.
```

---

## Atomic swap chi tiết Windows

Windows cho phép `rename` mà target tồn tại nếu cùng filesystem (qua MoveFileEx with MOVEFILE_REPLACE_EXISTING). Python `os.replace()` dùng API này.

```python
# Pseudocode
import os
from pathlib import Path

def atomic_swap(target: Path, new_file: Path) -> Path | None:
    """Swap target with new_file. Returns backup path for rollback, or None."""
    backup = target.with_suffix(target.suffix + ".old")
    if target.exists():
        if backup.exists():
            backup.unlink()  # remove stale backup
        os.replace(target, backup)
    os.replace(new_file, target)
    return backup if target.exists() else None
```

**Edge case**: nếu target đang BỊ LOCK (vd SSCStudio chưa exit hẳn), `os.replace` raise `PermissionError`. Cần catch + retry sau 100ms.

**Rollback**:

```python
def rollback(backup_map: dict[Path, Path]) -> None:
    """backup_map: {target: backup_path}"""
    for target, backup in backup_map.items():
        if target.exists():
            target.unlink()
        os.replace(backup, target)
```

---

## Build script changes (`build_dist.py`)

Thêm 3 việc mới sau khi build xong:

### 1. Build `update.exe`

Tạo `update.spec` (PyInstaller spec mới):

```python
# update.spec — minimal updater app
a = Analysis(
    ['updater/main.py'],     # NEW: source code của updater
    ...
    excludes=['cv2', 'numpy', 'scipy', 'librosa', 'PySide6.QtMultimedia',
              'PIL', 'pydub', 'librosa', 'matplotlib', 'tkinter', 'test'],
    # update.exe chỉ cần: PySide6.QtCore + Widgets, requests, hashlib (built-in)
)
exe = EXE(..., name='update', console=False, ...)
```

Output: `dist/update/update.exe` (~30-40MB với PySide6 minimal).

Merge vào `dist/SSCStudio/update.exe` ở step `merge_outputs()`.

### 2. Generate manifest

Function mới `generate_manifest()`:

```python
def generate_manifest() -> None:
    banner("Step 7 — Generating manifest.json")
    
    files = []
    for f in DIST_DIR.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(DIST_DIR)
        # Skip update artifacts
        if rel.parts and rel.parts[0] in {"_update_staging", "*.old", "*.lock"}:
            continue
        md5_hash = compute_md5(f)
        files.append({
            "path": str(rel).replace("\\", "/"),
            "size": f.stat().st_size,
            "md5": md5_hash,
        })
    
    manifest = {
        "engine_version": ENGINE_VERSION,    # NEW: read from VERSION file
        "min_app_version": "1.0.0",
        "released_at": datetime.utcnow().isoformat() + "Z",
        "base_url": f"https://your-vps.com/engine/v{ENGINE_VERSION}/files/",
        "files": files,
    }
    
    manifest_path = DIST_DIR.parent / "manifest.json"
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    
    # Gzip
    with open(manifest_path, 'rb') as fin, gzip.open(manifest_path.with_suffix('.json.gz'), 'wb') as fout:
        shutil.copyfileobj(fin, fout)
    
    print(f"  Generated manifest with {len(files)} files")
```

### 3. Auto-generate changelog

Function mới `generate_changelog()`:

```python
def generate_changelog() -> str:
    """Generate changelog from git log between last tag and HEAD."""
    banner("Step 8 — Generating changelog from git log")
    
    # Find last tag (assumes git tags like "v1.2.3")
    try:
        last_tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=ROOT, text=True
        ).strip()
    except subprocess.CalledProcessError:
        last_tag = ""
    
    range_spec = f"{last_tag}..HEAD" if last_tag else "HEAD"
    
    # Git log format: commit hash + subject
    log_output = subprocess.check_output(
        ["git", "log", range_spec, "--pretty=format:%h %s"],
        cwd=ROOT, text=True
    )
    
    if not log_output.strip():
        return "No changes since last release."
    
    # Categorize commits by prefix
    feats, fixes, others = [], [], []
    for line in log_output.split("\n"):
        if not line.strip():
            continue
        if "feat:" in line.lower() or "feature:" in line.lower():
            feats.append(line)
        elif "fix:" in line.lower() or "bug:" in line.lower():
            fixes.append(line)
        else:
            others.append(line)
    
    md = f"# Changelog — v{ENGINE_VERSION}\n\n"
    md += f"Released: {datetime.utcnow().isoformat()}\n\n"
    if feats:
        md += "## Features\n" + "\n".join(f"- {l}" for l in feats) + "\n\n"
    if fixes:
        md += "## Bug fixes\n" + "\n".join(f"- {l}" for l in fixes) + "\n\n"
    if others:
        md += "## Other\n" + "\n".join(f"- {l}" for l in others) + "\n\n"
    
    changelog_path = DIST_DIR.parent / "changelog.md"
    changelog_path.write_text(md)
    print(f"  Generated changelog: {len(feats)} features, {len(fixes)} fixes, {len(others)} other")
    return md
```

### 4. Optional: Upload to VPS

Nếu muốn auto-deploy:

```python
def upload_to_vps() -> None:
    banner("Step 9 — Uploading to VPS (optional)")
    
    # Use rsync over SSH (requires ssh-key setup)
    target = f"user@your-vps.com:/var/www/engine/v{ENGINE_VERSION}/"
    
    run([
        "rsync", "-avz", "--progress",
        "--delete",
        str(DIST_DIR.parent / "manifest.json"),
        str(DIST_DIR.parent / "manifest.json.gz"),
        str(DIST_DIR.parent / "changelog.md"),
        f"{target}",
    ])
    
    run([
        "rsync", "-avz", "--progress",
        f"{DIST_DIR}/",
        f"{target}files/",
    ])
    
    # Update "latest" symlink
    run(["ssh", "user@your-vps.com",
         f"ln -sfn /var/www/engine/v{ENGINE_VERSION} /var/www/engine/latest"])
```

CLI flag: `--upload` (default off để tránh accident).

### `build_dist.py` mới CLI

```
python build_dist.py [--skip-obfuscate] [--skip-ffmpeg] [--clean]
                      [--no-manifest] [--no-changelog] [--upload]
                      [--engine-version 1.2.3]
```

---

## Versioning

**File `VERSION` ở project root** (text file 1 line):

```
1.2.3
```

`build_dist.py` đọc file này. CLI `--engine-version` override.

Sau build, tag git: `git tag v1.2.3`. Lần build sau, changelog generate từ `v1.2.3..HEAD`.

---

## Update.exe — implementation outline

**Thư mục mới**: `updater/`

**Files:**

```
updater/
├── main.py              ← entry point (Qt window)
├── update_logic.py      ← download + verify + swap logic
├── manifest.py          ← parse manifest, compute diff
├── ui.py                ← Qt progress dialog
└── self_relaunch.py     ← copy-and-spawn pattern
```

**Dependencies tối thiểu:**
- `PySide6.QtCore`, `QtWidgets` (small)
- `requests` hoặc `urllib` built-in (HTTP)
- `hashlib` built-in (MD5)
- `concurrent.futures` (parallel hash + download)

KHÔNG cần: numpy, scipy, librosa, cv2, PIL, pydub. Excludes trong `update.spec`.

---

## SSCStudio.exe changes (GUI integration)

**File:** `studio/editor/main_window.py` (hoặc menu file riêng)

**Thêm menu action:**

```python
# Help menu
help_menu = menubar.addMenu("&Help")

update_action = help_menu.addAction("Update &Engine")
update_action.triggered.connect(self._on_update_engine)
```

**Handler:**

```python
def _on_update_engine(self) -> None:
    """Spawn update.exe in background and continue running."""
    from pathlib import Path
    import subprocess
    import sys
    
    if getattr(sys, "frozen", False):
        bundle_dir = Path(sys.executable).parent
        updater = bundle_dir / "update.exe"
    else:
        # Dev mode: run updater Python entry
        updater = None  # Show "Update only available in installed version"
    
    if updater is None or not updater.exists():
        QMessageBox.information(
            self, "Update Engine",
            "Update is only available in the installed version."
        )
        return
    
    # Spawn detached
    subprocess.Popen(
        [str(updater)],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    
    # User can keep working
    self.statusBar().showMessage(
        "Update Engine started in background. You can keep working.", 5000
    )
```

---

## File-level optimization

10k+ files trong `_internal/` → manifest có thể vài MB JSON. Optimization:

1. **Gzip manifest** (giảm 80-90%): `manifest.json.gz` ~200KB.
2. **Cache local hashes**: lưu `local_hashes.json` với (path, mtime, md5). Lần check sau, nếu mtime không đổi → dùng cache, không re-compute MD5.
3. **Parallel MD5 compute**: thread pool 4-8 workers.
4. **HTTP/2 multiplexing**: nếu VPS hỗ trợ, parallel download nhanh hơn.
5. **Delta encoding** (V2): chỉ tải diff binary thay vì full file (bsdiff). Phức tạp, V1 không cần.

---

## VPS deployment

### Server requirements

- Nginx hoặc Apache với HTTPS
- Static file serving
- Gzip compression bật (cho `.json` files)
- Đủ disk cho mỗi version (~500MB)
- Bandwidth proportional to user base

### Folder layout

```
/var/www/engine/
├── latest -> v1.2.3/         (symlink)
├── v1.0.0/
│   ├── manifest.json
│   ├── manifest.json.gz
│   ├── changelog.md
│   └── files/...
├── v1.1.0/
└── v1.2.3/
```

### Client URL convention

```
https://your-vps.com/engine/latest/manifest.json.gz
https://your-vps.com/engine/latest/files/SSCStudio.exe

# Or specific version (rollback):
https://your-vps.com/engine/v1.0.0/manifest.json.gz
```

### Nginx config snippet

```nginx
location /engine/ {
    root /var/www;
    autoindex off;
    
    # Gzip for manifests
    gzip on;
    gzip_types application/json text/plain;
    
    # CORS (if updater fetches from app)
    add_header Access-Control-Allow-Origin *;
    
    # Cache binary files long-term (versioned URLs)
    location ~ ^/engine/v[0-9]+\.[0-9]+\.[0-9]+/files/ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
    
    # Cache manifest short-term
    location ~ ^/engine/.+/manifest\.json(\.gz)?$ {
        expires 5m;
    }
}
```

---

## Touch points

### Files mới

1. **`updater/main.py`** — Update.exe entry point
2. **`updater/update_logic.py`** — Core update logic
3. **`updater/manifest.py`** — Manifest parser + diff
4. **`updater/ui.py`** — Qt dialog (progress + status)
5. **`updater/self_relaunch.py`** — Copy-and-spawn helper
6. **`updater/__init__.py`** — Package marker
7. **`update.spec`** — PyInstaller spec for update.exe
8. **`VERSION`** — Single-line version file (e.g. "1.0.0")

### Files modified

1. **`build_dist.py`** — Add: build update.exe, generate manifest, generate changelog, optional upload
2. **`studio/editor/main_window.py`** — Add Help → Update Engine menu + handler
3. **`.gitignore`** — Add `dist/manifest*.json*`, `dist/changelog.md`, `_update_staging/`, `*.old`

### VPS setup

1. Install Nginx/Apache with HTTPS
2. Create `/var/www/engine/` folder
3. Upload first release manually
4. Configure CI/CD or manual upload via `--upload` flag

---

## Test scenarios

### Test 1: First-time update (no local install)

```
Setup: fresh install of SSCStudio v1.0.0
Server: manifest v1.2.3 with 100 changed files
Action: Help → Update Engine
Verify:
  - update.exe launches
  - Shows "Found 100 updates, 250 MB"
  - Downloads in background
  - Verifies all hashes
  - Waits for SSCStudio to close (manual)
  - Swaps files atomically
  - Smoke test passes
  - Cleanup .old files
```

### Test 2: Incremental update (only worker changed)

```
Setup: v1.2.0 installed
Server: v1.2.1 with only rhythm_worker.exe changed
Action: Update Engine
Verify:
  - Only rhythm_worker.exe (~90MB) downloaded
  - Other files skipped (MD5 match)
  - Quick swap
  - Total time < 1 minute on broadband
```

### Test 3: Update.exe self-update

```
Setup: v1.2.0 installed (update.exe v1.0)
Server: v1.2.1 with update.exe v1.1
Action: Update Engine
Verify:
  - update.exe v1.0 self-copies → update_running.exe
  - update_running.exe downloads + swaps update.exe v1.1
  - Cleanup: update_running.exe self-deletes (via batch with delay)
```

### Test 4: SSCStudio still running (wait scenario)

```
Setup: SSCStudio.exe đang chạy
Action: Update Engine → all files downloaded
Verify:
  - update.exe shows "Waiting for SSCStudio to close..."
  - Provides "Close SSCStudio" button
  - User clicks → WM_CLOSE sent → SSCStudio exits
  - Swap proceeds
```

### Test 5: Network failure during download

```
Setup: download in progress
Action: disconnect network
Verify:
  - update.exe retries 3 times with backoff
  - If still fail: shows error, keeps staging files for resume
  - User can retry → Range header resume from where left off
```

### Test 6: Hash mismatch after download

```
Setup: VPS serves corrupted file (manually corrupted)
Verify:
  - update.exe detects MD5 mismatch
  - Re-downloads (max 3 retries)
  - If still mismatch: aborts update, no swap
  - Shows: "Download corrupted, please retry later"
```

### Test 7: Smoke test fail → rollback

```
Setup: download new files but rhythm_worker.exe is broken (hypothetical bad release)
Action: swap → smoke test (rhythm_worker.exe --help) returns non-zero
Verify:
  - Rollback: all .old files renamed back to target
  - Old SSCStudio + worker still functional
  - Shows: "Update failed, restored to previous version"
```

### Test 8: Disk space insufficient

```
Setup: only 50MB free disk, total update = 500MB
Verify:
  - Pre-check detects insufficient space
  - Aborts BEFORE download starts
  - Shows: "Insufficient disk space (need 500MB, have 50MB)"
```

### Test 9: Concurrent update attempt

```
Setup: update.exe đang chạy
Action: User click Update Engine again
Verify:
  - Lock file detected
  - Second update.exe shows: "Update already in progress"
  - Exits gracefully
```

### Test 10: Permission denied (admin install)

```
Setup: SSCStudio cài Program Files (admin write only)
Action: update from non-admin user
Verify:
  - Swap fails với PermissionError
  - update.exe shows: "Run as administrator to update, or move SSCStudio to user directory"
  - No partial state
```

### Test 11: Build script changelog gen

```
Setup: 5 commits since last tag, mix of feat/fix/other
Action: python build_dist.py --engine-version 1.2.3
Verify:
  - changelog.md generated
  - Categorized: Features (feat: prefix), Bug fixes (fix: prefix), Other
  - Linked to git commit hashes
```

### Test 12: Build script manifest gen

```
Setup: dist/SSCStudio/ has 10000 files
Action: python build_dist.py
Verify:
  - manifest.json generated với 10000 entries
  - manifest.json.gz also generated
  - All MD5s computed
  - Total time < 5 minutes (parallel hashing)
```

### Test 13: VPS upload

```
Setup: build complete, --upload flag
Action: python build_dist.py --upload --engine-version 1.2.3
Verify:
  - rsync uploads to vps:/var/www/engine/v1.2.3/
  - latest symlink updated
  - Files accessible via HTTPS
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **`SSCStudio.exe` standalone build**: `build_dist.py` workflow giữ nguyên cho dev (không cần manifest nếu chỉ test local).

2. **Existing `dist/SSCStudio/` structure**: cấu trúc folder không đổi. Update chỉ thêm `update.exe` + temporary `_update_staging/`.

3. **`rhythm_worker.exe` spawn từ GUI**: pattern hiện tại không đụng. Worker vẫn được spawn qua `bundle_paths.get_rhythm_command()`.

4. **Dev mode**: nếu `sys.frozen` False, "Update Engine" menu item disable hoặc show message. Không cố spawn update.exe vì không tồn tại.

5. **PyArmor obfuscation**: update.exe nếu chạy qua obfuscated source vẫn work (PyArmor không touch hash logic). Nhưng cần verify rằng updater main.py KHÔNG bị obfuscate (cần direct entry point).

6. **FFmpeg auto-download trong build_dist.py**: giữ nguyên. FFmpeg cũng được track trong manifest, sẽ tự update qua engine update.

7. **Existing `--clean` flag**: giữ nguyên hành vi. Có thể thêm option clean cả manifest/changelog files.

---

## Pattern code hiện có để tham khảo

- **`bundle_paths.find_ffmpeg()`** trong `src/bundle_paths.py`: pattern detect frozen vs dev. Updater dùng tương tự để find install dir.

- **`rhythm_worker_entry.py`**: pattern PyInstaller entry script. Updater entry tương tự.

- **`build_dist.py`**: pattern subprocess + banner + step-by-step. Add steps 7-9 dùng cùng pattern.

- **`subprocess.Popen` detached** (Windows): pattern spawn không block parent. Dùng cho launch update.exe từ SSCStudio.

---

## Thứ tự implement đề xuất

### Phase 1: Update.exe core (no UI)

1. **Tạo `updater/` package** với `manifest.py`, `update_logic.py`.
2. **CLI prototype**: `python -m updater --check-only` đọc manifest, compare MD5, list diffs (no download yet).
3. **Test với mock manifest**: tạo manifest local, verify diff logic.
4. **Add download**: parallel download với requests + retry. Save to staging.
5. **Add MD5 verify**: check sau download, retry nếu fail.

### Phase 2: Atomic swap + smoke test

6. **Add swap logic**: rename → move pattern.
7. **Add smoke test**: launch rhythm_worker.exe --help.
8. **Add rollback**: revert .old files nếu smoke fail.
9. **Test atomic swap**: simulate file lock, verify retry + error.

### Phase 3: UI (Qt)

10. **Tạo `updater/ui.py`**: progress dialog với Qt.
11. **Wire UI vào logic**: progress callback từ download.
12. **Test UI**: visual verify.

### Phase 4: Self-update

13. **Implement copy-and-relaunch** trong `self_relaunch.py`.
14. **Test self-update**: verify update.exe có thể tự update mình.

### Phase 5: Build integration

15. **Tạo `update.spec`** PyInstaller spec.
16. **Modify `build_dist.py`**: add Step 7 (build update.exe), Step 8 (manifest), Step 9 (changelog).
17. **Test full build**: `python build_dist.py` → verify manifest + changelog generated.

### Phase 6: VPS deployment

18. **Setup VPS Nginx**: configure paths + HTTPS + gzip.
19. **First manual upload**: rsync v1.0.0 lên.
20. **Test client → VPS**: from clean install, run updater, verify all files download.

### Phase 7: GUI integration

21. **Add Help → Update Engine menu** trong `main_window.py`.
22. **Test E2E**: từ GUI click menu → spawn updater → update completes → verify new version works.

### Phase 8: CI/CD (optional)

23. **GitHub Actions**: on git tag push → build → upload to VPS.
24. **Auto changelog từ commits**.

---

## Acceptance criteria

✓ User click Help → Update Engine, update.exe launches without blocking SSCStudio  
✓ Manifest fetched from VPS qua HTTPS  
✓ Local MD5s computed in parallel, cached for next check  
✓ Only changed files downloaded (skip unchanged)  
✓ Files verified by MD5 after download  
✓ Atomic swap with rollback on smoke test fail  
✓ SSCStudio.exe self-update works (via wait + swap pattern)  
✓ update.exe self-update works (copy-and-spawn pattern)  
✓ Concurrent update prevented via lock file  
✓ Disk space pre-checked  
✓ Network retry với exponential backoff  
✓ Build script auto-generates manifest.json + changelog.md from git log  
✓ VPS upload via `--upload` flag (rsync)  
✓ Versioned URLs cho rollback capability  

---

## Open questions

(1) **Update tự động vs manual**: chỉ user-triggered (Help → Update Engine) hay có check-on-startup auto? Tôi đề xuất V1 manual only, V2 add background check + notification.

(2) **Update notification trong GUI**: SSCStudio có cần show "Update available" badge khi có version mới? V1 manual = user phải click để biết. V2 background check + badge.

(3) **Beta channel**: có cần `?channel=beta` cho users thử nghiệm phiên bản mới? V1 không cần, V2 add.

(4) **Crash report**: nếu update fail, có gửi log lên VPS để bạn debug? Cần consent của user.

(5) **`update.exe` có cần code-sign**? V1 không cần (Windows SmartScreen sẽ warn lần đầu). V2 nên có nếu phân phối rộng.

(6) **Manifest format JSON vs binary** (vd Protobuf): JSON dễ debug + edit. V1 JSON. V2 nếu manifest > 5MB cân nhắc binary.

(7) **VPS bandwidth cost**: nếu user base lớn, mỗi update 500MB × N users → cost. Cân nhắc CDN (Cloudflare free tier OK cho < 100GB/tháng).

(8) **Multi-OS support**: spec này focus Windows. Mac/Linux tương lai? Update logic giống nhau, chỉ khác file extensions + atomic swap semantics. Để sau.

(9) **`min_app_version` enforcement**: nếu engine v2.0 không tương thích app v1.x, refuse update + prompt reinstall. Bạn confirm logic này?

(10) **Removed files cleanup**: nếu file local KHÔNG có trong manifest mới (deprecated), có nên auto-delete không? Risk: xóa nhầm user data. V1 KHÔNG xóa. V2 explicit `removed_files` list trong manifest.
