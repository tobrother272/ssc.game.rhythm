# Layer Countdown — Fix Toàn Bộ Spec

## Mục tiêu

Sửa 8 vấn đề đã phát hiện trong layer kind `countdown`, gồm 1 data-loss bug nghiêm trọng, vài inconsistency, missing UI controls, và dọn dead code. Spec này độc lập với 2 spec khác (`layer-resize-move-threshold-spec.md` và `drag-mechanics-offset-and-ergonomics-spec.md`).

---

## Hiện trạng (snapshot)

Layer countdown được biểu diễn bằng `Layer(kind="countdown", config={...})`. Config gồm tối đa 14 keys (xem `_VISUAL_FIELDS_BY_KIND["countdown"]` trong `studio/models/layer.py`):

```
Bốn loại keys:
  Visual          : relax_countdown_enabled, relax_countdown_color
  Hành vi         : relax_countdown_max_sec, relax_countdown_anim
  Audio           : relax_countdown_audio_enabled, _audio_mode, _audio_file,
                    _audio_volume, _audio_last_mode, _audio_last_file
  Vị trí / kích thước (BBox): relax_countdown_x, _y, _w, _h
```

Có 3 đường ghi/đọc config:

```
1. Inspector edit (live, debounced)
   inspector_panel.py::_commit_pending_edit
   → layer.config = section.get_config()  ← REPLACE, không merge

2. Drag countdown box trong preview panel
   preview_panel.py::_on_floor_wall_committed
   → seg.render_settings[x,y,w,h] = ...   ← bị strip ở renderer
   → main_window.py::_on_floor_wall_committed
   → top.config[x,y,w,h] = ...           ← đường này mới là đường thật

3. Render / Live preview
   resolve_segment_config(segment, layers) trong layer.py
   → strip visual keys khỏi seg.render_settings
   → effective.update(top_layer.config)
   → trả về effective dict
```

---

## Fix 1: Inspector edit nuốt mất `relax_countdown_x/y/w/h` (Critical, data loss)

### Bug

`_CountdownSection` ở `studio/editor/layer_edit_dialog.py` (dòng 242-460):

- `__init__` (dòng 287-398): chỉ đọc 10 keys vào widgets (enabled, color, max_sec, anim, audio_*). Không đọc x/y/w/h.
- `get_config` (dòng 444-460): chỉ trả 10 keys. Không có x/y/w/h.

Khi user đụng bất kỳ field nào trong Inspector, `inspector_panel._commit_pending_edit` (dòng 456-469) chạy:

```python
new_cfg = section.get_config()
layer.config = new_cfg   # REPLACE — x/y/w/h biến mất
```

Resolve dùng layer.config → renderer rơi về default `(0.88, 0.04, 0.10, 0.16)`.

### Fix

Cách tốt nhất: Bổ sung x/y/w/h vào UI của `_CountdownSection` (kết hợp với Fix 5 bên dưới). Khi Section đã quản lý 4 keys này, `get_config()` tự nhiên trả về đầy đủ.

Vị trí thêm: trong `_CountdownSection.__init__`, sau form rows hiện tại (sau `Audio` controls), thêm 1 group "Position & size" với 4 `QDoubleSpinBox`:

```python
# Pseudocode trong __init__
from PySide6.QtWidgets import QGroupBox, QFormLayout
self._pos_group = QGroupBox("Position & size", self)
pos_form = QFormLayout(self._pos_group)

self._x_sp = QDoubleSpinBox()
self._x_sp.setRange(0.0, 1.0)
self._x_sp.setSingleStep(0.01)
self._x_sp.setDecimals(3)
self._x_sp.setValue(float(config.get("relax_countdown_x", 0.88)))
self._x_sp.valueChanged.connect(self.changed)
pos_form.addRow("X (0..1)", self._x_sp)

# Tương tự cho _y_sp (default 0.04), _w_sp (default 0.10, range 0.02..1.0),
# _h_sp (default 0.16, range 0.02..1.0).

form.addRow(self._pos_group)  # add group vào main form
```

Trong `get_config` (dòng 444-460), thêm 4 keys:

