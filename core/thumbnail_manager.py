import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Optional

from core.qt_compat import QPixmap, Qt, QNetworkAccessManager, QNetworkRequest, QNetworkReply, QUrl, QLabel, QTimer, QObject

logger = logging.getLogger("SnapDownloader.ThumbnailManager")

class ThumbnailManager(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.thumbnail_cache = OrderedDict()
        self.thumbnail_cache_max = 120
        self.thumbnail_failed = OrderedDict()
        self.thumbnail_failed_max = 500
        self.thumbnail_failed_ttl_seconds = 240
        self.thumbnail_waiters = {}
        self._thumbnail_waiter_timestamps = {}
        self._thumbnail_state_lock = threading.RLock()
        self._active_thumbnail_requests = 0
        self._max_concurrent_thumbnails = 5
        self.jobs = []
        
        self.net_manager = QNetworkAccessManager(self)

    def get_thumbnail(self, url: str, width: int = 132, height: int = 74) -> Optional[QPixmap]:
        thumb_url = str(url or "").strip()
        if not thumb_url:
            return None
        cache_key = f"{thumb_url}|{width}x{height}"
        
        with self._thumbnail_state_lock:
            if cache_key in self.thumbnail_cache:
                self.thumbnail_cache.move_to_end(cache_key)
                return self.thumbnail_cache[cache_key]
        
        if self._is_failed(cache_key):
            return None
            
        if os.path.isfile(thumb_url):
            pixmap = QPixmap(thumb_url)
            if pixmap.isNull():
                self._mark_failed(cache_key)
                return None
            scaled = pixmap.scaled(
                width,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            with self._thumbnail_state_lock:
                self.thumbnail_cache[cache_key] = scaled
                self.thumbnail_cache.move_to_end(cache_key)
            self._trim_cache()
            return scaled
        return None

    def _is_failed(self, cache_key: str) -> bool:
        with self._thumbnail_state_lock:
            if cache_key not in self.thumbnail_failed:
                return False
            ts = float(self.thumbnail_failed.get(cache_key, 0.0) or 0.0)
            if (time.time() - ts) > self.thumbnail_failed_ttl_seconds:
                self.thumbnail_failed.pop(cache_key, None)
                return False
            return True

    def _mark_failed(self, cache_key: str):
        with self._thumbnail_state_lock:
            self.thumbnail_failed[cache_key] = time.time()
            if len(self.thumbnail_failed) > self.thumbnail_failed_max:
                self.thumbnail_failed.popitem(last=False)

    def _trim_cache(self):
        with self._thumbnail_state_lock:
            while len(self.thumbnail_cache) > self.thumbnail_cache_max:
                self.thumbnail_cache.popitem(last=False)
