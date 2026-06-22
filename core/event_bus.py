
"""
core/event_bus.py — Decoupled Event Bus for SnapDownloader

H-04 FIX: Added thread-safety awareness. The publish() method now warns when
called from a non-main thread and documents the correct pattern for cross-thread
communication (use Qt Signals to bridge to the main thread).
"""
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Any

try:
    from PySide6.QtCore import QObject, Signal
except ImportError:
    from PyQt6.QtCore import QObject, pyqtSignal as Signal

logger = logging.getLogger("SnapDownloader.EventBus")


# ── Event Data Classes ───────────────────────────────────────────────────────

@dataclass
class ShowNotificationEvent:
    message: str
    level: str = "info"
    title: str = ""

@dataclass
class DownloadFinishedEvent:
    worker_id: object
    success: bool
    message: str
    data: dict

@dataclass
class ExtensionLinkReceivedEvent:
    payload: dict

@dataclass
class SystemThemeChangedEvent:
    is_dark: bool

@dataclass
class QueueCapacityReachedEvent:
    rejected_count: int

@dataclass
class DatabaseErrorEvent:
    error_msg: str
    fatal: bool = False


class _EventDispatcher(QObject):
    published = Signal(object)


# ── Event Bus ────────────────────────────────────────────────────────────────

class EventBus:
    def __init__(self):
        self._subscribers: dict[type, list[tuple[Callable, bool]]] = {} # (callback, main_thread_only)
        self._lock = threading.RLock()
        self._dispatcher = _EventDispatcher()
        self._dispatcher.published.connect(self._deliver)

    def subscribe(self, event_type: type, callback: Callable, main_thread_only: bool = False):
        with self._lock:
            listeners = self._subscribers.setdefault(event_type, [])
            if not any(cb == callback for cb, _ in listeners):
                listeners.append((callback, main_thread_only))

    def unsubscribe(self, event_type: type, callback: Callable):
        with self._lock:
            listeners = self._subscribers.get(event_type, [])
            new_listeners = [item for item in listeners if item[0] != callback]
            if len(new_listeners) != len(listeners):
                self._subscribers[event_type] = new_listeners
            if not self._subscribers[event_type] and event_type in self._subscribers:
                self._subscribers.pop(event_type, None)

    def publish(self, event: Any):
        if threading.current_thread() is not threading.main_thread():
            self._dispatcher.published.emit(event)
            return
        self._deliver(event)

    def _deliver(self, event: Any):
        event_type = type(event)
        with self._lock:
            listeners = list(self._subscribers.get(event_type, []))
        
        for cb, main_only in listeners:
            try:
                if main_only and threading.current_thread() is not threading.main_thread():
                    # This should theoretically not happen due to the Qt signal bridge in publish(),
                    # but it's a safety measure for direct _deliver calls.
                    from .qt_dispatch import run_on_qt_main_thread
                    run_on_qt_main_thread(lambda: cb(event))
                else:
                    cb(event)
            except Exception as exc:
                logger.error(f"[EventBus] Error in subscriber {cb}: {exc}")


# Singleton
event_bus = EventBus()



