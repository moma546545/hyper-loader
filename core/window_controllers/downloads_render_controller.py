import logging
import time

import qtawesome as qta

from core.i18n import _
from core.media_size import coerce_size_bytes, format_size_label
from core.qt_compat import (
    QPoint,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    Qt,
    QTimer,
    QVBoxLayout,
)
from core.utils import redact_url
from ui.themes import get_theme

logger = logging.getLogger("SnapDownloader")


class DownloadsRenderController:
    def __init__(self, window):
        self.window = window

    @staticmethod
    def _live_badge_text(item: dict) -> str:
        live_status = str((item or {}).get("live_status", "") or "").strip().lower()
        if bool((item or {}).get("is_live", False)) or live_status in {"is_live", "live"}:
            return _("LIVE")
        if bool((item or {}).get("was_live", False)) or live_status == "was_live":
            return _("WAS LIVE")
        return ""

    @staticmethod
    def _media_mode(item: dict) -> str:
        mode = str((item or {}).get("mode", "") or "").strip().lower()
        if mode in {"audio", "sound", "صوت"}:
            return "audio"
        return "video"

    def visible_download_rows_window(self, buffer_rows: int = 8) -> tuple[int, int]:
        model = self.window.downloads_view.downloads_model
        total_rows = model.rowCount()
        if total_rows <= 0:
            return (0, -1)
        list_view = self.window.downloads_view.downloads_list
        viewport = list_view.viewport()
        if viewport is None:
            return (0, min(total_rows - 1, 12))
        rect = viewport.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return (0, min(total_rows - 1, 15))

        top_index = list_view.indexAt(rect.topLeft())
        bottom_probe = QPoint(rect.left() + 4, max(rect.top(), rect.bottom() - 4))
        bottom_index = list_view.indexAt(bottom_probe)
        if top_index.isValid():
            first_row = int(top_index.row())
        else:
            row_hint = list_view.sizeHintForRow(0) if total_rows > 0 else 0
            row_hint = max(1, int(row_hint or 84))
            first_row = max(0, int(list_view.verticalScrollBar().value() / row_hint))
        if bottom_index.isValid():
            last_row = int(bottom_index.row())
        else:
            last_row = min(total_rows - 1, first_row + 10)
        first_row = max(0, first_row - max(0, int(buffer_rows)))
        last_row = min(total_rows - 1, last_row + max(0, int(buffer_rows)))
        return (first_row, last_row)

    def render_download_entries_batch(self, generation: int, batch_size: int = 16):
        if generation != self.window._downloads_render_generation or self.window._downloads_render_loading:
            return
        self.window._downloads_render_loading = True
        try:
            model = self.window.downloads_view.downloads_model
            list_view = self.window.downloads_view.downloads_list
            start, end = self.visible_download_rows_window(buffer_rows=8)
            if end < start:
                return
            target_rows = set(range(start, end + 1))

            target_queue_indices = set()
            for r in target_rows:
                item = model.get_item(r)
                if item is not None:
                    q_idx = item.get("queue_index")
                    if isinstance(q_idx, int):
                        target_queue_indices.add(int(q_idx))

            for q_idx in list(self.window._active_download_card_refs.keys()):
                if q_idx not in target_queue_indices:
                    self.window._active_download_card_refs.pop(q_idx, None)

            for existing_row in list(self.window._rendered_download_rows.keys()):
                if existing_row in target_rows:
                    continue
                model_index = model.index(int(existing_row), 0)
                if model_index.isValid():
                    try:
                        widget = list_view.indexWidget(model_index)
                        if widget is not None:
                            list_view.setIndexWidget(model_index, None)
                            widget.hide()
                            widget.setParent(None)
                    except RuntimeError:
                        pass
                self.window._rendered_download_rows.pop(existing_row, None)

            rendered_now = 0
            pending_more = False
            for row_index in sorted(target_rows):
                if rendered_now >= max(1, int(batch_size)):
                    pending_more = True
                    break
                model_index = model.index(row_index, 0)
                if not model_index.isValid():
                    continue
                item = model.get_item(row_index)
                if item is None:
                    continue
                cache_key = self.window.download_controller.download_entry_cache_key(item, row_index)
                existing_key = self.window._rendered_download_rows.get(row_index)
                if existing_key == cache_key and list_view.indexWidget(model_index) is not None:
                    queue_index = item.get("queue_index")
                    if isinstance(queue_index, int):
                        try:
                            widget = list_view.indexWidget(model_index)
                            refs = getattr(widget, "_active_refs", None)
                            if isinstance(refs, dict):
                                self.window._active_download_card_refs[int(queue_index)] = refs
                        except RuntimeError:
                            pass
                    continue
                try:
                    self.add_download_entry_card(item, row_index)
                    self.window._rendered_download_rows[row_index] = cache_key
                    rendered_now += 1
                except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
                    logger.warning(f"تعذر رسم عنصر في قائمة التحميلات: {exc}")
                    self.add_error_card(row_index, str(exc))
                    self.window._rendered_download_rows[row_index] = f"error:{row_index}"
                    rendered_now += 1
            if pending_more:
                QTimer.singleShot(0, lambda g=generation: self.render_download_entries_batch(g, batch_size=batch_size))
        finally:
            self.window._downloads_render_loading = False
        self.window._schedule_visible_thumbnail_load(0)
        self.window.downloads_view.downloads_list.update()

    def on_downloads_list_scrolled(self, _value: int):
        self.window._schedule_visible_thumbnail_load(40)
        self.window._maybe_render_more_download_entries(self.window._downloads_render_generation)

    def on_downloads_list_range_changed(self, _min_value: int, _max_value: int):
        self.window._schedule_visible_thumbnail_load(0)
        self.window._maybe_render_more_download_entries(self.window._downloads_render_generation)

    def maybe_render_more_download_entries(self, generation: int, force: bool = False):
        if generation != self.window._downloads_render_generation:
            return
        if self.window._downloads_render_loading:
            return
        if force:
            self.render_download_entries_batch(generation, batch_size=24)
            return
        self.render_download_entries_batch(generation, batch_size=12)

    def add_error_card(self, row_index: int, error_msg: str):
        t = get_theme(self.window.theme)
        card = QFrame()
        card.setObjectName("playlist_row")
        card.setStyleSheet(
            f"""
            QFrame#playlist_row {{
                background: {t['panel']};
                border-radius: 12px;
                border: 1px solid {t['danger']};
            }}
        """
        )
        h = QHBoxLayout(card)
        h.setContentsMargins(12, 12, 12, 12)
        lbl = QLabel(f"{_('خطأ في عرض العنصر')}: {error_msg}")
        lbl.setStyleSheet(f"color: {t['danger']}; font-weight: bold;")
        h.addWidget(lbl)

        index = self.window.downloads_view.downloads_model.index(row_index, 0)
        self.window.downloads_view.downloads_list.setIndexWidget(index, card)

    def downloads_style_pack(self, t: dict) -> dict:
        theme_key = str(self.window.theme or "default")
        cached = self.window._downloads_styles_cache.get(theme_key)
        if cached is not None:
            return cached
        base_btn = (
            f"QPushButton {{ background: {t['panel_soft']}; color: {t['text']}; border: none; "
            "border-radius: 6px; font-size: 13px; font-weight: bold; padding: 0 12px; } "
            f"QPushButton:hover {{ background: {t['border']}; }}"
        )
        pack = {
            "card": (
                f"QFrame#playlist_row {{ background: {t['panel']}; border-radius: 12px; border: 1px solid {t['border']}; }} "
                f"QFrame#playlist_row:hover {{ background: {t['panel_soft']}; border: 1px solid {t['accent']}; }} "
                f"QLabel#status_badge {{ font-size: 12px; font-weight: bold; padding: 4px 8px; border-radius: 6px; background: {t['panel_soft']}; color: {t['muted']}; }} "
                f"QLabel#status_badge[status_class='text-success'] {{ color: {t['success']}; }} "
                f"QLabel#status_badge[status_class='text-danger'] {{ color: {t['danger']}; }} "
                f"QLabel#status_badge[status_class='text-warning'] {{ color: {t['warning']}; }} "
                f"QLabel#status_badge[status_class='text-accent'] {{ color: {t['accent']}; }}"
            ),
            "thumb_box": f"QFrame#thumb_preview {{ border-radius: 8px; background: {t['bg']}; border: 1px solid {t['border']}; }}",
            "title": f"font-size: 15px; font-weight: bold; color: {t['text']}; background: transparent; border: none;",
            "details": f"font-size: 13px; color: {t['muted']}; background: transparent; border: none;",
            "eta": f"font-size: 12px; font-weight: bold; color: {t['text']}; background: transparent; border: none;",
            "meta": f"font-size: 13px; color: {t['muted']}; background: transparent; border: none;",
            "badge": f"background: {t['accent']}; color: white; border-radius: 4px; font-size: 11px; font-weight: bold; padding: 3px 6px;",
            "progress": (
                f"QProgressBar {{ background-color: {t['bg']}; border-radius: 4px; border: 1px solid {t['border']}; }} "
                f"QProgressBar::chunk {{ background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {t['accent']}, stop:1 {t['accent_2']}); border-radius: 4px; }}"
            ),
            "btn_base": base_btn,
            "btn_success": base_btn
            + f" QPushButton {{ color: {t['success']}; background: {t['success']}22; }} QPushButton:hover {{ background: {t['success']}33; }}",
            "btn_warning": base_btn
            + f" QPushButton {{ color: {t['warning']}; background: {t['warning']}22; }} QPushButton:hover {{ background: {t['warning']}33; }}",
            "btn_accent": base_btn
            + f" QPushButton {{ color: {t['accent']}; background: {t['accent']}22; }} QPushButton:hover {{ background: {t['accent']}33; }}",
            "btn_danger": base_btn
            + f" QPushButton {{ color: {t['danger']}; background: {t['danger']}22; }} QPushButton:hover {{ background: {t['danger']}33; }}",
        }
        self.window._downloads_styles_cache[theme_key] = pack
        return pack

    def add_download_entry_card(self, item: dict, row_index: int):
        try:
            t = get_theme(self.window.theme)
            styles = self.downloads_style_pack(t)
            queue_index = item.get("queue_index")
            model_index = self.window.downloads_view.downloads_model.index(row_index, 0)

            # QWidget caching here is prone to stale C++ object handles with setIndexWidget.
            # Prefer correctness over reuse; model cache keys still avoid unnecessary refresh work.
            cache_enabled = False
            cache_key = self.window.download_controller.download_entry_cache_key(item, row_index)
            render_sig = self.window.download_controller.download_entry_render_signature(item)
            if cache_enabled:
                cached = self.window._download_card_cache.get(cache_key)
                cached_sig = self.window._download_card_state.get(cache_key)
                if cached is not None and cached_sig == render_sig:
                    try:
                        _cache_obj_name = cached.objectName()
                        if isinstance(queue_index, int):
                            refs = getattr(cached, "_active_refs", None)
                            if isinstance(refs, dict):
                                self.window._active_download_card_refs[int(queue_index)] = refs
                        self.window.downloads_view.downloads_list.setIndexWidget(model_index, cached)
                        return
                    except RuntimeError:
                        # Cached QWidget was already deleted by Qt; drop stale cache entry.
                        self.window._download_card_cache.pop(cache_key, None)
                        self.window._download_card_state.pop(cache_key, None)
            card = QFrame()
            card.setObjectName("playlist_row")
            card.setStyleSheet(styles["card"])
            card.setMinimumHeight(96)

            h = QHBoxLayout(card)
            h.setContentsMargins(12, 12, 12, 12)
            h.setSpacing(15)
            thumb_w, thumb_h = 140, 80
            thumb_box = QFrame()
            thumb_box.setFixedSize(thumb_w, thumb_h)
            thumb_box.setObjectName("thumb_preview")
            thumb_box.setStyleSheet(styles["thumb_box"])
            tg = QGridLayout(thumb_box)
            tg.setContentsMargins(0, 0, 0, 0)
            tg.setSpacing(0)
            thumb = QLabel()
            thumb.setFixedSize(thumb_w, thumb_h)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setStyleSheet("background:transparent; border:none; border-radius: 8px;")
            self.window._set_thumb_placeholder(thumb)
            self.window._queue_download_thumbnail(model_index, item, thumb, thumb_w, thumb_h)
            tg.addWidget(thumb, 0, 0)
            duration_seconds = int(item.get("duration_seconds") or 0)
            if duration_seconds > 0:
                dur_lbl = QLabel(self.window._format_seconds(duration_seconds))
                dur_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                dur_lbl.setStyleSheet(
                    f"background: {t['bg']}CC; color: #FFFFFF; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold;"
                )
                tg.addWidget(dur_lbl, 0, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
            h.addWidget(thumb_box)
            center = QVBoxLayout()
            center.setSpacing(6)

            # --- Top Row: Title and Speed ---
            top_row = QHBoxLayout()
            title_text = item.get("title", _("رابط غير معروف"))
            if not title_text or title_text == "Unknown":
                title_text = redact_url(item.get("url", "")) or _("رابط غير معروف")
            title = QLabel(title_text)
            title.setStyleSheet(styles["title"])
            title.setWordWrap(True)
            title.setMaximumHeight(42)
            top_row.addWidget(title, 1)

            speed_text = str(item.get("speed", "--"))
            speed_lbl = QLabel(speed_text)
            speed_lbl.setStyleSheet(f"color: {t['text']}; font-weight: bold; font-size: 13px;")
            top_row.addWidget(speed_lbl, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)

            center.addLayout(top_row)

            media_mode = self._media_mode(item)
            badges_row = QHBoxLayout()
            badges_row.setSpacing(6)
            media_badge = QLabel(_("Audio") if media_mode == "audio" else _("Video"))
            media_badge.setStyleSheet(
                f"""
                color: {"#34D399" if media_mode == "audio" else "#60A5FA"};
                font-size: 10px;
                font-weight: 900;
                background-color: {"rgba(16, 185, 129, 0.15)" if media_mode == "audio" else "rgba(59, 130, 246, 0.15)"};
                border: 1px solid {"rgba(16, 185, 129, 0.35)" if media_mode == "audio" else "rgba(59, 130, 246, 0.35)"};
                border-radius: 4px;
                padding: 1px 8px;
            """
            )
            badges_row.addWidget(media_badge, 0, Qt.AlignmentFlag.AlignLeft)

            engine_text = str(item.get("engine", "") or "").strip()
            engine_lbl = QLabel(engine_text)
            engine_lbl.setObjectName("engine_badge")
            engine_lbl.setStyleSheet(
                """
                color: #A78BFA;
                font-size: 10px;
                font-weight: 900;
                background-color: rgba(139, 92, 246, 0.15);
                border: 1px solid rgba(139, 92, 246, 0.3);
                border-radius: 4px;
                padding: 1px 6px;
                text-transform: uppercase;
            """
            )
            if engine_text:
                badges_row.addWidget(engine_lbl, 0, Qt.AlignmentFlag.AlignLeft)
            live_badge_text = self._live_badge_text(item)
            if live_badge_text:
                live_badge = QLabel(live_badge_text)
                live_badge.setObjectName("status_badge")
                live_badge.setProperty("status_class", "text-danger" if live_badge_text == _("LIVE") else "text-warning")
                live_badge.style().unpolish(live_badge)
                live_badge.style().polish(live_badge)
                badges_row.addWidget(live_badge, 0, Qt.AlignmentFlag.AlignLeft)
            badges_row.addStretch(1)
            center.addLayout(badges_row)

            status_text = str(item.get("status", "pending")).upper()
            integrity_state = str(item.get("integrity_state", "") or "").strip().lower()
            is_missing = bool(item.get("file_missing", False)) or integrity_state == "missing"
            status_key = "MISSING" if is_missing and status_text in {"SUCCESS", "COMPLETED"} else status_text
            resume_text = ""
            duplicate_text = ""
            if isinstance(queue_index, int) and status_text not in {"SUCCESS", "COMPLETED", "DELETED"}:
                resume_text = self.window.download_controller.describe_resume_snapshot(item)
                duplicate_text = self.window.download_controller.describe_duplicate_report(item)
            if resume_text or duplicate_text:
                badges_col = QVBoxLayout()
                badges_col.setSpacing(4)
                if is_missing:
                    missing_lbl = QLabel(_("Missing file on disk"))
                    missing_lbl.setWordWrap(True)
                    missing_lbl.setObjectName("status_badge")
                    missing_lbl.setProperty("status_class", "text-danger")
                    missing_lbl.style().unpolish(missing_lbl)
                    missing_lbl.style().polish(missing_lbl)
                    badges_col.addWidget(missing_lbl)
                if resume_text:
                    resume_lbl = QLabel(resume_text)
                    resume_lbl.setWordWrap(True)
                    resume_lbl.setStyleSheet(
                        f"color: {t['success']}; font-size: 12px; font-weight: bold; background: transparent; border: none;"
                    )
                    badges_col.addWidget(resume_lbl)
                if duplicate_text:
                    duplicate_lbl = QLabel(duplicate_text)
                    duplicate_lbl.setWordWrap(True)
                    duplicate_lbl.setStyleSheet(
                        f"color: {t['warning']}; font-size: 12px; font-weight: bold; background: transparent; border: none;"
                    )
                    badges_col.addWidget(duplicate_lbl)
                center.addLayout(badges_col)

            # --- Bottom Row: Size, Status Text, Progress Bar, Actions ---
            bottom_row = QHBoxLayout()
            bottom_row.setSpacing(10)

            # 1. Folder Icon + Size
            folder_icon_lbl = QLabel()
            folder_icon_lbl.setPixmap(qta.icon("fa5s.folder", color=t["muted"]).pixmap(14, 14))
            bottom_row.addWidget(folder_icon_lbl)

            size_text = str(item.get("size") or item.get("size_text") or "").strip()
            if not size_text or size_text == "--":
                estimated_bytes = coerce_size_bytes(item.get("estimated_size_bytes") or item.get("size_bytes"))
                size_text = format_size_label(
                    estimated_bytes,
                    estimated=bool(item.get("size_is_estimate", True)),
                    empty="--",
                )
            size_lbl = QLabel(size_text)
            size_lbl.setStyleSheet(f"color: {t['muted']}; font-size: 13px;")
            bottom_row.addWidget(size_lbl)

            bottom_row.addStretch(1)

            # 2. Status Text
            status_map = {
                "RUNNING": _("Downloading"),
                "PENDING": _("Queued"),
                "PAUSED": _("Paused"),
                "FAILED": _("Failed"),
                "CANCELLED": _("Cancelled"),
                "SUCCESS": _("Completed"),
                "COMPLETED": _("Completed"),
                "SCHEDULED": _("Scheduled"),
                "MISSING": _("Missing"),
            }
            status_en = status_map.get(status_key, status_key)

            raw_progress = float(item.get("progress", 0) or 0)
            progress_val = 1 if 0 < raw_progress < 1 else max(0, min(100, int(round(raw_progress))))
            eta_text = str(item.get("eta", "--:--"))

            if status_key == "RUNNING":
                status_desc = f"{status_en} ({progress_val}%) | {_('Time left:')} {eta_text}"
            else:
                status_desc = status_en

            status_desc_lbl = QLabel(status_desc)
            status_color = t["danger"] if is_missing else t["accent"]
            status_desc_lbl.setStyleSheet(f"color: {status_color}; font-size: 13px; font-weight: bold;")
            bottom_row.addWidget(status_desc_lbl)

            # 3. Progress Bar
            bar = None
            if status_key not in ["SUCCESS", "COMPLETED", "DELETED", "MISSING"]:
                from ui.views.search_view import create_status_progress_bar

                bar = create_status_progress_bar(status=status_key.lower(), value=progress_val)
                bar.setRange(0, 100)
                bar.setValue(progress_val)
                bar.setFixedSize(100, 16)
                bottom_row.addWidget(bar)

            # 4. Action Buttons
            def _icon_btn(icon_name: str, color: str, tooltip: str):
                btn = QPushButton()
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setIcon(qta.icon(icon_name, color=color))
                btn.setFixedSize(28, 28)
                btn.setToolTip(tooltip)
                btn.setStyleSheet(
                    f"QPushButton {{ border: none; background: transparent; }} QPushButton:hover {{ background: {color}22; border-radius: 4px; }}"
                )
                return btn

            if status_key in ["SUCCESS", "COMPLETED"] and not is_missing:
                play_icon = _icon_btn("fa5s.external-link-alt", t["success"], _("فتح الملف"))
                play_icon.clicked.connect(lambda _, row=item: self.window._open_history_item_file(row))
                bottom_row.addWidget(play_icon)
                folder_btn = _icon_btn("fa5s.folder-open", t["accent"], _("فتح المجلد"))
                folder_btn.clicked.connect(lambda _, row=item: self.window._open_history_item_folder(row))
                bottom_row.addWidget(folder_btn)
            elif is_missing:
                if isinstance(queue_index, int):
                    locate_icon = _icon_btn("fa5s.search", t["accent"], _("Locate"))
                    locate_icon.clicked.connect(lambda _, i=queue_index: self.window._locate_queue_item_file(i))
                    bottom_row.addWidget(locate_icon)
                    relocate_icon = _icon_btn("fa5s.link", t["warning"], _("Relocate"))
                    relocate_icon.clicked.connect(lambda _, i=queue_index: self.window._relocate_queue_item_file(i))
                    bottom_row.addWidget(relocate_icon)
                    redownload_icon = _icon_btn("fa5s.redo", t["success"], _("Redownload"))
                    redownload_icon.clicked.connect(lambda _, i=queue_index: self.window._redownload_queue_item(i))
                    bottom_row.addWidget(redownload_icon)
                else:
                    folder_btn = _icon_btn("fa5s.folder-open", t["accent"], _("فتح المجلد"))
                    folder_btn.clicked.connect(lambda _, row=item: self.window._open_history_item_folder(row))
                    bottom_row.addWidget(folder_btn)
            else:
                queue_index = item.get("queue_index")
                if status_text == "RUNNING":
                    pause_icon = _icon_btn("fa5s.pause", t["warning"], _("إيقاف مؤقت"))
                    pause_icon.clicked.connect(lambda _, i=queue_index: self.window._pause_queue_item(i))
                    bottom_row.addWidget(pause_icon)
                elif status_text == "PAUSED":
                    resume_icon = _icon_btn("fa5s.play", t["success"], _("استكمال"))
                    resume_icon.clicked.connect(lambda _, i=queue_index: self.window._resume_queue_item(i))
                    bottom_row.addWidget(resume_icon)
                elif status_text == "PENDING":
                    pause_icon = _icon_btn("fa5s.pause", t["warning"], _("إيقاف مؤقت"))
                    pause_icon.clicked.connect(lambda _, i=queue_index: self.window._pause_queue_item(i))
                    bottom_row.addWidget(pause_icon)

                if status_text in {"FAILED", "CANCELLED", "PAUSED"}:
                    retry_icon = _icon_btn("fa5s.redo", t["accent"], _("إعادة المحاولة"))
                    retry_icon.clicked.connect(lambda _, i=queue_index: self.window._retry_queue_item(i))
                    bottom_row.addWidget(retry_icon)

                cancel_icon = _icon_btn("fa5s.times", t["danger"], _("إلغاء"))
                if status_text == "RUNNING":
                    cancel_icon.clicked.connect(lambda _, i=queue_index: self.window._cancel_queue_item(i))
                    bottom_row.addWidget(cancel_icon)

                delete_icon = _icon_btn("fa5s.trash-alt", t["danger"], _("حذف"))
                if status_text != "RUNNING":
                    delete_icon.clicked.connect(lambda _, i=queue_index: self.window._delete_queue_item(i))
                    bottom_row.addWidget(delete_icon)

            center.addLayout(bottom_row)
            if isinstance(queue_index, int) and status_text not in {"SUCCESS", "COMPLETED", "DELETED"}:
                limit_row = QHBoxLayout()
                limit_row.setSpacing(8)
                limit_title = QLabel("حد السرعة:")
                limit_title.setStyleSheet(f"color: {t['muted']}; font-size: 12px;")
                limit_value_lbl = QLabel(self.window._format_bandwidth_limit(int(item.get("bandwidth_limit_kbps", 0) or 0)))
                limit_value_lbl.setStyleSheet(f"color: {t['text']}; font-size: 12px; font-weight: bold;")
                limit_slider = QSlider(Qt.Orientation.Horizontal)
                limit_slider.setRange(0, 20000)
                limit_slider.setSingleStep(100)
                limit_slider.setPageStep(500)
                limit_slider.setValue(max(0, min(20000, int(item.get("bandwidth_limit_kbps", 0) or 0))))
                limit_slider.setFixedWidth(150)
                limit_slider.setToolTip("0 = غير محدود")

                def _on_limit_changed(value: int, label=limit_value_lbl):
                    label.setText(self.window._format_bandwidth_limit(value))

                limit_slider.valueChanged.connect(_on_limit_changed)
                apply_limit_btn = QPushButton("تطبيق")
                apply_limit_btn.setFixedHeight(28)
                apply_limit_btn.setStyleSheet(styles["btn_accent"])
                apply_limit_btn.setToolTip("يطبق الحد فوراً. أثناء التشغيل سيتم استئناف العنصر تلقائياً بالحد الجديد.")
                apply_limit_btn.clicked.connect(
                    lambda _=False, i=queue_index, slider=limit_slider: self.window._set_queue_item_bandwidth_limit(i, slider.value())
                )
                limit_row.addWidget(limit_title)
                limit_row.addWidget(limit_value_lbl)
                limit_row.addWidget(limit_slider, 1)
                limit_row.addWidget(apply_limit_btn)
                center.addLayout(limit_row)
            center.addStretch(1)

            if isinstance(item.get("queue_index"), int) and status_text == "RUNNING":
                refs = {
                    "card": card,
                    "details_label": status_desc_lbl,
                    "eta_label": status_desc_lbl,
                    "progress_bar": bar,
                    "speed_label": speed_lbl,
                    "engine_label": engine_lbl,
                    "_last_ui_tuple": (progress_val, speed_text, eta_text),
                    "_last_ui_ts": time.monotonic(),
                }
                self.window._active_download_card_refs[int(item["queue_index"])] = refs
                setattr(card, "_active_refs", refs)

            h.addLayout(center, 1)
            self.window.downloads_view.downloads_list.setIndexWidget(model_index, card)
        except Exception as exc:
            logger.error(f"[UI] Failed to render download card at row {row_index}: {exc}")
            try:
                card = QFrame()
                card.setFixedHeight(80)
                l = QHBoxLayout(card)
                err = QLabel(_("خطأ في عرض العنصر: {exc}").format(exc=exc))
                err.setStyleSheet("color: #ff5555; font-size: 12px;")
                l.addWidget(err)
                self.window.downloads_view.downloads_list.setIndexWidget(model_index, card)
            except Exception:
                pass

        if cache_enabled:
            self.window._download_card_cache[cache_key] = card
            self.window._download_card_state[cache_key] = render_sig
            while len(self.window._download_card_cache) > int(self.window._download_card_cache_limit):
                stale_key = next(iter(self.window._download_card_cache.keys()))
                stale_widget = self.window._download_card_cache.pop(stale_key, None)
                if stale_widget is not None:
                    try:
                        stale_widget.hide()
                        stale_widget.setParent(None)
                        stale_widget.deleteLater()
                    except RuntimeError:
                        pass
                self.window._download_card_state.pop(stale_key, None)
        self.window._rendered_download_rows[int(row_index)] = cache_key