```python
return {
    # ... 10 keys hiện tại ...
    "relax_countdown_x": self._x_sp.value(),
    "relax_countdown_y": self._y_sp.value(),
    "relax_countdown_w": self._w_sp.value(),
    "relax_countdown_h": self._h_sp.value(),
}
```

### Phương án backup (nếu không muốn thêm UI)

Nếu quyết định không expose x/y/w/h trong UI (vì user dùng drag-in-preview là chính), Section vẫn phải PRESERVE 4 keys khi return:

```python
def __init__(self, config, parent):
    super().__init__("Countdown", parent)
    self._original_config = dict(config)  # NEW
    # ... existing widgets ...

def get_config(self):
    result = {
        # ... 10 UI-managed keys ...
    }
    # Preserve keys this section doesn't manage
    for key in ("relax_countdown_x", "relax_countdown_y",
                "relax_countdown_w", "relax_countdown_h"):
        if key in self._original_config:
            result[key] = self._original_config[key]
    return result
```

Trade-off: backup phương án không cho user nhập số chính xác qua UI, vẫn phải dùng kéo. Phương án chính (thêm UI) tốt hơn cho UX và là chuẩn nhất.

**Đề xuất chốt: phương án chính (thêm UI controls).**

### Đồng bộ ngược: spinbox cập nhật khi user kéo trong preview

Khi user mở Inspector, kéo countdown box trong preview, layer.config được update qua `_on_floor_wall_committed`. Inspector spinboxes hiện tại (đã instantiate) sẽ hiển thị giá trị stale.

Hai cách giải quyết:

(a) Inspector lắng nghe `layer_changed` signal từ MainWindow, nếu layer đang edit chính là layer vừa được drag → re-init Section. Đơn giản nhất nhưng có thể gây flicker UI nếu user đang gõ.

(b) Bỏ qua: chấp nhận spinbox stale tới khi user đóng/mở lại Inspector. Đơn giản hơn, ít rủi ro.

Đề xuất (b) cho spec này, để (a) làm nice-to-have sau.

---

## Fix 2: Default config thiếu x/y/w/h (High)

### Bug

`TimelinePanel._default_layer_config("countdown")` ở `studio/editor/timeline_panel.py` dòng 3238-3250 trả về dict 10 keys, không có x/y/w/h. Layer mới tạo từ nút "+" có config thiếu, renderer fall back default. Trùng với bug 1 — sau bug 1 fix, default cũng cần đủ.

### Fix

Trong `_default_layer_config` (dòng 3238-3250):

```python
if kind == "countdown":
    return {
        "relax_countdown_enabled": True,
        "relax_countdown_color": "#FFFFFF",
        "relax_countdown_max_sec": 5.0,           # đổi 3.0 → 5.0 (xem Fix 4)
        "relax_countdown_anim": "pop",
        "relax_countdown_audio_enabled": False,
        "relax_countdown_audio_mode": "default",
        "relax_countdown_audio_file": "",
        "relax_countdown_audio_volume": 0.65,
        "relax_countdown_audio_last_mode": "default",
        "relax_countdown_audio_last_file": "",
        # NEW — bbox defaults
        "relax_countdown_x": 0.88,
        "relax_countdown_y": 0.04,
        "relax_countdown_w": 0.10,
        "relax_countdown_h": 0.16,
    }
```

---

## Fix 3: Dual-write thừa vào `seg.render_settings` (Medium)

### Bug

Trong `_on_floor_wall_committed`:

- `preview_panel.py` dòng 1167-1170: ghi x/y/w/h vào `seg.render_settings`.
- `main_window.py` dòng 1304-1307: ghi lại lần nữa vào `seg.render_settings`.
- `main_window.py` dòng 1336-1339: ghi vào `top.config` (countdown layer) — đây mới là đường có hiệu lực với renderer.

`resolve_segment_config` strip x/y/w/h khỏi `seg.render_settings`, nên 2 lần write trên là vô hiệu (chỉ phục vụ preview overlay đọc fallback ở `preview_panel.py` dòng 1109-1112).

### Fix

Bước 1 — Cập nhật preview overlay đọc từ layer.config thay vì seg.render_settings:

