
from core.utils import get_app_data_dir
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

from core.qt_compat import Qt, QNetworkReply, QNetworkRequest, QPainter, QPainterPath, QPixmap, QUrl

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
from core.playlist_sync_scheduler import PlaylistSyncScheduler
from core.playlist_sync_service import PlaylistSyncService
from core.proxy_manager import proxy_manager
from core.media_size import apply_estimated_size
from core.storage_watchdog import format_bytes, has_enough_space
from core.sustainability import sustainability
from core.i18n import i18n, _
from core.error_handler import ErrorHandler
from core.task_types import DownloadTask, TaskStatus, normalize_task_status
from core.workers import AnalyzeWorker
from ui.themes import THEMES

logger = logging.getLogger("SnapDownloader")
_playlist_sync_service = PlaylistSyncService()


# Backward-compat wrappers retained for tests/integrations that monkeypatch
# analyze_controller.* playlist sync call sites directly.
def get_playlist_known_ids(playlist_url: str) -> set[str]:
    return _playlist_sync_service.get_known_ids(playlist_url)


def upsert_playlist_entries(playlist_url: str, entries: list[dict], payload: dict | None = None) -> None:
    _playlist_sync_service.upsert_entries(playlist_url, entries, payload=payload, sync_status="syncing")


def diff_playlist_entries(playlist_url: str, current_entry_ids: list[str]) -> dict:
    return _playlist_sync_service.diff_entries(playlist_url, current_entry_ids)


def sync_playlist_snapshot(playlist_url: str, current_entry_ids: list[str], payload: dict | None = None) -> int:
    return _playlist_sync_service.sync_snapshot(playlist_url, current_entry_ids, payload=payload)


