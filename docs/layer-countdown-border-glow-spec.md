# Layer Countdown — Border Thickness + Glow Strength Spec

## Mục tiêu

Bổ sung 2 option mới cho countdown layer cho phép user kiểm soát visual của số đếm:

1. **Độ dày viền (border thickness)** — viền màu bao quanh số (hiện hardcode `thickness + 2` pixel).
2. **Cường độ vầng quang (glow strength)** — quầng sáng quanh số, scale 0..100 (hiện hardcode 3 layer + sigma 4.0 + blend 0.95).

Spec này độc lập với:

- [`layer-countdown-fixes-spec.md`](./layer-countdown-fixes-spec.md) — fix bugs cho countdown layer.
- [`layer-resize-move-threshold-spec.md`](./layer-resize-move-threshold-spec.md) — threshold-based snap-to-fill cho layer.
- [`drag-mechanics-offset-and-ergonomics-spec.md`](./drag-mechanics-offset-and-ergonomics-spec.md) — segment offset + cursor + tooltip.

Tuy nhiên, spec này **dùng chung pattern UI Section** với fixes spec (Fix 1+5 đã thêm group "Position & size" vào `_CountdownSection`). Implement Feature này nên làm SAU Fix 1+5 để có sẵn pattern Section, hoặc làm song song nếu Cursor xử lý được merge. Xem section "Phụ thuộc & thứ tự" cuối spec.

---

## Code hiện tại cần sửa

**File chính:** `src/rhythm.py`

**Class:** `CountdownHUD` (dòng ~4522).

**Hàm:** `_draw_glow_text` (dòng ~4572-4615) chứa toàn bộ logic vẽ text + glow + border.

```python
# CŨ — hardcoded
def _draw_glow_text(self, canvas, *, text, x, y, font_scale, thickness,
                    alpha=1.0, glow_boost=1.0):
    if alpha <= 1e-4:
        return
    alpha = float(max(0.0, min(1.0, alpha)))
    glow_boost = float(max(0.2, glow_boost))

    # 3 glow layers hardcoded:
    glow = np.zeros_like(canvas)
    for thick, weight in (
        (thickness + 10, 0.10),
        (thickness + 6,  0.18),
        (thickness + 3,  0.28),
    ):
        layer = np.zeros_like(canvas)
        cv2.putText(layer, text, (x, y), self._font, font_scale,
                    self._color_bgr, thick, cv2.LINE_AA)
        glow = cv2.addWeighted(glow, 1.0, layer, float(weight * glow_boost), 0.0)
    glow = cv2.GaussianBlur(glow, (0, 0), sigmaX=4.0, sigmaY=4.0)
    canvas[:] = cv2.addWeighted(canvas, 1.0, glow, 0.95 * alpha, 0.0)

    # Border (color) + inner (white) — border thickness hardcode +2:
    crisp = np.zeros_like(canvas)
    cv2.putText(crisp, text, (x, y), self._font, font_scale,
                self._color_bgr, max(1, thickness + 2), cv2.LINE_AA)
    cv2.putText(crisp, text, (x, y), self._font, font_scale,
                (255, 255, 255), max(1, thickness), cv2.LINE_AA)
    canvas[:] = cv2.addWeighted(canvas, 1.0, crisp, alpha, 0.0)
```

Lưu ý semantics quan trọng:

- `self._color_bgr` (set từ `relax_countdown_color`) là màu BORDER, không phải màu inner.
- Inner luôn là white `(255,255,255)`, hardcode.
- Spec này KHÔNG đụng vào semantics đó. Nếu sau này muốn tách màu inner thành option riêng, là feature khác.

---

## Hai keys config mới

```
relax_countdown_border_thickness  : float  range [0.0, 10.0]   default 2.0
relax_countdown_glow_strength     : float  range [0.0, 100.0]  default 60.0
```

### `relax_countdown_border_thickness`

Số pixel cộng thêm vào base text thickness để vẽ viền màu (color border). Mapping:

```
0.0   → không vẽ border layer (chỉ inner white text)
2.0   → behavior hiện tại (default)
10.0  → viền cực dày, text trông "khối" / "blocky"
```

