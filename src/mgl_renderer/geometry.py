"""Cube geometry generator with face IDs and UVs for instanced rendering."""

import numpy as np
import cv2


def generate_cube_with_face_ids():
    """Generate cube vertices with per-face attributes.

    Returns:
        verts: (24, 3) float32
        normals: (24, 3) float32
        uvs: (24, 2) float32
        face_ids: (24,) float32  — 0=front, 1=back, 2=top, 3=bottom, 4=left, 5=right
        indices: (36,) uint32
    """
    H = 0.5
    faces = [
        # Front (z = -H, normal -Z, face_id 0)
        ([(-H, -H, -H), (H, -H, -H), (H, H, -H), (-H, H, -H)],
         (0, 0, -1), 0),
        # Back (z = +H, normal +Z, face_id 1)
        ([(H, -H, H), (-H, -H, H), (-H, H, H), (H, H, H)],
         (0, 0, 1), 1),
        # Top (y = -H, normal -Y, face_id 2)
        ([(-H, -H, H), (H, -H, H), (H, -H, -H), (-H, -H, -H)],
         (0, -1, 0), 2),
        # Bottom (y = +H, normal +Y, face_id 3)
        ([(-H, H, -H), (H, H, -H), (H, H, H), (-H, H, H)],
         (0, 1, 0), 3),
        # Left (x = -H, normal -X, face_id 4)
        ([(-H, -H, H), (-H, -H, -H), (-H, H, -H), (-H, H, H)],
         (-1, 0, 0), 4),
        # Right (x = +H, normal +X, face_id 5)
        ([(H, -H, -H), (H, -H, H), (H, H, H), (H, H, -H)],
         (1, 0, 0), 5),
    ]

    verts_list, normals_list, uvs_list, face_ids_list = [], [], [], []
    indices_list = []
    uv_corners = [(0, 0), (1, 0), (1, 1), (0, 1)]

    for fi, (corners, normal, face_id) in enumerate(faces):
        base = fi * 4
        for ci, c in enumerate(corners):
            verts_list.append(c)
            normals_list.append(normal)
            face_ids_list.append(float(face_id))
            uvs_list.append(uv_corners[ci])
        indices_list.extend([base, base + 1, base + 2,
                             base, base + 2, base + 3])

    return (
        np.array(verts_list, dtype=np.float32),
        np.array(normals_list, dtype=np.float32),
        np.array(uvs_list, dtype=np.float32),
        np.array(face_ids_list, dtype=np.float32),
        np.array(indices_list, dtype=np.uint32),
    )


def generate_fist_icon_texture(size: int = 256) -> np.ndarray:
    """Generate bold fist icon as RGBA texture for GL upload.

    Returns (size, size, 4) uint8 array in RGBA order.
    """
    img = np.zeros((size, size, 4), dtype=np.uint8)
    cx, cy = size // 2, size // 2
    s = int(size * 0.82)

    # Bold chunky fist - front-facing punch (like the reference)
    # Main fist body (rounded rectangle)
    body_w, body_h = 0.38, 0.30
    body_pts = [
        (-body_w, -0.05),
        (body_w, -0.05),
        (body_w, body_h),
        (body_w - 0.06, body_h + 0.08),
        (-body_w + 0.06, body_h + 0.08),
        (-body_w, body_h),
    ]

    # Knuckle bumps (4 fingers, chunky)
    k_y_base = -0.05
    k_y_top = -0.32
    k_xs = [-0.27, -0.09, 0.09, 0.27]
    k_hw = 0.085
    knuckle_pts = []
    for kx in k_xs:
        knuckle_pts.extend([
            (kx - k_hw, k_y_base),
            (kx - k_hw, k_y_top + 0.04),
            (kx - k_hw * 0.7, k_y_top),
            (kx + k_hw * 0.7, k_y_top),
            (kx + k_hw, k_y_top + 0.04),
            (kx + k_hw, k_y_base),
        ])

    # Thumb (left side, wrapping around)
    thumb_pts = [
        (-body_w - 0.02, 0.10),
        (-body_w - 0.10, 0.05),
        (-body_w - 0.13, -0.05),
        (-body_w - 0.10, -0.14),
        (-body_w - 0.02, -0.10),
        (-body_w, -0.05),
    ]

    # Combine all into one polygon (body outline + knuckles on top)
    all_pts = []
    all_pts.append((-body_w, body_h))
    all_pts.append((-body_w + 0.06, body_h + 0.08))
    all_pts.append((body_w - 0.06, body_h + 0.08))
    all_pts.append((body_w, body_h))
    all_pts.append((body_w, k_y_base))
    # Add knuckle tops right-to-left
    for kx in reversed(k_xs):
        all_pts.append((kx + k_hw, k_y_base))
        all_pts.append((kx + k_hw, k_y_top + 0.04))
        all_pts.append((kx + k_hw * 0.7, k_y_top))
        all_pts.append((kx - k_hw * 0.7, k_y_top))
        all_pts.append((kx - k_hw, k_y_top + 0.04))
        all_pts.append((kx - k_hw, k_y_base))
    all_pts.append((-body_w, k_y_base))

    abs_pts = np.array(
        [(int(cx + p[0] * s), int(cy + p[1] * s)) for p in all_pts],
        dtype=np.int32,
    )

    # Thumb (separate polygon)
    abs_thumb = np.array(
        [(int(cx + p[0] * s), int(cy + p[1] * s)) for p in thumb_pts],
        dtype=np.int32,
    )

    # Bold black outline behind silhouette (per gap-analysis spec điểm 6:
    # outline ~3-4% icon size for clear contrast). Drawn first as thick
    # polylines, then white fill on top covers the inside.
    outline_th = max(3, int(s * 0.045))
    cv2.polylines(img, [abs_pts], isClosed=True, color=(0, 0, 0, 255),
                  thickness=outline_th, lineType=cv2.LINE_AA)
    cv2.polylines(img, [abs_thumb], isClosed=True, color=(0, 0, 0, 255),
                  thickness=outline_th, lineType=cv2.LINE_AA)

    # White fill for main fist
    cv2.fillPoly(img, [abs_pts], (255, 255, 255, 255), lineType=cv2.LINE_AA)
    cv2.fillPoly(img, [abs_thumb], (255, 255, 255, 255), lineType=cv2.LINE_AA)

    # Finger grooves (dark lines between knuckles)
    groove_col = (0, 0, 0, 180)
    for kx in [-0.18, 0.0, 0.18]:
        x = int(cx + kx * s)
        y0 = int(cy + k_y_top * s + s * 0.06)
        y1 = int(cy + k_y_base * s - s * 0.02)
        cv2.line(img, (x, y0), (x, y1), groove_col,
                 max(2, int(s * 0.025)), lineType=cv2.LINE_AA)

    # Wrist line
    wrist_y = int(cy + 0.22 * s)
    cv2.line(img, (int(cx - body_w * s * 0.7), wrist_y),
             (int(cx + body_w * s * 0.7), wrist_y),
             (0, 0, 0, 120), max(2, int(s * 0.02)), lineType=cv2.LINE_AA)

    return img