Tại `preview_panel.py` dòng 1095-1112, thay đoạn đọc x/y/w/h từ `rs`:

```python
# CŨ (dòng 1109-1112):
cdx = float(rs.get("relax_countdown_x", 0.88) or 0.88)
cdy = float(rs.get("relax_countdown_y", 0.04) or 0.04)
cdw = float(rs.get("relax_countdown_w", 0.10) or 0.10)
cdh = float(rs.get("relax_countdown_h", 0.16) or 0.16)

# MỚI — đọc từ countdown layer của segment
from studio.models.layer import resolve_segment_config
effective = resolve_segment_config(seg, self._project.layers)  # cần passing project layers
cdx = float(effective.get("relax_countdown_x", 0.88) or 0.88)
cdy = float(effective.get("relax_countdown_y", 0.04) or 0.04)
cdw = float(effective.get("relax_countdown_w", 0.10) or 0.10)
cdh = float(effective.get("relax_countdown_h", 0.16) or 0.16)
```

Lưu ý: PreviewPanel có thể chưa giữ ref tới `project.layers`. Kiểm tra constructor — nếu chưa, thêm tham chiếu hoặc emit qua MainWindow. Có thể đơn giản hóa bằng cách dùng helper riêng:

```python
def _get_countdown_bbox(self, seg) -> tuple[float, float, float, float]:
    """Resolve countdown bbox for segment, falling back to defaults."""
    if self._project is None or seg is None:
        return (0.88, 0.04, 0.10, 0.16)
    cd_layers = [
        la for la in self._project.layers
        if la.kind == "countdown" and la.overlaps(seg.start_time_sec, seg.end_time_sec)
    ]
    if not cd_layers:
        return (0.88, 0.04, 0.10, 0.16)
    top = max(cd_layers, key=lambda la: la.z_index)
    cfg = top.config
    return (
        float(cfg.get("relax_countdown_x", 0.88) or 0.88),
        float(cfg.get("relax_countdown_y", 0.04) or 0.04),
        float(cfg.get("relax_countdown_w", 0.10) or 0.10),
        float(cfg.get("relax_countdown_h", 0.16) or 0.16),
    )
```

Bước 2 — Xoá 2 chỗ write thừa vào `seg.render_settings`:

`preview_panel.py` dòng 1167-1170: xoá 4 dòng `seg.render_settings["relax_countdown_*"] = ...`.

`main_window.py` dòng 1304-1307: xoá 4 dòng tương tự.

Giữ nguyên đường write vào `top.config` (main_window.py dòng 1336-1339) — đây mới là source of truth.

Bước 3 — Khi project được load, nếu có legacy seg.render_settings có chứa x/y/w/h (từ trước fix), migrate vào layer.config nếu chưa có:

`migrate_render_settings_to_layers` trong `studio/models/layer.py` dòng 163-198 đã làm được việc này (extract visual fields vào layer block). Verify rằng migration vẫn extract x/y/w/h đúng — chúng có trong `_VISUAL_FIELDS_BY_KIND["countdown"]` (dòng 148-159). OK, không cần thêm migration.

---

## Fix 4: Default `max_sec` lệch 5.0 vs 3.0 (Medium)

### Bug

5 chỗ default cho `relax_countdown_max_sec`:

```
studio/models/render_settings.py L177:    5.0   ← Pydantic schema (canonical)
src/rhythm.py L5603, L7349, L7543:        5.0   ← renderer
src/live_renderer.py L240:                 5.0   ← live renderer
studio/editor/main_window.py L1326:        5.0   ← _on_floor_wall_committed create
studio/editor/main_window.py L2457:        5.0   ← kwargs fallback
─────────────────────────────────────────────────
studio/editor/layer_edit_dialog.py L309:   3.0   ← UI default ← LỆCH
studio/editor/timeline_panel.py L3242:     3.0   ← layer default ← LỆCH
```

### Fix

Đổi 2 chỗ lệch về 5.0 cho nhất quán với Pydantic / renderer / model:

