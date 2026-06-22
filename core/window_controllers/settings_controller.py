
import json
import logging
import os
import glob
import re
import sys
import threading
import time
from datetime import datetime

try:
    import winreg
except ImportError:
    winreg = None

try:
    from PySide6.QtCore import QTimer, QTime
    from PySide6.QtWidgets import QFileDialog, QLineEdit, QSpinBox, QCheckBox, QComboBox, QSystemTrayIcon
except ImportError:
    from PyQt6.QtCore import QTimer, QTime
    from PyQt6.QtWidgets import QFileDialog, QLineEdit, QSpinBox, QCheckBox, QComboBox, QSystemTrayIcon

from core.audio_normalizer import normalize_folder
from core.utils import get_app_data_dir
from core.anti_detection import anti_detection_engine
from core.bandwidth_scheduler import scheduler
from core.config import DEFAULT_SETTINGS, THEME_MODE_MAP, default_download_dir, estimate_file_size_bytes
from core.cookie_importer import auto_detect_and_export, encrypt_cookie_file_inplace
from core.cookie_profiles import CookieProfileManager
from core.database import (
    close_thread_connection,
    fetch_history,
    get_all_stats,
    increment_stat,
    insert_history,
    load_queue_items,
    load_session_settings,
    record_peak_speed,
    save_queue_items,
    save_session_settings,
)
from core.downloader import DownloadWorker
from core.duplicate_finder import build_duplicate_report
from core.proxy_manager import proxy_manager
from core.storage_watchdog import format_bytes, has_enough_space
from core.secure_storage import protect_text, unprotect_text
from core.sustainability import sustainability
from core.i18n import i18n, _
from core.error_handler import ErrorHandler
from core.qt_dispatch import run_on_qt_main_thread
from core.task_types import TaskStatus, normalize_task_status
from core.workers import AnalyzeWorker
from ui.themes import THEMES

logger = logging.getLogger("SnapDownloader")
AUTO_MANAGED_COOKIES_REF_PREFIX = "appdata://"


