# PunchTarget — Gap Analysis Hiện tại vs Hình Mẫu

## Mục đích

Spec phân tích **những điểm cần thay đổi** để block hiện tại (tool render) trông giống **hình mẫu chuẩn**. Chỉ hướng dẫn — KHÔNG bao gồm code.

> Reference standard: `docs/punch-block-visual-reference.md`

---

## So sánh tổng quan

### Hình mẫu (target)
- Block vàng đậm sắc, 3D pop rõ rệt
- Top face SÁNG + ngả trắng nổi bật
- Front face canonical color
- Side face dark shadow
- 3 edges chính có rim light vàng-trắng
- Glow halo neon mạnh, lan rộng
- Fist icon đen bold trên front face
- Material glossy, có gradient mịn

### Hình hiện tại (output tool)
- Block cam đậm nhưng FLAT
- Top face chỉ sáng hơn front một chút (không ngả trắng)
- Front face OK nhưng không có gradient
- Side face hơi tối nhưng không đủ contrast
- KHÔNG có rim light trên edges
- Glow halo yếu
- Fist icon có nhưng outline mỏng + size nhỏ
- Material matte flat

---

## 8 ĐIỂM CẦN THAY ĐỔI

### Điểm 1 — Top face PHẢI SÁNG + NGẢ TRẮNG mạnh hơn

**Hiện tại**: Top face chỉ sáng hơn front ~10-15%, vẫn giữ tone cam.
**Mẫu**: Top face sáng hơn front ~40%, có **white tint rõ rệt** (cảm giác highlight neon).

**Yêu cầu thay đổi**:
- Tăng brightness factor cho top face từ `1.15` → **`1.20-1.25`**
- Tăng white tint từ `+15%` → **`+20-25%`**
- Result: top face phải nhìn rõ là "khác chất" với front (sáng + nhạt + có ngả trắng), không chỉ là "front version sáng hơn chút"

---

### Điểm 2 — Side face PHẢI TỐI HƠN nhiều

**Hiện tại**: Side face chỉ tối hơn front ~30-40%.
**Mẫu**: Side face tối hơn front ~60% (factor 0.45-0.55).

**Yêu cầu thay đổi**:
- Giữ side brightness factor `0.45-0.55` (không tăng lên)
- Đảm bảo side face contrast với front rõ rệt
- Result: nhìn vào block phải thấy 3 cấp độ sáng tách bạch (top sáng > front canonical > side dark), không phải "3 shade tương tự nhau"

---

### Điểm 3 — Thêm GRADIENT trong từng face

**Hiện tại**: Mỗi face fill solid 1 màu duy nhất → cảm giác phẳng, paper-cutout.
**Mẫu**: Có **gradient nhẹ** trong từng face → cảm giác material glossy có lighting.

**Yêu cầu thay đổi**:
- **Front face**: gradient từ TOP edge sáng → BOTTOM edge tối (~85% bright vs 100%) — mô tả light from above
- **Top face**: gradient từ FRONT edge sáng → BACK edge tối (~85%) — perspective lighting
- **Side face**: gradient từ FRONT edge sáng (~110%) → BACK edge tối (~85%)
- Gradient phải **subtle** (chênh lệch ~15-20%), không phải graphic design rõ rệt
- Result: cube nhìn như có "chiếu sáng từ phía trước-trên", không như cardboard

---

### Điểm 4 — THÊM RIM LIGHT trên 3 edges chính

**Hiện tại**: KHÔNG có rim light. Edges là hard transition giữa 2 face khác sáng.
**Mẫu**: 3 edges visible đều có **highlight neon** sáng (~2-3px), tạo cảm giác phát sáng.

**Yêu cầu thay đổi**:
- **Edge top-front**: rim light **MẠNH** màu base + ngả trắng (vd vàng → vàng-trắng), thickness 2-3px ở 1080p
- **Edge top-side**: rim light **MEDIUM**, cùng màu, có thể nhạt hơn 30%
- **Edge front-side** (vertical): rim light **WEAK** (subtle), thickness 1-2px
- **Edge bottom**: KHÔNG có rim (shadow zone)
- Result: cube có viền sáng nổi bật, edges trông "phát quang" thay vì hard cut

