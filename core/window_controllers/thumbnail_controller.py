import os
import time

from core.constants import THUMBNAIL_VISIBLE_BATCH_PER_TICK, THUMBNAIL_WAITER_TTL_SECONDS
from core.qt_compat import Qt, QNetworkReply, QNetworkRequest, QPixmap, QUrl
from ui.themes import get_theme


class ThumbnailController:
    def __init__(self, window):
        self.window = window
        self._thumbnail_inflight = set()

    def trim_thumbnail_cache(self):
        with self.window._thumbnail_state_lock:
            while len(self.window.thumbnail_cache) > self.window.thumbnail_cache_max:
                self.window.thumbnail_cache.popitem(last=False)

    def trim_thumbnail_failed_locked(self, now: float | None = None):
        current_ts = float(now or time.time())
        ttl = max(1, int(getattr(self.window, "thumbnail_failed_ttl_seconds", 120) or 120))
        expired_keys = [
            key
            for key, failed_at in self.window.thumbnail_failed.items()
            if (current_ts - float(failed_at or 0.0)) >= ttl
        ]
        for key in expired_keys:
            self.window.thumbnail_failed.pop(key, None)
        while len(self.window.thumbnail_failed) > self.window.thumbnail_failed_max:
            self.window.thumbnail_failed.popitem(last=False)

    def thumbnail_failed_contains(self, cache_key: str) -> bool:
        key = str(cache_key or "").strip()
        if not key:
            return False
        now = time.time()
        with self.window._thumbnail_state_lock:
            failed_at = self.window.thumbnail_failed.get(key)
            if failed_at is None:
                return False
            ttl = max(1, int(getattr(self.window, "thumbnail_failed_ttl_seconds", 120) or 120))
            if (now - float(failed_at or 0.0)) >= ttl:
                self.window.thumbnail_failed.pop(key, None)
                return False
            self.window.thumbnail_failed.move_to_end(key)
            return True

    def clear_thumbnail_failed(self, cache_key: str):
        key = str(cache_key or "").strip()
        if not key:
            return
        with self.window._thumbnail_state_lock:
            self.window.thumbnail_failed.pop(key, None)

    def mark_thumbnail_failed(self, cache_key: str):
        key = str(cache_key or "").strip()
        if not key:
            return
        with self.window._thumbnail_state_lock:
            self.window.thumbnail_failed[key] = time.time()
            self.window.thumbnail_failed.move_to_end(key)
            self.trim_thumbnail_failed_locked()

    def load_thumbnail(self, url: str, width: int = 132, height: int = 74):
        thumb_url = str(url or "").strip()
        if not thumb_url:
            return None
        cache_key = f"{thumb_url}|{width}x{height}"
        with self.window._thumbnail_state_lock:
            if cache_key in self.window.thumbnail_cache:
                self.window.thumbnail_cache.move_to_end(cache_key)
                return self.window.thumbnail_cache[cache_key]
        if self.thumbnail_failed_contains(cache_key):
            return None
        if os.path.isfile(thumb_url):
            pixmap = QPixmap(thumb_url)
            if pixmap.isNull():
                self.mark_thumbnail_failed(cache_key)
                return None
            scaled = pixmap.scaled(
                width,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            with self.window._thumbnail_state_lock:
                self.window.thumbnail_cache[cache_key] = scaled
                self.window.thumbnail_cache.move_to_end(cache_key)
            self.trim_thumbnail_cache()
            return scaled
        return None

    def queue_download_thumbnail(self, model_index, item: dict, label, width: int, height: int):
        self.window.downloads_thumbnail_jobs.append(
            {
                "model_index": model_index,
                "item": item,
                "label": label,
                "width": width,
                "height": height,
            }
        )

    def schedule_visible_thumbnail_load(self, delay_ms: int = 0):
        self.window._visible_thumb_timer.start(max(0, int(delay_ms)))

    def process_visible_thumbnail_jobs(self):
        if not self.window.downloads_thumbnail_jobs:
            return
        if self.window.active_view != "downloads":
            return
        viewport = self.window.downloads_view.downloads_list.viewport()
        if viewport is None:
            return
        viewport_rect = viewport.rect()
        remaining = []
        loaded = 0
        max_per_tick = THUMBNAIL_VISIBLE_BATCH_PER_TICK
        for job in self.window.downloads_thumbnail_jobs:
            model_index = job.get("model_index")
            label = job.get("label")
            if model_index is None or label is None:
                continue
            if loaded >= max_per_tick:
                remaining.append(job)
                continue
            item_rect = self.window.downloads_view.downloads_list.visualRect(model_index)
            if not item_rect.isValid():
                remaining.append(job)
                continue
            if item_rect.bottom() < 0 or item_rect.top() > viewport_rect.height():
                remaining.append(job)
                continue
            try:
                self.set_download_card_thumbnail(
                    job.get("item", {}),
                    label,
                    int(job.get("width", 140)),
                    int(job.get("height", 80)),
                )
            except RuntimeError:
                continue
            loaded += 1
        self.window.downloads_thumbnail_jobs = remaining
        if self.window.downloads_thumbnail_jobs and loaded >= max_per_tick:
            self.schedule_visible_thumbnail_load(20)

    def set_thumb_placeholder(self, label):
        t = get_theme(self.window.theme)
        try:
            label.setText("🎬")
            label.setStyleSheet(f"background:transparent; border:none; color:{t['muted']}; font-size:32px;")
        except RuntimeError:
            return

    def set_download_card_thumbnail(self, item: dict, label, width: int, height: int):
        thumb_url = str(item.get("thumbnail", "") or "").strip()
        if not thumb_url:
            self.set_thumb_placeholder(label)
            return
        pix = self.load_thumbnail(thumb_url, width, height)
        if pix is not None:
            try:
                label.setPixmap(pix)
                label.setText("")
            except RuntimeError:
                return
            return
        if not thumb_url.lower().startswith(("http://", "https://")):
            self.mark_thumbnail_failed(f"{thumb_url}|{width}x{height}")
            self.set_thumb_placeholder(label)
            return
        cache_key = f"{thumb_url}|{width}x{height}"
        should_start_request = False
        if self.thumbnail_failed_contains(cache_key):
            self.set_thumb_placeholder(label)
            return
        with self.window._thumbnail_state_lock:
            waiters = self.window.thumbnail_waiters.setdefault(cache_key, [])
            waiters.append(label)
            self.window._thumbnail_waiter_timestamps[cache_key] = time.time()
            if len(waiters) > 1:
                return
            if self.window._active_thumbnail_requests >= self.window._max_concurrent_thumbnails:
                return
            if thumb_url in self._thumbnail_inflight:
                return
            self.window._active_thumbnail_requests += 1
            self._thumbnail_inflight.add(thumb_url)
            should_start_request = True
        if not should_start_request:
            return
        request = QNetworkRequest(QUrl(thumb_url))
        request.setTransferTimeout(2500)
        reply = self.window.net_manager.get(request)
        reply.setParent(self.window)
        reply.finished.connect(
            lambda r=reply, u=thumb_url, w=width, h=height: self.on_download_thumbnail_loaded(r, u, w, h)
        )

    def on_download_thumbnail_loaded(self, reply: QNetworkReply, thumb_url: str, width: int, height: int):
        cache_key = f"{thumb_url}|{width}x{height}"
        with self.window._thumbnail_state_lock:
            waiters = list(self.window.thumbnail_waiters.pop(cache_key, []))
            self.window._thumbnail_waiter_timestamps.pop(cache_key, None)
            self._thumbnail_inflight.discard(thumb_url)
        scaled = None
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = reply.readAll()
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                scaled = pixmap.scaled(
                    width,
                    height,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                with self.window._thumbnail_state_lock:
                    self.window.thumbnail_failed.pop(cache_key, None)
                    self.window.thumbnail_cache[cache_key] = scaled
                    self.window.thumbnail_cache.move_to_end(cache_key)
                self.trim_thumbnail_cache()
            else:
                self.mark_thumbnail_failed(cache_key)
        else:
            self.mark_thumbnail_failed(cache_key)
        reply.deleteLater()
        with self.window._thumbnail_state_lock:
            self.window._active_thumbnail_requests = max(0, self.window._active_thumbnail_requests - 1)
        for label in waiters:
            try:
                if scaled is not None:
                    label.setPixmap(scaled)
                    label.setText("")
                else:
                    self.set_thumb_placeholder(label)
            except RuntimeError:
                continue
        self.process_pending_thumbnails()

    def process_pending_thumbnails(self):
        with self.window._thumbnail_state_lock:
            if self.window._active_thumbnail_requests >= self.window._max_concurrent_thumbnails:
                return
            pending_keys = list(self.window.thumbnail_waiters.keys())
            for cache_key in pending_keys:
                if self.window._active_thumbnail_requests >= self.window._max_concurrent_thumbnails:
                    return
                if cache_key in self.window.thumbnail_cache:
                    continue
                failed_at = self.window.thumbnail_failed.get(cache_key)
                if failed_at is not None:
                    ttl = max(1, int(getattr(self.window, "thumbnail_failed_ttl_seconds", 120) or 120))
                    if (time.time() - float(failed_at or 0.0)) < ttl:
                        self.window.thumbnail_failed.move_to_end(cache_key)
                        continue
                    self.window.thumbnail_failed.pop(cache_key, None)
                if cache_key not in self.window.thumbnail_waiters:
                    continue
                parts = cache_key.split("|")
                if len(parts) < 2:
                    continue
                thumb_url = parts[0]
                dims = parts[1].split("x")
                if len(dims) < 2:
                    continue
                try:
                    width, height = int(dims[0]), int(dims[1])
                except ValueError:
                    continue
                if thumb_url in self._thumbnail_inflight:
                    continue
                self.window._active_thumbnail_requests += 1
                self._thumbnail_inflight.add(thumb_url)
                request = QNetworkRequest(QUrl(thumb_url))
                request.setTransferTimeout(2500)
                reply = self.window.net_manager.get(request)
                reply.setParent(self.window)
                reply.finished.connect(
                    lambda r=reply, u=thumb_url, w=width, h=height: self.on_download_thumbnail_loaded(r, u, w, h)
                )

    def cleanup_stale_thumbnail_waiters(self):
        now = time.time()
        with self.window._thumbnail_state_lock:
            stale_keys = [
                key for key, ts in self.window._thumbnail_waiter_timestamps.items()
                if now - ts > THUMBNAIL_WAITER_TTL_SECONDS
            ]
            for key in stale_keys:
                self.window.thumbnail_waiters.pop(key, None)
                self.window._thumbnail_waiter_timestamps.pop(key, None)
        for key in stale_keys:
            self.mark_thumbnail_failed(key)
