# SSC Studio — Project Specification

> Human Tetris Studio — Internal build (`sscinternal`)  
> Rhythm-game video renderer + timeline editor built with PySide6 + OpenCV.

---

## 1. Tổng quan

SSC Studio là ứng dụng desktop cho phép biên tập và render video nhạc theo phong cách "Human Tetris / Rhythm Game" (first-person 3D tunnel, khối bay theo beat nhạc, stickman HUD). User nhập nhạc, chia timeline thành nhiều segment, tinh chỉnh beat, xem live preview và export ra file `.mp4`.

---

## 2. Tính năng chính

### Timeline & Segment
- Tạo / xóa / kéo-thả / chia / ghép / nhân đôi segment trên timeline kéo giãn được.
- Drag pixel-by-pixel với ripple-right (segment phía sau tự né khi kéo vào).
- **Pack segments**: tự gộp toàn bộ segment về t=0, không có khoảng trống.
- **Overview bar** cố định 200 px/segment để nhìn tổng quan.
- Undo / Redo (Qt `QUndoStack`).
- Waveform strip theo từng segment (RMS realtime).

### Beat Detection & Editing
- Tự động phát hiện beat từ audio (`tempo` / `beat` / `onset` via librosa).
- Hiển thị beat ticks trên timeline; user kéo / thêm / xóa từng tick.
- Tham số: beat sensitivity, beat source, BPM override, beat_min_gap, subdivide.
- Lọc theo `beat_height_threshold` và `min_beat_spacing_sec`.

### Live Preview (in-process)
- Render từng frame qua `LiveFrameRenderer` (không encode, không subprocess).
- Hot-reload toàn bộ config ngay frame kế tiếp: mode, floor panel, stickman, màu sắc, image…
- Playhead đồng bộ với `QMediaPlayer`; seek tự do không cần restart.
- Stickman HUD kéo thả để đặt vị trí trên preview.
- Loading spinner + disable button khi renderer đang khởi tạo.

### Render & Export
- Render từng segment hoặc toàn bộ ra `.mp4` (H.264 via FFmpeg).
- **Export Dialog**: song song nhiều segment (`N worker`), progress bar từng segment, hiển thị lỗi chi tiết, tự concat sau khi render xong.
- Chỉ render segment chưa có `video_path`; segment đã render giữ nguyên.
- Export popup không tự tắt; user chủ động đóng.

### Segment Config (Properties Panel)
- Cấu hình mode: `punch`, `dance`, `line`, `relax`, `combo`.
- Toàn bộ render_settings: speed, density, beat_source, max_per_lane, stickman on/off, bloom, floor_panels, cube color/image/model, pair cycles, line_beats…
- **Floor Panel Options** (section ẩn/hiện theo toggle):
  - Color picker chọn màu neon tile.
  - Blink to beat (nhấp nháy theo nhịp).
  - Tile image: load ảnh, warp perspective lên từng tile.
- Scroll-wheel trên mọi spinbox/combobox bị khóa để tránh đổi giá trị nhầm.

### Media Library
- Import video / audio / image.
- Hiển thị thumbnail, kéo thả vào timeline.
- Waveform + thumbnail generation background.

### Authentication
- Login bằng username/password → JWT từ `https://fmmonitor.sscapi.co`.
- Remember-me lưu token / credential vào keyring OS.
- Auto-restore token khi khởi động lại.

### Worker Update
- Button **⬇ Update Worker** trên toolbar.
- Download `rhythm_worker.exe` từ server (`https://toolmgt.mksoft.io/simple.rhythm/rhythm_worker.exe`).
- Progress bar + status message realtime; chạy trên QThread riêng.

---

## 3. Công nghệ chính

| Thành phần | Công nghệ |
|---|---|
| GUI | PySide6 (Qt 6) |
| Video render | OpenCV (`cv2`) — software rasterizer, no GPU required |
| Audio analysis | librosa, soundfile, audioread |
| Audio mux | FFmpeg subprocess (`-c:v copy`) |
| Beat detection | librosa `beat_track`, `onset_detect`, `tempo` |
| HTTP / Auth | httpx, keyring |
| Data models | Pydantic v2 (`BaseModel`) |
| Serialization | JSON (`.htproj`) |
| Packaging | PyInstaller 6 — hai target: `SSCStudio.exe` + `rhythm_worker.exe` |
| GPU (optional) | CuPy / cuFFT — tự detect, fallback CPU |
| 3D projection | Software (NumPy, `PerspectiveCamera` custom) |
| Threading | `QThread` (preview, render), `threading.Thread` (concat, download) |
| Undo/Redo | `QUndoStack` / `QUndoCommand` |

