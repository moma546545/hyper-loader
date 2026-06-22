import logging
import os
from datetime import datetime

from core.event_bus import event_bus, ShowNotificationEvent
from core.config import estimate_file_size_bytes, normalize_video_quality_label
from core.i18n import _
from core.qt_compat import QApplication, QSystemTrayIcon
from core.storage_watchdog import format_bytes
from ui.themes import THEMES

logger = logging.getLogger("SnapDownloader")


class PremiumWindowUtilityMixin:
    def _paste_clipboard(self):
        try:
            clip = QApplication.clipboard()
            if clip is None:
                return
            txt = (clip.text() or "").strip()
            if not txt:
                return
            self.search_view.url_input.setText(txt)
            self._start_analyze()
        except Exception as exc:
            logger.warning(f"Paste clipboard failed: {exc}")

    def _quality_value(self):
        for radio in self.search_view.quality_radios:
            if radio.isChecked():
                text = str(radio.property("base_quality") or radio.text()).strip()
                return normalize_video_quality_label(text)
        return "8K"

    def _audio_quality_value(self):
        for radio in getattr(self.search_view, "audio_quality_radios", []):
            if radio.isChecked():
                return str(radio.property("base_quality") or radio.text()).splitlines()[0].strip() or "320kbps"
        return "320kbps"

    def _validate_task(self, task):
        if not task.get("url"):
            self._warn(_("الرابط مطلوب"))
            return False
        if not task.get("out_dir"):
            self._warn(_("مسار الحفظ مطلوب"))
            return False
        return True

    def _format_seconds(self, value):
        total = int(value or 0)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _estimate_size_label(self, duration_seconds: int, quality_label: str):
        size_bytes = estimate_file_size_bytes(
            duration_seconds=int(duration_seconds or 0),
            mode="video",
            quality=str(quality_label or ""),
        )
        gb = 1024 * 1024 * 1024
        mb = 1024 * 1024
        if size_bytes >= gb:
            return f"{size_bytes / gb:.1f} GB"
        return f"{size_bytes / mb:.1f} MB"

    def _has_active_download_activity(self) -> bool:
        try:
            with self._active_workers_lock:
                for worker in self.active_workers.values():
                    if worker is None:
                        continue
                    try:
                        if worker.isRunning():
                            return True
                    except RuntimeError:
                        continue
        except Exception:
            pass
        return False

    def _update_scheduler_timer_state(self, force_refresh: bool = False):
        timer = getattr(self, "_scheduler_timer", None)
        if timer is None:
            return
        should_run = self._has_active_download_activity() or self._queue_is_running()
        if should_run:
            if not timer.isActive():
                timer.start()
            if force_refresh:
                self._refresh_bandwidth_schedule()
            return
        if timer.isActive():
            timer.stop()

    def _storage_guard_paths(self) -> list[str]:
        seen = set()
        paths = []
        with self._active_workers_lock:
            keys = list(self.active_workers.keys())
        for wid in keys:
            task = self.queue_manager.get_task(wid) if isinstance(wid, int) else None
            out_dir = str((task or {}).get("out_dir", "")).strip()
            if out_dir and out_dir not in seen:
                seen.add(out_dir)
                paths.append(out_dir)
        fallback = str(self.current_download_path or "").strip()
        if not fallback and hasattr(self, "out_dir_input"):
            fallback = self.search_view.out_dir_input.text().strip()
        if fallback and fallback not in seen:
            paths.append(fallback)
        return paths

    def _storage_guard_message(self, path: str, free: int, threshold: int) -> str:
        return (
            f"⚠️ المساحة الحرة منخفضة في {path} — المتاح {format_bytes(free)} "
            f"والحد المطلوب {format_bytes(threshold)}. تم إيقاف التحميل مؤقتاً."
        )

    def _pause_downloads_for_storage_guard(self, message: str):
        self._set_queue_runtime_state(paused=True)
        try:
            self.queue_manager.pause_queue()
        except (RuntimeError, AttributeError) as exc:
            logger.debug(f"تعذر إيقاف الطابور بسبب Storage Guard: {exc}")
        with self._active_workers_lock:
            items = list(self.active_workers.items())
        for wid, worker in items:
            self._mark_pause_requested(wid)
            if isinstance(wid, int):
                self.queue_manager.update_task_fields(wid, {"status": "paused", "next_retry_at": 0}, emit_changed=False)
            try:
                worker.stop()
            except (RuntimeError, AttributeError) as exc:
                logger.debug(f"تعذر إيقاف worker {wid} بسبب Storage Guard: {exc}")
        if message != self._storage_guard_last_message or not self._storage_guard_alerted:
            self._append_log(message)
            self._warn(message)
            self._show_tray_message(
                "SnapDownloader",
                message[:120],
                QSystemTrayIcon.MessageIcon.Warning,
                3500,
            )
        self._storage_guard_alerted = True
        self._storage_guard_last_message = message
        self._set_status(_("المساحة منخفضة"))
        self._save_session()
        self._refresh_downloads_list()

    def _append_log(self, message: str):
        text = str(message or "").strip()
        if not text:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        self.logs.append(line)
        if len(self.logs) > 260:
            self.logs = self.logs[-260:]
        log_widget = self.search_view.log_text
        doc = log_widget.document() if hasattr(log_widget, "document") else None
        if doc is not None and hasattr(doc, "maximumBlockCount"):
            if doc.maximumBlockCount() != 265:
                doc.setMaximumBlockCount(265)
        if hasattr(log_widget, "appendPlainText"):
            log_widget.appendPlainText(line)
        else:
            log_widget.append(line)

    def _size_to_bytes(self, size_text: str):
        text = str(size_text or "").strip().upper().replace("IB", "B")
        if not text or text == "--":
            return 0
        try:
            if text.endswith("GB"):
                return int(float(text[:-2].strip()) * 1024 * 1024 * 1024)
            if text.endswith("MB"):
                return int(float(text[:-2].strip()) * 1024 * 1024)
            if text.endswith("KB"):
                return int(float(text[:-2].strip()) * 1024)
            if text.endswith("B"):
                return int(float(text[:-1].strip()))
        except (ValueError, TypeError):
            return 0
        return 0


