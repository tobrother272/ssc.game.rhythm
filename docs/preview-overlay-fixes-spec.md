# Preview Overlay — Fixes & Consolidation Spec

## Mục tiêu

Spec này gồm 4 việc cho hệ thống overlay drag-edit trên preview panel:

1. **Fix 1 (data loss)** — Side_rails: kéo `rail_height` mà chưa có side_rails layer → auto-create thay vì write vào `seg.render_settings` rồi mất.
2. **Fix 2 (data loss)** — Start_gate: kéo `start_gate_h` mà chưa có start_gate layer → auto-create.
3. **Feature 1** — Floor layer: bổ sung overlay tối thiểu (chevron width handle + visual outline floor footprint). Floor layer hiện không có drag UI nào, chỉ edit qua Inspector.
4. **Feature 2** — Gộp Stickman box vào button Floor/Wall: khi user bấm 1 button, tất cả overlay spatial (stickman + floor/wall handles + countdown + start_gate + rail + floor) hiện cùng lúc.

Spec này độc lập với:

- [`layer-countdown-fixes-spec.md`](./layer-countdown-fixes-spec.md) — fix bugs countdown.
- [`layer-countdown-border-glow-spec.md`](./layer-countdown-border-glow-spec.md) — feature countdown border + glow.
- [`layer-start-gate-spec.md`](./layer-start-gate-spec.md) — feature Start Gate layer.
- [`layer-resize-move-threshold-spec.md`](./layer-resize-move-threshold-spec.md) — threshold snap-to-fill.
- [`drag-mechanics-offset-and-ergonomics-spec.md`](./drag-mechanics-offset-and-ergonomics-spec.md) — segment drag mechanics.

Tuy nhiên Fix 2 + Feature liên quan đến Start Gate phụ thuộc spec start-gate (cần class `StartGate` + layer kind đã tồn tại). Implement spec này SAU spec start-gate.

---

## Fix 1: Side_rails auto-create on rail_height drag

### Bug

`MainWindow._on_floor_wall_committed` ở `studio/editor/main_window.py` (dòng ~1346-1353):

```python
rail_layers = self.project.layers_overlapping(
    "side_rails", seg.start_time_sec, seg.end_time_sec
)
if rail_layers:
    rail_top = max(rail_layers, key=lambda la: la.z_index)
    rail_top.config["rail_height"] = round(max(0.15, rail_height), 4)
    rail_top.config.setdefault("side_rails", True)
```

Chỉ update khi side_rails layer **đã tồn tại**. Nếu user kéo Rail H handle mà segment chưa có side_rails layer:

- Code chỉ ghi vào `seg.render_settings["rail_height"]` (dòng 1309).
- `resolve_segment_config` strip `rail_height` khỏi seg.render_settings (vì nằm trong `_VISUAL_FIELDS_BY_KIND["side_rails"]`).
- Render dùng layer.config; layer không tồn tại → giá trị mất.
- Sau reload project hoặc rebuild renderer, rail_height reset về default 0.15.

### Fix

Thêm nhánh auto-create giống pattern stickman (dòng 1248-1265) và countdown (dòng 1314-1337):

```python
# CŨ:
rail_layers = self.project.layers_overlapping("side_rails", ...)
if rail_layers:
    rail_top = max(rail_layers, key=lambda la: la.z_index)
    rail_top.config["rail_height"] = round(max(0.15, rail_height), 4)
    rail_top.config.setdefault("side_rails", True)

# MỚI:
rail_layers = self.project.layers_overlapping(
    "side_rails", seg.start_time_sec, seg.end_time_sec
)
if not rail_layers:
    from studio.models.layer import Layer
    self.project.layers.append(Layer(
        kind="side_rails",
        start_time_sec=seg.start_time_sec,
        end_time_sec=seg.end_time_sec,
        z_index=0,
        name="Side Rails",
        config={
            "side_rails": True,
            "rail_height": round(max(0.15, rail_height), 4),
            # Các default khác từ TimelinePanel._default_layer_config("side_rails"):
            "rail_color": "#FF60FF",
            "rail_shape": "chunky",
            "rail_offset_x": 0.08,
            "rail_pulse": "beat",
            "rail_pulse_intensity": 0.6,
            "rail_pillar_highlight_count": 1,
        },
    ))
else:
    rail_top = max(rail_layers, key=lambda la: la.z_index)
    rail_top.config["rail_height"] = round(max(0.15, rail_height), 4)
    rail_top.config.setdefault("side_rails", True)
```

