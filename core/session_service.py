from __future__ import annotations
import json
import os
import threading
import time
import logging
from core.qt_compat import QTimer, Signal, QObject
from core.qt_dispatch import run_on_qt_main_thread

logger = logging.getLogger("SnapDownloader.SessionService")

class SessionServiceSignals(QObject):
    session_loaded_ui = Signal(object)

class SessionService:
    """Manages the background saving and loading of the application session."""

    def __init__(self, build_payload_callback=None, app_data_dir=None):
        self._state = {}
        self.build_payload_callback = build_payload_callback
        self.app_data_dir = app_data_dir
        
        self.signals = SessionServiceSignals()
        
        self._save_lock = threading.Lock()
        self._save_write_lock = threading.Lock()
        self._save_event = threading.Event()
        self._save_payload = None
        self._save_shutdown = False
        self._save_debounce_ms = 180
        self._save_max_deferral_ms = 1200
        self._save_requested = False
        self._save_first_request_ts = None
        
        self._last_saved_queue_signature = None
        self._last_saved_queue_revision = None
        self._last_saved_settings_signature = None
        
        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._flush_pending_save_request)
        
        self._save_thread = None
        self._load_thread = None
        self._load_lock = threading.Lock()
        self._load_stop_event = threading.Event()

    def start(self):
        if self._save_thread is not None and self._save_thread.is_alive():
            return
        self._save_shutdown = False
        self._save_thread = threading.Thread(
            target=self._save_loop,
            daemon=True,
            name="SessionSaveWorker",
        )
        self._save_thread.start()

    def stop(self):
        self._save_shutdown = True
        self._save_event.set()
        self._load_stop_event.set()
        self._stop_save_timer()
        if self._save_thread:
            self._save_thread.join(timeout=2.0)
        with self._load_lock:
            load_thread = self._load_thread
        if load_thread and load_thread.is_alive():
            load_thread.join(timeout=1.0)

    def session_path(self):
        if not self.app_data_dir:
            from core.database import get_app_data_dir
            self.app_data_dir = get_app_data_dir()
        os.makedirs(self.app_data_dir, exist_ok=True)
        return os.path.join(self.app_data_dir, 'xd_session.json')

    def migrate_legacy_session_json(self):
        legacy_path = self.session_path()
        migrated_path = legacy_path + '.migrated'
        if not os.path.exists(legacy_path) or os.path.exists(migrated_path):
            return
        try:
            with open(legacy_path, 'r', encoding='utf-8') as file:
                payload = json.load(file)
            if not isinstance(payload, dict):
                return
            queue_items = payload.get('queue_items', [])
            settings = payload.get('settings', {})
            from core.database import save_queue_items, save_session_settings
            if isinstance(queue_items, list):
                save_queue_items(queue_items)
            if isinstance(settings, dict):
                save_session_settings(settings)
            os.rename(legacy_path, migrated_path)
            logger.info('[DB] Legacy session migrated to SQLite')
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(f'[DB] Session migration failed: {exc}')

    def _save_loop(self):
        from core.database import close_thread_connection
        try:
            while not self._save_shutdown:
                self._save_event.wait(timeout=1.0)
                self._save_event.clear()
                if self._save_shutdown:
                    break
                while True:
                    with self._save_lock:
                        payload = self._save_payload
                        self._save_payload = None
                    if payload is None:
                        break
                    self.write_session_payload(payload)
        finally:
            close_thread_connection()

    def _flush_pending_save_request(self):
        if self._save_shutdown:
            return
        with self._save_lock:
            if not self._save_requested:
                return
            self._save_requested = False
            self._save_first_request_ts = None
        if self.build_payload_callback:
            payload = self.build_payload_callback()
            with self._save_lock:
                self._save_payload = payload
            self._save_event.set()

    def _timer_is_active(self) -> bool:
        try:
            return bool(self._save_timer.isActive())
        except Exception:
            return False

    def _stop_save_timer(self) -> None:
        def _stop():
            if self._save_timer.isActive():
                self._save_timer.stop()
        if not run_on_qt_main_thread(_stop):
            _stop()

    def _start_save_timer(self, delay_ms: int) -> None:
        delay = max(0, int(delay_ms or 0))
        def _start():
            if self._save_timer.isActive():
                self._save_timer.stop()
            self._save_timer.start(delay)
        if not run_on_qt_main_thread(_start):
            _start()

    def save_session(self, sync: bool=False):
        try:
            if sync:
                if self.build_payload_callback:
                    payload = self.build_payload_callback()
                    self._save_requested = False
                    self._save_first_request_ts = None
                    self._stop_save_timer()
                    self.write_session_payload(payload)
                return
            with self._save_lock:
                self._save_requested = True
                first_ts = self._save_first_request_ts
                now_ts = float(time.monotonic())
                if first_ts is None:
                    self._save_first_request_ts = now_ts
                    first_ts = now_ts
            debounce_ms = max(0, int(self._save_debounce_ms))
            max_deferral_ms = max(debounce_ms, int(self._save_max_deferral_ms))
            if self._timer_is_active():
                elapsed_ms = max(0, int((float(time.monotonic()) - float(first_ts or 0.0)) * 1000))
                if elapsed_ms < max_deferral_ms:
                    return
                self._start_save_timer(0)
                return
            self._start_save_timer(debounce_ms)
        except Exception as exc:
            logger.error(f'تعذر تجهيز بيانات حفظ الجلسة: {exc}', exc_info=True)

    def _stable_payload_signature(self, payload) -> str:
        try:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            import hashlib
            return hashlib.md5(text.encode('utf-8')).hexdigest()
        except Exception:
            return ""

    def write_session_payload(self, payload: dict):
        from core.database import save_queue_items, save_session_settings
        try:
            with self._save_write_lock:
                queue_items = payload.get('queue_items', [])
                queue_revision = payload.get('queue_revision', None)
                settings = payload.get('settings', {})
                settings_signature = self._stable_payload_signature(settings)
                if queue_items is not None:
                    if queue_revision is not None:
                        normalized_revision = int(queue_revision)
                        if normalized_revision != self._last_saved_queue_revision:
                            save_queue_items(queue_items)
                            self._last_saved_queue_revision = normalized_revision
                            self._last_saved_queue_signature = None
                    else:
                        queue_signature = self._stable_payload_signature(queue_items)
                        if queue_signature != self._last_saved_queue_signature:
                            save_queue_items(queue_items)
                            self._last_saved_queue_signature = queue_signature
                if settings_signature != self._last_saved_settings_signature:
                    save_session_settings(settings)
                    self._last_saved_settings_signature = settings_signature
        except (OSError, ValueError) as exc:
            logger.error(f'تعذر حفظ الجلسة في قاعدة البيانات: {exc}')

    def read_session_payload(self) -> dict:
        from core.database import load_queue_items, load_session_settings
        with self._save_write_lock:
            return {'queue_items': load_queue_items(), 'settings': load_session_settings()}

    def load_session_async(self):
        with self._load_lock:
            if self._load_thread is not None and self._load_thread.is_alive():
                return
            self._load_stop_event.clear()

        def _worker():
            from core.database import close_thread_connection
            try:
                payload = self.read_session_payload()
                if self._load_stop_event.is_set():
                    return
                self.signals.session_loaded_ui.emit({'ok': True, 'payload': payload, 'error': ''})
            except Exception as exc:
                if self._load_stop_event.is_set():
                    return
                self.signals.session_loaded_ui.emit({'ok': False, 'payload': None, 'error': str(exc)})
            finally:
                close_thread_connection()
                with self._load_lock:
                    current = threading.current_thread()
                    if self._load_thread is current:
                        self._load_thread = None
        load_thread = threading.Thread(target=_worker, daemon=True, name='SessionLoadWorker')
        with self._load_lock:
            self._load_thread = load_thread
        load_thread.start()

    def set(self, key: str, value):
        self._state[str(key)] = value

    def get(self, key: str, default=None):
        return self._state.get(str(key), default)

    def update(self, payload: dict | None):
        if isinstance(payload, dict):
            self._state.update(payload)

    def snapshot(self) -> dict:
        return dict(self._state)
