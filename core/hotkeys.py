
"""
core/hotkeys.py — Global Hotkey System for SnapDownloader
Registers system-wide keyboard shortcuts that work even when the window is minimized.
Uses Python's keyboard library if available, with graceful fallback.
"""
import os
import threading
import logging
import weakref
from typing import Callable

logger = logging.getLogger("SnapDownloader.Hotkeys")

_KEYBOARD_AVAILABLE = False
try:
    import keyboard as _kb
    _KEYBOARD_AVAILABLE = True
except ImportError:
    logger.info("[Hotkeys] 'keyboard' package not installed — global hotkeys disabled.")


def _env_flag(name: str, default: bool = False) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    if not value:
        return bool(default)
    return value in {"1", "true", "yes", "on"}


class GlobalHotkeyManager:
    """
    Manages system-wide keyboard shortcuts.

    Usage:
        manager = GlobalHotkeyManager()
        manager.register("ctrl+shift+d", on_download_pressed)
        manager.start()
        # ...
        manager.stop()
    """

    def __init__(self):
        self._hotkeys: dict[str, Callable] = {}
        self._handles: list = []
        self._running = False
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        return _KEYBOARD_AVAILABLE

    def register(self, hotkey: str, callback: Callable, description: str = ""):
        """Register a hotkey combination."""
        with self._lock:
            self._hotkeys[hotkey] = (callback, description)
        logger.debug(f"[Hotkeys] Registered: {hotkey!r} → {description}")

    def unregister(self, hotkey: str):
        with self._lock:
            self._hotkeys.pop(hotkey, None)

    def start(self):
        if not _KEYBOARD_AVAILABLE or self._running:
            return
        self._running = True
        self._bind_all()
        logger.info("[Hotkeys] Global hotkeys active.")

    def stop(self):
        if not _KEYBOARD_AVAILABLE:
            return
        self._running = False
        try:
            _kb.unhook_all()
        except Exception as exc:
            logger.warning(f"[Hotkeys] Failed to unhook all hotkeys: {exc}")
        self._handles.clear()
        logger.info("[Hotkeys] Global hotkeys stopped.")

    def _bind_all(self):
        if not _KEYBOARD_AVAILABLE:
            return
        with self._lock:
            pairs = list(self._hotkeys.items())
        for combo, (cb, _) in pairs:
            try:
                handle = _kb.add_hotkey(combo, cb)
                self._handles.append(handle)
            except Exception as exc:
                logger.warning(f"[Hotkeys] Failed to bind {combo!r}: {exc}")

    def get_registered(self) -> list[dict]:
        """Return list of registered hotkeys with descriptions."""
        with self._lock:
            return [
                {"hotkey": k, "description": v[1]}
                for k, v in self._hotkeys.items()
            ]


# ── Default SnapDownloader Hotkeys Definition ─────────────────────────────────

def setup_default_hotkeys(window) -> GlobalHotkeyManager:
    """
    Register the default SnapDownloader hotkeys against a PremiumWindow instance.
    Returns the configured manager (not yet started — call .start() after).
    """
    mgr = GlobalHotkeyManager()

    if not mgr.is_available():
        return mgr
    try:
        window_ref = weakref.ref(window)
    except TypeError:
        # Some tests use plain object() instances; real Qt windows are weakref-able.
        window_ref = lambda: window

    # Paste & Analyze URL
    mgr.register(
        "ctrl+shift+v",
        lambda: _trigger(window_ref, "_paste_and_analyze"),
        "Paste URL and Analyze"
    )
    if _env_flag("VIDDOWNLOADER_ENABLE_GLOBAL_CTRL_V", default=False):
        mgr.register(
            "ctrl+v",
            lambda: _trigger(window_ref, "_paste_and_analyze"),
            "Paste URL and Analyze (Quick)"
        )
    else:
        logger.info(
            "[Hotkeys] Skipping global 'ctrl+v' to avoid intercepting paste outside the app. "
            "Set VIDDOWNLOADER_ENABLE_GLOBAL_CTRL_V=1 to restore the legacy behavior."
        )
    # Show/Hide main window
    mgr.register(
        "ctrl+shift+s",
        lambda: _trigger(window_ref, "_toggle_window_visibility"),
        "Show / Hide SnapDownloader"
    )
    # Quick Download from clipboard
    mgr.register(
        "ctrl+shift+d",
        lambda: _trigger(window_ref, "_hotkey_quick_download"),
        "Quick Download from Clipboard"
    )
    # Pause all downloads
    mgr.register(
        "ctrl+shift+p",
        lambda: _trigger(window_ref, "_pause_queue_download"),
        "Pause Queue"
    )

    return mgr


def _trigger(window_or_ref, method: str):
    """Safely invoke a method on the window from a background thread."""
    try:
        window = window_or_ref() if callable(window_or_ref) and not hasattr(window_or_ref, "metaObject") else window_or_ref
        if window is None:
            return
        fn = getattr(window, method, None)
        if callable(fn):
            # Schedule on Qt main thread
            try:
                from PySide6.QtCore import QMetaObject, Qt
                QMetaObject.invokeMethod(window, method, Qt.ConnectionType.QueuedConnection)
            except Exception as exc:
                logger.warning(f"[Hotkeys] Could not queue {method}; skipped unsafe direct call: {exc}")
    except Exception as exc:
        logger.debug(f"[Hotkeys] Trigger failed for {method}: {exc}")