Default config khi auto-create lấy từ `_default_layer_config("side_rails")` ở `studio/editor/timeline_panel.py` dòng ~3222-3232 để đồng bộ.

**Lưu ý**: khi auto-create, set `"side_rails": True` (toggle on). Nếu user vô tình kéo handle khi chưa muốn enable rails → vẫn enable. Đây là chủ ý: user đã chủ động kéo handle nghĩa là muốn dùng rails.

### Cleanup

Có thể giữ hoặc xoá dòng `seg.render_settings["rail_height"] = ...` (dòng 1309). Vẫn ghi vào seg.render_settings cho legacy compat (giống countdown bug 9 trong fixes spec). Đề xuất giữ — không phá compat, chỉ thêm đường ghi đúng vào layer.

---

## Fix 2: Start_gate auto-create on start_gate_h drag

### Bug

Cùng pattern với Fix 1. `_on_floor_wall_committed` dòng ~1355-1361:

```python
gate_layers = self.project.layers_overlapping(
    "start_gate", seg.start_time_sec, seg.end_time_sec
)
if gate_layers:
    gate_top = max(gate_layers, key=lambda la: la.z_index)
    gate_top.config["start_gate_h"] = round(max(0.03, start_gate_h), 4)
    gate_top.config.setdefault("start_gate_enabled", True)
```

Update-only. Drag mà chưa có start_gate layer → giá trị mất.

### Fix

Thêm nhánh auto-create:

```python
gate_layers = self.project.layers_overlapping(
    "start_gate", seg.start_time_sec, seg.end_time_sec
)
if not gate_layers:
    from studio.models.layer import Layer
    self.project.layers.append(Layer(
        kind="start_gate",
        start_time_sec=seg.start_time_sec,
        end_time_sec=seg.end_time_sec,
        z_index=0,
        name="Start Gate",
        config={
            "start_gate_enabled": True,
            "start_gate_type": "color",
            "start_gate_color": "#1a1a1a",
            "start_gate_image": "",
            "start_gate_video": "",
            "start_gate_x": 0.30,
            "start_gate_y": 0.18,
            "start_gate_w": 0.40,
            "start_gate_h": round(max(0.03, start_gate_h), 4),
        },
    ))
else:
    gate_top = max(gate_layers, key=lambda la: la.z_index)
    gate_top.config["start_gate_h"] = round(max(0.03, start_gate_h), 4)
    gate_top.config.setdefault("start_gate_enabled", True)
```

**Lưu ý phụ thuộc**: spec này giả định `start_gate` layer kind đã tồn tại trong project (theo [layer-start-gate-spec.md](./layer-start-gate-spec.md)). Nếu spec start-gate chưa được implement, Fix 2 phải hoãn.

---

## Feature 1: Floor layer overlay

### Tình trạng hiện tại

Floor layer hoàn toàn không có drag overlay. Mọi config (`floor_panel_color`, `floor_panel_image`, `chevron_count`, `chevron_width_frac`, `floor_layout`, …) chỉ edit được qua Inspector.

So với các layer khác trong overlay:

- Stickman: full bbox edit (5 handles)
- Side_rails: 1 handle (rail_height)
- Countdown: full bbox edit (5 handles)
- Start_gate: 1 handle (height) + bbox sẽ thêm theo start-gate spec
- **Floor: 0 handle**

### Đánh giá floor config

Phần lớn floor config là **non-spatial**: color picker, opacity slider, blink toggle, layout enum. Không phù hợp drag handle.

Spatial keys khả dĩ:

- **`chevron_width_frac`** (0..1): độ rộng từng chevron tương đối với floor width. Có thể drag horizontal handle để thay đổi.
- **`chevron_count`** (int): số chevron. Có thể drag handle để add/remove (kém intuitive hơn slider).

Còn lại không spatial.

### Đề xuất V1

Bổ sung 2 thứ nhẹ vào `FloorWallOverlay`:

**A. Floor footprint outline** (visualize, không drag):

Vẽ một outline trapezoid mờ hiển thị vùng floor (từ horizon đến floor line, mở rộng từ near_spread đến far_spread). Mục đích: user thấy rõ floor layer "chiếm" vùng nào trên màn hình, tăng nhận biết. Không drag được, chỉ là visual feedback.

```
Vùng floor footprint:
   /─────────\          ← horizon line, far_spread
  /           \
 /             \
/───────────────\       ← floor line (hit_y), near_spread
```