Decimal cho phép tinh chỉnh khi font lớn (vd 1.5, 3.5).

### `relax_countdown_glow_strength`

Cường độ vầng quang, scale 0..100 đồng nhất với flag `-g` ở `src/main.py`. Mapping:

```
0     → tắt glow hoàn toàn (skip toàn bộ glow pass — tiết kiệm CPU)
60    → ~ behavior hiện tại (default)
100   → glow tối đa (intensity + spread đều scale lên)
```

Strength ảnh hưởng 3 thứ trong glow pass:

1. **Layer weights** — `weight = base_weight * glow_boost * (strength/100)`.
2. **Blur sigma** — `sigma = max(0.5, 4.0 * (strength/100))`.
3. **Final blend** — `final_blend = 0.95 * alpha * (strength/100)`.

Lý do scale cả 3: glow weak cần spread nhỏ và mờ, không chỉ giảm intensity. Nếu chỉ scale weight, glow sẽ trông "hard edge" khi yếu.

---

## Render logic mới

```python
def _draw_glow_text(self, canvas, *, text, x, y, font_scale, thickness,
                    alpha=1.0, glow_boost=1.0):
    if alpha <= 1e-4:
        return
    alpha = float(max(0.0, min(1.0, alpha)))
    glow_boost = float(max(0.2, glow_boost))

    # ── Glow pass (skip nếu strength = 0) ─────────────────────────────
    glow_norm = self._glow_strength / 100.0  # 0.0..1.0
    if glow_norm > 1e-4:
        glow = np.zeros_like(canvas)
        for thick_off, base_weight in (
            (10, 0.10), (6, 0.18), (3, 0.28),
        ):
            layer = np.zeros_like(canvas)
            cv2.putText(
                layer, text, (x, y), self._font, font_scale,
                self._color_bgr, thickness + thick_off, cv2.LINE_AA,
            )
            weight = base_weight * glow_boost * glow_norm
            glow = cv2.addWeighted(glow, 1.0, layer, weight, 0.0)
        sigma = max(0.5, 4.0 * glow_norm)
        glow = cv2.GaussianBlur(glow, (0, 0), sigmaX=sigma, sigmaY=sigma)
        final_blend = 0.95 * alpha * glow_norm
        canvas[:] = cv2.addWeighted(canvas, 1.0, glow, final_blend, 0.0)

    # ── Crisp text (border + inner) ───────────────────────────────────
    crisp = np.zeros_like(canvas)
    border_off = max(0, int(round(self._border_thickness)))
    if border_off > 0:
        cv2.putText(
            crisp, text, (x, y), self._font, font_scale,
            self._color_bgr, max(1, thickness + border_off), cv2.LINE_AA,
        )
    cv2.putText(
        crisp, text, (x, y), self._font, font_scale,
        (255, 255, 255), max(1, thickness), cv2.LINE_AA,
    )
    canvas[:] = cv2.addWeighted(canvas, 1.0, crisp, alpha, 0.0)
```

`CountdownHUD.__init__` thêm 2 params:

```python
def __init__(
    self, cam, color="#FFFFFF",
    max_show_sec=5.0,
    box=None,
    anim="pop",
    border_thickness: float = 2.0,    # NEW
    glow_strength: float = 60.0,      # NEW
):
    # ... existing init ...
    self._border_thickness = max(0.0, min(10.0, float(border_thickness)))
    self._glow_strength = max(0.0, min(100.0, float(glow_strength)))
```

Bổ sung public setter để hot-update không phải rebuild HUD:

```python
def set_style(self, *, border_thickness: float | None = None,
              glow_strength: float | None = None) -> None:
    """Hot-update visual style without rebuilding the HUD."""
    if border_thickness is not None:
        self._border_thickness = max(0.0, min(10.0, float(border_thickness)))
    if glow_strength is not None:
        self._glow_strength = max(0.0, min(100.0, float(glow_strength)))
```

---

## 9 touch points trong toàn pipeline