class PremiumWindowSystemMixin:
    def _toggle_window_visibility(self):
        """Hotkey: Ctrl+Shift+S — show or hide the main window."""
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            try:
                self.raise_()
            except Exception:
                pass
            self.activateWindow()

    def closeEvent(self, event):
        tray_icon = getattr(getattr(self, "tray_manager", None), "tray_icon", None)
        close_to_tray = bool(getattr(self, "close_to_tray_enabled", True))
        bypass = bool(getattr(self, "_quit_to_tray_bypass", False))
        app_is_quitting = bool(getattr(self, "_app_is_quitting", False))
        if close_to_tray and not bypass and not app_is_quitting and tray_icon is not None and tray_icon.isVisible():
            self.hide()
            try:
                self._show_tray_message("SnapDownloader", _("التطبيق يعمل في الخلفية"))
            except Exception:
                pass
            event.ignore()
            return

        self._is_closing = True
        self._set_queue_runtime_state(running=False, paused=False)
        try:
            if hasattr(self, "lifecycle_controller") and self.lifecycle_controller is not None:
                self.lifecycle_controller.close_event()
        except Exception as exc:
            logger.debug(f"تعذر تنفيذ lifecycle_controller.close_event: {exc}")
        self._quit_to_tray_bypass = False
        self._app_is_quitting = False
        super().closeEvent(event)

    def _on_config_changed(self, key: str, value):
        k = str(key or "").strip().lower()
        if k == "theme":
            name = str(value or "").strip()
            if name in THEMES:
                self._apply_theme(name, persist=False)
        elif k == "save_path":
            path = str(value or "").strip()
            if path:
                self.current_download_path = path
                if hasattr(self, "search_view") and hasattr(self.search_view, "set_out_dir"):
                    self.search_view.set_out_dir(path)
        elif k == "max_concurrent":
            try:
                self.max_concurrent = max(1, int(value))
            except Exception:
                pass
        elif k == "use_aria2c":
            checked = bool(value)
            if hasattr(self, "search_view") and hasattr(self.search_view, "set_aria2_checked"):
                self.search_view.set_aria2_checked(checked)
        elif k == "play_sound":
            self.play_sound_enabled = bool(value)

    def _info(self, message: str):
        event_bus.publish(ShowNotificationEvent(message, "info"))

    def _warn(self, message: str):
        event_bus.publish(ShowNotificationEvent(message, "warn", title="Warning"))

    def _record_error(self, error_text: str, *, url: str = "", title: str = "", source: str = ""):
        text = str(error_text or "").strip()
        if not text:
            return
        if text.lower() == "cancelled":
            return
        title_hint = str(title or source or "").strip()
        dashboard = getattr(self, "error_dashboard", None)
        if dashboard is None or not hasattr(dashboard, "report_error"):
            return
        try:
            dashboard.report_error(url=str(url or "").strip(), error_text=text, title=title_hint)
        except Exception as exc:
            logger.debug(f"تعذر تسجيل الخطأ في Error Dashboard: {exc}")

    def _stats_path(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, "xd_stats.json")
