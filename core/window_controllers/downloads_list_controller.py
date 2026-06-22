from datetime import datetime

from core.database import fetch_completed_history_page_from_db
from core.i18n import _
from core.qt_compat import QFrame, QLabel, QTimer, QVBoxLayout, Qt
from ui.models import EMPTY_STATE_SENTINEL
from ui.themes import get_theme


class DownloadsListController:
    def __init__(self, window):
        self.window = window

    @staticmethod
    def _history_sort_to_sql(sort_key: str) -> str:
        key = str(sort_key or "Date (Newest)")
        if key == "Date (Oldest)":
            return "timestamp ASC"
        if key == "Alphabetical (A → Z)":
            return "title ASC"
        if key == "Alphabetical (Z → A)":
            return "title DESC"
        if key == "Size (Largest)":
            return "size_bytes DESC"
        if key == "Size (Smallest)":
            return "size_bytes ASC"
        return "timestamp DESC"

    def _normalize_completed_entry(self, row: dict) -> dict:
        item = dict(row or {})
        mode_value = item.get("mode", "video")
        if hasattr(self.window, "_normalize_history_mode"):
            mode = self.window._normalize_history_mode(mode_value)
        else:
            mode = "audio" if str(mode_value or "").strip().lower() in {"audio", "صوت"} else "video"
        return {
            "timestamp": item.get("timestamp", ""),
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "mode": mode,
            "format": item.get("format", "--"),
            "quality": item.get("quality", "--"),
            "size": item.get("size_text", "--"),
            "thumbnail": item.get("thumbnail", ""),
            "file_path": item.get("file_path", ""),
            "status": item.get("status", "success"),
            "message": item.get("message", ""),
            "attempts": item.get("attempts", 1),
            "error": item.get("error", ""),
            "size_bytes": int(item.get("size_bytes", 0) or 0),
        }

    def refresh_downloads_list(self):
        if str(getattr(self.window, "active_view", "")).lower() != "downloads":
            return
        self.window._update_downloads_dashboard()
        self.window.downloads_view.set_queue_state_visible(
            self.window.downloads_filter in {"active", "queued", "scheduled"}
        )

        query = self.window.downloads_view.get_search_query().lower()
        history_filters = self.window.downloads_view.get_history_filters()

        entries = []
        now_ts = datetime.now().timestamp()
        media_counts = {"all": 0, "video": 0, "audio": 0}

        if self.window.downloads_filter == "completed":
            page_payload = fetch_completed_history_page_from_db(
                media_filter=history_filters.get("mode", "all"),
                format_filter=history_filters.get("format", "all"),
                date_filter=history_filters.get("date", "all"),
                query=query,
                sort=self._history_sort_to_sql(getattr(self.window, "downloads_sort", "Date (Newest)")),
                page=self.window.downloads_page,
                page_size=self.window.downloads_page_size,
            )
            media_counts = dict(page_payload.get("media_counts", {}) or media_counts)
            total_pages = int(page_payload.get("total_pages", 1) or 1)
            requested_page = int(page_payload.get("page", 1) or 1)
            if requested_page != self.window.downloads_page:
                self.window.downloads_page = requested_page
                page_payload = fetch_completed_history_page_from_db(
                    media_filter=history_filters.get("mode", "all"),
                    format_filter=history_filters.get("format", "all"),
                    date_filter=history_filters.get("date", "all"),
                    query=query,
                    sort=self._history_sort_to_sql(getattr(self.window, "downloads_sort", "Date (Newest)")),
                    page=self.window.downloads_page,
                    page_size=self.window.downloads_page_size,
                )
                media_counts = dict(page_payload.get("media_counts", {}) or media_counts)
                total_pages = int(page_payload.get("total_pages", 1) or 1)
            entries = [self._normalize_completed_entry(row) for row in list(page_payload.get("entries", []) or [])]
            self.window.downloads_view.set_page_options(total_pages, self.window.downloads_page)
        else:
            page_payload = self.window.queue_manager.get_download_entries_page(
                view=self.window.downloads_filter,
                now_ts=now_ts,
                queue_state_filter=self.window.queue_state_filter,
                media_filter=history_filters.get("mode", "all"),
                query=query,
                page=self.window.downloads_page,
                page_size=self.window.downloads_page_size,
            )
            media_counts = dict(page_payload.get("media_counts", {}) or media_counts)
            total_pages = int(page_payload.get("total_pages", 1) or 1)
            requested_page = int(page_payload.get("page", 1) or 1)
            if requested_page != self.window.downloads_page:
                # Clamp page when queue shrinks so the current page stays valid.
                self.window.downloads_page = requested_page
                page_payload = self.window.queue_manager.get_download_entries_page(
                    view=self.window.downloads_filter,
                    now_ts=now_ts,
                    queue_state_filter=self.window.queue_state_filter,
                    media_filter=history_filters.get("mode", "all"),
                    query=query,
                    page=self.window.downloads_page,
                    page_size=self.window.downloads_page_size,
                )
                media_counts = dict(page_payload.get("media_counts", {}) or media_counts)
                total_pages = int(page_payload.get("total_pages", 1) or 1)
            entries = [e for e in list(page_payload.get("entries", []) or []) if str(e.get("status", "")).lower() != "deleted"]
            self.window.downloads_view.set_page_options(total_pages, self.window.downloads_page)

        self.window.downloads_view.set_media_filter_counts(
            int(media_counts.get("all", 0) or 0),
            int(media_counts.get("video", 0) or 0),
            int(media_counts.get("audio", 0) or 0),
        )
        refresh_fingerprint, entry_fingerprints, active_keys = self.window.download_controller.build_downloads_refresh_fingerprint(
            entries=entries,
            theme=self.window.theme,
            downloads_filter=self.window.downloads_filter,
            queue_state_filter=self.window.queue_state_filter,
            media_filter=history_filters.get("mode", "all"),
            query=query,
            downloads_page=self.window.downloads_page,
        )
        if refresh_fingerprint == self.window._downloads_last_fingerprint and self.window._rendered_download_rows:
            return
        previous_entry_fingerprints = tuple(self.window._downloads_last_entry_fingerprints or ())
        self.window._downloads_last_fingerprint = refresh_fingerprint
        self.window._downloads_last_entry_fingerprints = tuple(entry_fingerprints)

        self.window._active_download_card_refs.clear()
        self.window.downloads_thumbnail_jobs.clear()
        for key in list(self.window._download_card_cache.keys()):
            if key not in active_keys:
                stale_widget = self.window._download_card_cache.pop(key, None)
                if stale_widget is not None:
                    try:
                        stale_widget.hide()
                        stale_widget.setParent(None)
                        stale_widget.deleteLater()
                    except RuntimeError:
                        pass
                self.window._download_card_state.pop(key, None)

        if not entries:
            self.clear_rendered_download_widgets(drop_cache=False)
            self.window._downloads_last_entry_fingerprints = ()
            self.window.downloads_view.downloads_model.update_items([dict(EMPTY_STATE_SENTINEL)])
            self._render_empty_state_card()
            return

        self.window._downloads_render_generation += 1
        self.window._downloads_render_cursor = 0
        self.window._downloads_render_loading = False
        model = self.window.downloads_view.downloads_model
        preserve_rows = bool(self.window._rendered_download_rows) and model.rowCount() == len(entries)
        if preserve_rows:
            changed_rows = [
                row_index
                for row_index, fingerprint in enumerate(entry_fingerprints)
                if row_index >= len(previous_entry_fingerprints) or previous_entry_fingerprints[row_index] != fingerprint
            ]
            if changed_rows:
                self.drop_rendered_download_rows(changed_rows)
            model.update_items(entries, preserve_rows=True)
        else:
            self.clear_rendered_download_widgets(drop_cache=False)
            model.update_items(entries)
        QTimer.singleShot(
            0,
            lambda g=self.window._downloads_render_generation: self.window._maybe_render_more_download_entries(g, force=True),
        )

    def _render_empty_state_card(self):
        empty_card = QFrame()
        empty_card.setObjectName("playlist_row")
        empty_card.setProperty("is_empty_state", True)
        try:
            viewport_h = int(self.window.downloads_view.downloads_list.viewport().height() or 0)
        except Exception:
            viewport_h = 0
        if viewport_h <= 0:
            try:
                viewport_h = int(self.window.downloads_view.downloads_list.height() or 0)
            except Exception:
                viewport_h = 0
        h = max(340, viewport_h)
        empty_card.setMinimumHeight(h)
        empty_card.setMaximumHeight(h)
        empty_card.setStyleSheet(
            f"""
            QFrame#playlist_row {{
                background: {get_theme(self.window.theme)['panel']};
                border-radius: 16px;
                border: 1px solid {get_theme(self.window.theme)['border']};
            }}
            """
        )
        empty_layout = QVBoxLayout(empty_card)
        empty_layout.setContentsMargins(24, 24, 24, 24)
        empty_layout.setSpacing(12)

        icon_lbl = QLabel("📭")
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet("font-size: 48px; background: transparent; border: none;")

        theme = get_theme(self.window.theme)
        title = QLabel(_("لا توجد عناصر هنا حالياً"))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(
            f"font-size: 18px; font-weight: 800; color: {theme['text']}; background: transparent; border: none;"
        )

        subtitle_text = _("أضف روابط جديدة من صفحة البحث لبدء التحميل")
        if self.window.downloads_filter == "active":
            subtitle_text = _("لا يوجد تحميل نشط الآن، راجع تبويب 'الطابور' لبدء التحميل")
        elif self.window.downloads_filter == "completed":
            subtitle_text = _("لم تقم بإكمال أي تحميلات بعد.")

        subtitle = QLabel(subtitle_text)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"font-size: 14px; color: {theme['muted']}; background: transparent; border: none;"
        )

        empty_layout.addStretch(1)
        empty_layout.addWidget(icon_lbl)
        empty_layout.addWidget(title)
        empty_layout.addWidget(subtitle)
        empty_layout.addStretch(1)
        index = self.window.downloads_view.downloads_model.index(0, 0)
        self.window.downloads_view.downloads_list.setIndexWidget(index, empty_card)
        self.window._rendered_download_rows[0] = "empty"

    def clear_rendered_download_widgets(self, drop_cache: bool = False):
        list_view = self.window.downloads_view.downloads_list
        model = self.window.downloads_view.downloads_model
        for row_index, cache_key in list(self.window._rendered_download_rows.items()):
            model_index = model.index(int(row_index), 0)
            if model_index.isValid():
                try:
                    widget = list_view.indexWidget(model_index)
                    if widget is not None:
                        list_view.setIndexWidget(model_index, None)
                        widget.hide()
                        widget.setParent(None)
                        widget.deleteLater()
                except RuntimeError:
                    pass
            if drop_cache and isinstance(cache_key, str):
                stale_widget = self.window._download_card_cache.pop(cache_key, None)
                if stale_widget is not None:
                    try:
                        stale_widget.hide()
                        stale_widget.setParent(None)
                        stale_widget.deleteLater()
                    except RuntimeError:
                        pass
                self.window._download_card_state.pop(cache_key, None)
        self.window._rendered_download_rows.clear()

    def drop_rendered_download_rows(self, row_indices):
        rows = sorted({int(row) for row in (row_indices or []) if isinstance(row, int) or str(row).isdigit()})
        if not rows:
            return
        list_view = self.window.downloads_view.downloads_list
        model = self.window.downloads_view.downloads_model
        for row_index in rows:
            model_index = model.index(int(row_index), 0)
            if model_index.isValid():
                try:
                    widget = list_view.indexWidget(model_index)
                    if widget is not None:
                        list_view.setIndexWidget(model_index, None)
                        widget.hide()
                        widget.setParent(None)
                        widget.deleteLater()
                except RuntimeError:
                    pass
            self.window._rendered_download_rows.pop(int(row_index), None)