Truyền 2 keys mới end-to-end qua mọi layer của hệ thống.

### 1. `studio/models/render_settings.py` — Pydantic schema

Sau các `relax_countdown_*` fields hiện có (dòng ~175-188), thêm:

```python
relax_countdown_border_thickness: float = Field(default=2.0, ge=0.0, le=10.0)
relax_countdown_glow_strength: float = Field(default=60.0, ge=0.0, le=100.0)
```

### 2. `studio/models/layer.py` — Visual fields registry

`_VISUAL_FIELDS_BY_KIND["countdown"]` (dòng ~148-159): thêm 2 keys vào list để migration extract đúng.

```python
"countdown": [
    # ... existing 14 keys ...
    "relax_countdown_border_thickness",
    "relax_countdown_glow_strength",
],
```

### 3. `studio/editor/timeline_panel.py` — Default config

`_default_layer_config("countdown")` (dòng ~3238-3250): thêm 2 keys với default.

```python
"relax_countdown_border_thickness": 2.0,
"relax_countdown_glow_strength": 60.0,
```

### 4. `studio/editor/layer_edit_dialog.py` — UI Section

`_CountdownSection.__init__`: thêm 2 spinbox trong group "Style" (xem section "Layout UI" bên dưới).

```python
self._border_thick_sp = QDoubleSpinBox()
self._border_thick_sp.setRange(0.0, 10.0)
self._border_thick_sp.setSingleStep(0.5)
self._border_thick_sp.setDecimals(1)
self._border_thick_sp.setValue(float(config.get("relax_countdown_border_thickness", 2.0)))
self._border_thick_sp.setToolTip("Độ dày viền của số (0 = không viền)")
self._border_thick_sp.valueChanged.connect(self.changed)

self._glow_strength_sp = QSpinBox()
self._glow_strength_sp.setRange(0, 100)
self._glow_strength_sp.setSingleStep(5)
self._glow_strength_sp.setSuffix(" %")
self._glow_strength_sp.setValue(int(round(float(config.get("relax_countdown_glow_strength", 60.0)))))
self._glow_strength_sp.setToolTip("Cường độ vầng quang (0 = tắt)")
self._glow_strength_sp.valueChanged.connect(self.changed)
```

`get_config` trả thêm 2 keys:

```python
"relax_countdown_border_thickness": self._border_thick_sp.value(),
"relax_countdown_glow_strength": float(self._glow_strength_sp.value()),
```

### 5. `src/rhythm.py` — Renderer + Visualizer + argparse

**a.** `Visualizer.__init__` (sau dòng ~5614, sau các `RELAX_COUNTDOWN_*` hiện có):

```python
self.RELAX_COUNTDOWN_BORDER_THICKNESS: float = 2.0
self.RELAX_COUNTDOWN_GLOW_STRENGTH: float = 60.0
```

**b.** Main flow xây dựng `CountdownHUD` (dòng ~6087-6099): pass 2 params mới.

```python
countdown_hud = CountdownHUD(
    self.cam,
    color=str(getattr(self, "RELAX_COUNTDOWN_COLOR", "#FFFFFF")),
    max_show_sec=float(getattr(self, "RELAX_COUNTDOWN_MAX_SEC", 5.0)),
    anim=str(getattr(self, "RELAX_COUNTDOWN_ANIM", "pop")),
    box=(...),
    border_thickness=float(getattr(self, "RELAX_COUNTDOWN_BORDER_THICKNESS", 2.0)),  # NEW
    glow_strength=float(getattr(self, "RELAX_COUNTDOWN_GLOW_STRENGTH", 60.0)),       # NEW
)
```

**c.** Argparse (sau dòng ~7375, sau các flag countdown hiện có):

```python
p.add_argument('--relax_countdown_border_thickness', type=float, default=2.0,
               help='Countdown text border thickness in pixels (0..10).')
p.add_argument('--relax_countdown_glow_strength', type=float, default=60.0,
               help='Countdown text glow strength (0..100).')
```

**d.** Wire viz attrs từ args (sau dòng ~7558):