---

## 4. Core Functions

### `RhythmVisualizer.process_video` (`src/rhythm.py`)
Pipeline render chính:
1. Librosa load audio → beat frames (hoặc nhận `--beat_source array`).
2. `GameManager.pre_schedule` → lên lịch targets + stickman events.
3. Vòng lặp per-frame: `TunnelRenderer.draw` → targets → particles → stickman HUD → viewport frame → bloom.
4. Encode sang temp `.mp4` (H.264 NVENC nếu CuPy, else libx264).
5. `merge_audio`: mux AAC audio với `-c:v copy`.

### `LiveFrameRenderer` (`src/live_renderer.py`)
- Load audio **một lần** khi khởi tạo.
- `render_at(t_sec)` → trả `np.ndarray` BGR frame tại thời điểm t.
- `update_beats(times)` → hoán đổi beat array, rebuild schedule (< 100ms).
- `update_mode(mode, **kwargs)` → rebuild toàn bộ scene (cam/tunnel/HUD). Hot-reload mọi config.

### `GameManager.pre_schedule` (`src/rhythm.py`)
- Nhận beat_frames, áp `density`, `lane_filter`, `min_lane_gap`.
- Alternating L↔R, sub-lane cycling, walls on bass.
- Paired punch/dance cycle (`punch_pair_cycle`, `dance_pair_cycle`).
- Combo mode: round-robin target class per beat.
- Output: list targets + stickman event stream.

### `TunnelRenderer.draw` (`src/rhythm.py`)
- Perspective-project 3D floor tiles → trapezoid polys.
- Lane-aligned (dance) hoặc legacy 2-column (punch).
- Custom color, blink (`frame//15 % 2`), image warp (`cv2.warpPerspective`).
- Neon edge glow, scroll animation.

### `RenderService` (`studio/core_bridge/render_service.py`)
- Nhận `RenderJob` (segment + output path + beat_times + settings).
- Chạy subprocess: `rhythm_worker.exe` (prod) hoặc `python src/rhythm.py` (dev).
- Parse stdout progress → emit `progress(segment_id, pct)`.
- Emit `finished` / `failed` / `trimmed`.

### `AudioTrimService` (`studio/core_bridge/audio_trim_service.py`)
- FFmpeg `-ss start -t duration -i src -c copy out`.
- Lưu vào `<project>/temps/audio_<segment_id>.<ext>`.

### `BeatDetectService` (`studio/core_bridge/beat_detect_service.py`)
- Chạy `rhythm_worker.exe --detect_only --export_events <tmp.json>`.
- Parse JSON → list `(t, kind, height)` → emit `ready`.

---

## 5. Cấu trúc Project

```
ssc.game.rhythm/
│
├── src/                          # Engine / render core
│   ├── rhythm.py                 # Main renderer, CLI, game engine
│   ├── live_renderer.py          # In-process preview renderer
│   ├── stickman.py               # 2D stickman HUD + scheduler
│   ├── bundle_paths.py           # Frozen vs dev path resolver
│   ├── rhythm_worker_entry.py    # PyInstaller entry wrapper
│   ├── authorization.py          # Token auth stub
│   ├── visible.py / bubble.py / lightle.py  # Other effects (legacy)
│   ├── main.py                   # Legacy audio-reactive composer
│   └── ...utils, zoom, filter, combine, srt_merge
│
├── studio/                       # Desktop application
│   ├── app.py                    # QApplication entry, auth loop
│   │
│   ├── auth/
│   │   ├── api_client.py         # httpx → fmmonitor.sscapi.co
│   │   ├── auth_service.py       # RemoteAuthService + AuthUser
│   │   ├── login_window.py       # Login UI modal
│   │   ├── login_worker.py       # QRunnable login/restore
│   │   └── token_store.py        # keyring persistence
│   │
│   ├── core_bridge/
│   │   ├── render_service.py     # Subprocess render queue
│   │   ├── beat_detect_service.py
│   │   ├── audio_trim_service.py
│   │   ├── waveform_service.py
│   │   └── thumbnail_service.py
│   │
│   ├── editor/
│   │   ├── main_window.py        # QMainWindow: wires all panels
│   │   ├── timeline_panel.py     # Timeline editor + beat editor
│   │   ├── preview_panel.py      # Media player + live frame display
│   │   ├── segment_config_panel.py  # Properties inspector
│   │   ├── media_library.py      # Import + media list
│   │   ├── export_dialog.py      # Parallel export UI
│   │   └── worker_update_dialog.py  # Download rhythm_worker.exe
│   │
│   ├── models/
│   │   ├── segment.py            # Segment dataclass + RenderStatus
│   │   ├── project.py            # Project root dataclass
│   │   ├── media_item.py         # MediaItem + MediaKind
│   │   └── render_settings.py    # Pydantic per-mode settings
│   │
│   ├── persistence/
│   │   └── project_store.py      # .htproj JSON load/save
│   │
│   └── resources/
│       └── styles.qss            # Global Qt stylesheet
│
├── tests/studio/                 # Unit tests
├── dist/                         # Build output (gitignored)
├── temps/                        # Runtime audio trims
├── rhythm_worker.spec            # PyInstaller: rhythm_worker.exe
├── ssc_studio.spec               # PyInstaller: SSCStudio.exe
└── build_dist.py                 # Build orchestration script
```