`layer_edit_dialog.py` dòng 309:
```python
self._max_sec_sp.setValue(float(config.get("relax_countdown_max_sec", 5.0)))
```

`timeline_panel.py` dòng 3242: đã sửa ở Fix 2 (5.0).

### Lý do chọn 5.0 thay vì 3.0

Pydantic schema `render_settings.py` là single source of truth cho default. Mọi layer mới và mọi UI fallback phải khớp.

Nếu user muốn 3.0 là behavior mặc định mới, sửa Pydantic schema thành 3.0 và đổi cả renderer args. Phạm vi rộng hơn, không nên làm trong spec này.

---

## Fix 5: Bổ sung UI controls cho x/y/w/h

Đã gộp vào Fix 1 (phương án chính). Nhắc lại spec ngắn để dễ tra:

Trong `_CountdownSection.__init__`:

```
Group "Position & size":
  X      QDoubleSpinBox  range [0.0, 1.0]      step 0.01  decimals 3  default 0.88
  Y      QDoubleSpinBox  range [0.0, 1.0]      step 0.01  decimals 3  default 0.04
  Width  QDoubleSpinBox  range [0.02, 1.0]     step 0.01  decimals 3  default 0.10
  Height QDoubleSpinBox  range [0.02, 1.0]     step 0.01  decimals 3  default 0.16
```

Mỗi spinbox connect signal `valueChanged` → `self.changed` (giống các field khác trong section).

Tooltip cho group: "Vị trí và kích thước hộp số đếm ngược, tỉ lệ 0..1 theo khung hình. Có thể kéo trực tiếp trong preview để chỉnh nhanh."

---

## Fix 6: Auto-create countdown layer khi mode = relax (Optional, UX)

### Bug

`auto_create_default_layers` (file `studio/models/layer.py` dòng 78-114) chỉ tạo background/floor/stickman. User tạo segment mới (mặc định mode="punch") rồi đổi sang relax → không có countdown layer → countdown box không hiện trong preview, user không biết tại sao.

### Fix (đề xuất)

Phương án A — Auto-create khi tạo segment relax:

Sửa `auto_create_default_layers` thêm logic conditional:

```python
def auto_create_default_layers(project, segment):
    # ... existing 3 defaults ...
    defaults = [...]  # 3 hiện tại

    # NEW: countdown chỉ tạo cho segment relax
    if _is_relax_segment(segment):
        defaults.append(("countdown", {
            "relax_countdown_enabled": True,
            "relax_countdown_color": "#FFFFFF",
            "relax_countdown_max_sec": 5.0,
            "relax_countdown_anim": "pop",
            "relax_countdown_x": 0.88,
            "relax_countdown_y": 0.04,
            "relax_countdown_w": 0.10,
            "relax_countdown_h": 0.16,
        }))
    # ... rest ...

def _is_relax_segment(segment) -> bool:
    """True if segment uses relax mode (sole or in combo)."""
    mode = str(getattr(segment, "mode", "") or "").lower()
    if mode == "relax":
        return True
    if mode == "combo":
        rs = getattr(segment, "render_settings", None) or {}
        modes = rs.get("mode_list") or []
        return any(str(m).lower() == "relax" for m in modes)
    return False
```

Hiện `auto_create_default_layers` chỉ chạy 1 lần lúc segment được tạo qua media drop (`main_window.py` dòng 1469-1470), thời điểm đó mode="punch" cứng. Vậy phương án A chưa đủ.

Phương án B — Hook vào event "segment mode changed":

Khi user đổi mode sang relax (hoặc combo có relax), check xem segment đã có countdown layer overlap chưa. Nếu chưa → auto-create. Cần hook ở chỗ nào sửa segment.mode (search `segment.mode = ` để tìm).

Phương án này phức tạp hơn và rủi ro hơn. **Không bao gồm trong spec này — đánh dấu open question.**

Phương án C — Update test `test_auto_create_does_not_include_side_rails_countdown` để phản ánh hành vi mới (nếu chọn A): nếu segment tạo với mode relax, countdown phải có trong layers. Test hiện tại tạo segment mặc định không relax nên test giữ nguyên cũng OK.

