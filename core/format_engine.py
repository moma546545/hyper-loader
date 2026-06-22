"""
Backward-compat shim.

Use `core.media_engine.format_decision_engine.FormatDecisionEngine` instead.
"""

from .media_engine import FormatDecisionEngine

__all__ = ["FormatDecisionEngine"]