```python
viz.RELAX_COUNTDOWN_BORDER_THICKNESS = max(
    0.0, min(10.0, float(args.relax_countdown_border_thickness))
)
viz.RELAX_COUNTDOWN_GLOW_STRENGTH = max(
    0.0, min(100.0, float(args.relax_countdown_glow_strength))
)
```

### 6. `src/live_renderer.py` — Live frame renderer

**a.** `LiveFrameRenderer.__init__` (sau dòng ~251, sau các countdown params):

```python
relax_countdown_border_thickness: float = 2.0,
relax_countdown_glow_strength: float = 60.0,
```

**b.** Lưu state (sau dòng ~342):

```python
self._relax_countdown_border_thickness = max(0.0, min(10.0, float(relax_countdown_border_thickness)))
self._relax_countdown_glow_strength = max(0.0, min(100.0, float(relax_countdown_glow_strength)))
```

**c.** Khi tạo `CountdownHUD` (dòng ~1031-1041), pass thêm:

```python
self._countdown_hud = CountdownHUD(
    ...
    border_thickness=self._relax_countdown_border_thickness,
    glow_strength=self._relax_countdown_glow_strength,
)
```

**d.** `update_render_settings` (dòng ~552-565) thêm 2 params optional:

```python
relax_countdown_border_thickness: Optional[float] = None,
relax_countdown_glow_strength: Optional[float] = None,
```

Trong body (dòng ~698-729), update state + hot-apply qua setter mới:

```python
border_changed = relax_countdown_border_thickness is not None
glow_changed = relax_countdown_glow_strength is not None

if border_changed:
    self._relax_countdown_border_thickness = max(
        0.0, min(10.0, float(relax_countdown_border_thickness))
    )
if glow_changed:
    self._relax_countdown_glow_strength = max(
        0.0, min(100.0, float(relax_countdown_glow_strength))
    )

if (border_changed or glow_changed) and self._countdown_hud is not None:
    self._countdown_hud.set_style(
        border_thickness=self._relax_countdown_border_thickness if border_changed else None,
        glow_strength=self._relax_countdown_glow_strength if glow_changed else None,
    )
```

### 7. `studio/editor/main_window.py` — Live renderer kwargs

`_live_renderer_kwargs` (dòng ~2370): thêm 2 keys vào kwargs dict (sau dòng ~2468):

```python
"relax_countdown_border_thickness": float(_get("relax_countdown_border_thickness", 2.0) or 2.0),
"relax_countdown_glow_strength": float(_get("relax_countdown_glow_strength", 60.0) or 60.0),
```

### 8. `studio/editor/preview_panel.py` — Update render settings forwarding

`update_render_settings` (dòng ~2431) tương tự live_renderer:

```python
relax_countdown_border_thickness: Optional[float] = None,
relax_countdown_glow_strength: Optional[float] = None,
```

Pass tới live_renderer (dòng ~2522-2535):

```python
relax_countdown_border_thickness=relax_countdown_border_thickness,
relax_countdown_glow_strength=relax_countdown_glow_strength,
```

### 9. `studio/core_bridge/render_service.py` — CLI export args

`_ALLOWED_KEYS` (dòng ~716-744): thêm 2 keys mới vào set để CLI render path pass đúng.

```python
"relax_countdown_border_thickness",
"relax_countdown_glow_strength",
```

---

## Layout UI Section

Sau khi gộp với Fix 5 (Position & size group), `_CountdownSection` cuối cùng có 3 phần:

```
[Main form rows]
  Countdown        [✓] Enabled
  Color            [colored button]
  Max seconds      [spinbox 5.0]
  Number effect    [combo: Pop / Flash / Fade Cross / Shake]
  Audio            [✓] Enable countdown sound
  Sound source     [combo: Default beep / Audio file]   (visible khi audio on)
  Sound file       [path browse]                         (visible khi mode=file)
  Sound volume     [spinbox 0.65]                       (visible khi audio on)
  Last count sound [combo: Default / Use another / Same] (visible khi audio on)
  Last sound file  [path browse]                         (visible khi last mode=file)

[Position & size groupbox]   ← từ fixes spec Fix 5
  X (0..1)         [spinbox 0.88]
  Y (0..1)         [spinbox 0.04]
  Width            [spinbox 0.10]
  Height           [spinbox 0.16]

[Style groupbox]              ← Feature spec này (mới)
  Border thickness [spinbox 2.0]    tooltip: "Độ dày viền (0 = không viền)"
  Glow strength    [spinbox 60 %]   tooltip: "Cường độ vầng quang (0 = tắt)"
```