class SettingsController:
    def __init__(self, window):
        self.window = window
        self.cookie_profiles = CookieProfileManager()

    def _sync_config_manager(self, settings: dict):
        cfg = getattr(self.window, "config_manager", None)
        if cfg is None or not isinstance(settings, dict):
            return
        try:
            if "theme" in settings:
                cfg.set("theme", str(settings.get("theme") or self.window.theme))
            if "out_dir" in settings:
                cfg.set("save_path", str(settings.get("out_dir") or self.window.current_download_path))
            if "max_concurrent" in settings:
                cfg.set("max_concurrent", max(1, int(settings.get("max_concurrent") or self.window.max_concurrent)))
            if "use_aria2" in settings:
                cfg.set("use_aria2c", bool(settings.get("use_aria2")))
            if "use_native_engine" in settings:
                cfg.set("use_native_engine", bool(settings.get("use_native_engine")))
            if "play_sound" in settings:
                cfg.set("play_sound", bool(settings.get("play_sound")))
        except Exception as exc:
            logger.debug(f"تعذر مزامنة ConfigManager: {exc}")

    def _ensure_session_sync_state(self):
        if not hasattr(self.window, "_session_save_lock"):
            self.window._session_save_lock = threading.Lock()
        if not hasattr(self.window, "_session_save_write_lock"):
            self.window._session_save_write_lock = threading.Lock()
        if not hasattr(self.window, "_session_save_event"):
            self.window._session_save_event = threading.Event()
        if not hasattr(self.window, "_session_save_payload"):
            self.window._session_save_payload = None
        if not hasattr(self.window, "_session_save_shutdown"):
            self.window._session_save_shutdown = False
        if not hasattr(self.window, "_session_save_debounce_ms"):
            self.window._session_save_debounce_ms = 180
        if not hasattr(self.window, "_session_save_max_deferral_ms"):
            self.window._session_save_max_deferral_ms = 1200
        if not hasattr(self.window, "_session_save_requested"):
            self.window._session_save_requested = False
        if not hasattr(self.window, "_session_save_first_request_ts"):
            self.window._session_save_first_request_ts = None
        if not hasattr(self.window, "_session_last_saved_queue_signature"):
            self.window._session_last_saved_queue_signature = None
        if not hasattr(self.window, "_session_last_saved_queue_revision"):
            self.window._session_last_saved_queue_revision = None
        if not hasattr(self.window, "_session_last_saved_settings_signature"):
            self.window._session_last_saved_settings_signature = None
        if not hasattr(self.window, "_session_save_timer"):
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(self._flush_pending_session_save_request)
            self.window._session_save_timer = timer

    def _stable_payload_signature(self, value) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return repr(value)

    def _serialize_cookies_path(self, path_value: str) -> str:
        path_text = str(path_value or "").strip()
        if not path_text:
            return ""
        try:
            absolute = os.path.abspath(path_text)
            app_data_dir = os.path.abspath(get_app_data_dir())
            filename = os.path.basename(absolute)
            if (
                filename.lower().startswith("auto_cookies")
                and os.path.commonpath([absolute, app_data_dir]) == app_data_dir
            ):
                return f"{AUTO_MANAGED_COOKIES_REF_PREFIX}{filename}"
        except Exception:
            return protect_text(path_text)
        return protect_text(path_text)

    def _resolve_cookies_path(self, path_value: str) -> str:
        path_text = str(path_value or "").strip()
        if not path_text.startswith(AUTO_MANAGED_COOKIES_REF_PREFIX):
            return unprotect_text(path_text)
        filename = os.path.basename(path_text[len(AUTO_MANAGED_COOKIES_REF_PREFIX):].strip())
        if not filename:
            return ""
        return os.path.join(get_app_data_dir(), filename)

    def _cookies_paths_match(self, left: str, right: str) -> bool:
        left_text = str(left or "").strip()
        right_text = str(right or "").strip()
        if not left_text or not right_text:
            return False
        try:
            return os.path.normcase(os.path.abspath(left_text)) == os.path.normcase(os.path.abspath(right_text))
        except Exception:
            return left_text == right_text

    def _build_persisted_cookies_path(self, form: dict) -> str:
        path_text = str(form.get("cookies_path", self.window.cookies_path) or "").strip()
        if not path_text:
            return ""
        profile_name = str(form.get("cookie_profile_name", "") or "").strip()
        if profile_name:
            try:
                profile_path = str(self.cookie_profiles.get_profile_path(profile_name) or "").strip()
            except Exception:
                profile_path = ""
            if self._cookies_paths_match(path_text, profile_path):
                return ""
        return self._serialize_cookies_path(path_text)

    def detect_system_theme_mode(self):
        if sys.platform != "win32" or winreg is None:
            return None
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(value) else "dark"
        except Exception as exc:
            logger.debug(f"تعذر قراءة وضع الثيم من النظام: {exc}")
            return None

    def set_system_theme_sync_enabled(self, enabled: bool):
        self.window.system_theme_sync_enabled = bool(enabled)
        sv = getattr(self.window, "settings_view", None)
        if sv is not None and hasattr(sv, "apply_form_settings"):
            sv.apply_form_settings({"system_theme_sync_enabled": self.window.system_theme_sync_enabled}, block_signals=True)
            return
        if sv is not None and hasattr(sv, "settings_system_theme_sync"):
            sv.settings_system_theme_sync.blockSignals(True)
            sv.settings_system_theme_sync.setChecked(self.window.system_theme_sync_enabled)
            sv.settings_system_theme_sync.blockSignals(False)

    def apply_theme(self, theme_name: str, persist: bool = False):
        if theme_name not in THEMES:
            return
        changed = theme_name != self.window.theme
        self.window.theme = theme_name
        if hasattr(self.window, "clip_watcher") and self.window.clip_watcher is not None:
            try:
                self.window.clip_watcher.update_theme(theme_name)
            except Exception as exc:
                logger.debug(f"تعذر تحديث ثيم Clip Watcher: {exc}")
        if hasattr(self.window, "mini_window") and self.window.mini_window is not None:
            try:
                self.window.mini_window.update_theme(THEMES.get(theme_name, THEMES["Modern Dark"]))
            except Exception as exc:
                logger.debug(f"تعذر تحديث ثيم Mini Mode: {exc}")
        if hasattr(self.window, "sidebar") and self.window.sidebar is not None:
            try:
                self.window.sidebar.set_theme(THEMES.get(theme_name, THEMES["Modern Dark"]))
            except Exception as exc:
                logger.debug(f"تعذر تحديث ثيم Sidebar: {exc}")
        if hasattr(self.window, "subscriptions_view") and self.window.subscriptions_view is not None:
            try:
                self.window.subscriptions_view.update_theme(theme_name)
            except Exception as exc:
                logger.debug(f"تعذر تحديث ثيم SubscriptionsView: {exc}")
        if hasattr(self.window, "main_stack"):
            self.window.setStyleSheet(self.window._qss())
            self.window._apply_downloads_inline_theme()
            sv = getattr(self.window, "search_view", None)
            if sv is not None and hasattr(sv, "refresh_theme_styles"):
                try:
                    sv.refresh_theme_styles()
                except Exception as exc:
                    logger.debug(f"تعذر تحديث Theme Styles في SearchView: {exc}")
        if persist and changed:
            self.save_session()

    def set_ui_language(self, lang_code: str, persist: bool = True, notify: bool = True):
        code = str(lang_code or "").strip().lower()
        available = i18n.available_languages()
        if code not in available:
            code = "en"
        i18n.set_language(code)
        self.window.ui_language = code
        sv = getattr(self.window, "settings_view", None)
        if sv is not None and hasattr(sv, "apply_form_settings"):
            sv.apply_form_settings({"ui_language": code}, block_signals=True)
        elif sv is not None and hasattr(sv, "settings_language_combo"):
            sv.settings_language_combo.blockSignals(True)
            idx = sv.settings_language_combo.findData(code)
            if idx >= 0:
                sv.settings_language_combo.setCurrentIndex(idx)
            sv.settings_language_combo.blockSignals(False)
        refresh_ui = getattr(self.window, "_refresh_translations", None)
        if callable(refresh_ui):
            refresh_ui()
        available = i18n.available_languages()
        if notify:
            self.window._info(_("Language switched to {language}").format(language=available.get(code, code)))
        if persist:
            self.save_session()

    def refresh_system_theme(self, force: bool = False, persist: bool = False):
        if not self.window.system_theme_sync_enabled and not force:
            return
        mode = self.detect_system_theme_mode()
        if mode not in THEME_MODE_MAP:
            return
        if not force and mode == self.window._last_system_theme_mode:
            return
        self.window._last_system_theme_mode = mode
        self.apply_theme(str(THEME_MODE_MAP.get(mode, self.window.theme)), persist=persist)

    def toggle_theme(self):
        theme_names = list(THEMES.keys())
        current_idx = theme_names.index(self.window.theme) if self.window.theme in theme_names else 0
        next_idx = (current_idx + 1) % len(theme_names)
        self.set_system_theme_sync_enabled(False)
        self.apply_theme(theme_names[next_idx], persist=True)
        logger.info(f"Theme switched to: {self.window.theme}")

    def toggle_dark_light_mode(self):
        dark_theme = str(THEME_MODE_MAP.get("dark", "Modern Dark"))
        light_theme = str(THEME_MODE_MAP.get("light", "Elegant Light"))
        self.set_system_theme_sync_enabled(False)
        self.apply_theme(light_theme if self.window.theme == dark_theme else dark_theme, persist=True)

    def pick_cookies(self):
        path, _ = QFileDialog.getOpenFileName(self.window, _("Select Cookies File"), "", _("Text Files (*.txt);;All Files (*)"))
        if path:
            sv = getattr(self.window, "settings_view", None)
            if sv is not None and hasattr(sv, "apply_form_settings"):
                sv.apply_form_settings({"cookies_path": path}, block_signals=False)
            elif sv is not None and hasattr(sv, "settings_cookies"):
                sv.settings_cookies.setText(path)

    def pick_post_download_script(self):
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            _("Select Post-Download Script"),
            "",
            _("Scripts (*.py *.ps1 *.bat *.cmd);;All Files (*)"),
        )
        if not path:
            return
        sv = getattr(self.window, "settings_view", None)
        if sv is not None and hasattr(sv, "apply_form_settings"):
            sv.apply_form_settings({"post_download_script": path}, block_signals=False)
        elif sv is not None and hasattr(sv, "settings_post_download_script"):
            sv.settings_post_download_script.setText(path)

    def refresh_cookie_profiles_ui(self, selected_name: str = ""):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        profiles = self.cookie_profiles.list_profiles()
        names = [p.name for p in profiles]
        if hasattr(sv, "set_cookie_profile_items"):
            sv.set_cookie_profile_items(names, selected_name=selected_name, block_signals=True)
            return
        if not hasattr(sv, "settings_cookie_profile_combo"):
            return
        combo = sv.settings_cookie_profile_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Default (no profile)")
        for name in names:
            combo.addItem(str(name))
        if selected_name:
            idx = combo.findText(str(selected_name))
            if idx >= 0:
                combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def save_cookie_profile_from_ui(self):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        name = ""
        if hasattr(sv, "settings_cookie_profile_name"):
            name = str(sv.settings_cookie_profile_name.text() or "").strip()
        if not name and hasattr(sv, "settings_cookie_profile_combo"):
            name = str(sv.settings_cookie_profile_combo.currentText() or "").strip()
        path = ""
        if hasattr(sv, "settings_cookies"):
            path = str(sv.settings_cookies.text() or "").strip()
        if not name or name.lower().startswith("default"):
            self.window._warn("اكتب اسم بروفايل كوكيز صالح أولاً")
            return
        if not path:
            self.window._warn("حدد مسار cookies أولاً قبل حفظ البروفايل")
            return
        self.cookie_profiles.upsert_profile(name, path)
        self.refresh_cookie_profiles_ui(selected_name=name)
        self.window._info(f"تم حفظ بروفايل الكوكيز: {name}")

    def load_selected_cookie_profile(self):
        sv = getattr(self.window, "settings_view", None)
        if sv is None or not hasattr(sv, "settings_cookie_profile_combo"):
            return
        selected = str(sv.settings_cookie_profile_combo.currentText() or "").strip()
        if not selected or selected.lower().startswith("default"):
            self.window._warn("اختر بروفايل كوكيز أولاً")
            return
        path = self.cookie_profiles.get_profile_path(selected)
        if not path:
            self.window._warn("لم يتم العثور على مسار صالح لهذا البروفايل")
            return
        if hasattr(sv, "apply_form_settings"):
            sv.apply_form_settings({"cookies_path": path, "cookie_profile_name": selected}, block_signals=False)
        else:
            if hasattr(sv, "settings_cookies"):
                sv.settings_cookies.setText(path)
            if hasattr(sv, "settings_cookie_profile_name"):
                sv.settings_cookie_profile_name.setText(selected)
        self.window.cookies_path = path
        self.save_session()
        self.window._info(f"تم تحميل بروفايل الكوكيز: {selected}")

    def delete_selected_cookie_profile(self):
        sv = getattr(self.window, "settings_view", None)
        if sv is None or not hasattr(sv, "settings_cookie_profile_combo"):
            return
        selected = str(sv.settings_cookie_profile_combo.currentText() or "").strip()
        if not selected or selected.lower().startswith("default"):
            self.window._warn("اختر بروفايل لحذفه")
            return
        self.cookie_profiles.remove_profile(selected)
        self.refresh_cookie_profiles_ui(selected_name="")
        if hasattr(sv, "settings_cookie_profile_name"):
            sv.settings_cookie_profile_name.setText("")
        self.window._info(f"تم حذف بروفايل الكوكيز: {selected}")

    def auto_import_cookies(self):
        app_data_dir = get_app_data_dir()
        os.makedirs(app_data_dir, exist_ok=True)
        out_path = os.path.join(app_data_dir, "auto_cookies.txt")
        try:
            browser, count = auto_detect_and_export(out_path)
            secure_path = out_path
            encrypt_enabled = os.name == "nt" and str(os.getenv("VIDDOWNLOADER_ENCRYPT_COOKIES", "")).strip().lower() not in {"0", "false", "no", "off"}
            if encrypt_enabled:
                try:
                    secure_path = encrypt_cookie_file_inplace(out_path)
                except Exception as exc:
                    logger.warning(f"[Cookies] Failed to encrypt cookies file: {exc}")
                    secure_path = out_path
            try:
                os.chmod(secure_path, 0o600)
            except Exception:
                pass
            sv = getattr(self.window, "settings_view", None)
            if sv is not None and hasattr(sv, "apply_form_settings"):
                sv.apply_form_settings({"cookies_path": secure_path}, block_signals=False)
            elif sv is not None and hasattr(sv, "settings_cookies"):
                sv.settings_cookies.setText(secure_path)
            self.window.cookies_path = secure_path
            self.window._info(f"✅ Imported {count} cookies from {browser.title()}")
            logger.info(f"[Cookies] Imported {count} from {browser}")
        except Exception as exc:
            self.window._warn(f"Cookie import failed: {exc}")
            logger.warning(f"[Cookies] {exc}")

    def format_bandwidth_limit(self, limit_kbps: int) -> str:
        value = max(0, int(limit_kbps or 0))
        if value <= 0:
            return "غير محدود"
        if value >= 1024:
            return f"{value / 1024:.1f} MB/s"
        return f"{value} KB/s"

    def current_bandwidth_limit_kbps(self) -> int:
        if not self.window.bandwidth_scheduler_enabled:
            return 0
        try:
            return max(0, int(scheduler.get_current_limit() or 0))
        except Exception as exc:
            logger.debug(f"تعذر قراءة حد السرعة الحالي من الجدول: {exc}")
            return 0

    def refresh_bandwidth_schedule(self, notify: bool = False):
        self.window.bandwidth_scheduler_enabled = bool(scheduler.enabled)
        summary = scheduler.format_schedule_summary()
        limit_text = self.format_bandwidth_limit(self.current_bandwidth_limit_kbps())
        if self.window.bandwidth_scheduler_enabled:
            summary = f"{summary} — الحد الحالي: {limit_text}"
        previous = getattr(self.window, "_bandwidth_schedule_summary", "")
        self.window._bandwidth_schedule_summary = summary
        sv = getattr(self.window, "settings_view", None)
        if sv is not None and hasattr(sv, "set_bandwidth_scheduler_checked"):
            sv.set_bandwidth_scheduler_checked(self.window.bandwidth_scheduler_enabled)
        elif sv is not None and hasattr(sv, "settings_bandwidth_scheduler"):
            sv.settings_bandwidth_scheduler.setChecked(self.window.bandwidth_scheduler_enabled)
        if sv is not None and hasattr(sv, "set_bandwidth_scheduler_summary"):
            sv.set_bandwidth_scheduler_summary(summary)
        elif sv is not None and hasattr(sv, "settings_bandwidth_scheduler_summary"):
            sv.settings_bandwidth_scheduler_summary.setText(summary)
        if notify and summary != previous:
            self.window._append_log(f"[Scheduler] {summary}")

    def bandwidth_schedule_editor_text(self) -> str:
        try:
            return json.dumps(scheduler.schedule, ensure_ascii=False, indent=2)
        except Exception:
            return "[]"

    def reload_bandwidth_schedule_editor(self):
        sv = getattr(self.window, "settings_view", None)
        rules = scheduler.normalize_schedule(scheduler.schedule)
        if sv is not None and hasattr(sv, "set_bandwidth_schedule_editor_text"):
            sv.set_bandwidth_schedule_editor_text(json.dumps(rules, ensure_ascii=False, indent=2), block_signals=False)
        elif sv is not None and hasattr(sv, "settings_bandwidth_schedule_editor"):
            sv.settings_bandwidth_schedule_editor.setPlainText(json.dumps(rules, ensure_ascii=False, indent=2))
        if sv is not None and hasattr(sv, "bandwidth_calendar"):
            sv.bandwidth_calendar.import_schedule(rules)

    def get_bandwidth_schedule_editor_rules(self) -> list[dict]:
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return []
        if hasattr(sv, "get_bandwidth_schedule_editor_text"):
            raw_text = sv.get_bandwidth_schedule_editor_text().strip()
        elif hasattr(sv, "settings_bandwidth_schedule_editor"):
            raw_text = sv.settings_bandwidth_schedule_editor.toPlainText().strip()
        else:
            raw_text = ""
        if raw_text:
            parsed = json.loads(raw_text)
            normalized = scheduler.normalize_schedule(parsed)
            if hasattr(sv, "bandwidth_calendar"):
                sv.bandwidth_calendar.import_schedule(normalized)
            return normalized
        if hasattr(sv, "bandwidth_calendar"):
            return scheduler.normalize_schedule(sv.bandwidth_calendar.export_schedule())
        return []

    def set_bandwidth_schedule_editor_rules(self, rules: list[dict], selected_index: int | None = None):
        normalized = scheduler.normalize_schedule(rules)
        sv = getattr(self.window, "settings_view", None)
        editor_text = json.dumps(normalized, ensure_ascii=False, indent=2)
        if sv is not None and hasattr(sv, "set_bandwidth_schedule_editor_text"):
            sv.set_bandwidth_schedule_editor_text(editor_text, block_signals=True)
        elif sv is not None and hasattr(sv, "settings_bandwidth_schedule_editor"):
            sv.settings_bandwidth_schedule_editor.blockSignals(True)
            sv.settings_bandwidth_schedule_editor.setPlainText(editor_text)
            sv.settings_bandwidth_schedule_editor.blockSignals(False)
        if sv is not None and hasattr(sv, "bandwidth_calendar"):
            sv.bandwidth_calendar.import_schedule(normalized)
        self.sync_bandwidth_schedule_rule_list(selected_index=selected_index)

    def sync_bandwidth_schedule_rule_list(self, selected_index: int | None = None):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        if not hasattr(sv, "set_bandwidth_rule_list_items") and not hasattr(sv, "settings_bandwidth_rule_list"):
            return
        current_row = self.window._editing_bandwidth_rule_index if isinstance(self.window._editing_bandwidth_rule_index, int) else -1
        target_row = current_row if selected_index is None else selected_index
        try:
            rules = self.get_bandwidth_schedule_editor_rules()
        except Exception as exc:
            if sv is not None and hasattr(sv, "set_bandwidth_schedule_status"):
                sv.set_bandwidth_schedule_status(f"خطأ في القواعد الحالية: {exc}")
            elif sv is not None and hasattr(sv, "settings_bandwidth_schedule_status"):
                sv.settings_bandwidth_schedule_status.setText(f"خطأ في القواعد الحالية: {exc}")
            return
        items = [
            f"{rule['start']} → {rule['end']} • {self.format_bandwidth_limit(rule.get('limit_kbps', 0))} • {rule.get('label', '')}"
            for rule in rules
        ]
        if hasattr(sv, "set_bandwidth_rule_list_items"):
            sv.set_bandwidth_rule_list_items(items, selected_index=-1, block_signals=True)
        else:
            sv.settings_bandwidth_rule_list.blockSignals(True)
            sv.settings_bandwidth_rule_list.clear()
            for item in items:
                sv.settings_bandwidth_rule_list.addItem(item)
            sv.settings_bandwidth_rule_list.blockSignals(False)
        if not rules:
            self.window._editing_bandwidth_rule_index = None
            if hasattr(sv, "set_bandwidth_schedule_status"):
                sv.set_bandwidth_schedule_status("لا توجد قواعد محفوظة في المحرر")
            elif hasattr(sv, "settings_bandwidth_schedule_status"):
                sv.settings_bandwidth_schedule_status.setText("لا توجد قواعد محفوظة في المحرر")
            return
        if target_row is None or target_row < 0:
            target_row = 0
        target_row = max(0, min(target_row, len(rules) - 1))
        if hasattr(sv, "set_bandwidth_rule_list_items"):
            sv.set_bandwidth_rule_list_items(items, selected_index=target_row, block_signals=True)
        else:
            sv.settings_bandwidth_rule_list.setCurrentRow(target_row)
        if hasattr(sv, "set_bandwidth_schedule_status"):
            sv.set_bandwidth_schedule_status(f"عدد القواعد الجاهزة للتطبيق: {len(rules)}")
        elif hasattr(sv, "settings_bandwidth_schedule_status"):
            sv.settings_bandwidth_schedule_status.setText(f"عدد القواعد الجاهزة للتطبيق: {len(rules)}")

    def set_bandwidth_rule_form(self, rule: dict | None = None):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        if hasattr(sv, "set_bandwidth_rule_form"):
            sv.set_bandwidth_rule_form(rule)
            return
        if not hasattr(sv, "settings_bandwidth_rule_start"):
            return
        value = rule or {"start": "08:00", "end": "12:00", "limit_kbps": 0, "label": ""}
        start_time = QTime.fromString(str(value.get("start", "08:00")), "HH:mm")
        end_time = QTime.fromString(str(value.get("end", "12:00")), "HH:mm")
        if not start_time.isValid():
            start_time = QTime(8, 0)
        if not end_time.isValid():
            end_time = QTime(12, 0)
        sv.settings_bandwidth_rule_start.setTime(start_time)
        sv.settings_bandwidth_rule_end.setTime(end_time)
        sv.settings_bandwidth_rule_limit.setValue(max(0, int(value.get("limit_kbps", 0) or 0)))
        sv.settings_bandwidth_rule_label.setText(str(value.get("label", "") or ""))

    def prepare_new_bandwidth_rule(self):
        sv = getattr(self.window, "settings_view", None)
        self.window._editing_bandwidth_rule_index = None
        if sv is not None and hasattr(sv, "clear_bandwidth_rule_selection"):
            sv.clear_bandwidth_rule_selection(block_signals=True)
        elif sv is not None and hasattr(sv, "settings_bandwidth_rule_list"):
            sv.settings_bandwidth_rule_list.blockSignals(True)
            sv.settings_bandwidth_rule_list.clearSelection()
            sv.settings_bandwidth_rule_list.blockSignals(False)
        self.set_bandwidth_rule_form()
        if sv is not None and hasattr(sv, "set_bandwidth_schedule_status"):
            sv.set_bandwidth_schedule_status("جاهز لإضافة قاعدة جديدة إلى المحرر")
        elif sv is not None and hasattr(sv, "settings_bandwidth_schedule_status"):
            sv.settings_bandwidth_schedule_status.setText("جاهز لإضافة قاعدة جديدة إلى المحرر")

    def load_selected_bandwidth_rule(self, row: int):
        if row < 0:
            self.window._editing_bandwidth_rule_index = None
            self.set_bandwidth_rule_form()
            return
        try:
            rules = self.get_bandwidth_schedule_editor_rules()
        except Exception:
            self.window._editing_bandwidth_rule_index = None
            return
        if 0 <= row < len(rules):
            self.window._editing_bandwidth_rule_index = row
            self.set_bandwidth_rule_form(rules[row])
        else:
            self.window._editing_bandwidth_rule_index = None
            self.set_bandwidth_rule_form()

    def save_bandwidth_rule_from_form(self):
        try:
            rules = self.get_bandwidth_schedule_editor_rules()
        except Exception as exc:
            self.window._warn(f"تعذر قراءة قواعد المحرر الحالية: {exc}")
            return
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            self.window._warn("تعذر حفظ القاعدة: واجهة الجدول غير جاهزة")
            return
        try:
            if hasattr(sv, "get_bandwidth_rule_form"):
                form_rule = sv.get_bandwidth_rule_form()
            else:
                if not hasattr(sv, "settings_bandwidth_rule_start"):
                    raise ValueError("Bandwidth form is not available")
                form_rule = {
                    "start": sv.settings_bandwidth_rule_start.time().toString("HH:mm"),
                    "end": sv.settings_bandwidth_rule_end.time().toString("HH:mm"),
                    "limit_kbps": int(sv.settings_bandwidth_rule_limit.value()),
                    "label": sv.settings_bandwidth_rule_label.text().strip(),
                }
            rule = scheduler.normalize_schedule(
                [
                    form_rule
                ]
            )[0]
        except Exception as exc:
            self.window._warn(f"تعذر حفظ القاعدة: {exc}")
            return
        index = self.window._editing_bandwidth_rule_index if isinstance(self.window._editing_bandwidth_rule_index, int) else -1
        if 0 <= index < len(rules):
            rules[index] = rule
            message = "تم تحديث القاعدة داخل المحرر"
        else:
            rules.append(rule)
            index = len(rules) - 1
            message = "تمت إضافة القاعدة داخل المحرر"
        self.set_bandwidth_schedule_editor_rules(rules, selected_index=index)
        if sv is not None and hasattr(sv, "set_bandwidth_schedule_status"):
            sv.set_bandwidth_schedule_status(f"{message} — اضغط تطبيق جدول السرعة للحفظ النهائي")
        elif sv is not None and hasattr(sv, "settings_bandwidth_schedule_status"):
            sv.settings_bandwidth_schedule_status.setText(f"{message} — اضغط تطبيق جدول السرعة للحفظ النهائي")

    def remove_selected_bandwidth_rule(self):
        try:
            rules = self.get_bandwidth_schedule_editor_rules()
        except Exception as exc:
            self.window._warn(f"تعذر قراءة قواعد المحرر الحالية: {exc}")
            return
        sv = getattr(self.window, "settings_view", None)
        index = self.window._editing_bandwidth_rule_index if isinstance(self.window._editing_bandwidth_rule_index, int) else -1
        if not (0 <= index < len(rules)):
            self.window._warn("اختر قاعدة أولاً لحذفها")
            return
        rules.pop(index)
        next_index = index if index < len(rules) else len(rules) - 1
        self.window._editing_bandwidth_rule_index = None
        self.set_bandwidth_schedule_editor_rules(rules, selected_index=next_index)
        self.prepare_new_bandwidth_rule()
        if sv is not None and hasattr(sv, "set_bandwidth_schedule_status"):
            sv.set_bandwidth_schedule_status("تم حذف القاعدة من المحرر — اضغط تطبيق جدول السرعة للحفظ النهائي")
        elif sv is not None and hasattr(sv, "settings_bandwidth_schedule_status"):
            sv.settings_bandwidth_schedule_status.setText("تم حذف القاعدة من المحرر — اضغط تطبيق جدول السرعة للحفظ النهائي")

    def apply_bandwidth_schedule_rules(self):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        if hasattr(sv, "get_bandwidth_schedule_editor_text"):
            raw_text = sv.get_bandwidth_schedule_editor_text().strip()
        elif hasattr(sv, "settings_bandwidth_schedule_editor"):
            raw_text = sv.settings_bandwidth_schedule_editor.toPlainText().strip()
        else:
            return
        if not raw_text:
            self.window._warn("أدخل قواعد جدول السرعة أولاً")
            return
        try:
            scheduler.set_schedule(self.get_bandwidth_schedule_editor_rules())
            self.reload_bandwidth_schedule_editor()
            self.refresh_bandwidth_schedule(notify=True)
            if hasattr(sv, "set_bandwidth_schedule_status"):
                sv.set_bandwidth_schedule_status("تم حفظ القواعد وتطبيقها بنجاح")
            elif hasattr(sv, "settings_bandwidth_schedule_status"):
                sv.settings_bandwidth_schedule_status.setText("تم حفظ القواعد وتطبيقها بنجاح")
            self.window._info("تم تحديث جدول السرعة")
        except Exception as exc:
            self.window._warn(f"تعذر تطبيق جدول السرعة: {exc}")

    def reset_bandwidth_schedule_rules(self):
        try:
            scheduler.reset_to_defaults()
            self.reload_bandwidth_schedule_editor()
            self.refresh_bandwidth_schedule(notify=True)
            self.prepare_new_bandwidth_rule()
            sv = getattr(self.window, "settings_view", None)
            if sv is not None and hasattr(sv, "set_bandwidth_schedule_status"):
                sv.set_bandwidth_schedule_status("تمت استعادة القواعد الافتراضية")
            elif sv is not None and hasattr(sv, "settings_bandwidth_schedule_status"):
                sv.settings_bandwidth_schedule_status.setText("تمت استعادة القواعد الافتراضية")
            self.window._info("تمت استعادة جدول السرعة الافتراضي")
        except Exception as exc:
            self.window._warn(f"تعذر استعادة جدول السرعة: {exc}")

    def pick_settings_dir(self):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        if hasattr(sv, "get_out_dir"):
            initial_dir = sv.get_out_dir() or os.getcwd()
        else:
            initial_dir = sv.settings_out_dir.text().strip() if hasattr(sv, "settings_out_dir") else os.getcwd()
        selected = QFileDialog.getExistingDirectory(
            self.window,
            "اختيار مجلد",
            initial_dir,
        )
        if selected:
            if hasattr(sv, "set_out_dir"):
                sv.set_out_dir(selected)
            elif hasattr(sv, "settings_out_dir"):
                sv.settings_out_dir.setText(selected)

    def bind_settings_autosave(self):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        if hasattr(sv, "autosave_controls"):
            controls = list(sv.autosave_controls() or [])
        else:
            controls = [
                sv.settings_out_dir,
                sv.settings_retries,
                sv.settings_retry_delay,
                sv.settings_queue_retry_limit,
                sv.settings_aria2,
                sv.settings_concurrent,
                sv.settings_search_history_limit,
                sv.settings_search_history_ttl_days,
                sv.settings_thumbnail_cache_max,
                sv.settings_storage_guard,
                sv.settings_storage_min_free_gb,
                sv.settings_system_theme_sync,
                sv.settings_bandwidth_scheduler,
                sv.settings_cookies,
                sv.settings_embed_subs,
                sv.settings_rename_template,
                sv.settings_split_chapters,
                sv.settings_verify_checksum,
                sv.settings_virus_scan_after_download,
                sv.settings_clean_metadata,
                sv.proxy_enabled_cb,
                sv.proxy_input,
                sv.sustainability_combo,
                sv.sustainability_spin,
            ]
        for control in controls:
            if control is None:
                continue
            if isinstance(control, QLineEdit):
                control.textChanged.connect(self.schedule_settings_autosave)
            elif isinstance(control, QSpinBox):
                control.valueChanged.connect(self.schedule_settings_autosave)
            elif isinstance(control, QCheckBox):
                control.toggled.connect(self.schedule_settings_autosave)
            elif isinstance(control, QComboBox):
                control.currentIndexChanged.connect(self.schedule_settings_autosave)
            elif hasattr(control, "toggled"):
                control.toggled.connect(self.schedule_settings_autosave)
            elif hasattr(control, "currentIndexChanged"):
                control.currentIndexChanged.connect(self.schedule_settings_autosave)
            elif hasattr(control, "valueChanged"):
                control.valueChanged.connect(self.schedule_settings_autosave)
        pipeline_editor = getattr(sv, "pipeline_editor", None)
        if pipeline_editor is not None and hasattr(pipeline_editor, "pipelineChanged"):
            pipeline_editor.pipelineChanged.connect(self.schedule_settings_autosave)

    def schedule_settings_autosave(self, *_):
        if hasattr(self.window, "_settings_autosave_timer") and self.window._settings_autosave_timer is not None:
            self.window._settings_autosave_timer.start()

    def apply_settings_to_search(self, silent: bool = False):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        if hasattr(sv, "get_form_settings"):
            form = sv.get_form_settings()
        else:
            form = {}
        out_dir = str(form.get("out_dir") or sv.settings_out_dir.text()).strip()
        self.window.search_view.out_dir_input.setText(out_dir)
        self.window.search_view.aria2_checkbox.setChecked(bool(form.get("use_aria2", sv.settings_aria2.isChecked() if hasattr(sv, "settings_aria2") else True)))
        if hasattr(self.window.search_view, "post_script_input"):
            self.window.search_view.post_script_input.setText(str(form.get("post_download_script", "") or "").strip())
        self.window.auto_retry_delay_seconds = max(1, int(form.get("auto_retry_delay_seconds", sv.settings_retry_delay.value() if hasattr(sv, "settings_retry_delay") else self.window.auto_retry_delay_seconds)))
        self.window.queue_auto_retry_limit = max(0, int(form.get("queue_auto_retry_limit", sv.settings_queue_retry_limit.value() if hasattr(sv, "settings_queue_retry_limit") else self.window.queue_auto_retry_limit)))
        self.window.max_concurrent = max(1, int(form.get("max_concurrent", sv.settings_concurrent.value() if hasattr(sv, "settings_concurrent") else self.window.max_concurrent)))
        self.window._search_history_limit = max(5, int(form.get("search_history_limit", sv.settings_search_history_limit.value() if hasattr(sv, "settings_search_history_limit") else self.window._search_history_limit)))
        self.window.search_history_ttl_days = max(1, int(form.get("search_history_ttl_days", sv.settings_search_history_ttl_days.value() if hasattr(sv, "settings_search_history_ttl_days") else self.window.search_history_ttl_days)))
        self.window.search_history = self.window._normalize_search_history(self.window.search_history)
        self.window.search_history_model.setStringList([entry["url"] for entry in self.window.search_history])
        self.window.thumbnail_cache_max = max(50, int(form.get("thumbnail_cache_max", sv.settings_thumbnail_cache_max.value() if hasattr(sv, "settings_thumbnail_cache_max") else self.window.thumbnail_cache_max)))
        self.window.clean_metadata_enabled = bool(form.get("clean_metadata", getattr(self.window, "clean_metadata_enabled", True)))
        self.window._trim_thumbnail_cache()
        self.window.storage_guard_enabled = bool(form.get("storage_guard_enabled", sv.settings_storage_guard.isChecked() if hasattr(sv, "settings_storage_guard") else self.window.storage_guard_enabled))
        self.window.storage_min_free_gb = max(1, int(form.get("storage_min_free_gb", sv.settings_storage_min_free_gb.value() if hasattr(sv, "settings_storage_min_free_gb") else self.window.storage_min_free_gb)))
        if hasattr(sv, "settings_system_theme_sync") or "system_theme_sync_enabled" in form:
            self.set_system_theme_sync_enabled(bool(form.get("system_theme_sync_enabled", sv.settings_system_theme_sync.isChecked() if hasattr(sv, "settings_system_theme_sync") else self.window.system_theme_sync_enabled)))
            if self.window.system_theme_sync_enabled:
                self.refresh_system_theme(force=True)
        if not silent:
            if hasattr(sv, "get_bandwidth_schedule_editor_text"):
                raw_text = sv.get_bandwidth_schedule_editor_text().strip()
            elif hasattr(sv, "settings_bandwidth_schedule_editor"):
                raw_text = sv.settings_bandwidth_schedule_editor.toPlainText().strip()
            else:
                raw_text = ""
            if raw_text:
                try:
                    scheduler.set_schedule(self.get_bandwidth_schedule_editor_rules())
                    self.reload_bandwidth_schedule_editor()
                    if hasattr(sv, "settings_bandwidth_schedule_status"):
                        sv.settings_bandwidth_schedule_status.setText("تم حفظ قواعد جدول السرعة مع بقية الإعدادات")
                except Exception as exc:
                    self.window._warn(f"تعذر تطبيق قواعد جدول السرعة: {exc}")
                    return
        if hasattr(sv, "settings_bandwidth_scheduler") or "bandwidth_scheduler_enabled" in form:
            scheduler.set_enabled(bool(form.get("bandwidth_scheduler_enabled", sv.settings_bandwidth_scheduler.isChecked() if hasattr(sv, "settings_bandwidth_scheduler") else False)))
            self.window.bandwidth_scheduler_enabled = bool(scheduler.enabled)
            self.refresh_bandwidth_schedule(notify=not silent)
        self.window.cookies_path = str(form.get("cookies_path", sv.settings_cookies.text() if hasattr(sv, "settings_cookies") else self.window.cookies_path) or "").strip()
        self.window.current_download_path = out_dir
        self._sync_config_manager({
            "theme": self.window.theme,
            "out_dir": out_dir,
            "max_concurrent": self.window.max_concurrent,
            "use_aria2": bool(form.get("use_aria2", True)),
        })
        self.save_session()
        if not silent:
            self.window._info("تم تطبيق الإعدادات")

    def add_proxy(self):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        if hasattr(sv, "get_proxy_text"):
            proxy = sv.get_proxy_text()
        else:
            proxy = sv.proxy_input.text().strip() if hasattr(sv, "proxy_input") else ""
        if not proxy:
            self.window._warn("Enter a proxy URL first (e.g. http://1.2.3.4:8080)")
            return
        proxy_manager.add_proxy(proxy)
        redacted_proxy = proxy_manager.redact_proxy(proxy)
        self.window._info(f"✅ Proxy added: {redacted_proxy[:40]}")
        logger.info(f"[Proxy] Added: {redacted_proxy}")

    def test_proxy(self):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        if hasattr(sv, "get_proxy_text"):
            proxy = sv.get_proxy_text()
        else:
            proxy = sv.proxy_input.text().strip() if hasattr(sv, "proxy_input") else ""
        if not proxy:
            self.window._warn(_("Enter a proxy URL to test"))
            return
        redacted_proxy = proxy_manager.redact_proxy(proxy)
        self.window._info(_("Testing proxy: {proxy} ...").format(proxy=redacted_proxy[:40]))

        def do_test():
            ok, result = proxy_manager.test_proxy(proxy)
            msg = f"{'✅ OK' if ok else '❌ Failed'}: {result[:80]}"
            if not run_on_qt_main_thread(self.window._show_toast, msg, "info" if ok else "warn"):
                self.window._show_toast(msg, "info" if ok else "warn")
            log_message = f"[Proxy Test] {redacted_proxy} → {msg}"
            if not run_on_qt_main_thread(self.window._append_log, log_message):
                self.window._append_log(log_message)

        threading.Thread(target=do_test, daemon=True).start()

    def apply_sustainability(self):
        sv = getattr(self.window, "settings_view", None)
        if sv is None:
            return
        if hasattr(sv, "get_sustainability_config"):
            config = sv.get_sustainability_config()
            action = str(config.get("action", "") or "").strip()
            delay = int(config.get("delay_seconds", 60) or 60)
        else:
            if not hasattr(sv, "sustainability_combo"):
                return
            raw = sv.sustainability_combo.currentText()
            action = raw.split(" — ")[0].strip()
            delay = sv.sustainability_spin.value() if hasattr(sv, "sustainability_spin") else 60
        try:
            sustainability.configure(action=action, delay_seconds=delay)
            label = sustainability.get_action_label()
            self.window._info(f"⚡ After-queue action set: {label} (delay {delay}s)")
        except Exception as exc:
            self.window._warn(f"Sustainability error: {exc}")

    def export_settings(self):
        payload = self.build_session_payload()
        settings = self._sanitize_settings_for_export(payload.get("settings", {}))
        if not isinstance(settings, dict) or not settings:
            self.window._warn(_("لا توجد إعدادات صالحة لتصديرها"))
            return
        default_path = os.path.join(os.getcwd(), "snapdownloader-settings.json")
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            _("Export Settings"),
            default_path,
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"settings": settings}, f, ensure_ascii=False, indent=2)
            self.window._info(_("تم تصدير الإعدادات بنجاح إلى: {path}").format(path=path))
        except Exception as exc:
            logger.error(f"Failed to export settings: {exc}", exc_info=True)
            self.window._warn(_("فشل تصدير الإعدادات: {err}").format(err=str(exc)))

    def export_settings_qr(self):
        payload = self.build_session_payload()
        settings = self._sanitize_settings_for_export(payload.get("settings", {}))
        if not isinstance(settings, dict) or not settings:
            self.window._warn(_("لا توجد إعدادات صالحة لتصديرها"))
            return
        try:
            text = json.dumps({"settings": settings}, ensure_ascii=False, separators=(",", ":"))
        except Exception as exc:
            logger.error(f"Failed to serialize settings for QR: {exc}", exc_info=True)
            self.window._warn(_("فشل تجهيز الإعدادات لرمز QR: {err}").format(err=str(exc)))
            return
        try:
            import qrcode
        except Exception:
            self.window._warn(_("حزمة qrcode غير متوفرة. ثبّت qrcode عبر pip أو استخدم Export Settings العادي."))
            return
        try:
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
            qr.add_data(text)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
        except Exception as exc:
            logger.error(f"Failed to build QR image: {exc}", exc_info=True)
            self.window._warn(_("فشل إنشاء رمز QR: {err}").format(err=str(exc)))
            return
        try:
            tmp_dir = os.path.join(get_app_data_dir(), "qr")
            os.makedirs(tmp_dir, exist_ok=True)
            path = os.path.join(tmp_dir, "settings-qr.png")
            img.save(path)
        except Exception as exc:
            logger.error(f"Failed to save QR image: {exc}", exc_info=True)
            self.window._warn(_("فشل حفظ ملف QR: {err}").format(err=str(exc)))
            return
        try:
            from PySide6.QtGui import QPixmap
            from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton
        except Exception:
            self.window._info(_("تم إنشاء رمز QR في الملف: {path}").format(path=path))
            return
        try:
            dialog = QDialog(self.window)
            dialog.setWindowTitle(_("Settings QR"))
            layout = QVBoxLayout(dialog)
            lbl = QLabel()
            pix = QPixmap(path)
            lbl.setPixmap(pix)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(lbl)
            info_lbl = QLabel(_("امسح رمز QR لنقل الإعدادات إلى جهاز آخر"))
            info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(info_lbl)
            close_btn = QPushButton(_("إغلاق"))
            close_btn.clicked.connect(dialog.accept)
            layout.addWidget(close_btn)
            dialog.exec()
        except Exception as exc:
            logger.error(f"Failed to show QR dialog: {exc}", exc_info=True)
            self.window._info(_("تم إنشاء رمز QR في الملف: {path}").format(path=path))

    def _sanitize_settings_for_export(self, settings: dict) -> dict:
        if not isinstance(settings, dict):
            return {}
        sanitized = dict(settings)
        # Exclude local/sensitive data that is not useful for sharing settings exports.
        for key in ("search_history", "cookies_path", "proxy", "proxy_value"):
            sanitized.pop(key, None)
        return sanitized

    def import_settings(self):
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            _("Import Settings"),
            os.getcwd(),
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("Invalid settings file format")
            settings = data.get("settings", data)
            if not isinstance(settings, dict):
                raise ValueError("Invalid settings payload")
        except Exception as exc:
            logger.error(f"Failed to import settings: {exc}", exc_info=True)
            self.window._warn(_("فشل استيراد الإعدادات: {err}").format(err=str(exc)))
            return
        payload = {"queue_items": [], "settings": settings}
        try:
            self.apply_session_payload(payload)
            self.save_session(sync=True)
            self.window._info(_("تم استيراد الإعدادات وتطبيقها بنجاح"))
        except Exception as exc:
            logger.error(f"Failed to apply imported settings: {exc}", exc_info=True)
            self.window._warn(_("فشل تطبيق الإعدادات المستوردة: {err}").format(err=str(exc)))

    def session_path(self):
        app_data_dir = get_app_data_dir()
        os.makedirs(app_data_dir, exist_ok=True)
        return os.path.join(app_data_dir, "xd_session.json")

    def migrate_legacy_session_json(self):
        session_service = getattr(self.window, "session_service", None)
        if session_service is not None and hasattr(session_service, "migrate_legacy_session_json"):
            session_service.migrate_legacy_session_json()
            return
        legacy_path = self.session_path()
        migrated_path = legacy_path + ".migrated"
        if not os.path.exists(legacy_path) or os.path.exists(migrated_path):
            return
        try:
            with open(legacy_path, "r", encoding="utf-8") as file:
                payload = json.load(file)
            if not isinstance(payload, dict):
                return
            queue_items = payload.get("queue_items", [])
            settings = payload.get("settings", {})
            if isinstance(queue_items, list):
                save_queue_items(queue_items)
            if isinstance(settings, dict):
                save_session_settings(settings)
            os.rename(legacy_path, migrated_path)
            logger.info("[DB] Legacy session migrated to SQLite")
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(f"[DB] Session migration failed: {exc}")

    def session_save_loop(self):
        session_service = getattr(self.window, "session_service", None)
        if session_service is not None and hasattr(session_service, "start"):
            session_service.start()
            return
        self._ensure_session_sync_state()
        try:
            while not self.window._session_save_shutdown:
                self.window._session_save_event.wait(timeout=1.0)
                self.window._session_save_event.clear()
                if self.window._session_save_shutdown:
                    break
                while True:
                    with self.window._session_save_lock:
                        payload = self.window._session_save_payload
                        self.window._session_save_payload = None
                    if payload is None:
                        break
                    self.write_session_payload(payload)
        finally:
            close_thread_connection()

    def build_session_payload(self) -> dict:
        queue_manager = getattr(self.window, "queue_manager", None)
        queue_revision = None
        if queue_manager is not None and hasattr(queue_manager, "get_change_token"):
            try:
                queue_revision = int(queue_manager.get_change_token())
            except Exception:
                queue_revision = None
        # When QueueManager exposes a revision token, SessionService can
        # decide whether persistence is needed without forcing a full snapshot copy.
        queue_items = None
        if queue_revision is None:
            queue_items = []
            for item in self.window.queue_manager.get_queue_items_snapshot():
                if isinstance(item, dict):
                    queue_items.append(dict(item))
                else:
                    queue_items.append(item)
        return {
            "queue_items": queue_items,
            "queue_revision": queue_revision,
            "settings": self.build_settings_payload(),
        }

    def build_settings_payload(self) -> dict:
        sv = getattr(self.window, "settings_view", None)
        if sv is not None and hasattr(sv, "get_form_settings"):
            form = sv.get_form_settings()
        else:
            form = {}
        return {
            "out_dir": str(form.get("out_dir") or (self.window.search_view.out_dir_input.text().strip() if hasattr(self.window.search_view, "out_dir_input") else "")),
            "retries": int(form.get("retries", 3) or 3),
            "auto_retry_delay_seconds": int(form.get("auto_retry_delay_seconds", self.window.auto_retry_delay_seconds) or self.window.auto_retry_delay_seconds),
            "queue_auto_retry_limit": int(form.get("queue_auto_retry_limit", self.window.queue_auto_retry_limit) or self.window.queue_auto_retry_limit),
            "use_aria2": bool(form.get("use_aria2", bool(self.window.search_view.aria2_checkbox.isChecked()) if hasattr(self.window.search_view, "aria2_checkbox") else True)),
            "use_ytdlp_api": bool(form.get("use_ytdlp_api", False)),
            "queue_priority": str(form.get("queue_priority", "fifo") or "fifo"),
            "max_concurrent": int(form.get("max_concurrent", self.window.max_concurrent) or self.window.max_concurrent),
            "search_history_limit": int(form.get("search_history_limit", self.window._search_history_limit) or self.window._search_history_limit),
            "search_history_ttl_days": int(form.get("search_history_ttl_days", self.window.search_history_ttl_days) or self.window.search_history_ttl_days),
            "thumbnail_cache_max": int(form.get("thumbnail_cache_max", self.window.thumbnail_cache_max) or self.window.thumbnail_cache_max),
            "clean_metadata": bool(form.get("clean_metadata", getattr(self.window, "clean_metadata_enabled", True))),
            "storage_guard_enabled": bool(form.get("storage_guard_enabled", self.window.storage_guard_enabled)),
            "storage_min_free_gb": int(form.get("storage_min_free_gb", self.window.storage_min_free_gb) or self.window.storage_min_free_gb),
            "system_theme_sync_enabled": bool(getattr(self.window, "system_theme_sync_enabled", True)),
            "bandwidth_schedule_grid": list(form.get("bandwidth_schedule_grid", scheduler.schedule) or []),
            "cookies_path": self._build_persisted_cookies_path(form),
            "cookie_profile_name": str(form.get("cookie_profile_name", "") or ""),
            "cookies_from_browser": str(form.get("cookies_from_browser", "none") or "none").strip().lower(),
            "post_download_script": str(form.get("post_download_script", "") or ""),
            "post_process_pipeline": list(form.get("post_process_pipeline", []) or []),
            "theme": str(self.window.theme or "dark"),
            "embed_subs": bool(form.get("embed_subs", True)),
            "whisper_fallback": bool(form.get("whisper_fallback", False)),
            "split_chapters": bool(form.get("split_chapters", False)),
            "verify_checksum": bool(form.get("verify_checksum", False)),
            "virus_scan_after_download": bool(form.get("virus_scan_after_download", False)),
            "sponsorblock_enabled": bool(form.get("sponsorblock_enabled", DEFAULT_SETTINGS.get("sponsorblock_enabled", False))),
            "normalize_audio_postprocess": bool(form.get("normalize_audio_postprocess", DEFAULT_SETTINGS.get("normalize_audio_postprocess", False))),
            "auto_categorize_downloads": bool(form.get("auto_categorize_downloads", DEFAULT_SETTINGS.get("auto_categorize_downloads", False))),
            "auto_categorize_mode": str(form.get("auto_categorize_mode", DEFAULT_SETTINGS.get("auto_categorize_mode", "off")) or DEFAULT_SETTINGS.get("auto_categorize_mode", "off")).strip(),
            "rename_template": str(form.get("rename_template", "Default") or "Default"),
            "proxy_enabled": bool(form.get("proxy_enabled", False)),
            "use_native_engine": bool(form.get("use_native_engine", True)),
            "trial_started_at": str(self.window.trial_started_at or ""),
            "trial_total_days": int(self.window.trial_total_days),
            "search_history": list(self.window.search_history),
            "ui_language": str(getattr(self.window, "ui_language", i18n.current_lang)),
            # M-16: Advanced Merging Settings
            "custom_merge_enabled": bool(form.get("custom_merge_enabled", False)),
            "custom_merge_video_codec": str(form.get("custom_merge_video_codec", "copy") or "copy"),
            "custom_merge_video_crf": int(form.get("custom_merge_video_crf", 23) or 23),
            "custom_merge_audio_codec": str(form.get("custom_merge_audio_codec", "aac") or "aac"),
            "custom_merge_audio_bitrate": str(form.get("custom_merge_audio_bitrate", "192k") or "192k"),
            "custom_merge_hw_encoder": str(form.get("custom_merge_hw_encoder", "off") or "off"),
            "custom_merge_force_reencode": bool(form.get("custom_merge_force_reencode", False)),
            "custom_merge_video_preset": str(form.get("custom_merge_video_preset", "p5") or "p5"),
        }

    def save_settings_only(self):
        if hasattr(self.window, "session_service"):
            self.window.session_service.save_session(sync=False)

    def save_session(self, sync: bool = False):
        if hasattr(self.window, "session_service"):
            self.window.session_service.save_session(sync=sync)

    def read_session_payload(self) -> dict:
        session_service = getattr(self.window, "session_service", None)
        if session_service is not None and hasattr(session_service, "read_session_payload"):
            return session_service.read_session_payload()
        self._ensure_session_sync_state()
        with self.window._session_save_write_lock:
            return {
                "queue_items": load_queue_items(),
                "settings": load_session_settings(),
            }

    def apply_session_payload(self, payload: dict):
        queue_items = payload.get("queue_items")
        if isinstance(queue_items, list):
            def _scan_resume_from_disk(path_value: str) -> dict | None:
                p = str(path_value or "").strip()
                if not p:
                    return None
                try:
                    ap = os.path.abspath(p)
                except Exception:
                    ap = p
                base_no_ext, _ext = os.path.splitext(ap)
                candidates = [
                    ap + ".part",
                    ap + ".ytdl",
                    ap + ".aria2",
                    base_no_ext + ".ytdl",
                    base_no_ext + ".aria2",
                ]
                found = []
                for c in candidates:
                    try:
                        if c and os.path.isfile(c):
                            found.append(os.path.abspath(c))
                    except Exception:
                        continue
                try:
                    for c in glob.glob(base_no_ext + ".*"):
                        if not c or not os.path.isfile(c):
                            continue
                        lc = c.lower()
                        if lc.endswith((".part", ".ytdl", ".aria2", ".tmp", ".temp")):
                            found.append(os.path.abspath(c))
                except Exception:
                    pass
                if not found:
                    return None
                unique = []
                seen = set()
                total_bytes = 0
                for c in found:
                    if c in seen:
                        continue
                    seen.add(c)
                    try:
                        size = int(os.path.getsize(c))
                    except Exception:
                        size = 0
                    try:
                        mtime = float(os.path.getmtime(c))
                    except Exception:
                        mtime = 0.0
                    total_bytes += max(0, size)
                    unique.append({"path": c, "size": size, "mtime": mtime})
                return {
                    "output_path": ap,
                    "partials_total_bytes": int(total_bytes),
                    "partials_count": int(len(unique)),
                    "partials": unique,
                    "updated_at": datetime.now().timestamp(),
                }

            for item in queue_items:
                if normalize_task_status(item.get("status")) == TaskStatus.RUNNING.value:
                    item["status"] = TaskStatus.PENDING.value
                raw_resume = str(item.get("resume_json", "") or "").strip()
                if raw_resume:
                    try:
                        parsed = json.loads(raw_resume)
                        if isinstance(parsed, dict):
                            item["resume"] = parsed
                    except Exception:
                        item["resume_json"] = ""
                disk_resume = _scan_resume_from_disk(item.get("last_output_path", ""))
                if isinstance(disk_resume, dict):
                    item["resume"] = disk_resume
                    try:
                        item["resume_json"] = json.dumps(disk_resume, ensure_ascii=False)
                    except Exception:
                        item["resume_json"] = ""
            self.window.queue_manager.add_tasks(queue_items)
        settings = payload.get("settings", {})
        if isinstance(settings, dict):
            sv = getattr(self.window, "settings_view", None)
            search_view = getattr(self.window, "search_view", None)
            out_dir = str(settings.get("out_dir", "")).strip()
            if out_dir:
                self.window.current_download_path = out_dir
                if search_view is not None and hasattr(search_view, "set_out_dir"):
                    search_view.set_out_dir(out_dir)
                elif search_view is not None and hasattr(search_view, "out_dir_input"):
                    search_view.out_dir_input.setText(out_dir)

            self.window.auto_retry_delay_seconds = max(1, int(settings.get("auto_retry_delay_seconds", self.window.auto_retry_delay_seconds)))
            self.window.queue_auto_retry_limit = max(0, int(settings.get("queue_auto_retry_limit", self.window.queue_auto_retry_limit)))
            self.window._search_history_limit = max(5, int(settings.get("search_history_limit", self.window._search_history_limit)))
            self.window.search_history_ttl_days = max(1, int(settings.get("search_history_ttl_days", self.window.search_history_ttl_days)))
            self.window.thumbnail_cache_max = max(50, int(settings.get("thumbnail_cache_max", self.window.thumbnail_cache_max)))
            self.window.clean_metadata_enabled = bool(settings.get("clean_metadata", getattr(self.window, "clean_metadata_enabled", True)))
            self.window._trim_thumbnail_cache()

            use_aria2 = bool(settings.get("use_aria2", True))
            if search_view is not None and hasattr(search_view, "set_aria2_checked"):
                search_view.set_aria2_checked(use_aria2)
            elif search_view is not None and hasattr(search_view, "aria2_checkbox"):
                search_view.aria2_checkbox.setChecked(use_aria2)
            if search_view is not None and hasattr(search_view, "post_script_input"):
                search_view.post_script_input.setText(str(settings.get("post_download_script", "") or "").strip())

            self.window.max_concurrent = max(1, int(settings.get("max_concurrent", self.window.max_concurrent)))
            self.window.storage_guard_enabled = bool(settings.get("storage_guard_enabled", self.window.storage_guard_enabled))
            self.window.storage_min_free_gb = max(1, int(settings.get("storage_min_free_gb", self.window.storage_min_free_gb)))

            self.set_system_theme_sync_enabled(
                bool(settings.get("system_theme_sync_enabled", getattr(self.window, "system_theme_sync_enabled", True)))
            )

            self.window.cookies_path = self._resolve_cookies_path(settings.get("cookies_path", self.window.cookies_path))
            profile_name = str(settings.get("cookie_profile_name", "") or "").strip()
            if profile_name and not self.window.cookies_path:
                profile_path = str(self.cookie_profiles.get_profile_path(profile_name) or "").strip()
                if profile_path:
                    self.window.cookies_path = profile_path

            legacy_proxy_value = str(settings.get("proxy_value", "")).strip()
            current_proxy_value = proxy_manager.get_current_proxy() or ""
            if legacy_proxy_value and not current_proxy_value:
                proxy_manager.add_proxy(legacy_proxy_value)
                current_proxy_value = proxy_manager.get_current_proxy() or legacy_proxy_value
                self.save_session()

            if sv is not None and hasattr(sv, "apply_form_settings"):
                to_apply = dict(settings)
                to_apply["auto_retry_delay_seconds"] = int(self.window.auto_retry_delay_seconds)
                to_apply["queue_auto_retry_limit"] = int(self.window.queue_auto_retry_limit)
                to_apply["search_history_limit"] = int(self.window._search_history_limit)
                to_apply["search_history_ttl_days"] = int(self.window.search_history_ttl_days)
                to_apply["thumbnail_cache_max"] = int(self.window.thumbnail_cache_max)
                to_apply["clean_metadata"] = bool(self.window.clean_metadata_enabled)
                to_apply["max_concurrent"] = int(self.window.max_concurrent)
                to_apply["storage_guard_enabled"] = bool(self.window.storage_guard_enabled)
                to_apply["storage_min_free_gb"] = int(self.window.storage_min_free_gb)
                to_apply["system_theme_sync_enabled"] = bool(getattr(self.window, "system_theme_sync_enabled", True))
                to_apply["bandwidth_schedule_grid"] = list(settings.get("bandwidth_schedule_grid", scheduler.schedule) or scheduler.schedule)
                to_apply["cookies_path"] = str(self.window.cookies_path or "")
                to_apply["cookie_profile_name"] = profile_name
                to_apply["post_download_script"] = str(settings.get("post_download_script", "") or "")
                to_apply["post_process_pipeline"] = list(settings.get("post_process_pipeline", []) or [])
                to_apply["use_aria2"] = bool(use_aria2)
                to_apply["proxy"] = str(current_proxy_value or "")
                sv.apply_form_settings(to_apply, block_signals=True)
            self.refresh_cookie_profiles_ui(selected_name=profile_name)
            self.window.trial_started_at = str(settings.get("trial_started_at", self.window.trial_started_at)).strip() or self.window.trial_started_at
            self.window.trial_total_days = max(1, int(settings.get("trial_total_days", self.window.trial_total_days)))
            self.window._recompute_trial_days()
            self.window.search_history = self.window._normalize_search_history(settings.get("search_history", self.window.search_history))
            self.window.search_history_model.setStringList([e["url"] for e in self.window.search_history])
            self.set_ui_language(
                str(settings.get("ui_language", getattr(self.window, "ui_language", i18n.current_lang))),
                persist=False,
                notify=False,
            )
            theme = str(settings.get("theme", self.window.theme))
            if self.window.system_theme_sync_enabled:
                self.refresh_system_theme(force=True)
            elif theme in THEMES:
                self.apply_theme(theme, persist=False)
            self._sync_config_manager({
                "theme": self.window.theme,
                "out_dir": str(settings.get("out_dir", self.window.current_download_path)),
                "max_concurrent": self.window.max_concurrent,
                "use_aria2": bool(settings.get("use_aria2", True)),
            })
        pending_after_restore = [
            q for q in self.window.queue_manager.get_queue_items_snapshot()
            if normalize_task_status(q.get("status")) == TaskStatus.PENDING.value
        ]
        if pending_after_restore:
            QTimer.singleShot(900, self.window._start_queue_download)
            self.window._append_log(f"تم استرجاع {len(pending_after_restore)} عنصر من الجلسة")
        self.window._refresh_downloads_list()

    def load_session_async(self):
        session_service = getattr(self.window, "session_service", None)
        if session_service is not None and hasattr(session_service, "load_session_async"):
            session_service.load_session_async()
            return
        if self.window._session_load_thread is not None and self.window._session_load_thread.is_alive():
            return

        def _worker():
            try:
                payload = self.read_session_payload()
                self.window.session_loaded_ui.emit({"ok": True, "payload": payload, "error": ""})
            except Exception as exc:
                self.window.session_loaded_ui.emit({"ok": False, "payload": None, "error": str(exc)})
            finally:
                close_thread_connection()

        self.window._session_load_thread = threading.Thread(target=_worker, daemon=True, name="SessionLoadWorker")
        self.window._session_load_thread.start()

    def handle_session_load_result(self, result: dict):
        if not result.get("ok"):
            self.window._append_log(f"تعذر تحميل الجلسة: {result.get('error', 'Unknown error')}")
            return
        try:
            self.apply_session_payload(result.get("payload") or {})
        except Exception as apply_exc:
            self.window._append_log(f"تعذر تطبيق بيانات الجلسة: {apply_exc}")


