import atexit
import signal
import sys
import threading
import logging

logger = logging.getLogger("SystemKeepAlive")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

class SystemKeepAlive:
    _lock = threading.Lock()
    _active_count = 0

    @classmethod
    def prevent_sleep(cls):
        """Prevent the system from going to sleep while operations are running."""
        with cls._lock:
            cls._active_count += 1
            if cls._active_count == 1:
                if sys.platform == "win32":
                    try:
                        import ctypes
                        # Prevent system sleep and display sleep
                        ctypes.windll.kernel32.SetThreadExecutionState(
                            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                        )
                        logger.info("System sleep prevented (Anti-Sleep ON).")
                    except Exception as e:
                        logger.error(f"Failed to set Anti-Sleep on Windows: {e}")

    @classmethod
    def allow_sleep(cls, force=False):
        """Allow the system to resume normal sleep behavior."""
        with cls._lock:
            if force:
                cls._active_count = 0
            elif cls._active_count > 0:
                cls._active_count -= 1

            if cls._active_count == 0:
                if sys.platform == "win32":
                    try:
                        import ctypes
                        # Reset thread execution state to allow sleep
                        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                        logger.info("System sleep allowed (Anti-Sleep OFF).")
                    except Exception as e:
                        logger.error(f"Failed to remove Anti-Sleep on Windows: {e}")


# BUG-03 FIX: Register atexit handler to ensure sleep is always re-enabled
# even if the application is force-killed or crashes unexpectedly.
# atexit handlers run on normal interpreter shutdown & some signal-based exits.
def _atexit_cleanup():
    try:
        SystemKeepAlive.allow_sleep(force=True)
    except Exception:
        pass

atexit.register(_atexit_cleanup)


def _install_signal_handlers() -> None:
    handled = []
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous = signal.getsignal(sig)
        except Exception:
            previous = None

        def _handler(signum, frame, _prev=previous):
            try:
                SystemKeepAlive.allow_sleep(force=True)
            finally:
                if callable(_prev):
                    try:
                        _prev(signum, frame)
                    except Exception:
                        pass
                elif _prev in (signal.SIG_DFL, signal.SIG_IGN):
                    try:
                        signal.signal(signum, signal.SIG_DFL)
                    except Exception:
                        return
                    try:
                        os_kill = getattr(signal, "raise_signal", None)
                        if callable(os_kill):
                            os_kill(signum)
                    except Exception:
                        pass

        try:
            signal.signal(sig, _handler)
            handled.append(name)
        except Exception:
            continue
    if handled:
        logger.info(f"SystemKeepAlive signal cleanup enabled for: {', '.join(handled)}")


_install_signal_handlers()