**Đề xuất tạm hoãn Fix 6.** Test `test_auto_create_does_not_include_side_rails_countdown` xác nhận hành vi hiện tại là intentional. Bỏ qua. Nếu sau này user phàn nàn thì xử lý sau.

---

## Cleanup 1: Xoá `_LayerEditDialog` dead code

`studio/editor/layer_edit_dialog.py` dòng 463-548 (~80 dòng). Class này định nghĩa modal dialog wrap các Section nhưng KHÔNG nơi nào instantiate. Verify bằng grep:

```bash
grep -rn "LayerEditDialog(" .
# → chỉ 1 hit là dòng define, không có caller
```

Xoá hoàn toàn class `_LayerEditDialog` (dòng 463-548). Imports liên quan (`QDialog`, `QDialogButtonBox`, `QVBoxLayout`) dùng trong file kiểm tra xem còn cần không sau khi xoá; nếu không còn ai dùng thì xoá luôn.

Các Section (`_BackgroundSection`, `_StickmanSection`, `_CountdownSection`) GIỮ NGUYÊN — Inspector dùng trực tiếp.

Docstring đầu file (dòng 1-12 nếu có nhắc tới `_LayerEditDialog`) cập nhật cho khớp.

---

## Cleanup 2: Xoá dead labels/specs trong `segment_config_panel.py`

Comment ở dòng 1793 đã ghi "countdown fields moved to Countdown layer — removed from here". Nhưng `_FIELD_LABELS` (dòng 1817-1819) và `_SPECS` (dòng 1900) vẫn còn entries cho:

```
"relax_countdown_enabled": "Countdown",
"relax_countdown_color": "Countdown color",
"relax_countdown_max_sec": "Countdown max sec",
"relax_countdown_max_sec": (0.0, 20.0, 0.1, 2, "Countdown visible window (seconds)."),
```

Các keys này không còn render trong UI mode=relax (đã remove khỏi `_FIELDS_BY_MODE["relax"]` dòng 1781-1794). Labels/specs là dead.

Xoá 3 entries trong `_FIELD_LABELS` (dòng 1817-1819).

Xoá entry trong `_SPECS` (dòng 1900).

Cũng kiểm tra dòng 1935-1938 (xử lý đặc biệt cho `relax_countdown_color` color picker) — nếu không còn field nào trigger code path này, xoá luôn. Cẩn thận vì có thể có code path khác dùng.

---

## Test scenarios

### Test 1: Inspector edit không nuốt bbox (Fix 1)

```
Setup: tạo countdown layer mới qua nút "+". Layer.config có x=0.88, y=0.04, w=0.10, h=0.16 (sau Fix 2).
Action: kéo countdown box trong preview panel đến vị trí mới (x=0.5, y=0.3, w=0.2, h=0.25).
Verify: layer.config có x=0.5, y=0.3, w=0.2, h=0.25.
Action: mở Inspector, đổi color thành "#FF0000". Đợi debounce.
Verify: layer.config có color="#FF0000" VÀ x=0.5, y=0.3, w=0.2, h=0.25 (không bị reset).
Render → countdown box xuất hiện ở vị trí 0.5/0.3 màu đỏ.
```

### Test 2: Default config có đủ bbox (Fix 2)

```
Setup: tạo countdown layer mới qua nút "+".
Verify: layer.config bao gồm 14 keys (10 cũ + 4 bbox).
Verify: x=0.88, y=0.04, w=0.10, h=0.16 (default).
```

### Test 3: Preview overlay đọc đúng từ layer.config (Fix 3)

```
Setup: countdown layer config có x=0.5, y=0.3, w=0.2, h=0.25.
Action: bật preview overlay (tap floor/wall edit mode).
Verify: overlay countdown box hiện ở x=0.5, y=0.3, w=0.2, h=0.25 (không phải 0.88/0.04).
Verify: seg.render_settings KHÔNG còn keys relax_countdown_x/y/w/h (đã xoá đường dual-write).
```

### Test 4: max_sec default consistency (Fix 4)