class AnalyzeController:
    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, window):
        self.window = window
        self._ensure_search_timers()
        self._playlist_cached_ids_before_fetch: set[str] = set()
        self._playlist_seen_ids_current_fetch: set[str] = set()
        self._playlist_new_ids_current_fetch: set[str] = set()
        self._playlist_removed_ids_current_fetch: set[str] = set()
        self._playlist_reanalyze_mode = False
        self._playlist_sync_scheduler = PlaylistSyncScheduler(
            window=self.window,
            playlist_sync_service=_playlist_sync_service,
            has_active_process_callback=self.has_active_process,
            on_due_playlist_callback=self._start_background_sync_for_playlist,
        )
        self._playlist_sync_scheduler.start()

    def _ensure_search_timers(self):
        if not hasattr(self.window, "search_spinner_timer") or self.window.search_spinner_timer is None:
            self.window.search_spinner_timer = QTimer(self.window)
            self.window.search_spinner_timer.timeout.connect(self.window._update_search_spinner)
        if not hasattr(self.window, "_search_timeout_timer") or self.window._search_timeout_timer is None:
            self.window._search_timeout_timer = QTimer(self.window)
            self.window._search_timeout_timer.setSingleShot(True)
            self.window._search_timeout_timer.timeout.connect(self.window._force_reset_search_ui)

    def stop_playlist_sync_scheduler(self) -> None:
        try:
            self._playlist_sync_scheduler.stop()
        except Exception as exc:
            logger.debug(f"[AnalyzeController] failed to stop playlist sync scheduler: {exc}")

    def _start_background_sync_for_playlist(self, playlist_url: str) -> None:
        if not playlist_url:
            return
        self.window._append_log("🔄 بدء مزامنة Playlist تلقائية في الخلفية...")
        try:
            self.start_worker(
                playlist_url,
                lambda success, message, payload, items, expected_url=playlist_url: self._on_background_playlist_sync_finished(
                    expected_url, success, message, payload, items
                ),
                background_sync=True,
            )
        except Exception:
            _playlist_sync_service.release_inflight_sync(playlist_url)
            raise

    def _on_background_playlist_sync_finished(
        self,
        expected_url: str,
        success: bool,
        message: str,
        payload: dict,
        items: list,
    ):
        self.window.analyze_worker = None
        playlist_url = _playlist_sync_service.extract_playlist_url(
            payload if isinstance(payload, dict) else {},
            fallback_url=expected_url,
        )
        _playlist_sync_service.release_inflight_sync(expected_url)
        if playlist_url and playlist_url != expected_url:
            _playlist_sync_service.release_inflight_sync(playlist_url)
        if not success:
            msg_text = str(message or "").strip() or "فشل مزامنة Playlist الخلفية"
            if playlist_url:
                try:
                    _playlist_sync_service.mark_sync_failed(playlist_url, msg_text, payload=payload)
                except Exception as exc:
                    logger.debug(f"[AnalyzeController] background playlist sync failure update failed: {exc}")
            self.window._record_error(
                msg_text,
                url=playlist_url or expected_url,
                title="Background Playlist Sync",
                source="playlist_sync",
            )
            self.window._append_log(f"⚠️ {msg_text}")
            return
        if payload.get("kind") == "playlist":
            self._mark_seen_ids(items)
            self._persist_playlist_cache_entries(payload, items)
            self._finalize_playlist_diff_state()
        self.window._append_log(str(message or "تمت مزامنة Playlist في الخلفية بنجاح").strip())

    def has_active_process(self) -> bool:
        worker = getattr(self.window, "analyze_worker", None)
        if worker is None:
            return False
        try:
            return bool(worker.isRunning())
        except Exception:
            # Stale/deleted Qt worker reference: clear it so search can proceed.
            self.window.analyze_worker = None
            return False

    def stop_analyze_worker(self):
        worker = getattr(self.window, "analyze_worker", None)
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
                worker.wait(2000)
                if worker.isRunning():
                    logger.warning("عامل التحليل لم يتوقف خلال المهلة بعد requestInterruption()")
        except Exception as exc:
            logger.debug(f"تعذر إيقاف عامل التحليل الحالي: {exc}")
        self.window.analyze_worker = None

    def start_worker(self, url: str, finished_handler, *, background_sync: bool = False):
        self.stop_analyze_worker()
        self._current_playlist_url = str(url or "").strip()
        self._prepare_playlist_diff_state(self._current_playlist_url)
        if self._current_playlist_url and (
            "list=" in self._current_playlist_url or "/playlist" in self._current_playlist_url
        ):
            try:
                backoff = _playlist_sync_service.get_backoff_info(self._current_playlist_url)
                if bool(backoff.get("is_active")):
                    remaining = int(backoff.get("remaining_seconds", 0) or 0)
                    failures = int(backoff.get("consecutive_failures", 0) or 0)
                    self.window._append_log(
                        f"ℹ️ مزامنة القائمة في وضع backoff ({failures} فشل متتالي). المتبقي ~{remaining}s"
                    )
                _playlist_sync_service.mark_sync_started(self._current_playlist_url, payload={"url": self._current_playlist_url})
            except Exception as exc:
                logger.debug(f"[AnalyzeController] playlist sync start failed: {exc}")
        playlist_view = getattr(self.window, "playlist_view", None)
        if (not background_sync) and playlist_view is not None and hasattr(playlist_view, "playlist_items"):
            self.window.playlist_items = playlist_view.playlist_items
        elif not background_sync:
            self.window.playlist_items = []
        if (not background_sync) and self._current_playlist_url and (
            "list=" in self._current_playlist_url or "/playlist" in self._current_playlist_url
        ):
            if playlist_view is not None and hasattr(playlist_view, "prepare_for_playlist_fetch"):
                preserve_hint = bool(self._playlist_cached_ids_before_fetch)
                try:
                    self._playlist_reanalyze_mode = bool(
                        playlist_view.prepare_for_playlist_fetch(
                            self._current_playlist_url,
                            preserve_existing=preserve_hint,
                        )
                    )
                except Exception:
                    self._playlist_reanalyze_mode = False
            else:
                self._playlist_reanalyze_mode = False
        else:
            self._playlist_reanalyze_mode = False
        self.window.analyze_worker = AnalyzeWorker(
            url,
            cookies_file=getattr(self.window, "cookies_path", ""),
            extra_args=anti_detection_engine.get_yt_dlp_analysis_options(),
        )
        self.window.analyze_worker.playlist_chunk.connect(self._on_playlist_chunk_received)
        self.window.analyze_worker.finished.connect(finished_handler)
        self.window.analyze_worker.start()

    def _apply_playlist_analysis_result(self, payload: dict, items: list):
        self._mark_seen_ids(items)
        playlist_url = _playlist_sync_service.extract_playlist_url(
            payload if isinstance(payload, dict) else {},
            fallback_url=getattr(self, "_current_playlist_url", ""),
        )
        if playlist_url:
            try:
                _playlist_sync_service.mark_sync_started(playlist_url, payload=payload)
            except Exception as exc:
                logger.debug(f"[AnalyzeController] playlist sync metadata refresh failed: {exc}")
        self._persist_playlist_cache_entries(payload, items)
        diff_state = self._finalize_playlist_diff_state()
        new_entry_ids = diff_state["new_ids"]
        removed_entry_ids = diff_state["removed_ids"]
        playlist_view = self.window.playlist_view
        candidate_items = list(items or [])
        if self._playlist_reanalyze_mode and candidate_items:
            filtered_items = []
            for item in candidate_items:
                entry_id = self._extract_entry_id(item if isinstance(item, dict) else {})
                if entry_id and entry_id in new_entry_ids:
                    filtered_items.append(item)
            candidate_items = filtered_items
        if candidate_items:
            playlist_view.set_playlist_data(
                payload,
                candidate_items,
                new_entry_ids=new_entry_ids,
                removed_entry_ids=removed_entry_ids,
                reset=not self._playlist_reanalyze_mode,
            )
            self.window.playlist_items = playlist_view.playlist_items
            return
        if self._playlist_reanalyze_mode and removed_entry_ids:
            playlist_view.remove_entries_by_ids(removed_entry_ids)
        self.window.playlist_items = playlist_view.playlist_items
        playlist_view.finalize_playlist_data(
            payload,
            len(self.window.playlist_items),
            new_entry_ids=new_entry_ids,
            removed_entry_ids=removed_entry_ids,
        )

    def _mark_seen_ids(self, items: list | None):
        if not items:
            return
        for item in items:
            entry_id = self._extract_entry_id(item if isinstance(item, dict) else {})
            if entry_id:
                self._playlist_seen_ids_current_fetch.add(entry_id)

    def _persist_playlist_cache_entries(self, payload: dict | None, items: list | None):
        entries = [item for item in (items or []) if isinstance(item, dict)]
        if not entries:
            return
        playlist_url = str(
            (payload or {}).get("url")
            or (payload or {}).get("webpage_url")
            or getattr(self, "_current_playlist_url", "")
            or ""
        ).strip()
        if not playlist_url:
            return
        try:
            _playlist_sync_service.mark_sync_started(playlist_url, payload=payload)
            if payload:
                _playlist_sync_service.upsert_entries(playlist_url, entries, payload=payload, sync_status="syncing")
            else:
                upsert_playlist_entries(playlist_url, entries)
        except Exception as exc:
            logger.debug(f"[AnalyzeController] playlist_cache upsert(finalize) failed: {exc}")

    def _on_playlist_chunk_received(self, payload: dict, items: list):
        if not isinstance(payload, dict) or payload.get("kind") != "playlist":
            return
        chunk = [item for item in (items or []) if isinstance(item, dict)]
        if not chunk:
            return
        # Persist to playlist_cache for differential sync
        playlist_url = str(
            payload.get("url") or payload.get("webpage_url")
            or getattr(self, "_current_playlist_url", "")
            or ""
        ).strip()
        new_chunk: list[dict] = []
        new_ids_chunk: set[str] = set()
        seen_ids_chunk: set[str] = set()
        for item in chunk:
            entry_id = self._extract_entry_id(item)
            if entry_id:
                seen_ids_chunk.add(entry_id)
                if entry_id not in self._playlist_cached_ids_before_fetch:
                    self._playlist_new_ids_current_fetch.add(entry_id)
                    new_ids_chunk.add(entry_id)
            if not self._playlist_reanalyze_mode:
                new_chunk.append(item)
            elif entry_id and entry_id in new_ids_chunk:
                new_chunk.append(item)
        self._playlist_seen_ids_current_fetch |= seen_ids_chunk
        if playlist_url:
            try:
                _playlist_sync_service.mark_sync_started(playlist_url, payload=payload)
                if payload:
                    _playlist_sync_service.upsert_entries(playlist_url, chunk, payload=payload, sync_status="syncing")
                else:
                    upsert_playlist_entries(playlist_url, chunk)
            except Exception as exc:
                logger.debug(f"[AnalyzeController] playlist_cache upsert failed: {exc}")
        if new_chunk:
            if _playlist_sync_service.has_inflight_sync(getattr(self, "_current_playlist_url", "")):
                return
            self.window.playlist_view.append_playlist_items(
                payload,
                new_chunk,
                new_entry_ids=new_ids_chunk,
            )

    def stop_search_timers(self):
        if hasattr(self.window, "_search_timeout_timer"):
            self.window._search_timeout_timer.stop()
        if hasattr(self.window, "search_spinner_timer"):
            self.window.search_spinner_timer.stop()

    def reset_search_controls(self):
        sv = getattr(self.window, "search_view", None)
        if sv is not None and hasattr(sv, "set_search_button"):
            sv.set_search_button("Search", True)
            return
        self.window.search_view.search_btn.setText("Search")
        self.window.search_view.search_btn.setEnabled(True)

    def normalize_search_history(self, values):
        out = []
        seen = set()
        now = datetime.now().timestamp()
        ttl_seconds = max(1, int(self.window.search_history_ttl_days)) * 24 * 3600

        for raw in (values or []):
            if isinstance(raw, dict):
                url = str(raw.get("url", "")).strip()
                ts = float(raw.get("timestamp", now))
            else:
                url = str(raw or "").strip()
                ts = now

            if not url or not url.startswith("http"):
                continue
            if now - ts > ttl_seconds:
                continue

            low = url.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append({"url": url, "timestamp": ts})
        return out[: self.window._search_history_limit]

    def record_search_history(self, value: str):
        text = str(value or "").strip()
        if not text.startswith("http"):
            return
        entry = {"url": text, "timestamp": datetime.now().timestamp()}
        merged = [entry] + self.window.search_history
        self.window.search_history = self.normalize_search_history(merged)
        self.window.search_history_model.setStringList([e["url"] for e in self.window.search_history])
        if hasattr(self.window, "_search_history_save_timer") and self.window._search_history_save_timer is not None:
            self.window._search_history_save_timer.start()
        else:
            self.window._save_session()

    def clear_search_history(self):
        self.window.search_history = []
        self.window.search_history_model.setStringList([])
        self.window._save_session()
        self.window._info(_("تم مسح سجل البحث"))

    def set_search_state(self, state: str):
        if state == "single":
            self.window.search_view.search_stack.setCurrentIndex(1)
        elif state == "trim":
            self.window.search_view.search_stack.setCurrentIndex(2)
        else:
            self.window.search_view.search_stack.setCurrentIndex(0)

    def set_single_preview(self, payload: dict):
        self.window.preview_data = payload or {}
        if not self.window.preview_data.get("url") and hasattr(self.window, "url_input"):
            current_url = self.window.search_view.url_input.text().strip()
            if current_url:
                self.window.preview_data["url"] = current_url
        title = self.window.preview_data.get("title", "--")
        channel = self.window.preview_data.get("channel", "--")
        duration = self.window._format_seconds(self.window.preview_data.get("duration_seconds", 0))
        views = self.window.preview_data.get("views", "0")
        categories = self.window.preview_data.get("categories", [])

        self.window.search_view.single_title.setText(f"{title} ({duration})")
        self.window.search_view.single_channel.setText(
            _("By {channel}  •  {views} views").format(channel=channel, views=views)
        )

        live_label = ""
        live_status = str(self.window.preview_data.get("live_status", "") or "").strip().lower()
        if bool(self.window.preview_data.get("is_live", False)) or live_status in {"is_live", "live"}:
            live_label = _("LIVE")
        elif bool(self.window.preview_data.get("was_live", False)) or live_status == "was_live":
            live_label = _("WAS LIVE")
        if hasattr(self.window.search_view, "single_status_chip"):
            if live_label:
                self.window.search_view.single_status_chip.setText(live_label)
                self.window.search_view.single_status_chip.show()
            else:
                self.window.search_view.single_status_chip.hide()

        if categories:
            self.window.search_view.single_category.setText(categories[0])
            self.window.search_view.single_category.show()
        else:
            self.window.search_view.single_category.hide()

        thumb_url = self.window.preview_data.get("thumbnail")
        if thumb_url:
            request = QNetworkRequest(QUrl(thumb_url))
            reply = self.window.net_manager.get(request)
            reply.finished.connect(lambda r=reply: self.on_single_preview_thumbnail_loaded(r))
        else:
            self.window.search_view.single_thumb.setText(_("No Thumbnail Found"))

        if hasattr(self.window.search_view, "update_quality_size_labels"):
            self.window.search_view.update_quality_size_labels(self.window.preview_data)
        self.window._animate_single_state_widgets()

    def on_single_preview_thumbnail_loaded(self, reply: QNetworkReply):
        try:
            if getattr(self.window, "_is_closing", False):
                return
            try:
                label = self.window.search_view.single_thumb
            except Exception:
                label = None
            if label is None:
                return
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = reply.readAll()
                pixmap = QPixmap()
                if pixmap.loadFromData(data):
                    scaled_pixmap = pixmap.scaled(
                        label.size(),
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    rounded = QPixmap(label.size())
                    rounded.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(rounded)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    path = QPainterPath()
                    path.addRoundedRect(rounded.rect(), 8, 8)
                    painter.setClipPath(path)
                    x = (label.width() - scaled_pixmap.width()) // 2
                    y = (label.height() - scaled_pixmap.height()) // 2
                    painter.drawPixmap(x, y, scaled_pixmap)
                    painter.end()
                    try:
                        label.setPixmap(rounded)
                        label.setText("")
                    except RuntimeError:
                        return
            else:
                try:
                    label.setText("Preview Fail")
                except RuntimeError:
                    return
        finally:
            try:
                reply.deleteLater()
            except Exception:
                pass

    def normalize_search_error_message(self, message: str) -> str:
        msg_text = str(message or "").strip() or "تعذر تحليل الرابط"
        if "unable to download api page" in msg_text.lower() or "http error 400" in msg_text.lower():
            return "تعذر قراءة البلاي ليست. غالباً الرابط غير صحيح أو القائمة خاصة/محذوفة."
        if "truncated_id" in msg_text.lower() or "incomplete youtube id" in msg_text.lower():
            return "الرابط غير مكتمل: معرّف فيديو يوتيوب ناقص. تأكد أن الرابط يحتوي على 11 حرف بعد v=."
        return msg_text

    def _validate_search_target(self, value: str) -> tuple[bool, str]:
        text = str(value or "").strip()
        if not text:
            return False, "الرابط مطلوب"
        lower = text.lower()
        if "youtube.com/watch" in lower:
            match = re.search(r"[?&]v=([A-Za-z0-9_-]+)", text)
            video_id = str(match.group(1) if match else "").strip()
            if video_id and len(video_id) != 11:
                return False, "الرابط غير مكتمل: معرّف فيديو يوتيوب ناقص (لازم 11 حرف)."
        elif "youtu.be/" in lower:
            match = re.search(r"youtu\.be/([A-Za-z0-9_-]+)", text, re.IGNORECASE)
            video_id = str(match.group(1) if match else "").strip()
            if video_id and len(video_id) != 11:
                return False, "الرابط المختصر غير مكتمل: معرّف فيديو يوتيوب ناقص (لازم 11 حرف)."
        return True, ""

    def on_playlist_view_analyze_requested(self, url: str, force: bool = False):
        if self.has_active_process():
            self.window._warn("There is an active process. Please wait.")
            self.window.playlist_view.set_loading_state(False)
            return
        playlist_url = str(url or "").strip()
        if playlist_url:
            try:
                known_ids = _playlist_sync_service.get_known_ids(playlist_url)
                decision = _playlist_sync_service.should_defer_sync(
                    playlist_url,
                    has_cached_ids=bool(known_ids),
                    force=bool(force),
                )
                if bool(decision.get("should_defer")):
                    remaining = int(decision.get("remaining_seconds", 0) or 0)
                    failures = int(decision.get("consecutive_failures", 0) or 0)
                    self.window._append_log(
                        f"⏳ تم تأجيل إعادة تحليل القائمة مؤقتًا (backoff: {failures} فشل متتالي، متبقي ~{remaining}s)."
                    )
                    self.window._warn(
                        f"المزامنة مؤجلة مؤقتًا بعد أخطاء متتالية. جرّب بعد ~{remaining} ثانية."
                    )
                    self.window.playlist_view.set_loading_state(False)
                    return
                if bool(force):
                    self.window._append_log("🚀 تم تجاوز backoff يدويًا وإجبار إعادة التحليل.")
            except Exception as exc:
                logger.debug(f"[AnalyzeController] playlist backoff gate check failed: {exc}")
        self.window.playlist_view.set_loading_state(True)
        self.start_worker(playlist_url, self.window._on_playlist_analyze_finished)

    def on_playlist_analyze_finished(self, success: bool, message: str, payload: dict, items: list):
        self.window.analyze_worker = None
        self.window.playlist_view.set_loading_state(False)
        if not success:
            msg_text = str(message or "").strip() or "Failed to analyze link"
            playlist_url = _playlist_sync_service.extract_playlist_url(
                payload if isinstance(payload, dict) else {},
                fallback_url=getattr(self, "_current_playlist_url", ""),
            )
            if playlist_url:
                try:
                    _playlist_sync_service.mark_sync_failed(playlist_url, msg_text, payload=payload)
                except Exception as exc:
                    logger.debug(f"[AnalyzeController] playlist sync failure update failed: {exc}")
            self.window._record_error(
                msg_text,
                url=playlist_url or getattr(self, "_current_playlist_url", ""),
                title="Playlist Analysis",
                source="playlist_analysis",
            )
            self.window._warn(msg_text)
            self.window.playlist_view.status_lbl.setText("Error: " + msg_text)
            return
        if payload.get("kind") != "playlist":
            items = [payload]
        if payload.get("kind") == "playlist":
            self._apply_playlist_analysis_result(payload, items)
        else:
            self.window.playlist_view.set_playlist_data(payload, items)
        self.window._append_log(message)

    def start_analyze(self):
        if self.has_active_process():
            self.window._warn("تحليل رابط آخر قيد التنفيذ. انتظر انتهاءه ثم أعد المحاولة.")
            return
        sv = getattr(self.window, "search_view", None)
        if sv is not None and hasattr(sv, "get_url"):
            url = sv.get_url()
        else:
            url = self.window.search_view.url_input.text().strip()
        if not url:
            self.window._warn("الرابط مطلوب")
            return
        valid, validation_error = self._validate_search_target(url)
        if not valid:
            self.window._set_status("جاهز")
            self.window._warn(validation_error)
            self.window._append_log(validation_error)
            return
        self.record_search_history(url)
        self.window._set_status("جاري تحليل الرابط")
        if sv is not None and hasattr(sv, "set_search_button"):
            sv.set_search_button(enabled=False)
        else:
            self.window.search_view.search_btn.setEnabled(False)
        self.window.search_spinner_frames = list(self.SPINNER_FRAMES)
        self.window.search_spinner_idx = 0
        self._ensure_search_timers()
        self.window.search_spinner_timer.start(100)
        timeout_ms = 130_000 if ("list=" in url or "/playlist" in url) else 70_000
        self.window._search_timeout_timer.start(timeout_ms)
        self.start_worker(url, self.window._on_analyze_finished)

    def force_reset_search_ui(self):
        self.window.analyze_worker = None
        self.stop_search_timers()
        self.reset_search_controls()
        self.window._set_status("جاهز")

    def update_search_spinner(self):
        frames = getattr(self.window, "search_spinner_frames", None) or self.SPINNER_FRAMES
        idx = int(getattr(self.window, "search_spinner_idx", 0) or 0) % len(frames)
        sv = getattr(self.window, "search_view", None)
        if sv is not None and hasattr(sv, "set_search_button"):
            sv.set_search_button(f"{frames[idx]} Searching...")
        else:
            self.window.search_view.search_btn.setText(f"{frames[idx]} Searching...")
        self.window.search_spinner_idx = (idx + 1) % len(frames)

    def on_analyze_finished(self, success: bool, message: str, payload: dict, items: list):
        self.window.analyze_worker = None
        self.stop_search_timers()
        self.reset_search_controls()
        if not success:
            playlist_url = _playlist_sync_service.extract_playlist_url(
                payload if isinstance(payload, dict) else {},
                fallback_url=getattr(self, "_current_playlist_url", ""),
            )
            if playlist_url and ("list=" in playlist_url or "/playlist" in playlist_url):
                try:
                    _playlist_sync_service.mark_sync_failed(playlist_url, message, payload=payload)
                except Exception as exc:
                    logger.debug(f"[AnalyzeController] playlist sync failure update failed: {exc}")
            self.window._set_status("جاهز")
            self.set_search_state("empty")
            msg_text = self.normalize_search_error_message(message)
            self.window._record_error(
                msg_text,
                url=str(getattr(self, "_current_playlist_url", "") or ""),
                title="Link Analysis",
                source="analyze",
            )
            self.window._warn(msg_text)
            self.window._append_log(msg_text)
            return
        if payload.get("kind") == "playlist":
            self._apply_playlist_analysis_result(payload, items)
            self.window._switch_view("playlists")
            self.window._set_status("جاهز")
            self.window._append_log(message)
            return
        self.set_single_preview(payload or {})
        self.set_search_state("single")
        self.window._set_status("جاهز")
        self.window._append_log(message)

    def on_playlist_view_download_requested(self, tasks: list):
        normalized_tasks: list[DownloadTask] = []
        default_retries = int(DEFAULT_SETTINGS["retries"])
        fallback_out_dir = self.default_out_dir()
        for raw in tasks or []:
            item = raw if isinstance(raw, dict) else {}
            fmt = str(item.get("format", "MP4")).strip() or "MP4"
            quality = str(item.get("quality", "1080p")).strip() or "1080p"
            subtitle = str(item.get("subtitle", "None")).strip() or "None"
            task = self.window._build_task(
                url=item.get("url", ""),
                title=item.get("title", ""),
                thumbnail=item.get("thumbnail", ""),
                fmt=fmt,
                quality=quality,
            )
            task = self.window._normalize_task(
                task,
                subtitle=subtitle,
                duration_seconds=int(item.get("duration_seconds") or task.get("duration_seconds") or 0),
                retries=int(item.get("retries", task.get("retries", default_retries)) or default_retries),
                out_dir=str(item.get("out_dir", task.get("out_dir", ""))).strip() or fallback_out_dir,
            )
            scheduled_at = float(item.get("scheduled_at", 0) or 0)
            if scheduled_at > 0:
                task["scheduled_at"] = scheduled_at
                task["schedule_repeat"] = str(item.get("schedule_repeat", "none") or "none")
            if int(item.get("estimated_size_bytes", 0) or item.get("size_bytes", 0) or 0) > 0:
                task["estimated_size_bytes"] = int(item.get("estimated_size_bytes", item.get("size_bytes", 0)) or 0)
                task["size_bytes"] = int(item.get("size_bytes", item.get("estimated_size_bytes", 0)) or 0)
                task["size"] = str(item.get("size", item.get("size_text", "")) or "")
                task["size_text"] = str(item.get("size_text", task.get("size", "")) or task.get("size", ""))
                task["size_is_estimate"] = bool(item.get("size_is_estimate", True))
            apply_estimated_size(task, item, duration_seconds=int(task.get("duration_seconds") or 0))
            normalized_tasks.append(task)
        if not normalized_tasks:
            self.window._warn("لا توجد عناصر صالحة للإضافة للطابور")
            return
        self.window.queue_manager.add_tasks(normalized_tasks)
        self.window._append_log(f"Added {len(normalized_tasks)} items to the queue from Playlist View")
        self.window._switch_view("downloads")
        if any(float((task or {}).get("scheduled_at", 0) or 0) > 0 for task in normalized_tasks):
            self.window._set_downloads_filter("scheduled")
            if hasattr(self.window, "_update_scheduler_timer_state"):
                QTimer.singleShot(0, lambda: self.window._update_scheduler_timer_state(force_refresh=True))
        else:
            self.window._set_downloads_filter("active")
            self.window._start_queue_download()

    def default_out_dir(self) -> str:
        if hasattr(self.window, "out_dir_input"):
            out_dir = self.window.search_view.out_dir_input.text().strip()
            if out_dir:
                return out_dir
        return default_download_dir()

    def toggle_trim_options(self):
        preview_data = getattr(self.window, "preview_data", {}) or {}
        if not preview_data:
            return
        self.window.trim_context = {"mode": "search"}
        self.window.search_view.trim_view.set_task(preview_data, net_manager=self.window.net_manager)
        self.window._switch_view("search")
        self.set_search_state("trim")

    def open_trim_for_queue_item(self, item_index: int):
        item = self.window.queue_manager.get_task(item_index)
        if not item:
            return
        status = normalize_task_status(item.get("status"))
        if status == TaskStatus.RUNNING.value:
            self.window._warn("مينفعش تعديل القص أثناء التحميل. اعمل Pause الأول.")
            return
        self.window.trim_context = {"mode": "queue", "queue_index": item_index}
        self.window.search_view.trim_view.set_task(item, net_manager=self.window.net_manager)
        self.window._switch_view("search")
        self.set_search_state("trim")

    def on_trim_view_saved(self, data: dict):
        payload = data or {}
        start = str(payload.get("start", "")).strip()
        end = str(payload.get("end", "")).strip()
        title = str(payload.get("title", "")).strip()
        trims = payload.get("trims", None)
        ctx = getattr(self.window, "trim_context", {}) or {}
        mode = ctx.get("mode", "search")
        if mode == "queue":
            idx = ctx.get("queue_index")
            if isinstance(idx, int):
                fields = {
                    "start_time": start,
                    "end_time": end,
                }
                if title:
                    fields["title"] = title
                if isinstance(trims, list):
                    fields["trims"] = trims
                updated = self.window.queue_manager.update_task_fields(idx, fields, emit_changed=False)
                if updated:
                    self.window._save_session()
                    self.window._refresh_downloads_list()
            self.window._switch_view("downloads")
            self.window._set_downloads_filter("queued")
            return
        self.window.search_view.start_input.setText(start)
        self.window.search_view.end_input.setText(end)
        if title:
            duration = self.window._format_seconds(self.window.preview_data.get("duration_seconds", 0))
            self.window.search_view.single_title.setText(f"{title} ({duration})")
            self.window.preview_data["title"] = title
        if isinstance(trims, list):
            self.window.preview_data["trims"] = trims
        self.window.trim_btn.setProperty("trimActive", True)
        self.window.trim_btn.style().unpolish(self.window.trim_btn)
        self.window.trim_btn.style().polish(self.window.trim_btn)
        self.window._append_log(f"Trim set: {start} to {end}")
        self.window._switch_view("search")
        self.set_search_state("single")

    def on_trim_view_back(self):
        ctx = getattr(self.window, "trim_context", {}) or {}
        mode = ctx.get("mode", "search")
        if mode == "queue":
            self.window._switch_view("downloads")
            self.window._set_downloads_filter("queued")
            return
        self.window._switch_view("search")
        self.set_search_state("single")

    # ── Playlist Cache Helpers ────────────────────────────────────────────────

    def _extract_entry_id(self, item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        return str(item.get("id") or item.get("entry_id") or item.get("video_id") or "").strip()

    def _prepare_playlist_diff_state(self, playlist_url: str):
        url = str(playlist_url or "").strip()
        self._playlist_seen_ids_current_fetch = set()
        self._playlist_new_ids_current_fetch = set()
        self._playlist_removed_ids_current_fetch = set()
        if not url:
            self._playlist_cached_ids_before_fetch = set()
            return
        try:
            self._playlist_cached_ids_before_fetch = set(get_playlist_known_ids(url))
        except Exception as exc:
            logger.debug(f"[AnalyzeController] get_playlist_known_ids failed: {exc}")
            self._playlist_cached_ids_before_fetch = set()

    def _finalize_playlist_diff_state(self) -> dict:
        current_seen_ids = set(self._playlist_seen_ids_current_fetch)
        new_ids = set(self._playlist_new_ids_current_fetch)
        removed_ids = set(self._playlist_cached_ids_before_fetch) - current_seen_ids
        playlist_url = getattr(self, "_current_playlist_url", "")
        try:
            db_diff = diff_playlist_entries(playlist_url, list(current_seen_ids))
            new_ids |= set(db_diff.get("new_ids", set()))
            removed_ids = set(db_diff.get("removed_ids", removed_ids))
        except Exception as exc:
            logger.debug(f"[AnalyzeController] diff_playlist_entries failed: {exc}")
        self._playlist_new_ids_current_fetch = set(new_ids)
        self._playlist_removed_ids_current_fetch = set(removed_ids)
        try:
            sync_playlist_snapshot(playlist_url, list(current_seen_ids))
        except Exception as exc:
            logger.debug(f"[AnalyzeController] sync_playlist_snapshot failed: {exc}")
        return {
            "new_ids": set(self._playlist_new_ids_current_fetch),
            "removed_ids": set(self._playlist_removed_ids_current_fetch),
        }

