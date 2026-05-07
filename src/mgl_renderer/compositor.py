"""Alpha composite GL output onto cv2 canvas."""

from __future__ import annotations

import cv2
import numpy as np


def composite_alpha(
    cv2_canvas: np.ndarray,
    gl_canvas: np.ndarray,
    gl_alpha: np.ndarray,
) -> np.ndarray:
    """Alpha composite gl_canvas over cv2_canvas using gl_alpha mask.

    All inputs (H, W, 3) BGR uint8 for canvases, (H, W) uint8 for alpha.
    Returns composited canvas (modifies in-place for speed).
    """
    mask = gl_alpha > 0
    if not mask.any():
        return cv2_canvas
    a = gl_alpha.astype(np.float32) / 255.0
    a3 = a[:, :, np.newaxis]
    roi_bg = cv2_canvas.astype(np.float32)
    roi_fg = gl_canvas.astype(np.float32)
    cv2_canvas[:] = np.clip(
        roi_bg * (1.0 - a3) + roi_fg * a3, 0, 255
    ).astype(np.uint8)
    return cv2_canvas


def composite_alpha_with_halo(
    cv2_canvas: np.ndarray,
    gl_canvas: np.ndarray,
    gl_alpha: np.ndarray,
    *,
    halo_strength: float = 2.0,
) -> np.ndarray:
    """Composite GL render with a soft neon halo behind the blocks.

    Pipeline (per `.cursor/rules/neon-glow-effect.mdc`):
    1. Halo source = alpha mask × block colour (separated from content).
    2. Downsample to a fixed working resolution (max dim ≈ 256px) so the
       blur stage has constant cost regardless of canvas resolution. The
       halo is low-frequency, so this is perceptually identical to full-res.
    3. Two-pass Gaussian blur with 51×51 kernel (covers ~20% of working
       width — equivalent to a wide multi-pass at full res).
    4. Upscale back to canvas resolution.
    5. Additive blend behind the crisp content composite.

    Skips the halo work entirely when the alpha mask is empty (cheap path
    for frames with no blocks).
    """
    mask = gl_alpha > 0
    if not mask.any():
        return cv2_canvas

    H, W = cv2_canvas.shape[:2]
    target = 256
    scale = max(1, max(W, H) // target)
    sw = max(1, W // scale)
    sh = max(1, H // scale)

    small_bgr = cv2.resize(gl_canvas, (sw, sh), interpolation=cv2.INTER_AREA)
    small_alpha = cv2.resize(gl_alpha, (sw, sh), interpolation=cv2.INTER_AREA)
    small = small_bgr.astype(np.float32) * (
        small_alpha[:, :, np.newaxis].astype(np.float32) / 255.0)

    halo_small = cv2.GaussianBlur(small, (51, 51), 0)
    halo_small = cv2.GaussianBlur(halo_small, (51, 51), 0)
    halo_small *= halo_strength
    halo_small_u8 = np.clip(halo_small, 0, 255).astype(np.uint8)
    halo_u8 = cv2.resize(halo_small_u8, (W, H), interpolation=cv2.INTER_LINEAR)

    cv2.add(cv2_canvas, halo_u8, dst=cv2_canvas)
    composite_alpha(cv2_canvas, gl_canvas, gl_alpha)
    return cv2_canvas
