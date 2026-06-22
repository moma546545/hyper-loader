import logging

from core.event_bus import DownloadFinishedEvent, ExtensionLinkReceivedEvent, ShowNotificationEvent, event_bus
from core.memory_guard import memory_guard


logger = logging.getLogger("SnapDownloader.LifecycleController")


class LifecycleController:
    def __init__(self, window):
        self.window = window
        self._close_started = False

    def close_event(self):
        if self._close_started:
            return
        self._close_started = True
        setattr(self.window, "_app_is_quitting", True)
        self.stop_runtime_helpers()
        try:
            extension_server = getattr(self.window, "extension_server", None)
            if extension_server is None:
                from core.extension_server import extension_server as shared_extension_server

                extension_server = shared_extension_server
            if extension_server is not None:
                extension_server.stop()
        except (RuntimeError, AttributeError) as exc:
            logger.debug(f"تعذر إيقاف extension_server: {exc}")

        try:
            if hasattr(self.window, "clip_watcher") and self.window.clip_watcher is not None:
                self.window.clip_watcher.hide()
        except (RuntimeError, AttributeError) as exc:
            logger.debug(f"تعذر إيقاف clip_watcher: {exc}")

        try:
            from core.channel_subscriptions import subscription_manager

            subscription_manager.stop()
        except Exception as exc:
            logger.debug(f"تعذر إيقاف subscription_manager: {exc}")

        try:
            memory_guard.stop()
        except RuntimeError as exc:
            logger.debug(f"تعذر إيقاف memory_guard: {exc}")

        self._shutdown_analyze_worker()
        self._shutdown_bulk_analysis_workers()
        self._stop_active_workers()
        self._shutdown_controllers()
        self._unsubscribe_events()
        self._shutdown_session_threads()
        self._cleanup_tray()

    def stop_runtime_helpers(self):
        for timer_name in (
            "trial_timer",
            "_scheduler_timer",
            "_storage_watchdog_timer",
            "toast_timer",
            "_visible_thumb_timer",
            "_thumbnail_cleanup_timer",
            "_settings_autosave_timer",
            "_search_history_save_timer",
            "_system_theme_timer",
            "_queue_ai_timer",
        ):
            self._stop_timer(timer_name)

        try:
            hotkey_manager = getattr(self.window, "hotkey_manager", None)
            if hotkey_manager is not None:
                hotkey_manager.stop()
        except (RuntimeError, AttributeError) as exc:
            logger.debug(f"تعذر إيقاف hotkey_manager: {exc}")

        self._hide_and_delete_widget("mini_window")

    def _stop_timer(self, attr_name: str):
        try:
            timer = getattr(self.window, attr_name, None)
            if timer is not None:
                timer.stop()
        except (RuntimeError, AttributeError) as exc:
            logger.debug(f"تعذر إيقاف {attr_name}: {exc}")

    def _hide_and_delete_widget(self, attr_name: str):
        try:
            widget = getattr(self.window, attr_name, None)
            if widget is None:
                return
            if hasattr(widget, "hide"):
                widget.hide()
            if hasattr(widget, "deleteLater"):
                widget.deleteLater()
        except (RuntimeError, AttributeError) as exc:
            logger.debug(f"تعذر تنظيف {attr_name}: {exc}")

    def _shutdown_analyze_worker(self):
        try:
            worker = getattr(self.window, "analyze_worker", None)
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.wait(2000)
                if worker.isRunning():
                    logger.warning("analyze_worker did not stop before shutdown timeout")
        except (RuntimeError, AttributeError) as exc:
            logger.debug(f"تعذر إيقاف analyze_worker: {exc}")

    def _stop_active_workers(self):
        with self.window._active_workers_lock:
            workers = list(self.window.active_workers.values())
        for worker in workers:
            try:
                worker.stop()
            except (RuntimeError, AttributeError) as exc:
                logger.debug(f"تعذر إرسال stop إلى worker: {exc}")

        with self.window._active_workers_lock:
            workers = list(self.window.active_workers.values())
        for worker in workers:
            try:
                worker.wait(5000)
                if worker.isRunning():
                    logger.warning("Worker is still running during shutdown after cooperative stop request")
            except (RuntimeError, AttributeError) as exc:
                logger.debug(f"تعذر إنهاء worker أثناء الإغلاق: {exc}")

    def _shutdown_bulk_analysis_workers(self):
        try:
            controller = getattr(self.window, "bulk_analysis_controller", None)
            shutdown = getattr(controller, "shutdown", None)
            if callable(shutdown):
                shutdown()
                return
        except Exception as exc:
            logger.debug(f"تعذر إيقاف bulk_analysis_controller: {exc}")
        workers = getattr(self.window, "_bulk_analysis_workers", {})
        if not isinstance(workers, dict):
            return
        running_workers = list(workers.values())
        workers.clear()
        for worker in running_workers:
            try:
                if hasattr(worker, "requestInterruption"):
                    worker.requestInterruption()
                if hasattr(worker, "quit"):
                    worker.quit()
            except (RuntimeError, AttributeError) as exc:
                logger.debug(f"تعذر إرسال إيقاف إلى bulk analysis worker: {exc}")
        for worker in running_workers:
            try:
                if hasattr(worker, "isRunning") and worker.isRunning():
                    worker.wait(3000)
            except (RuntimeError, AttributeError) as exc:
                logger.debug(f"تعذر انتظار bulk analysis worker: {exc}")

    def _shutdown_controllers(self):
        try:
            if hasattr(self.window, "analyze_controller") and self.window.analyze_controller is not None:
                stop_scheduler = getattr(self.window.analyze_controller, "stop_playlist_sync_scheduler", None)
                if callable(stop_scheduler):
                    stop_scheduler()
        except Exception as exc:
            logger.debug(f"تعذر إيقاف playlist sync scheduler: {exc}")

        try:
            if hasattr(self.window, "download_controller") and self.window.download_controller is not None:
                self.window.download_controller.shutdown()
        except Exception as exc:
            logger.debug(f"تعذر إيقاف download_controller: {exc}")

        try:
            if hasattr(self.window, "update_controller") and self.window.update_controller is not None:
                self.window.update_controller.shutdown()
        except Exception as exc:
            logger.debug(f"تعذر إيقاف update_controller: {exc}")

    def _unsubscribe_events(self):
        try:
            for event_type, attr_name in [
                (ShowNotificationEvent, "_on_show_notification_event"),
                (DownloadFinishedEvent, "_on_download_finished_event"),
                (ExtensionLinkReceivedEvent, "_on_extension_link_event"),
            ]:
                callback = getattr(self.window, attr_name, None)
                if callback is not None:
                    event_bus.unsubscribe(event_type, callback)
        except (RuntimeError, ValueError) as exc:
            logger.debug(f"تعذر إلغاء الاشتراك من event_bus: {exc}")

    def _shutdown_session_threads(self):
        try:
            if hasattr(self.window, "_save_session"):
                self.window._save_session(sync=True)
        except Exception as exc:
            logger.debug(f"تعذر تنفيذ حفظ الجلسة النهائي: {exc}")

        session_service = getattr(self.window, "session_service", None)
        if session_service is not None:
            try:
                session_service.stop()
            except Exception as exc:
                logger.debug(f"تعذر إيقاف session_service: {exc}")
            try:
                load_thread = getattr(session_service, "_load_thread", None)
                if load_thread is not None and load_thread.is_alive():
                    load_thread.join(timeout=1.0)
            except RuntimeError as exc:
                logger.debug(f"تعذر انتظار session_service load thread: {exc}")
            return

        # Legacy fallback for windows that still carry the pre-SessionService state.
        try:
            if hasattr(self.window, "_session_save_shutdown"):
                self.window._session_save_shutdown = True
            if hasattr(self.window, "_session_save_event"):
                self.window._session_save_event.set()
            save_thread = getattr(self.window, "_session_save_thread", None)
            if save_thread is not None and save_thread.is_alive():
                save_thread.join(timeout=2.0)
        except RuntimeError as exc:
            logger.debug(f"تعذر انتظار legacy session_save_thread: {exc}")
        try:
            load_thread = getattr(self.window, "_session_load_thread", None)
            if load_thread is not None and load_thread.is_alive():
                load_thread.join(timeout=1.0)
        except RuntimeError as exc:
            logger.debug(f"تعذر انتظار legacy session_load_thread: {exc}")

    def _cleanup_tray(self):
        try:
            tray_manager = getattr(self.window, "tray_manager", None)
            if tray_manager is not None and hasattr(tray_manager, "cleanup"):
                tray_manager.cleanup()
        except Exception as exc:
            logger.debug(f"تعذر تنظيف tray_manager: {exc}")
        try:
            if hasattr(self.window, "tray_icon") and self.window.tray_icon is not None:
                self.window.tray_icon.hide()
        except (RuntimeError, AttributeError) as exc:
            logger.debug(f"تعذر تنظيف tray_icon: {exc}")
