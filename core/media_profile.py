"""
Backward-compat shim.

Use `core.media_engine.media_profile.MediaProfile` instead.
"""

from .media_engine import MediaProfile

__all__ = ["MediaProfile"]