---

### Điểm 5 — Tăng GLOW HALO mạnh hơn + lan rộng hơn

**Hiện tại**: Glow halo yếu, lan ra ~10-20px, gần như không thấy.
**Mẫu**: Glow halo NEON mạnh, lan ra ~30-50px, nhiều layer (gần thì sáng đậm, xa thì fade).

**Yêu cầu thay đổi**:
- Tăng kernel blur từ `35` → **`45-55`**
- Tăng weight từ `0.95` → **giữ 0.95-1.0** + thêm 1-2 layer outer halo (kernel lớn hơn, weight thấp hơn)
- **Multi-pass**: 3 pass blur với kernel `(20, 40, 70)` và weights `(0.8, 0.5, 0.25)`
- Glow source: **convex hull của 8 corners** (không chỉ top face)
- Glow color: base color × 1.2 (vibrant)
- Result: cube nhìn như đang "phát sáng" trong không gian, không phải "object solid trên bg đen"

---

### Điểm 6 — Tăng size + outline cho FIST ICON

**Hiện tại**: Icon size khá nhỏ (~50-60% front face), outline mỏng (~1-2% size).
**Mẫu**: Icon **CHIẾM ĐẦY** front face (~75-80%), outline đậm (~3-4% size).

**Yêu cầu thay đổi**:
- Tăng icon size từ `front_w * 0.58` → **`front_w * 0.75-0.80`**
- Tăng outline thickness từ `~1px` → **`~3-4% icon size`** (vd icon 80px → outline 3px)
- Đảm bảo fist là **closed fist** (knuckles + grooves + thumb wrap), không phải open palm
- Fill trắng `(245, 245, 245)`, outline đen `(20, 20, 20)`
- Result: icon "đập vào mắt" trên front face, đọc rõ ngay cả ở scale nhỏ

---

### Điểm 7 — Subtle CORNER ROUNDING (đã có nhưng kiểm tra)

**Hiện tại**: Có corner radius `0.08` áp cho front + top face (đã tốt).
**Mẫu**: Cùng mức rounding subtle ~5-8%.

**Yêu cầu thay đổi**:
- **Giữ nguyên** `CORNER_RADIUS=0.08`
- Verify corner rounding áp dụng cho cả front và top face
- Side face có thể skip rounding (vì quá mỏng → artifact)
- Result: corners nhẹ nhàng, không sharp 90° nhưng cũng không pill-shape

---

### Điểm 8 — Anti-aliasing edges THIẾT YẾU

**Hiện tại**: Edges có anti-alias `LINE_AA` của cv2, nhưng vẫn có thể jagged ở scale lớn (preview 540p).
**Mẫu**: Edges hoàn toàn smooth, không có pixel staircase.

**Yêu cầu thay đổi**:
- Giữ `LINE_AA` cho mọi `fillConvexPoly` và `polylines` trong block render
- Khi migrate ModernGL: bật **MSAA 8×** trên framebuffer
- Kiểm tra: ở preview 720p (sau khi đã tăng từ 540p), edges phải smooth ≤ 1px step
- Result: edges block không có pixelation, dù ở mọi resolution

---

## Bảng tóm tắt mức độ ưu tiên

| # | Điểm | Mức độ ảnh hưởng visual | Effort |
|---|---|---|---|
| 1 | Top face sáng + ngả trắng mạnh hơn | ⭐⭐⭐⭐ | Thấp (formula) |
| 2 | Side face tối rõ rệt | ⭐⭐⭐ | Thấp (formula) |
| 3 | Gradient trong từng face | ⭐⭐⭐⭐⭐ | Trung (helper mới) |
| 4 | Rim light 3 edges | ⭐⭐⭐⭐⭐ | Trung (3 cv2.line + tính color) |
| 5 | Glow halo mạnh hơn + multi-pass | ⭐⭐⭐⭐ | Trung (multi-pass blur) |
| 6 | Fist icon to + outline đậm | ⭐⭐⭐ | Thấp (config thay đổi) |
| 7 | Verify corner rounding | ⭐ | Thấp (verification only) |
| 8 | AA edges | ⭐⭐ | Thấp (đã có, verify) |