---

## 6. Data Flow

```
User edits segment
       │
       ▼
segment_changed.emit()
       │
       ├──► TimelinePanel redraws
       │
       ├──► [debounce 80ms] _perform_live_preview_update()
       │         │
       │         ▼
       │    preview_panel.update_live_mode()
       │         │
       │         ▼
       │    LiveFrameRenderer.update_mode()  ← rebuild scene
       │         │
       │         ▼
       │    render_at(t_sec) → np.ndarray → QPixmap → display
       │
       └──► [on render click] RenderService.enqueue(RenderJob)
                 │
                 ▼
           subprocess: rhythm_worker.exe
                 │
                 ├── AudioTrimService.trim_audio_ffmpeg()
                 ├── librosa beat detection (unless beat_source=array)
                 ├── GameManager.pre_schedule()
                 ├── Per-frame: TunnelRenderer + targets + stickman
                 ├── FFmpeg encode → temp.mp4
                 └── FFmpeg mux audio → output.mp4
                       │
                       ▼
                 finished.emit(segment_id, output_path)
                       │
                       ▼
                 segment.video_path = output_path
                 segment.render_status = DONE
                 autosave project
```

---

## 7. Signal/Slot Map (các kết nối quan trọng)

| Emitter | Signal | Receiver / Handler |
|---|---|---|
| `MediaLibraryPanel` | `media_selected` | `PreviewPanel.set_source_media` |
| `TimelinePanel` | `segment_selected` | `MainWindow._on_segment_selected` |
| `TimelinePanel` | `segment_changed` | `MainWindow._on_segment_changed_by_timeline` |
| `TimelinePanel` | `playhead_seek_requested` | `PreviewPanel.seek_to_seconds` |
| `TimelinePanel` | `beat_events_edited` | `MainWindow._on_beat_events_edited` |
| `TimelinePanel` | `auto_gen_block_requested` | `MainWindow._on_auto_gen_block_requested` |
| `PreviewPanel` | `playhead_changed` | `TimelinePanel.set_playhead` |
| `PreviewPanel` | `stickman_location_changed` | `MainWindow._on_stickman_location_edited` |
| `PreviewPanel` | `live_preview_stopped` | `MainWindow._on_live_preview_panel_stopped` |
| `SegmentConfigPanel` | `segment_changed` | `MainWindow._on_segment_changed_by_form` |
| `SegmentConfigPanel` | `render_requested` | `MainWindow._on_render_segment_requested` |
| `SegmentConfigPanel` | `preview_requested` | `MainWindow._on_preview_segment_requested` |
| `RenderService` | `progress` | `MainWindow._on_render_progress` |
| `RenderService` | `finished` | `MainWindow._on_render_finished` |
| `RenderService` | `failed` | `MainWindow._on_render_failed` |
| `RenderService` | `trimmed` | `MainWindow._on_trim_ready` |
| `BeatDetectService` | `ready` | `MainWindow._on_beat_detect_ready` |
| `_RendererWorker` | `ready` | `MainWindow._on_renderer_ready` |
| `ExportDialog` | `segment_rendered` | `MainWindow._on_export_segment_rendered` |