```
Setup: tạo countdown layer mới qua "+".
Verify: layer.config["relax_countdown_max_sec"] == 5.0 (không phải 3.0).
Verify: Inspector spinbox hiện 5.0.
Setup: load project có countdown layer KHÔNG có key max_sec.
Verify: Inspector spinbox hiện 5.0 (consistent với renderer fallback).
```

### Test 5: UI spinboxes cho bbox hoạt động (Fix 5)

```
Setup: chọn countdown layer trong Inspector.
Verify: section có 4 spinbox X/Y/Width/Height với giá trị hiện tại.
Action: đổi spinbox X từ 0.88 → 0.50. Đợi debounce.
Verify: layer.config["relax_countdown_x"] == 0.5.
Verify: render countdown box ở vị trí mới.
Action: undo (Ctrl+Z) — layer.config restore về cũ.
```

### Test 6: Round-trip drag preview ↔ Inspector

```
Setup: countdown layer config x=0.88.
Action 1: kéo countdown trong preview đến x=0.5 → layer.config["relax_countdown_x"] == 0.5.
Action 2: mở Inspector lần đầu (sau drag) → spinbox X hiện 0.5 ✓
Action 3: trong Inspector đổi spinbox X thành 0.7 → layer.config = 0.7 ✓
Action 4: kéo trong preview đến 0.3 → layer.config = 0.3.
Verify: nếu Inspector vẫn open, spinbox hiện 0.7 (stale, OK theo Fix 1 quyết định bỏ qua sync ngược) HOẶC cập nhật về 0.3 (nếu chọn implement sync).
```

### Test 7: `_LayerEditDialog` đã xoá không break gì (Cleanup 1)

```
grep -rn "LayerEditDialog" studio/  → không còn hit nào (ngoài file đã xoá).
Imports trong layer_edit_dialog.py: QDialog/QDialogButtonBox không còn dùng → xoá.
Application launch: không error.
Inspector edit countdown layer: vẫn work (dùng _CountdownSection trực tiếp).
```

### Test 8: Dead labels/specs đã xoá (Cleanup 2)

```
grep "relax_countdown" studio/editor/segment_config_panel.py → 0 hit (sau cleanup).
Mode = "relax" segment: form không còn 3 dòng Countdown legacy.
```

### Test 9: Migration projects cũ vẫn work

```
Setup: project cũ có seg.render_settings = {relax_countdown_x: 0.7, relax_countdown_y: 0.5, ...}, không có countdown layer.
Load project (qua ProjectStore.load).
Verify: migrate_render_settings_to_layers tạo countdown layer với x=0.7, y=0.5 trong config.
Verify: render box hiện ở x=0.7, y=0.5.
```

### Test 10: Existing tests vẫn pass