---

## Thứ tự thực hiện đề xuất

### Phase 1 — Quick wins (15-30 phút)
- Điểm 1: Top brightness factor `1.20 + 0.20`
- Điểm 2: Verify side factor `0.55`
- Điểm 6: Tăng icon size + outline
- Điểm 7: Verify corner radius

→ Đem lại ~50% gap to mẫu.

### Phase 2 — Major lift (1-2 giờ)
- Điểm 3: Gradient mỗi face (helper mới `_fill_face_with_gradient`)
- Điểm 4: Rim light 3 edges (3 cv2.line calls với color tính từ base × 1.45)

→ Đem lại ~85% gap to mẫu.

### Phase 3 — Polish (1 giờ)
- Điểm 5: Multi-pass glow halo (3 pass)
- Điểm 8: AA verification cho mọi face render

→ Đem lại ~95% gap to mẫu.

### Phase 4 — Final polish (optional)
- Migrate sang ModernGL theo `docs/moderngl-migration-punch-spec.md`
- Match 100% reference với GLSL shader (built-in gradient, rim, glow)

---

## Acceptance criteria — verify với mẫu

Khi render output match được 8 checkmark dưới đây:

```
✓ Top face SÁNG hơn front rõ rệt (~40%) + có ngả trắng
✓ Side face TỐI hơn front rõ rệt (~45%)
✓ Mỗi face có gradient subtle (chênh lệch ~15-20%)
✓ Edge top-front có rim light vàng-trắng nổi bật
✓ Edge top-side có rim light medium
✓ Edge front-side có rim subtle
✓ Glow halo lan ra ~30-50px với 3 layer
✓ Fist icon ~75-80% front face với outline ~3-4% size
```

→ Khi đủ 8 ✓ → block sẽ trông **gần như identical** với hình mẫu.

---

## Lưu ý quan trọng

1. **KHÔNG đụng** trajectory + yaw fix (đã có spec riêng `docs/block-positioning-fix-spec.md`)
2. **KHÔNG đụng** game logic (timing, hit detection, scoring)
3. **KHÔNG đụng** mesh/texture path (đó là path khác, chỉ áp default neon path)
4. **GIỮ** corner radius = 0.08 (subtle, không pill)
5. **GIỮ** fist icon vị trí trên FRONT face (không chuyển về top)

---

## Open questions

1. ~~**Gradient strength**: chênh lệch 15% hay 20% trong từng face?~~ → ✅ **CHỐT 15%** (subtle, không over-design)
2. **Rim light color**: dùng `base × 1.45 + (30,30,30)` hay `base × 1.30 + (60,60,60)` (ngả trắng nhiều hơn)?
3. ~~**Glow passes**: 3 pass đủ chưa hay cần 5 pass?~~ → ✅ **CHỐT 3 PASS**
4. ~~**Block xa (z_norm > 0.5)**: simplified để giữ performance?~~ → ✅ **FULL EFFECTS** (không simplified — block xa cũng phải có rim + gradient + glow đầy đủ)
5. **Phase 1 trước hay full Phase 2 luôn**: bạn muốn iterate từng phase hay làm 1 lần?

Bạn confirm phase nào muốn implement trước, tôi có thể giúp refine spec hoặc viết test acceptance chi tiết hơn.

---

# 🎯 CONFIRMED VALUES (chốt từ user)

## Gradient strength = **15%**