---

## 8. Render Settings (per-mode)

| Field | Type | Default | Mô tả |
|---|---|---|---|
| `mode_list` | list[str] | `["punch"]` | Sub-modes cho combo |
| `travel` | int | `-1` | Travel frames (-1 = auto) |
| `speed` | float | `0.8` | Block speed multiplier |
| `density` | float | `0.5` | Beat density |
| `max_per_lane` | int | `2` | Max blocks/lane |
| `beat_source` | enum | `onset` | `tempo`/`beat`/`onset` |
| `beat_sens` | float | `0.7` | Sensitivity 0–1 |
| `beat_min_gap` | int | `4` | Min frames giữa 2 target |
| `bloom` | bool | `True` | Screen-space bloom |
| `floor_panels` | bool | `True` | Hiện floor tiles |
| `floor_panel_color` | str? | `None` | Hex color tile (`#RRGGBB`) |
| `floor_panel_blink` | bool | `False` | Tile nhấp nháy |
| `floor_panel_image` | str? | `None` | Ảnh warp lên tile |
| `stickman` | bool | `True` | Hiện stickman HUD |
| `cube_radius` | float | `0.154` | Punch: kích thước cube |
| `cube_image` | str? | `None` | Texture override cube |
| `dance_pair_cycle` | int | `4` | Dance paired beat cycle |
| `punch_pair_cycle` | int | `4` | Punch paired beat cycle |
| `line_beats` | int | `2` | Hold-note length (beats) |

---

## 9. Build & Distribution

### Hai target PyInstaller

**`rhythm_worker.exe`** (`rhythm_worker.spec`)
- Entry: `src/rhythm_worker_entry.py`
- Console EXE — chạy nền, log progress ra stdout.
- Hidden imports: numpy, scipy, librosa, cv2, soundfile, pydantic, trimesh, **unittest** (bắt buộc vì numpy.testing).
- Excludes: `tkinter`, `PySide6`, `matplotlib` — **KHÔNG được exclude `test`** (sẽ strip `unittest`).

**`SSCStudio.exe`** (`ssc_studio.spec`)
- Entry: `studio/app.py`
- Windowed EXE — GUI chính.
- Data: `studio/resources/` (styles.qss, assets).
- Hidden imports: studio packages, `src.bundle_paths`, `src.live_renderer`, Qt Multimedia, keyring, httpx.

### Quy trình build
```bash
python -m PyInstaller rhythm_worker.spec --noconfirm
python -m PyInstaller ssc_studio.spec --noconfirm
```

### Pre-build checklist (bắt buộc)
- [ ] `excludes` không chứa `"test"` hay bất kỳ stdlib package nào.
- [ ] `"unittest"` và `"unittest.mock"` có trong `hiddenimports`.
- [ ] Smoke test: `dist\rhythm_worker\rhythm_worker.exe --help` → exit 0, không có `ModuleNotFoundError`.
- [ ] Test render thật: `rhythm_worker.exe -i <audio> -o out.mp4 --duration 5`.

---

## 10. Project File Format (`.htproj`)

JSON file, lưu tại thư mục project. Các path tương đối với thư mục project.

```json
{
  "id": "<uuid>",
  "name": "My Project",
  "output_width": 1920,
  "output_height": 1080,
  "output_fps": 30,
  "segments": [
    {
      "id": "<uuid>",
      "name": "Segment A",
      "start_time_sec": 0.0,
      "end_time_sec": 30.0,
      "audio_path": "audio/song.mp3",
      "mode": "punch",
      "render_settings": { "density": 0.8, "floor_panels": true },
      "beat_events": [[0.5, "L", 1.0], [1.0, "R", 0.8]],
      "video_path": "out/segment_a.mp4",
      "render_status": "done"
    }
  ],
  "media_items": [...]
}
```

---

## 11. API & Auth

- **Base URL**: `https://fmmonitor.sscapi.co` (override qua env `HT_STUDIO_API_BASE_URL`)
- **Login**: `POST /api/v1/auth/login` → `{ access_token }`
- **Validate**: `GET /api/v1/auth/me` với `Bearer <token>`
- **Token storage**: OS keyring (`keyring` library) — key `HTStudio/<username>`
- **Worker update URL**: `https://toolmgt.mksoft.io/simple.rhythm/rhythm_worker.exe`
