
from core.utils import get_app_data_dir, redact_url
import json
import logging
import os
import glob
import re
import sys
import threading
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
from core.anti_detection import anti_detection_engine
from core.bandwidth_scheduler import scheduler
from core.config import DEFAULT_SETTINGS, THEME_MODE_MAP, default_download_dir, estimate_file_size_bytes
from core.cookie_importer import auto_detect_and_export, encrypt_cookie_file_inplace
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
from core.sustainability import sustainability
from core.i18n import i18n, _
from core.media_size import apply_estimated_size
from core.error_handler import ErrorHandler
from core.post_actions import PostDownloadManager
from core.task_types import DownloadTask
from core.workers import AnalyzeWorker
from ui.themes import THEMES

logger = logging.getLogger("SnapDownloader")


class ExtensionController:
    def __init__(self, window):
        self.window = window

    def handle_extension_link(self, payload: dict):
        data = payload if isinstance(payload, dict) else {}
        url = str(data.get("url", "")).strip()
        if not url:
            return
        auto_download = bool(data.get("auto_download", True))
        fmt = str(data.get("format", "MP4")).strip() or "MP4"
        quality = str(data.get("quality", "1080p")).strip() or "1080p"
        subtitle = str(data.get("subtitle", "None")).strip() or "None"
        bandwidth_limit_kbps = max(0, int(data.get("bandwidth_limit_kbps", 0) or 0))
        post_action_raw = str(data.get("post_action", "none")).strip().lower() or "none"
        post_action = PostDownloadManager.normalize_action(post_action_raw, extension_safe=True)
        if post_action != post_action_raw:
            logger.warning(f"[Extension] Ignored unsafe post_action '{post_action_raw}'.")
        # Security hardening: browser extensions must not provide local script paths.
        requested_script = str(data.get("post_download_script", "")).strip()
        if requested_script:
            logger.warning("[Extension] Ignored post_download_script from extension payload.")
        schedule_repeat = str(data.get("schedule_repeat", "none")).strip() or "none"
        title = str(data.get("title", "")).strip()
        thumbnail = str(data.get("thumbnail", "")).strip()
        if not auto_download:
            sv = getattr(self.window, "search_view", None)
            if sv is not None and hasattr(sv, "set_url"):
                sv.set_url(url)
            else:
                self.window.search_view.url_input.setText(url)
            self.window._start_analyze()
            self.window._switch_view("search")
            return
        task: DownloadTask = self.window._build_task(
            url=url,
            title=title,
            thumbnail=thumbnail,
            fmt=fmt,
            quality=quality,
        )
        duration_seconds = int(data.get("duration_seconds", data.get("duration", 0)) or 0)
        try:
            task = self.window._normalize_task(
                task,
                subtitle=subtitle,
                duration_seconds=duration_seconds,
            )
        except TypeError:
            task = self.window._normalize_task(task, subtitle=subtitle)
            if isinstance(task, dict):
                task["duration_seconds"] = duration_seconds
        apply_estimated_size(task, data)
        task["bandwidth_limit_kbps"] = bandwidth_limit_kbps
        task["post_action"] = post_action
        task["post_download_script"] = ""
        task["schedule_repeat"] = schedule_repeat
        idx = self.window.queue_manager.add_task(task)
        if idx == -1:
            self.window._warn("تعذر إضافة الرابط من الإضافة: الطابور ممتلئ")
            return
        limit_text = self.window._format_bandwidth_limit(bandwidth_limit_kbps)
        self.window._append_log(f"[Extension] Added URL from browser: {redact_url(url)} | limit={limit_text}")
        self.window._switch_view("downloads")
        self.window._set_downloads_filter("queued")
        if not self.window.queue_running:
            self.window._start_queue_download()