Group "Style" đặt sau "Position & size" (visual sau bbox). Nếu fixes spec chưa được implement (chưa có Position & size group), thì spec này thêm "Style group" ngay sau main form. Layout vẫn coherent.

---

## Backward compatibility

**Project cũ không có 2 keys mới:**

- `resolve_segment_config` trả về effective không có 2 keys.
- Pydantic `BaseRenderSettings` fall back về default (2.0 và 60.0).
- Renderer dùng default → behavior giống hệt trước feature.
- **Không có visual regression**, verify qua Test 14.

**Migration tự động:**

Khi load project cũ qua `migrate_render_settings_to_layers` (`studio/models/layer.py` dòng ~163-198), nếu seg.render_settings ngẫu nhiên có 2 keys mới (chỉ xảy ra nếu user đã chạy CLI với flag manual hoặc edit JSON tay), chúng được extract vào layer.config nhờ touch point #2 đã thêm vào `_VISUAL_FIELDS_BY_KIND["countdown"]`. OK, tự động.

---

## Test scenarios

### Test 1: Border thickness control

```
Setup: countdown layer config border_thickness=2.0.
Render frame → snapshot pixel ở rìa text.
Action: đổi UI spinbox → 8.0. Đợi debounce. Render lại.
Verify: rìa text dày hơn rõ rệt (>= 2x pixel count vùng border).
Action: đổi → 0.0. Render lại.
Verify: text chỉ có inner white, không có rìa màu — pixel ở rìa giáp background không phải màu countdown.
```

### Test 2: Glow strength control

```
Setup: countdown layer config glow_strength=60.
Render frame → snapshot.
Action: đổi → 0. Render lại.
Verify: vùng halo quanh text không còn (tổng pixel sáng giảm đáng kể).
Action: đổi → 100. Render lại.
Verify: glow mạnh hơn (vùng halo lan rộng hơn, sáng hơn).
```

### Test 3: Default values consistency

```
Layer mới tạo qua "+":
  config["relax_countdown_border_thickness"] == 2.0
  config["relax_countdown_glow_strength"] == 60.0
UI spinbox hiện 2.0 và "60 %" mặc định.
Render với defaults → visual giống behavior trước khi feature thêm.
```

### Test 4: Backward compat

```
Project cũ không có 2 keys mới.
Load → resolve_segment_config → effective không có 2 keys.
Pydantic schema fall back: 2.0 và 60.0.
Render → identical với pre-feature behavior. Không regression.
```

### Test 5: Hot-update không phải rebuild HUD

```
Setup: live preview đang chạy, countdown layer đang render.
Action: đổi border_thickness từ 2 → 5 trong Inspector. Đợi debounce.
Verify: frame kế tiếp hiển thị border dày 5 ngay, KHÔNG bị flicker / tạo lại HUD object.
Action: đổi glow_strength từ 60 → 0.
Verify: glow biến mất ngay tại frame kế, không tạo lại HUD.
Action: đổi glow_strength → 100.
Verify: glow mạnh tối đa ngay frame kế.
```

### Test 6: CLI export pipeline

```
Setup: countdown layer có border_thickness=5.0, glow_strength=80.
Action: trigger render export qua MainWindow → render_service → src/rhythm.py CLI.
Verify: CLI args bao gồm --relax_countdown_border_thickness 5.0 --relax_countdown_glow_strength 80.
Verify: rendered video frame có border dày 5 và glow strength 80.
```

### Test 7: Pydantic schema validation

