
"""
core/antigravity_engine.py — PROJECT: ANTIGRAVITY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Dynamic bandwidth unthrottling engine for HyperLoader.

Responsibilities
────────────────
1. **Adaptive Chunk Sizing** — Monitors real-time throughput and dynamically
   expands download chunks when speed is healthy, shrinks them when the server
   starts throttling (detects sustained speed drops or stalls).

2. **Throttle Detection & Recovery** — Identifies streaming-platform throttling
   patterns (sudden ≥40 % speed drop, or zero-byte stalls >N seconds) and issues
   recovery actions:
     - Fragmented request ranges (Range: bytes=X-Y)
     - Recommended yt-dlp ``--http-chunk-size`` injection
     - Back-pressure delay jitter to appear more organic

3. **Easter Egg — "import antigravity"** — When the developer flag
   ``VIDDOWNLOADER_ANTIGRAVITY=1`` is set **or** the string ``"antigravity"``
   is passed to :meth:`trigger_easter_egg`, the engine launches
   `xkcd.com/353 <https://xkcd.com/353/>`_ (the classic Python comic) in the
   default system browser via ``webbrowser.open`` — completely non-blocking,
   runs in a daemon thread so it never stalls the Qt event loop.

   In a full PyQt session, if a ``QWidget`` parent is provided, the engine
   also schedules a brief on-screen overlay animation instead of / in addition
   to the browser launch.

Integration
───────────
Import and use the singleton::

    from core.antigravity_engine import antigravity_engine

    # In your download speed monitor:
    chunk_size = antigravity_engine.next_chunk_size(current_speed_bps)
    yt_dlp_flags = antigravity_engine.get_unthrottle_flags()

    # Easter Egg (from URL-bar or dev console):
    if url_bar_text.strip().lower() == "antigravity":
        antigravity_engine.trigger_easter_egg()
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
import webbrowser
from collections import deque
from typing import Callable

logger = logging.getLogger("SnapDownloader.Antigravity")

# ─── Constants ────────────────────────────────────────────────────────────────

_XKCD_ANTIGRAVITY_URL = "https://xkcd.com/353/"
_EASTER_EGG_ENV_FLAG = "VIDDOWNLOADER_ANTIGRAVITY"

# Chunk sizing (bytes)
_CHUNK_MIN = 64 * 1024          # 64 KB  — floor; never request less
_CHUNK_DEFAULT = 256 * 1024     # 256 KB — warm-start
_CHUNK_MAX = 8 * 1024 * 1024    # 8 MB   — ceiling; above this aria2 wins anyway

# Throttle detection
_SPEED_HISTORY_WINDOW = 8           # number of samples to keep
_SPEED_HISTORY_MIN_SAMPLES = 3      # need at least this many before deciding
_THROTTLE_DROP_RATIO = 0.55         # if latest speed < 55 % of rolling peak → throttled
_STALL_TIMEOUT_SECONDS = 6.0        # zero-byte or near-zero stall threshold
_NEAR_ZERO_BPS = 4 * 1024           # 4 KB/s treated as "stall"

# Recovery
_RECOVERY_BACKOFF_MAX = 6           # max consecutive throttle escalations
_FRAGMENT_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB HTTP Range fragments when throttled
_RECOVERY_JITTER_SECONDS = (0.05, 0.25)  # sleep range injected between recovery requests

# ─── Main engine ──────────────────────────────────────────────────────────────


class AntigravityEngine:
    """
    Elite download unthrottling and speed optimization engine.

    Thread-safe. All public methods may be called from any thread.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

        # Chunk sizing
        self._chunk_size: int = _CHUNK_DEFAULT

        # Speed telemetry (ring buffer of (timestamp, speed_bps) tuples)
        self._speed_history: deque[tuple[float, float]] = deque(maxlen=_SPEED_HISTORY_WINDOW)
        self._peak_speed_bps: float = 0.0
        self._last_byte_time: float = time.monotonic()
        self._throttle_count: int = 0         # consecutive throttle detections
        self._in_recovery: bool = False       # True while a recovery action is active

        # Easter egg state
        self._easter_egg_launched: bool = False
        self._easter_egg_lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    def record_speed(self, speed_bps: float) -> None:
        """Feed a live speed measurement (bytes/second) into the engine."""
        now = time.monotonic()
        with self._lock:
            safe_speed = max(0.0, float(speed_bps or 0.0))
            self._speed_history.append((now, safe_speed))
            if safe_speed > 0:
                self._last_byte_time = now
            if safe_speed > self._peak_speed_bps:
                self._peak_speed_bps = safe_speed

    def next_chunk_size(self, current_speed_bps: float | None = None) -> int:
        """
        Return the recommended next chunk size in bytes.

        The engine grows chunks when speed is healthy (to reduce request
        overhead) and shrinks them when throttling is detected (to stay
        below per-request rate ceilings enforced by some CDNs).
        """
        with self._lock:
            speed = max(0.0, float(current_speed_bps or 0.0))
            if speed > 0:
                self.record_speed(speed)

            throttled = self._is_throttled_unsafe()
            stalled = self._is_stalled_unsafe()

            if stalled or (throttled and self._throttle_count >= 2):
                # Aggressive shrink to slip under per-request bandwidth caps
                new_size = max(_CHUNK_MIN, self._chunk_size // 2)
                logger.debug(
                    "[Antigravity] Shrink chunk %s→%s (stall=%s throttle_count=%s)",
                    _fmt_bytes(self._chunk_size),
                    _fmt_bytes(new_size),
                    stalled,
                    self._throttle_count,
                )
                self._chunk_size = new_size
            elif throttled:
                # Mild throttle: keep current chunk, let recovery handle it
                pass
            else:
                # Healthy: gradually grow towards ceiling
                growth = min(
                    int(self._chunk_size * 0.25),  # +25% per step
                    _CHUNK_MAX - self._chunk_size,
                )
                if growth > 0:
                    self._chunk_size = min(_CHUNK_MAX, self._chunk_size + growth)

            return int(self._chunk_size)

    def detect_throttle(self, speed_bps: float) -> bool:
        """
        Returns True if throttling is detected based on ``speed_bps``.

        Callers should invoke :meth:`record_speed` before calling this, or
        pass the speed here and it will be recorded automatically.
        """
        with self._lock:
            self.record_speed(speed_bps)
            return self._is_throttled_unsafe()

    def get_recovery_jitter_seconds(self) -> float:
        """
        Returns a random jitter delay (seconds) that should be inserted
        between requests during a throttle recovery sequence.
        Appears more organic to CDN traffic analyzers.
        """
        low, high = _RECOVERY_JITTER_SECONDS
        return round(random.uniform(low, high), 3)

    def get_unthrottle_flags(self) -> list[str]:
        """
        Returns yt-dlp command-line flags to help defeat throttling.

        Injects ``--http-chunk-size`` sized to the current adaptive chunk.
        When in active recovery, also returns a Range-based fragment strategy.
        """
        with self._lock:
            throttled = self._is_throttled_unsafe()
            stalled = self._is_stalled_unsafe()
            chunk = int(self._chunk_size)
            in_recovery = bool(self._in_recovery)

        flags: list[str] = ["--http-chunk-size", str(chunk)]

        if throttled or stalled:
            # Enable concurrent fragment fetching to bypass per-stream limits
            flags += ["--concurrent-fragments", "4"]
            logger.info(
                "[Antigravity] Injecting unthrottle flags: chunk=%s concurrent=4",
                _fmt_bytes(chunk),
            )

        if in_recovery and (throttled or stalled):
            flags += ["--retries", "5", "--retry-sleep", "1"]

        return flags

    def on_throttle_detected(self, on_recovery_action: Callable[[], None] | None = None) -> None:
        """
        Call when throttling is confirmed by the download layer.

        Escalates the recovery state and optionally calls ``on_recovery_action``
        (e.g., to inject new yt-dlp flags into a running worker).
        """
        with self._lock:
            self._throttle_count = min(_RECOVERY_BACKOFF_MAX, self._throttle_count + 1)
            self._in_recovery = True
            count = self._throttle_count

        logger.warning(
            "[Antigravity] Throttle confirmed (escalation level %s/%s). Initiating recovery.",
            count,
            _RECOVERY_BACKOFF_MAX,
        )
        if callable(on_recovery_action):
            try:
                on_recovery_action()
            except Exception as exc:
                logger.debug("[Antigravity] Recovery action error: %s", exc)

    def on_speed_healthy(self) -> None:
        """
        Call periodically when speed is confirmed healthy.
        Gradually de-escalates recovery mode.
        """
        with self._lock:
            if self._throttle_count > 0:
                self._throttle_count -= 1
            if self._throttle_count == 0:
                self._in_recovery = False

    def get_fragment_range(self, offset: int, total_size: int) -> tuple[int, int]:
        """
        Returns an HTTP Range (start, end) for the next fragment to fetch
        when using fragmented requests as a throttle-evasion technique.

        Fragment size is dynamically sized to the current chunk ceiling.
        Returns (offset, end_inclusive).
        """
        with self._lock:
            frag_size = int(self._chunk_size)
        end = min(offset + frag_size - 1, max(0, total_size - 1))
        return offset, end

    def reset(self) -> None:
        """Reset all telemetry. Call between separate download tasks."""
        with self._lock:
            self._chunk_size = _CHUNK_DEFAULT
            self._speed_history.clear()
            self._peak_speed_bps = 0.0
            self._last_byte_time = time.monotonic()
            self._throttle_count = 0
            self._in_recovery = False

    # ── Easter Egg ─────────────────────────────────────────────────────────────

    def trigger_easter_egg(
        self,
        qt_parent=None,
        *,
        force: bool = False,
    ) -> bool:
        """
        Launch the Antigravity Easter Egg — the classic Python xkcd #353 comic.

        Behaviour
        ─────────
        - Opens ``https://xkcd.com/353/`` in the default system browser via
          ``webbrowser.open`` in a daemon thread (never blocks the event loop).
        - If ``qt_parent`` is a ``QWidget``, schedules a brief floating overlay
          animation inside the PyQt window using ``QTimer.singleShot``.
        - Returns ``True`` if launched, ``False`` if already triggered (deduplicated
          unless ``force=True``).

        The Easter Egg is also auto-triggered on import when the environment
        variable ``VIDDOWNLOADER_ANTIGRAVITY=1`` is set — faithful to
        ``import antigravity`` behaviour.
        """
        with self._easter_egg_lock:
            if self._easter_egg_launched and not force:
                logger.debug("[Antigravity] Easter egg already launched; skipping.")
                return False
            self._easter_egg_launched = True

        logger.info("[Antigravity] 🚀 Easter egg triggered — launching xkcd #353!")

        def _open_browser() -> None:
            try:
                webbrowser.open(_XKCD_ANTIGRAVITY_URL, new=2, autoraise=True)
                logger.info("[Antigravity] Browser opened: %s", _XKCD_ANTIGRAVITY_URL)
            except Exception as exc:
                logger.warning("[Antigravity] Could not open browser: %s", exc)

        t = threading.Thread(target=_open_browser, daemon=True, name="Antigravity-EasterEgg")
        t.start()

        # Optional: schedule a Qt overlay animation if a parent widget is provided
        if qt_parent is not None:
            self._schedule_qt_animation(qt_parent)

        return True

    def _schedule_qt_animation(self, parent) -> None:
        """
        Schedule a non-blocking floating text overlay on the PyQt window.
        Uses QTimer.singleShot so it is dispatched on the GUI thread safely.
        """
        try:
            try:
                from PySide6.QtCore import QTimer
                from PySide6.QtWidgets import QLabel
                from PySide6.QtCore import Qt as QtCore_Qt
            except ImportError:
                from PyQt6.QtCore import QTimer
                from PyQt6.QtWidgets import QLabel
                from PyQt6.QtCore import Qt as QtCore_Qt

            def _show_overlay() -> None:
                try:
                    label = QLabel(
                        "🚀 import antigravity\n\"That's it. I just up and left. No planning, nothing.\"\n— xkcd #353",
                        parent,
                    )
                    label.setStyleSheet(
                        "QLabel {"
                        "  color: #e8f4ff;"
                        "  background: rgba(10, 20, 50, 0.88);"
                        "  border: 1px solid rgba(100, 180, 255, 0.6);"
                        "  border-radius: 12px;"
                        "  font-size: 14px;"
                        "  font-family: 'Consolas', 'Fira Code', monospace;"
                        "  padding: 18px 24px;"
                        "}"
                    )
                    label.setWindowFlags(
                        QtCore_Qt.WindowType.FramelessWindowHint
                        | QtCore_Qt.WindowType.SubWindow
                    )
                    label.adjustSize()
                    # Centre over parent
                    try:
                        pw = parent.width()
                        ph = parent.height()
                        lw = label.width()
                        lh = label.height()
                        label.move((pw - lw) // 2, (ph - lh) // 2)
                    except Exception:
                        pass
                    label.show()
                    label.raise_()
                    # Auto-dismiss after 4.5 seconds
                    QTimer.singleShot(4500, label.hide)
                    QTimer.singleShot(5000, label.deleteLater)
                except Exception as exc:
                    logger.debug("[Antigravity] Qt animation error: %s", exc)

            QTimer.singleShot(0, _show_overlay)
        except Exception as exc:
            logger.debug("[Antigravity] Could not schedule Qt animation: %s", exc)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _is_throttled_unsafe(self) -> bool:
        """Must be called with self._lock held."""
        history = list(self._speed_history)
        if len(history) < _SPEED_HISTORY_MIN_SAMPLES:
            return False
        peak = max((s for _, s in history), default=0.0)
        if peak <= 0:
            return False
        latest_speed = history[-1][1]
        # Throttle = latest sample is significantly below the window peak
        return latest_speed < (peak * _THROTTLE_DROP_RATIO)

    def _is_stalled_unsafe(self) -> bool:
        """Must be called with self._lock held."""
        elapsed_since_bytes = time.monotonic() - self._last_byte_time
        if elapsed_since_bytes < _STALL_TIMEOUT_SECONDS:
            return False
        # Also treat near-zero speed as stall
        if self._speed_history:
            latest_speed = self._speed_history[-1][1]
            return latest_speed < _NEAR_ZERO_BPS
        return elapsed_since_bytes >= _STALL_TIMEOUT_SECONDS

    def get_diagnostics(self) -> dict:
        """Return a snapshot of internal telemetry for debugging/logging."""
        with self._lock:
            history = list(self._speed_history)
            speeds = [s for _, s in history]
            return {
                "chunk_size_bytes": self._chunk_size,
                "chunk_size_human": _fmt_bytes(self._chunk_size),
                "peak_speed_bps": round(self._peak_speed_bps, 1),
                "peak_speed_human": _fmt_bytes(self._peak_speed_bps) + "/s",
                "latest_speed_bps": round(speeds[-1], 1) if speeds else 0.0,
                "speed_samples": len(speeds),
                "throttle_count": self._throttle_count,
                "in_recovery": self._in_recovery,
                "is_throttled": self._is_throttled_unsafe(),
                "is_stalled": self._is_stalled_unsafe(),
                "peak_chunk_bytes": _CHUNK_MAX,
                "min_chunk_bytes": _CHUNK_MIN,
            }


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _fmt_bytes(n: float) -> str:
    """Human-friendly byte size string."""
    n = max(0.0, float(n or 0))
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{int(n)} B"


# ─── Singleton ────────────────────────────────────────────────────────────────

antigravity_engine = AntigravityEngine()

# Auto-trigger Easter egg if env flag is set (honours `import antigravity` spirit)
if str(os.getenv(_EASTER_EGG_ENV_FLAG, "")).strip().lower() in {"1", "true", "yes", "on"}:
    antigravity_engine.trigger_easter_egg()
