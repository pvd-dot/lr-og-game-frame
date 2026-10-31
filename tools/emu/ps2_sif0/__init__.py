"""Bounded source-qualified PCSX2 v2.8.2 DMA9/SIF0 launch model.

Call :func:`launch` with live IOP/EE memory, serialized SIF0 state and the
scheduler fields documented in the PS2 functions guide. This is a synchronous
software-path model, not PS2 hardware or a complete emulator.
"""

from .launch import launch

__all__ = ["launch"]