```
pytest tests/studio/test_layers.py
- test_migration_extracts_countdown: pass
- test_auto_create_does_not_include_side_rails_countdown: pass (Fix 6 đã chốt không làm)
- mọi test khác: pass
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **Drag countdown trong preview vẫn ghi vào layer.config** (dòng 1336-1339 main_window.py). Giữ nguyên đường này — đây là source of truth. Fix 3 chỉ xoá 2 đường dual-write thừa, không động vào đường này.

2. **`migrate_render_settings_to_layers`** (layer.py dòng 163-198): giữ nguyên. Nó vẫn extract x/y/w/h từ legacy `seg.render_settings` vào layer.config khi load project cũ.

3. **`resolve_segment_config`** (layer.py dòng 201-269): giữ nguyên logic strip + update. Đây là single source of truth cho effective config tại render-time.

4. **Pydantic schema `BaseRenderSettings`** trong `studio/models/render_settings.py`: giữ nguyên, không sửa. Nếu sau này muốn đổi default `max_sec` thì sửa schema và đồng bộ tất cả nơi khác.

5. **Auto-create test** `test_auto_create_does_not_include_side_rails_countdown`: giữ nguyên. Fix 6 không thực hiện, test vẫn phải pass.

6. **Render path từ `render_service.py`** (`_settings_to_args` dòng 751): giữ nguyên `_ALLOWED_KEYS` đã có 4 keys countdown bbox (dòng 740-743). Không cần đụng.

7. **CLI args** trong `src/rhythm.py` (`--relax_countdown_*`): giữ nguyên. Fix này không động đến renderer.

8. **Live renderer hot-update** (`_relax_countdown_x`, `update_countdown_box`): giữ nguyên. Fix 3 chỉ động đến đường preview overlay, không động đến live_renderer.

9. **Inspector debounce + edit-session compound undo**: giữ nguyên. Fix 1 chỉ thêm fields vào Section, không thay đổi Inspector pipeline.

10. **Section `changed` signal pattern** (mọi widget edit emit `self.changed`): giữ nguyên. 4 spinbox mới phải connect đúng signal này để debounce hoạt động.

---

## Pattern code hiện có để tham khảo

- **Section spinbox pattern**: xem `_max_sec_sp` (dòng 305-312). Copy paste cấu trúc cho 4 spinbox mới.

- **Section `changed` signal**: mọi spinbox/checkbox emit `self.changed` qua connect. Inspector debounce sẽ trigger commit.

- **Section `get_config`**: trả về dict đầy đủ keys. Inspector ghi đè layer.config.

- **`_StickmanSection`** (`layer_edit_dialog.py` dòng ~50-235): tham khảo cách Section quản lý nhiều spinbox cho location object (x/y/w/h là pattern tương tự).

- **`auto_create_default_layers`**: nếu sau này muốn implement Fix 6, tham khảo `defaults` list dòng 89-96.

---

## Thứ tự implement đề xuất

1. **Cleanup 1 + Cleanup 2 trước.** Xoá `_LayerEditDialog` và dead labels/specs. Verify imports không bị thiếu, app vẫn chạy. Đây là baseline sạch.

2. **Fix 4 — đổi 2 chỗ default 3.0 → 5.0.** Trivial, chạy test verify.

3. **Fix 2 — thêm 4 keys bbox vào `_default_layer_config("countdown")`.** Trivial.

4. **Fix 1 + Fix 5 — thêm UI Position/Size group vào `_CountdownSection`.** Đây là phần lớn nhất:
   - Thêm 4 spinbox với default từ config.
   - Connect signal `changed`.
   - Cập nhật `get_config` trả thêm 4 keys.
   - Run Test 1, 2, 4, 5.

5. **Fix 3 — refactor preview overlay đọc từ layer.config + xoá dual-write.**
   - Thêm helper `_get_countdown_bbox` trong PreviewPanel (hoặc tương đương).
   - Thay đoạn `rs.get("relax_countdown_*")` bằng resolved values.
   - Xoá 4 dòng write trong `preview_panel.py::_on_floor_wall_committed`.
   - Xoá 4 dòng write trong `main_window.py::_on_floor_wall_committed`.
   - Run Test 3, 6.

6. **Smoke test** cuối cùng: tạo project mới, tạo countdown layer, drag, edit Inspector, render preview, render export. Mọi pipeline hoạt động.

7. **Optional**: implement Fix 6 nếu user yêu cầu sau. Hiện không thuộc scope.

---

## Open questions

(1) **Sync ngược spinbox khi user kéo trong preview lúc Inspector đang mở**: phương án (a) tự refresh hay (b) chấp nhận stale tới khi đóng/mở lại? Tôi đề xuất (b). Bạn có ý kiến khác?

(2) **Layout của Position & size group trong Section**: nên là QGroupBox riêng (như spec) hay flat 4 rows trong main form? GroupBox rõ hơn nhưng tốn 1 hàng tiêu đề.

(3) **Tooltip / placeholder text** cho 4 spinbox: cần bằng tiếng Việt hay Anh? Spec hiện viết tiếng Việt. Bạn quyết định.

(4) **Decimals 3 vs 4** cho spinbox: 3 đủ chính xác cho fraction 0..1 (precision 0.001 = 1 pixel ở 1000px wide). Bạn có muốn chính xác hơn không?

(5) **Range của X+W và Y+H**: hiện spec không clamp X+W <= 1.0 (box có thể vượt biên). Có cần validation không, hay để renderer tự lo? Pydantic không clamp, renderer cũng không. Tôi nghĩ để vậy là OK.
