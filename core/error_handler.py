
import logging
import os
import socket
import sys
import time
import threading
import functools
import traceback
from datetime import datetime

try:
    from PySide6.QtWidgets import QMessageBox
    from PySide6.QtCore import QCoreApplication, QThread
except ImportError:
    from PyQt6.QtWidgets import QMessageBox
    from PyQt6.QtCore import QCoreApplication, QThread
from .qt_dispatch import run_on_qt_main_thread
from .utils import get_app_data_dir

logger = logging.getLogger("ErrorHandler")


def is_timeout_exception(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    text = str(exc or "").strip().lower()
    if not text:
        return False
    timeout_tokens = ("timed out", "timeout", "time out", "deadline exceeded")
    return any(token in text for token in timeout_tokens)


def format_background_task_error(exc: BaseException | None, operation: str = "Background task") -> str:
    label = str(operation or "Background task").strip()
    if is_timeout_exception(exc):
        return f"{label} timed out"
    text = str(exc or "").strip()
    if text:
        return text
    return f"{label} failed"


def _ensure_crash_dir() -> str:
    base = get_app_data_dir()
    path = os.path.join(base, "crash_reports")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        logger.warning("Failed to create crash reports directory", exc_info=True)
    return path



_CRASH_REPORTS_MAX_COUNT = 50
_CRASH_REPORTS_MAX_AGE_DAYS = 30


def _cleanup_old_crash_reports(crash_dir: str) -> None:
    """SEC-06 FIX: Remove old crash reports to prevent unbounded accumulation."""
    try:
        files = sorted(
            [os.path.join(crash_dir, f) for f in os.listdir(crash_dir) if f.endswith(".log")],
            key=lambda p: os.path.getmtime(p),
        )
        # Remove files older than max age
        now = time.time()
        cutoff = now - (_CRASH_REPORTS_MAX_AGE_DAYS * 86400)
        for f in files:
            try:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
            except Exception:
                pass
        # Re-list after age-based cleanup and enforce max count
        files = sorted(
            [os.path.join(crash_dir, f) for f in os.listdir(crash_dir) if f.endswith(".log")],
            key=lambda p: os.path.getmtime(p),
        )
        while len(files) > _CRASH_REPORTS_MAX_COUNT:
            try:
                os.remove(files.pop(0))
            except Exception:
                break
    except Exception:
        pass


def _write_crash_report(exc_type, exc_value, exc_tb, thread_name: str | None = None) -> None:
    try:
        crash_dir = _ensure_crash_dir()
        _cleanup_old_crash_reports(crash_dir)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        tid = threading.get_ident()
        name = thread_name or threading.current_thread().name
        filename = f"crash-{ts}-pid{os.getpid()}-tid{tid}.log"
        path = os.path.join(crash_dir, filename)
        header = f"Timestamp: {ts}\n"
        header += f"Process ID: {os.getpid()}\n"
        header += f"Thread ID: {tid}\n"
        header += f"Thread Name: {name}\n"
        header += f"Python: {sys.version.splitlines()[0]}\n"
        header += f"Platform: {sys.platform}\n\n"
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        with open(path, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(tb_text)
        logger.error("Crash report written to %s", path)
    except Exception:
        logger.error("Failed to write crash report", exc_info=True)


def install_global_error_handlers() -> None:
    original_excepthook = sys.excepthook

    def _handle_main_thread(exc_type, exc_value, exc_tb):
        _write_crash_report(exc_type, exc_value, exc_tb, thread_name="MainThread")
        original_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _handle_main_thread

    if hasattr(threading, "excepthook"):
        original_thread_hook = threading.excepthook

        def _handle_thread(args):
            try:
                thread = getattr(args, "thread", None)
                name = thread.name if thread is not None else "Thread"
            except Exception:
                name = "Thread"
            _write_crash_report(args.exc_type, args.exc_value, args.exc_traceback, thread_name=name)
            original_thread_hook(args)

        threading.excepthook = _handle_thread


class ErrorHandler:
    @staticmethod
    def show_error(parent, title: str, message: str, exc: Exception = None):
        details = traceback.format_exc() if exc else ""
        def _show():
            if exc:
                msg = QMessageBox(parent)
                msg.setIcon(QMessageBox.Icon.Critical)
                msg.setWindowTitle(title)
                msg.setText(message)
                msg.setDetailedText(details)
                msg.exec()
                return
            try:
                from ui.toast import ToastManager
                ToastManager.show(parent, f"{title}: {message}", "error")
            except Exception:
                QMessageBox.critical(parent, title, message)

        if exc:
            logger.error(f"{title}: {message} - {exc}", exc_info=True)
        else:
            logger.error(f"{title}: {message}")
        if not run_on_qt_main_thread(_show):
            _show()

    @staticmethod
    def show_warning(parent, title: str, message: str):
        logger.warning(f"{title}: {message}")
        def _show():
            try:
                from ui.toast import ToastManager
                ToastManager.show(parent, f"{title}: {message}", "warning")
            except Exception:
                QMessageBox.warning(parent, title, message)
        if not run_on_qt_main_thread(_show):
            _show()

    @staticmethod
    def show_info(parent, title: str, message: str):
        logger.info(f"{title}: {message}")
        def _show():
            try:
                from ui.toast import ToastManager
                ToastManager.show(parent, f"{title}: {message}" if title else message, "success")
            except Exception:
                QMessageBox.information(parent, title, message)
        if not run_on_qt_main_thread(_show):
            _show()

    @staticmethod
    def confirm(parent, title: str, message: str) -> bool:
        logger.info(f"{title}: {message}")
        result = {"accepted": False}
        done = threading.Event()

        def _show():
            try:
                resp = QMessageBox.question(
                    parent,
                    title,
                    message,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                result["accepted"] = resp == QMessageBox.StandardButton.Yes
            finally:
                done.set()

        app = QCoreApplication.instance()
        is_ui_thread = bool(app is not None and QThread.currentThread() == app.thread())
        if is_ui_thread or not run_on_qt_main_thread(_show):
            _show()
            return bool(result["accepted"])
        # Wait for the queued UI callback to complete and return a stable answer.
        if not done.wait(timeout=15.0):
            logger.error("Confirmation dialog dispatch timed out.")
            return False
        return bool(result["accepted"])

def safe_execute(func=None, *, handled_exceptions=(), default_return=None, rethrow_unexpected=True):
    """
    Decorator for controller helpers.

    By default, unexpected exceptions are logged and re-raised so they are
    visible during development and still reach the global crash pipeline.
    Callers may opt-in to swallowing a narrow set of handled exceptions.
    """

    def decorator(target):
        @functools.wraps(target)
        def wrapper(*args, **kwargs):
            try:
                return target(*args, **kwargs)
            except handled_exceptions as exc:
                logger.warning("Handled exception in %s: %s", target.__name__, exc, exc_info=True)
                return default_return
            except Exception as exc:
                logger.exception("Unhandled exception in UI Controller %s: %s", target.__name__, exc)
                if rethrow_unexpected:
                    raise
                return default_return

        return wrapper

    if func is None:
        return decorator
    return decorator(func)