Vẽ với color `#0891b2` (cyan, khớp với LAYER_KIND_COLORS["floor"]) alpha ~30, viền dashed.

**B. Chevron width handle** (1 spatial control):

Một handle ngang ở giữa floor surface, drag trái-phải thay đổi `chevron_width_frac`:

- Vị trí: middle của floor footprint, y = (hit_y + horizon_y) / 2
- Drag horizontally → `chevron_width_frac` từ 0.05 đến 0.95
- Visual: 2 vạch dọc ngắn (chiều cao ~10px) cách nhau khoảng `chevron_width_frac × floor_width_at_mid`, có handle tròn ở giữa
- Label: "Chevron W X.XX"

User kéo handle → chevron_width_frac update real-time → renderer update chevron rendering.

### Auto-create floor layer khi drag chevron width

Cùng pattern Fix 1, Fix 2. Thêm vào `_on_floor_wall_committed`:

```python
floor_layers = self.project.layers_overlapping(
    "floor", seg.start_time_sec, seg.end_time_sec
)
if not floor_layers:
    from studio.models.layer import Layer
    from studio.models.layer import _default_floor_config
    cfg = _default_floor_config()
    cfg["chevron_width_frac"] = round(max(0.05, min(0.95, chevron_width_frac)), 4)
    self.project.layers.append(Layer(
        kind="floor",
        start_time_sec=seg.start_time_sec,
        end_time_sec=seg.end_time_sec,
        z_index=0,
        name="Floor",
        config=cfg,
    ))
else:
    floor_top = max(floor_layers, key=lambda la: la.z_index)
    floor_top.config["chevron_width_frac"] = round(max(0.05, min(0.95, chevron_width_frac)), 4)
```

Lưu ý: floor layer **luôn được auto-create** khi tạo segment mới (qua `auto_create_default_layers`), nên trường hợp "không có floor layer" hiếm. Nhưng vẫn cần handle defensive.

### Signal payload cập nhật

`FloorWallOverlay.changing` / `committed` hiện 11 floats. Thêm `chevron_width_frac` → 12 floats. Sẽ tăng tiếp nếu thêm handle khác.

