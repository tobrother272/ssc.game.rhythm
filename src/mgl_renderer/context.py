"""Singleton ModernGL context manager.

Creates 1 standalone (headless) OpenGL context per process. Reused across
all renders to avoid setup overhead.
"""

from __future__ import annotations
import threading
from typing import Optional

import moderngl


class MGLContext:
    _instance: Optional["MGLContext"] = None
    _lock = threading.Lock()

    def __init__(self):
        self.ctx = moderngl.create_standalone_context(require=330)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA
        self._fbo_cache: dict = {}

    @classmethod
    def get(cls) -> "MGLContext":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def get_fbo(self, w: int, h: int, samples: int = 8):
        """Get or create a multisample framebuffer + resolve target."""
        key = (w, h, samples)
        if key not in self._fbo_cache:
            ctx = self.ctx
            # Try requested samples; fall back to 4 then 1 if unsupported
            for s in (samples, 4, 1):
                try:
                    if s > 1:
                        color = ctx.texture((w, h), 4, samples=s)
                        depth = ctx.depth_renderbuffer((w, h), samples=s)
                    else:
                        color = ctx.texture((w, h), 4)
                        depth = ctx.depth_renderbuffer((w, h))
                    fbo = ctx.framebuffer(
                        color_attachments=[color], depth_attachment=depth
                    )
                    break
                except Exception:
                    continue
            else:
                raise RuntimeError("Cannot create framebuffer")
            color_resolved = ctx.texture((w, h), 4)
            fbo_resolved = ctx.framebuffer(color_attachments=[color_resolved])
            self._fbo_cache[key] = (fbo, fbo_resolved, color, color_resolved, s)
        return self._fbo_cache[key][:4]

    def release(self):
        """Cleanup on shutdown."""
        for items in self._fbo_cache.values():
            fbo, fbo_r, c, cr = items[:4]
            fbo.release()
            fbo_r.release()
            c.release()
            cr.release()
        self._fbo_cache.clear()
        self.ctx.release()
        type(self)._instance = None
