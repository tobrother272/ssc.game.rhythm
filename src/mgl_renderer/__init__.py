"""ModernGL render pipeline for selected 3D primitives.

Currently supports:
  - PunchTarget cube blocks (batched instanced rendering)
"""

from .punch_renderer import MGLPunchRenderer, PunchBlockInstance
from .compositor import composite_alpha, composite_alpha_with_halo

__all__ = [
    "MGLPunchRenderer",
    "PunchBlockInstance",
    "composite_alpha",
    "composite_alpha_with_halo",
]