**Đề xuất tạm thời**: thêm 1 float vào signal (`changing` 12 floats, `committed` 12 floats). Đây là technical debt (god-object signal), nhưng spec này không refactor — đó là việc lớn riêng (đã ghi nhận ở phần review trước, item #4).

---

## Feature 2: Gộp Stickman box vào button Floor/Wall

### Hiện trạng

Hai button toolbar độc lập:

- **Stick Box** button → toggle `StickmanBoxOverlay` (preview_panel.py dòng 1671-1680).
- **Floor / Wall** button → toggle `FloorWallOverlay` (dòng 1685-1688).

User phải bấm 2 lần để xem cả 2 overlay. Bất tiện và không có lý do gì để chia 2 button khi user thường muốn edit "tất cả layout" cùng lúc.

### Thiết kế gộp

**Phương án chính**: Bỏ button Stick Box. Button Floor/Wall (rename thành "Edit Layout" hoặc giữ nguyên) → toggle TẤT CẢ overlay spatial cùng lúc:

```
Button "Edit Layout" (hoặc "Layout") toggle ON:
  ✓ FloorWallOverlay (camera handles + countdown + start_gate + rail + chevron)
  ✓ StickmanBoxOverlay (nếu segment có stickman enabled)

Button OFF:
  ✗ Tất cả overlay hide
```

### Logic implement

`_on_floor_wall_edit_toggled` (dòng 1175) cần extend:

```python
def _on_floor_wall_edit_toggled(self, checked: bool) -> None:
    self._floor_wall_edit_active = bool(checked)
    if checked:
        # ── Show FloorWallOverlay (existing logic) ─────────────
        seg = self._selected_segment
        # ... existing set_fractions, set_start_gate_proxy ...
        self._sync_floor_wall_overlay_pos()
        self.floor_wall_overlay.show()
        self._floor_wall_pos_timer.start()

        # ── NEW: Show StickmanBoxOverlay if segment has stickman ─
        if self._segment_stickman_enabled(seg):
            self.stickman_overlay.set_normalized(
                *self._segment_stick_fractions(seg)
            )
            self._sync_stickman_overlay_pos()
            self.stickman_overlay.show()
            self._stickman_pos_timer.start()
    else:
        # ── Hide both ────────────────────────────────────────
        self._floor_wall_pos_timer.stop()
        self.floor_wall_overlay.hide()
        self._stickman_pos_timer.stop()
        self.stickman_overlay.hide()
```

State flag `_stickman_edit_active` được sync với `_floor_wall_edit_active` khi gộp:

```python
self._stickman_edit_active = self._floor_wall_edit_active
```

### Xử lý button "Stick Box" cũ

3 lựa chọn:

**(a) Xoá hoàn toàn.** Đơn giản nhất, một button quản tất.
**(b) Ẩn nhưng giữ code** (cho phép re-enable nếu có quirk).
**(c) Giữ làm "Stickman only" mode** — bấm Stick Box chỉ hiện stickman overlay, không hiện cái khác. Floor/Wall hiện tất.

Đề xuất **(a) — xoá hoàn toàn**. Lý do:

- UX đơn giản hơn cho user mới.
- Stick Box mode cô lập không có use case rõ — user thường edit layout chung.
- Nếu sau cần lại, dễ thêm.

Khi xoá, dọn:

- Button widget `self.stickman_button` (dòng 1671-1680).
- Slot `_on_stickman_edit_toggled` (dòng 1121-1137) — KHÔNG xoá, vì logic show/hide stickman overlay vẫn cần. Refactor thành helper `_show_stickman_overlay` / `_hide_stickman_overlay` để `_on_floor_wall_edit_toggled` gọi.
- `_refresh_stickman_button_state` (dòng 1038-1052) — refactor: bỏ phần button enable/disable; giữ phần update overlay normalized cho stickman_button reference (xoá), giữ floor_wall_button reference.
- Toolbar layout (dòng 1721): xoá `control_row.addWidget(self.stickman_button)`.

### Tên button mới

Đề xuất: rename "Floor / Wall" → **"Edit Layout"** hoặc **"Layout"** để phản ánh đúng phạm vi mới (không chỉ floor/wall mà còn stickman + countdown + start_gate + rail).

Nếu giữ tên "Floor / Wall" thì nhầm lẫn — user không biết bấm vào sẽ thấy stickman.

### Trường hợp segment không có stickman

Nếu `_segment_stickman_enabled(seg)` trả về False (vd segment dance không cần stickman), không hiện stickman overlay. Floor/wall overlay vẫn hiện bình thường.

Logic check `_segment_stickman_enabled` đã có sẵn (dòng 1028-1036).

### Trường hợp live preview không bật

`set_floor_wall_edit_enabled(enabled: bool)` (dòng 1312-1316) hiện disable button khi không live preview. Giữ nguyên — Edit Layout button cũng chỉ available khi live preview active.

Tuy nhiên stickman overlay hiện **không cần** live preview (chỉ cần video frame nào đó). Logic cũ stick_button enabled khi `seg is not None` chứ không cần live. Sau gộp, cần quyết định:

- Nếu Edit Layout button vẫn require live preview → user mất khả năng edit stickman khi xem video rendered (vì lúc đó không live).
- Nếu Edit Layout enable bất cứ khi nào có segment → cần handle case "live preview off, floor/wall handles không có meaning vì preview không re-render".

**Đề xuất**: Edit Layout button enable khi `seg is not None`. Khi bấm:

- Nếu live preview ON: hiện cả floor/wall overlay + stickman.
- Nếu live preview OFF: chỉ hiện stickman overlay (không hiện floor/wall vì không có realtime feedback). Hoặc hiện cả nhưng floor/wall handles không live update (giống behavior hiện tại của stickman).

Phương án đơn giản hơn: Edit Layout chỉ available khi live preview. Nếu user muốn edit stickman ngoài live preview, dùng Inspector spinbox (sau khi spec stickman thêm UI position controls — không trong scope này).

**Chốt**: Edit Layout require live preview. Nếu user muốn edit stickman ngoài live, dùng Inspector.

---

## Touch points

### Code cần sửa

**1. `studio/editor/main_window.py` — `_on_floor_wall_committed` (dòng ~1283-1370):**

- Fix 1: thêm auto-create side_rails layer.
- Fix 2: thêm auto-create start_gate layer.
- Feature 1: thêm xử lý `chevron_width_frac` (mới signal param), auto-create floor layer nếu cần.

**2. `studio/editor/preview_panel.py` — `FloorWallOverlay` class (dòng 241-717):**

- Thêm state `_chevron_width_frac` (init từ floor layer config khi `set_fractions`).
- Thêm method `set_floor_proxy(chevron_width_frac, …)` để nhận data từ floor layer.
- Thêm `_floor_footprint_polygon()` helper (compute trapezoid outline).
- Thêm `_chevron_width_handle_pos()` helper.
- Thêm `_handle_at` nhận diện `"chevron_width"` handle.
- Thêm logic drag trong `mouseMoveEvent` cho `"chevron_width"`.
- Cập nhật `paintEvent`: vẽ floor footprint outline (faint), vẽ chevron width handle.
- Cập nhật signal `changing` / `committed` thêm 1 float (`chevron_width_frac`) → 12 floats.

**3. `studio/editor/preview_panel.py` — `_on_floor_wall_edit_toggled` (dòng 1175):**

- Extend show/hide logic gộp stickman.

**4. `studio/editor/preview_panel.py` — `_on_floor_wall_changing` (dòng 1235):**

- Update signature: thêm `chevron_width_frac` param.
- Update live renderer cho chevron width: `self._live_renderer.update_floor_chevron_width(chevron_width_frac)` (cần thêm method).

**5. `studio/editor/preview_panel.py` — `_on_floor_wall_committed` (dòng 1267):**

- Update signature: thêm `chevron_width_frac` param.
- Lưu vào `seg.render_settings["chevron_width_frac"]` cho legacy.
- Forward signal lên main_window.

**6. `studio/editor/preview_panel.py` — Button toolbar (dòng 1671-1721):**

- Xoá `self.stickman_button` widget + connect.
- Rename `floor_wall_button` thành `edit_layout_button` (hoặc giữ tên cũ, rename label).
- Cập nhật label button thành "Edit Layout" (hoặc "Layout").
- Xoá `control_row.addWidget(self.stickman_button)`.

**7. `studio/editor/preview_panel.py` — `_refresh_stickman_button_state` (dòng 1038):**

- Refactor: bỏ stickman_button enable/disable.
- Giữ phần update `self.stickman_overlay.set_normalized(...)` để khi user toggle edit layout, overlay hiển thị đúng vị trí.
- Rename method thành `_refresh_stickman_overlay_state` để phản ánh đúng.

**8. `studio/editor/preview_panel.py` — `_on_stickman_edit_toggled` (dòng 1121):**

- KHÔNG xoá, refactor thành 2 helper:
  - `_show_stickman_overlay()`: extract logic show.
  - `_hide_stickman_overlay()`: extract logic hide.
- `_on_floor_wall_edit_toggled` gọi 2 helper này.
- Hoặc inline luôn — tuỳ taste.

**9. `studio/editor/preview_panel.py` — clear / cleanup paths:**

- Dòng 1420-1437: stop_live_preview hoặc clear() đang stop + hide cả 2 overlay. Giữ nguyên.

**10. `src/live_renderer.py` — Thêm `update_floor_chevron_width`:**

```python
def update_floor_chevron_width(self, chevron_width_frac: float) -> None:
    """Hot-update chevron width without rebuilding the whole renderer."""
    self._chevron_width_frac = max(0.05, min(0.95, float(chevron_width_frac)))
    if self._tunnel is not None:
        self._tunnel.set_chevron_width_frac(self._chevron_width_frac)
```

Cần thêm `set_chevron_width_frac` vào `TunnelRenderer` ở `src/rhythm.py` để hot-update.

**11. `studio/editor/preview_panel.py` — `_on_floor_wall_edit_toggled` đoạn set_fractions:**

Truyền `chevron_width_frac` vào `set_fractions`:

```python
chevron_w = float(rs.get("chevron_width_frac", 0.45) or 0.45)
# Resolve from floor layer if exists (similar to countdown)
if self._project_layers:
    floor_layers = [
        la for la in self._project_layers
        if la.kind == "floor" and la.overlaps(seg.start_time_sec, seg.end_time_sec)
    ]
    if floor_layers:
        top = max(floor_layers, key=lambda la: la.z_index)
        chevron_w = float(top.config.get("chevron_width_frac", chevron_w) or chevron_w)
```

Pattern này tương tự `_get_countdown_bbox` (dòng 1288-1310). Đề xuất tạo helper `_get_floor_chevron_width(seg) -> float`.

---

## Layout & visual

### Edit Layout button position

Giữ ở vị trí cũ của Floor/Wall button (dòng 1721 trong control_row). Stick Box button (dòng 1721) bị remove.

### Icon

Có thể cập nhật icon để phản ánh "edit all layouts": vd icon dạng "layers" hoặc "grid" thay vì "wall". Hoặc giữ icon cũ và chỉ rename label.

### Khi Edit Layout active

Cả 2 overlay (FloorWallOverlay + StickmanBoxOverlay) hiển thị. User có thể tương tác với bất kỳ overlay nào. Hit-test giữa 2 overlay:

- StickmanBoxOverlay setGeometry vào image rect (centered with letterbox).
- FloorWallOverlay setGeometry vào full stage.

→ Stickman overlay nằm "trên" floor wall ở vùng image rect. Click vào stickman handle sẽ hit stickman, không hit floor wall behind.

Cần verify Qt window stacking order — Tool windows mới show có thể nằm trên cũ. Stickman set show SAU floor wall trong `_on_floor_wall_edit_toggled` → stickman trên top → hit-test priority đúng.

Nếu stickman overlay che mất handle quan trọng của floor wall (vd horizon handle), user không grab được. Cần test thực tế.

### Visual khi 2 overlay show

Cẩn thận về visual conflict:
- Stickman: cyan dashed box + cyan corners
- FloorWallOverlay: nhiều màu (cyan, amber, magenta, orange, red, pink)

Tránh cyan-cyan conflict cho stickman + floor handle (cũng cyan). Có thể:
- Đổi stickman color thành màu khác (vd green) khi gộp.
- Hoặc giữ và chấp nhận user phân biệt theo shape (line vs box).

Đề xuất giữ màu cũ, không đổi — overhead không đáng.

### Floor footprint outline visual

Trapezoid mờ (cyan #0891b2 alpha 30) viền dashed nét mảnh (1px). Không vẽ label. Mục đích: visualize, không clickable.

```
                 _________
               /           \      ← horizon line
              /             \
             /               \
            /                 \
           /___________________\   ← floor line
```

Vẽ trong `paintEvent` của FloorWallOverlay, sau khi vẽ camera handles, trước khi vẽ countdown/gate boxes (ở dưới z-order so với handle solid).

### Chevron width handle visual

Handle nhỏ (radius 6) màu cyan, đặt ở giữa floor surface. Khi drag, hiện 2 vạch dọc nhỏ (chiều cao 10px) ở 2 bên, cách nhau theo `chevron_width_frac × floor_mid_width`.

Label dưới handle: "Chevron W 0.45".

---

## Test scenarios

### Test 1: Side_rails auto-create on drag

```
Setup: segment không có side_rails layer.
Action: bật Edit Layout, kéo handle Rail H xuống.
Verify: side_rails layer mới được tạo cho segment range.
Verify: layer.config["rail_height"] == giá trị kéo.
Verify: layer.config["side_rails"] == True.
Action: reload project.
Verify: rail_height giữ nguyên, không reset về default.
```

### Test 2: Side_rails update existing layer

```
Setup: segment có side_rails layer rail_height=0.20.
Action: kéo handle Rail H đến 0.30.
Verify: layer.config["rail_height"] == 0.30 (update, không tạo mới).
```

### Test 3: Start_gate auto-create on drag

```
Setup: segment không có start_gate layer.
Action: bật Edit Layout, kéo handle gate_top.
Verify: start_gate layer mới được tạo với 9 keys default + start_gate_h từ kéo.
Verify: layer.config["start_gate_enabled"] == True.
```

### Test 4: Floor footprint outline visualization

```
Setup: segment bất kỳ.
Action: bật Edit Layout.
Verify: thấy trapezoid cyan mờ trên preview, viền dashed.
Verify: outline khớp với floor footprint thực tế.
Action: kéo handle Horizon hoặc Floor.
Verify: outline cập nhật real-time theo handle move.
```

### Test 5: Chevron width handle drag

```
Setup: segment có floor layer chevron_width_frac=0.45.
Action: bật Edit Layout, kéo handle chevron width sang phải.
Verify: handle visual move + 2 vạch dọc rộng ra.
Verify: live preview chevron rộng hơn.
Verify: layer.config["chevron_width_frac"] cập nhật.
Verify: label "Chevron W X.XX" cập nhật.
Release: commit, push undo.
```

### Test 6: Floor auto-create khi drag chevron (defensive)

```
Setup: segment không có floor layer (xoá thủ công).
Action: kéo chevron width handle.
Verify: floor layer mới tạo với default config + chevron_width_frac từ kéo.
```

### Test 7: Edit Layout button toggle gộp stickman + floor

```
Setup: segment có stickman enabled.
Action: bấm Edit Layout button.
Verify: cả 2 overlay hiện (StickmanBox + FloorWall).
Verify: thấy floor footprint, camera handles, countdown box, gate handle, rail handle, chevron handle, stickman bbox.
Action: bấm Edit Layout lần 2.
Verify: cả 2 overlay hide.
```

### Test 8: Edit Layout với segment không có stickman

```
Setup: segment dance (stickman = False).
Action: bấm Edit Layout.
Verify: chỉ hiện FloorWallOverlay, không có StickmanBoxOverlay.
Verify: không có error log.
```

### Test 9: Stick Box button đã xoá

```
grep "stickman_button" preview_panel.py → 0 hit (sau cleanup).
Toolbar không còn button "Stick Box".
Edit Layout button vẫn work.
```

### Test 10: Hit-test khi 2 overlay chồng

```
Setup: Edit Layout active, stickman bbox đè handle Horizon.
Action: click vào stickman handle (corner).
Verify: trigger stickman drag, không trigger horizon drag.
Action: click ngoài stickman bbox vào horizon handle.
Verify: trigger horizon drag.
```

### Test 11: Live preview off → Edit Layout disabled

```
Setup: live preview chưa bật.
Verify: Edit Layout button disabled.
Action: bật live preview.
Verify: Edit Layout button enable.
```

### Test 12: Edit Layout active + switch segment

```
Setup: Edit Layout đang ON ở segment A.
Action: chọn segment B trong timeline.
Verify: overlay update giá trị từ segment B (camera, layer config mới).
Verify: nếu B không có stickman, stickman overlay hide; floor wall vẫn show.
Verify: nếu B không có start_gate enabled, gate box ẩn (không vẽ).
```

### Test 13: Backward compat — load project cũ không có chevron_width_frac key

```
Setup: project cũ, floor layer không có chevron_width_frac.
Load → resolve_segment_config fall back default.
Edit Layout: chevron handle hiện ở vị trí default 0.45.
Drag: tạo / update key trong layer config.
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **Stickman overlay logic cốt lõi** (set_normalized, drag, commit signal): KHÔNG đổi. Chỉ refactor cách show/hide (gộp vào Edit Layout button).

2. **`stickman_location_changed` signal** (preview_panel → main_window): giữ nguyên. Logic persist ở main_window.py không đụng.

3. **`_on_stickman_location_edited`** ở main_window.py: giữ nguyên (đã đúng — auto-create stickman layer).

4. **Camera perspective writes** vào `seg.render_settings` (floor_hit_frac, horizon_frac, etc.): giữ nguyên. Đây là source of truth cho camera, không phải layer config.

5. **`resolve_segment_config`**: không đụng. Vẫn strip visual fields, update từ layer.

6. **`_default_layer_config("side_rails" / "start_gate" / "floor")`**: dùng làm template cho default config khi auto-create — verify khớp 100% với pattern hiện có.

7. **Live preview update path** (`update_floor_wall`, `update_side_rail_height`, `update_countdown_box`, `update_start_gate_height`): giữ nguyên. Spec thêm `update_floor_chevron_width` mới, không sửa cái cũ.

8. **Auto-create cho countdown** (đã có): không đụng. Chỉ thêm side_rails + start_gate + floor để consistency.

9. **`_segment_stickman_enabled` check** (dòng 1028): giữ nguyên. Edit Layout dùng để quyết định show stickman overlay hay không.

10. **Tooltip / cursor logic** trong overlay: giữ nguyên. Chỉ thêm cursor handling cho chevron handle mới.

11. **FloorWallOverlay signal payload**: hiện 11 floats, sẽ thành 12 floats sau spec. Mọi connect tới signal phải update signature. Không có connect nào ngoài preview_panel.py — verify grep.

---

## Pattern code hiện có để tham khảo

- **Auto-create layer pattern**: xem `_on_stickman_location_edited` dòng 1248-1265 và `_on_floor_wall_committed` countdown branch dòng 1314-1337. Copy y nguyên cho side_rails / start_gate / floor.

- **`_default_layer_config`**: file `studio/editor/timeline_panel.py`. Cho mỗi kind có default dict — dùng làm template khi auto-create.

- **`_get_countdown_bbox` helper**: file `studio/editor/preview_panel.py` dòng 1288-1310. Pattern resolve từ layer config với fallback. Copy cho `_get_floor_chevron_width`.

- **`StickmanBoxOverlay` class**: file `studio/editor/preview_panel.py` dòng 43-238. Pattern Tool window translucent + bbox drag. Reference cho mọi overlay mới (V2).

- **`FloorWallOverlay.changing` / `committed`**: pattern signal float* tuple. Xem dòng 256-259 cho cách add float vào tuple.

- **`_PathBrowseWidget`** trong segment_config_panel.py: nếu sau này thêm floor_panel_image edit qua overlay (V2), tham khảo.

- **`_floor_wall_pos_timer` + `_stickman_pos_timer`**: 2 QTimer độc lập sync overlay position theo stage rect. Edit Layout active → start cả 2 timer.

---

## Thứ tự implement đề xuất

1. **Fix 1 — Side_rails auto-create.** Trivial: thêm `if not rail_layers: append(Layer(...))`. Test 1, 2.

2. **Fix 2 — Start_gate auto-create.** Tương tự Fix 1. Test 3.

3. **Feature 1a — Floor footprint outline.** Vẽ trapezoid trong `paintEvent` của `FloorWallOverlay`. Không đụng signal payload. Test 4.

4. **Feature 1b — Chevron width handle.** 
   - Thêm state `_chevron_width_frac` + method `set_floor_proxy`.
   - Thêm `_chevron_width_handle_pos`, hit-test, drag logic.
   - Cập nhật signal payload từ 11 → 12 floats.
   - Thêm `update_floor_chevron_width` ở live_renderer.
   - `TunnelRenderer.set_chevron_width_frac` ở rhythm.py.
   - `_on_floor_wall_edit_toggled` truyền chevron_width_frac vào set_fractions.
   - `_on_floor_wall_committed` extend signature, lưu vào layer.config + seg.render_settings.
   - Auto-create floor layer (defensive). Test 5, 6.

5. **Feature 2 — Gộp button.**
   - Refactor `_on_stickman_edit_toggled` thành 2 helper `_show_stickman_overlay` / `_hide_stickman_overlay`.
   - Extend `_on_floor_wall_edit_toggled` gọi helper.
   - Xoá `stickman_button` widget + connect + toolbar add.
   - Refactor `_refresh_stickman_button_state` thành `_refresh_stickman_overlay_state`.
   - Rename `floor_wall_button` label → "Edit Layout" (hoặc "Layout").
   - Test 7, 8, 9.

6. **Smoke test toàn bộ.**
   - Switch segment khi Edit Layout active (Test 12).
   - Live preview off → button disabled (Test 11).
   - Hit-test 2 overlay chồng (Test 10).
   - Backward compat project cũ (Test 13).

---

## Open questions

(1) **Tên button mới**: "Edit Layout" hay "Layout" hay giữ "Floor / Wall"? Tôi đề xuất **"Edit Layout"** rõ semantic. Bạn quyết định.

(2) **Stick Box button: xoá hoàn toàn hay giữ làm "stickman only mode"?** Tôi đề xuất xoá. Bạn confirm?

(3) **Edit Layout active khi live preview off**: vẫn enable hay disable? Spec đề xuất disable (giống Floor/Wall hiện tại). Nhưng stickman có thể edit ngoài live preview. Bạn quyết.

(4) **Floor overlay V1 có cần chevron width handle không, hay chỉ outline visualization là đủ?** Outline visualize đơn giản, chevron handle phức tạp hơn nhưng functional. Tôi đề xuất cả 2. Bạn cắt scope nếu muốn.

(5) **Color của floor outline**: cyan (cùng màu LAYER_KIND_COLORS["floor"]) — có conflict với handle Floor hiện tại (cũng cyan)? Có thể đổi outline sang màu khác nếu confuse. Tôi giữ cyan với alpha thấp.

(6) **Khi auto-create side_rails layer qua drag, có nên set `side_rails: True` (toggle on)?** Spec đề xuất YES (user kéo handle = chủ ý dùng rails). Bạn confirm?

(7) **Stickman overlay color khi gộp**: giữ cyan (giống handle Floor) hay đổi sang green (LAYER_KIND_COLORS["stickman"] = yellow)? Cyan có thể confuse với handle Floor. Tôi đề xuất giữ cyan vì shape khác (box vs line). Bạn quyết.

(8) **Signal payload 12 floats**: refactor thành dict signal để dễ maintain (ngoài scope spec này) hay tiếp tục thêm float? Tôi giữ float tuple cho consistency. Refactor là việc lớn riêng.