```
Setup: BaseRenderSettings(relax_countdown_border_thickness=15.0)
Verify: ValidationError (out of range 0..10).
Setup: BaseRenderSettings(relax_countdown_glow_strength=-5)
Verify: ValidationError (out of range 0..100).
```

---

## Quan trọng: KHÔNG được phá vỡ

1. **Default visual behavior** phải tái hiện đúng visual hardcode hiện tại với `border_thickness=2.0` và `glow_strength=60.0`. Test 4 verify. Nếu Test 4 fail tức là default mapping chưa đúng — phải tune lại.

2. **`CountdownHUD._color_bgr` semantics** tiếp tục là màu BORDER (inner luôn white). Spec này KHÔNG đổi semantics — chỉ thêm option chỉnh ĐỘ DÀY của border.

3. **Hot-update qua `set_style()`** thay vì rebuild `CountdownHUD`. Rebuild làm `_last_text`, `_prev_text`, `_last_change_frame`, `_audio_events` reset → animation và audio events bị nhỡ. Set attr trực tiếp giữ state.

4. **Glow strength = 0 phải skip toàn bộ glow pass** (early return, không tốn CPU). Quan trọng cho live preview.

5. **Border thickness = 0 phải skip border `cv2.putText`** (chỉ vẽ inner white). Tiết kiệm 1 putText call.

6. **Pydantic Field range** `ge=0.0, le=10.0` cho border và `ge=0.0, le=100.0` cho glow. Nếu user paste JSON với giá trị ngoài range, Pydantic raise validation error. UI spinbox đã clamp sẵn nên user thường không gặp.

7. **Inspector debounce** giữ nguyên — 2 spinbox mới connect `valueChanged → self.changed` đúng pattern.

8. **Section `get_config` trả dict ĐẦY ĐỦ** — không bỏ sót 2 keys mới. Nếu bỏ sót, Inspector commit `layer.config = new_cfg` sẽ nuốt mất 2 keys (giống bug Fix 1 trong fixes spec). Verify qua Test 1 + Test 2.

9. **Migration tự động** qua `_VISUAL_FIELDS_BY_KIND` — touch point #2 bắt buộc, không được skip. Nếu không thêm, project cũ load không extract 2 keys vào layer.config.

10. **CLI export** qua `_ALLOWED_KEYS` — touch point #9 bắt buộc. Nếu không thêm, render export sẽ silently bỏ 2 flag, video xuất ra dùng default thay vì giá trị user set.

---

## Pattern code hiện có để tham khảo

- **Section spinbox với range float**: xem `_max_sec_sp` trong `_CountdownSection` (dòng ~305-312). Copy cho `_border_thick_sp`.

- **Section spinbox int với suffix `%`**: xem `_glow_strength_sp` trong `_FloorPanelSection` (search "%") nếu có, hoặc dùng pattern mới với `setSuffix(" %")`.

- **Public setter cho hot-update**: xem `CountdownHUD.set_box(x, y, w, h)` (dòng ~4562-4570) làm template cho `set_style()`.

- **`update_render_settings` pattern Optional[T]**: xem các params hiện có trong `LiveFrameRenderer.update_render_settings` (dòng ~552-729).

- **CLI flag argparse**: xem các `--relax_countdown_*` flag hiện có (dòng ~7344-7375).

- **`_ALLOWED_KEYS` set**: xem `studio/core_bridge/render_service.py` dòng ~716-744.

---

## Phụ thuộc & thứ tự

### Phụ thuộc bắt buộc

KHÔNG có phụ thuộc cứng. Spec này tự đứng độc lập.

### Phụ thuộc khuyến nghị

**Implement SAU `layer-countdown-fixes-spec.md` Fix 1+5** (thêm UI Position & size). Lý do:

- Fix 1 fix bug "Section get_config nuốt keys" — spec này cũng thêm keys mới qua get_config, nên cần Fix 1 trước để không dính bug tương tự.
- Fix 5 thêm pattern groupbox vào Section. Spec này thêm groupbox thứ 2 (Style) — dùng cùng pattern.

Nếu implement spec này TRƯỚC fixes spec:

