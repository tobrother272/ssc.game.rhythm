"""ModernGL renderer for batched PunchTarget cubes.

Renders all alive punch blocks in a single GL pass with instancing.
Output: (BGR canvas, alpha mask) for compositing onto cv2 canvas.
"""

from __future__ import annotations
from pathlib import Path
from typing import List, Tuple

import numpy as np

from .context import MGLContext
from .geometry import generate_cube_with_face_ids, generate_fist_icon_texture


class PunchBlockInstance:
    """Per-block instance data."""
    __slots__ = ('position', 'scale', 'color', 'z_norm', 'yaw')

    def __init__(self, position: tuple, scale: tuple, color: tuple, z_norm: float, yaw: float = 0.0):
        self.position = position   # (x, y, z) world
        self.scale = scale         # (half_w, half_h, half_d) per-axis
        self.color = color         # (r, g, b) 0..1 floats
        self.z_norm = z_norm       # 0..1
        self.yaw = yaw             # rotation around Y axis (radians)


class MGLPunchRenderer:
    """Batched instanced renderer for PunchTarget cubes."""

    _instance = None

    def __init__(self):
        self.mgl = MGLContext.get()
        ctx = self.mgl.ctx

        # Load shaders
        shader_dir = self._find_shader_dir()
        vert_src = (shader_dir / "punch_block.vert").read_text(encoding="utf-8")
        frag_src = (shader_dir / "punch_block.frag").read_text(encoding="utf-8")
        self.prog = ctx.program(vertex_shader=vert_src, fragment_shader=frag_src)

        # Cube geometry
        verts, normals, uvs, face_ids, indices = generate_cube_with_face_ids()

        self.vbo_verts = ctx.buffer(verts.tobytes())
        self.vbo_normals = ctx.buffer(normals.tobytes())
        self.vbo_uvs = ctx.buffer(uvs.tobytes())
        self.vbo_face_ids = ctx.buffer(face_ids.tobytes())
        self.ibo = ctx.buffer(indices.tobytes())
        self.n_indices = len(indices)

        # Instance buffer: pos(3f) + scale(3f) + color(3f) + z_norm(1f) + yaw(1f) = 11 floats = 44 bytes
        self.INSTANCE_FLOATS = 11
        self.INSTANCE_BYTES = self.INSTANCE_FLOATS * 4
        self.MAX_INSTANCES = 64
        self.vbo_instance = ctx.buffer(reserve=self.MAX_INSTANCES * self.INSTANCE_BYTES)

        # VAO
        self.vao = ctx.vertex_array(
            self.prog,
            [
                (self.vbo_verts, '3f', 'in_position'),
                (self.vbo_normals, '3f', 'in_normal'),
                (self.vbo_uvs, '2f', 'in_uv'),
                (self.vbo_face_ids, '1f', 'in_face_id'),
                (self.vbo_instance, '3f 3f 3f 1f 1f /i',
                 'in_inst_pos', 'in_inst_scale', 'in_inst_color', 'in_inst_z_norm', 'in_inst_yaw'),
            ],
            self.ibo,
        )

        # Icon texture (RGBA)
        icon_data = generate_fist_icon_texture(256)
        self.tex_icon = ctx.texture((256, 256), 4, icon_data.tobytes())
        self.tex_icon.filter = (ctx.LINEAR, ctx.LINEAR)
        self.tex_icon.use(location=0)
        self.prog['u_icon_tex'] = 0

        # Default uniforms
        self.prog['u_corner_radius'] = 0.22
        self.prog['u_depth_extrude'] = 0.0
        self.prog['u_camera_pos'] = (0.0, 0.0, 0.0)

    @staticmethod
    def _find_shader_dir() -> Path:
        """Find shader directory (works in dev and frozen builds)."""
        import sys
        if getattr(sys, 'frozen', False):
            base = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
            candidates = [
                base / 'mgl_renderer' / 'shaders',
                base / 'src' / 'mgl_renderer' / 'shaders',
                Path(sys.executable).parent / 'mgl_renderer' / 'shaders',
            ]
            for c in candidates:
                if c.exists():
                    return c
        return Path(__file__).parent / 'shaders'

    @classmethod
    def get(cls) -> "MGLPunchRenderer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        """Force re-creation on next get() (e.g. after shader edit)."""
        cls._instance = None

    def render(
        self,
        blocks: List[PunchBlockInstance],
        view_proj_matrix: np.ndarray,
        camera_pos: tuple,
        width: int,
        height: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Render all blocks -> (BGR canvas H*W*3, alpha H*W) uint8."""
        if not blocks:
            return (np.zeros((height, width, 3), dtype=np.uint8),
                    np.zeros((height, width), dtype=np.uint8))

        n = len(blocks)
        if n > self.MAX_INSTANCES:
            self.MAX_INSTANCES = max(self.MAX_INSTANCES * 2, n)
            self.vbo_instance.orphan(self.MAX_INSTANCES * self.INSTANCE_BYTES)

        # Pack instance data: [pos(3) + scale(3) + color(3) + z_norm(1) + yaw(1)]
        data = np.empty(n * self.INSTANCE_FLOATS, dtype=np.float32)
        for i, b in enumerate(blocks):
            off = i * self.INSTANCE_FLOATS
            data[off] = b.position[0]
            data[off + 1] = b.position[1]
            data[off + 2] = b.position[2]
            data[off + 3] = b.scale[0]
            data[off + 4] = b.scale[1]
            data[off + 5] = b.scale[2]
            data[off + 6] = b.color[0]
            data[off + 7] = b.color[1]
            data[off + 8] = b.color[2]
            data[off + 9] = b.z_norm
            data[off + 10] = b.yaw
        self.vbo_instance.write(data.tobytes())

        # Framebuffer
        fbo, fbo_resolved, _, _ = self.mgl.get_fbo(width, height, samples=8)

        # Uniforms
        vp = view_proj_matrix.astype(np.float32)
        self.prog['u_view_proj'].write(vp.tobytes())
        self.prog['u_camera_pos'] = camera_pos

        # Render
        fbo.use()
        fbo.clear(0.0, 0.0, 0.0, 0.0)
        self.mgl.ctx.enable(self.mgl.ctx.DEPTH_TEST)
        self.vao.render(instances=n)

        # Resolve MSAA
        self.mgl.ctx.copy_framebuffer(fbo_resolved, fbo)

        # Readback RGBA
        raw = fbo_resolved.read(components=4)
        rgba = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)
        rgba = np.flipud(rgba).copy()

        # RGBA -> BGR + alpha
        bgr = rgba[:, :, [2, 1, 0]].copy()
        alpha = rgba[:, :, 3].copy()

        return bgr, alpha