```
Front face: top edge 100% → bottom edge 85%   (chênh lệch 15%)
Top face:   front edge 100% → back edge 85%   (chênh lệch 15%)
Side face:  front edge 105% → back edge 90%   (chênh lệch 15%)
```

→ Subtle gradient, không phải graphic design. Đủ để cảm giác "có chiếu sáng" nhưng không over.

## Glow passes = **3**

```
Pass 1: kernel ~ block_size × 0.20, weight 0.65   (inner tight glow)
Pass 2: kernel ~ block_size × 0.50, weight 0.40   (mid spread)
Pass 3: kernel ~ block_size × 1.00, weight 0.20   (outer aura)
```

→ Multi-layer glow lan ra ~30-50px. Đủ neon feel without performance hit.

## Block xa = **FULL EFFECTS**

Apply **TOÀN BỘ** effects cho block ở mọi z_norm (kể cả z_norm > 0.5):
- ✅ Rim light 3 edges
- ✅ Gradient 3 face
- ✅ Multi-pass glow halo
- ✅ Fist icon (full hoặc simplified theo size threshold)

**Lý do**: visual consistency — block xa cũng phải neon, không break aesthetic.

**Performance note**: Block xa silhouette nhỏ → ROI glow cũng nhỏ → cost thấp. Không cần simplify.

## Recommend defaults cho 2 câu chưa chốt

### Q2 — Rim light color (recommend)
**Đề xuất**: `base × 1.45 + (30, 30, 30)` (subtle white shift)

Lý do:
- Hình mẫu rim light có ngả trắng NHẸ, không phải trắng đậm
- `× 1.45 + 30` → giữ saturation màu base, chỉ brighten + slight white
- Vd block vàng `(50, 180, 255)` → rim `(102, 291→255, 400→255)` ≈ `(102, 255, 255)` (vàng-trắng)

### Q5 — Phase order (recommend)
**Đề xuất**: Làm SEQUENTIAL từng Phase, render thử sau mỗi phase.

Workflow:
1. **Phase 1** (15-30 phút) → render preview → so sánh mẫu → confirm 50% gap closed
2. **Phase 2** (1-2 giờ) → render → confirm 85% gap
3. **Phase 3** (1 giờ) → render → confirm 95% gap
4. (Optional) Phase 4 ModernGL nếu muốn 100%

Lý do:
- Mỗi phase isolated → easy to debug nếu visual không match
- Tránh "làm hết 1 lần thấy xấu không biết phase nào lỗi"
- Có thể commit từng phase riêng

→ Bạn confirm Q2 (rim color) và Q5 (phase order), tôi finalize spec.

---

# 📋 SUMMARY — confirmed spec sau answers

| Setting | Value | Status |
|---|---|---|
| Top brightness factor | `c × 1.20 + 255 × 0.20` | ✅ Confirmed |
| Side brightness factor | `c × 0.55` | ✅ Confirmed |
| Front brightness | `c` (canonical) | ✅ Confirmed |
| Gradient strength | **15%** chênh lệch | ✅ Confirmed |
| Rim light color | `base × 1.45 + (30,30,30)` | ⏳ Recommended |
| Rim thickness | `2-3px` (top-front), `1-2px` (others) | ✅ Confirmed |
| Glow passes | **3 layers** (0.20/0.50/1.00 × block_size) | ✅ Confirmed |
| Glow weights | `(0.65, 0.40, 0.20)` | ✅ Confirmed |
| Block xa effects | **FULL** (no simplification) | ✅ Confirmed |
| Fist icon size | `front_w × 0.75-0.80` | ✅ Confirmed |
| Fist icon outline | `~3-4% icon size` | ✅ Confirmed |
| Corner radius | `0.08` (giữ nguyên) | ✅ Confirmed |
| AA | `LINE_AA` everywhere | ✅ Confirmed |
| Phase order | Sequential 1→2→3 (recommend) | ⏳ Recommended |

→ Khi bạn confirm Q2 + Q5, spec ready để implement bất kỳ phase nào.