- Cẩn thận với bug Fix 1: phải đảm bảo `get_config` trả ĐẦY ĐỦ 12 keys (10 cũ + 2 mới). Test 1 + 2 verify.
- Layout chỉ có 1 groupbox "Style" sau main form, không có "Position & size" — sau này khi implement Fix 5 thêm vào trên.

### Thứ tự implement đề xuất (cho riêng spec này)

1. **Touch point 1 — Pydantic schema.** Thêm 2 fields. Run Test 7 verify validation.

2. **Touch point 2 — `_VISUAL_FIELDS_BY_KIND`.** Thêm 2 keys. Verify migration tự nhận.

3. **Touch point 3 — Default config.** Thêm 2 keys với default. Verify layer mới tạo có đủ.

4. **Touch points 5a, 5b — Visualizer + CountdownHUD constructor + render logic.** Sửa `_draw_glow_text`, thêm 2 attrs, pass vào constructor. Manual test: chạy CLI với flag mặc định → verify visual giống hệt trước (Test 4).

5. **Touch points 5c, 5d — Argparse + wire viz attrs.** Thêm 2 flag CLI. Manual test: chạy với `--relax_countdown_border_thickness 8` → verify border dày hơn (Test 1).

6. **Touch point 9 — `_ALLOWED_KEYS`.** Thêm 2 keys. Verify render export pass đúng (Test 6).

7. **Touch point 6 — `LiveFrameRenderer`.** Thêm params + state + pass vào HUD + `update_render_settings` + `set_style()` setter. Verify live preview update mượt (Test 5).

8. **Touch point 7 — `_live_renderer_kwargs`.** Thêm 2 keys. Verify live preview load đúng giá trị từ layer.config.

9. **Touch point 8 — `preview_panel.update_render_settings`.** Forward 2 params. Verify Inspector edit propagate xuống live renderer.

10. **Touch point 4 — UI Section.** Thêm 2 spinbox vào `_CountdownSection`. `__init__` đọc + `get_config` trả thêm 2 keys. Verify Test 1, 2, 3, 5.

11. **Smoke test toàn bộ:** create new layer, edit border + glow trong Inspector, render preview, render export. Verify pipeline end-to-end.

Note: Bước 4-9 có thể làm vì có sẵn đủ data trong layer.config (từ bước 3). Bước 10 (UI) cuối cùng để user có thể test trực tiếp.

---

## Open questions

(1) **Mapping glow strength**: hiện spec scale tuyến tính cả 3 yếu tố (weight, sigma, blend) theo `glow_norm = strength/100`. Có thể tinh chỉnh:

- Logarit: glow strength 60 cho cảm giác "trung bình" hơn (linear: 60 vẫn khá yếu so với hardcoded current).
- Chỉ scale weight + blend, giữ sigma cố định 4.0: glow nhỏ vẫn có spread, chỉ giảm intensity.

Tôi đề xuất linear (đơn giản, dễ hiểu). Bạn có ý kiến khác?

(2) **Default border_thickness = 2.0 vs round int 2**: hiện spec dùng float `2.0`. Nếu user thấy decimal khó hiểu, có thể đổi spinbox thành QSpinBox int (bỏ decimals). Tôi giữ float để consistent với 4 spinbox bbox. Bạn quyết định.

(3) **Glow strength range [0, 100] vs [0.0, 1.0]**: hiện 0..100 để đồng nhất với main.py `-g`. Cũng có thể dùng 0..1.0 cho thuần "fraction". Bạn có preference?

(4) **Có cần thêm option `relax_countdown_inner_color`** (màu trong, hiện hardcode white) không? Spec này KHÔNG bao gồm — đó là feature mở rộng riêng. Nếu bạn muốn thêm, nói tôi tạo spec con.

(5) **Có cần animation giữa các giá trị border/glow** khi user kéo slider? Vd kéo glow từ 60 → 0 nên fade dần thay vì cắt đột ngột? Hiện không có animation — frame kế đổi luôn. Tôi nghĩ OK vì user sẽ test giá trị tĩnh, không thường xuyên kéo qua range.
