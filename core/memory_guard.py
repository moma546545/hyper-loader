
"""
core/memory_guard.py — Memory Leak Guard & System Health Monitor
Monitors RAM usage and automatically cleans up resources if consumption is too high.
Non-blocking background watcher thread.

BUG-02 FIX: Callbacks are dispatched through a QObject living on the Qt main
thread so GUI updates are queued safely even when detection runs on a Python thread.
"""
import os
import gc
import sys
import time
import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger("SnapDownloader.MemGuard")
_qt_dispatcher_lock = threading.Lock()

try:
    from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
except ImportError:
    try:
        from PyQt6.QtCore import QCoreApplication, QObject, Qt, pyqtSignal as Signal
    except ImportError:
        QCoreApplication = None
        QObject = None
        Qt = None
        Signal = None


if QObject is not None and Signal is not None and Qt is not None:
    class _QtCallbackDispatcher(QObject):
        invoke = Signal(object, tuple)

        def __init__(self):
            super().__init__()
            self.invoke.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)

        def _dispatch(self, callback, args):
            try:
                callback(*tuple(args or ()))
            except Exception as exc:
                logger.error(f"[MemGuard] Callback error: {exc}")
else:
    _QtCallbackDispatcher = None

_qt_dispatcher: Optional[object] = None


def _get_process_memory_mb() -> float:
    """Return current process memory usage in MB."""
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        return proc.memory_info().rss / (1024 * 1024)
    except ImportError:
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)
                    ]
                p = PROCESS_MEMORY_COUNTERS()
                p.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                handle = ctypes.windll.kernel32.GetCurrentProcess()
                if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(p), p.cb):
                    return p.WorkingSetSize / (1024 * 1024)
            except Exception as exc:
                logger.debug(f"[MemGuard] Windows memory fallback failed: {exc}")
        else:
            # Fallback: read /proc/self/status on Linux
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            return int(line.split()[1]) / 1024
            except Exception as exc:
                logger.debug(f"[MemGuard] Fallback memory read failed: {exc}")
        return 0.0


def _get_system_memory_percent() -> float:
    """Return system-wide memory usage percentage."""
    try:
        import psutil
        return psutil.virtual_memory().percent
    except ImportError:
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", wintypes.DWORD),
                        ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_uint64),
                        ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64),
                        ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64),
                        ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64),
                    ]
                m = MEMORYSTATUSEX()
                m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
                return float(m.dwMemoryLoad)
            except Exception:
                pass
        return 0.0


def _safe_invoke_callback(callback: Optional[Callable], *args) -> None:
    """BUG-02 FIX: Safely invoke a callback on the main thread via Qt queued signal."""
    if callback is None:
        return
    dispatcher = _get_qt_dispatcher()
    if dispatcher is not None:
        try:
            dispatcher.invoke.emit(callback, tuple(args))
            return
        except Exception as exc:
            logger.debug(f"[MemGuard] Qt callback dispatch failed: {exc}")
    try:
        callback(*args)
    except Exception as exc:
        logger.error(f"[MemGuard] Callback error (fallback): {exc}")


def _get_qt_dispatcher():
    global _qt_dispatcher
    if _QtCallbackDispatcher is None or QCoreApplication is None:
        return None
    app = QCoreApplication.instance()
    if app is None:
        return None
    with _qt_dispatcher_lock:
        if _qt_dispatcher is None:
            _qt_dispatcher = _QtCallbackDispatcher()
        try:
            app_thread = app.thread()
            if _qt_dispatcher.thread() != app_thread:
                _qt_dispatcher.moveToThread(app_thread)
        except Exception as exc:
            logger.debug(f"[MemGuard] Failed to move dispatcher to app thread: {exc}")
        return _qt_dispatcher


class MemoryGuard:
    """
    Background thread that:
    1. Monitors process RAM usage every N seconds.
    2. Triggers garbage collection when usage exceeds warn_mb.
    3. Calls an alert callback when usage exceeds critical_mb.
    4. Reports memory stats on demand.
    """

    def __init__(
        self,
        warn_mb: float = 400.0,
        critical_mb: float = 800.0,
        check_interval_seconds: int = 30,
        on_warning: Optional[Callable[[float], None]] = None,
        on_critical: Optional[Callable[[float], None]] = None,
    ):
        self.warn_mb = warn_mb
        self.critical_mb = critical_mb
        self.check_interval = check_interval_seconds
        self.on_warning = on_warning
        self.on_critical = on_critical

        self._running_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._peak_mb = 0.0
        self._gc_count = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        if self._running_event.is_set():
            return
        self._running_event.set()
        self._thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="MemoryGuard"
        )
        self._thread.start()
        logger.info("[MemGuard] Started.")

    def stop(self):
        self._running_event.clear()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=min(max(int(self.check_interval), 1), 2))
        self._thread = None

    def get_stats(self) -> dict:
        current = _get_process_memory_mb()
        return {
            "current_mb": round(current, 1),
            "peak_mb": round(self._peak_mb, 1),
            "gc_runs": self._gc_count,
            "system_pct": round(_get_system_memory_percent(), 1),
            "warn_threshold_mb": self.warn_mb,
            "critical_threshold_mb": self.critical_mb,
        }

    def force_gc(self) -> float:
        """Force garbage collection. Returns freed MB estimate."""
        before = _get_process_memory_mb()
        after = self._collect_staged_gc(target_mb=max(self.warn_mb, self.critical_mb * 0.95))
        freed = max(0.0, before - after)
        logger.info(f"[MemGuard] GC freed ~{freed:.1f} MB")
        return freed

    def _collect_staged_gc(self, target_mb: float) -> float:
        """Run lighter GC generations first, then escalate only if needed."""
        gc.collect(0)
        self._gc_count += 1
        after = _get_process_memory_mb()
        if after > target_mb:
            gc.collect(1)
            self._gc_count += 1
            after = _get_process_memory_mb()
        if after > target_mb:
            gc.collect(2)
            self._gc_count += 1
            after = _get_process_memory_mb()
        return after

    def format_status(self) -> str:
        stats = self.get_stats()
        return (
            f"RAM: {stats['current_mb']} MB  "
            f"(Peak: {stats['peak_mb']} MB  System: {stats['system_pct']}%)"
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _watch_loop(self):
        while self._running_event.is_set():
            try:
                current_mb = _get_process_memory_mb()
                self._peak_mb = max(self._peak_mb, current_mb)

                if current_mb >= self.critical_mb:
                    logger.warning(f"[MemGuard] CRITICAL: {current_mb:.1f} MB — forcing GC")
                    self.force_gc()
                    # BUG-02 FIX: dispatch callback to main thread
                    _safe_invoke_callback(self.on_critical, current_mb)

                elif current_mb >= self.warn_mb:
                    logger.info(f"[MemGuard] High RAM: {current_mb:.1f} MB — collecting young gen")
                    gc.collect(0)
                    self._gc_count += 1
                    # BUG-02 FIX: dispatch callback to main thread
                    _safe_invoke_callback(self.on_warning, current_mb)

            except Exception as exc:
                logger.debug(f"[MemGuard] Check error: {exc}")

            for _ in range(self.check_interval):
                if not self._running_event.is_set():
                    return
                time.sleep(1)


# Singleton — can be started from app.py
memory_guard = MemoryGuard(warn_mb=400, critical_mb=800)
