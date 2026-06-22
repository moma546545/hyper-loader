from __future__ import annotations

import os
from collections import defaultdict
from typing import DefaultDict

from .qt_compat import QFileSystemWatcher, QObject, QTimer, Signal

_IGNORED_TRACKING_SUFFIXES = {".part", ".tmp", ".ytdl"}


class FileIntegrityWatcher(QObject):
    file_missing = Signal(int, str)

    def __init__(self, parent: QObject | None = None, debounce_ms: int = 750) -> None:
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._queue_path_check)
        self._path_by_task: dict[int, str] = {}
        self._tasks_by_path: DefaultDict[str, set[int]] = defaultdict(set)
        self._pending_paths: set[str] = set()
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(max(100, int(debounce_ms)))
        self._debounce_timer.timeout.connect(self._flush_pending_checks)

    def track_completed_file(self, task_index: int, path: str) -> None:
        if not isinstance(task_index, int) or task_index < 0:
            return
        normalized_path = self._normalize_path(path)
        if not normalized_path or self._should_ignore_path(normalized_path):
            return
        previous_path = self._path_by_task.get(task_index, "")
        if previous_path and previous_path != normalized_path:
            self._detach_task_path(task_index, previous_path)
        self._path_by_task[task_index] = normalized_path
        self._tasks_by_path[normalized_path].add(task_index)
        self._ensure_path_registered(normalized_path)

    def untrack_task(self, task_index: int) -> None:
        if not isinstance(task_index, int):
            return
        previous_path = self._path_by_task.pop(task_index, "")
        if previous_path:
            self._detach_task_path(task_index, previous_path)

    def _queue_path_check(self, path: str) -> None:
        normalized_path = self._normalize_path(path)
        if not normalized_path:
            return
        self._pending_paths.add(normalized_path)
        if not self._debounce_timer.isActive():
            self._debounce_timer.start()

    def _flush_pending_checks(self) -> None:
        pending_paths = tuple(self._pending_paths)
        self._pending_paths.clear()
        for path in pending_paths:
            if os.path.exists(path):
                self._ensure_path_registered(path)
                continue
            self._remove_path_from_watcher(path)
            for task_index in sorted(self._tasks_by_path.get(path, set())):
                self.file_missing.emit(task_index, path)

    def _ensure_path_registered(self, path: str) -> None:
        if not os.path.isfile(path):
            return
        tracked_files = set(self._watcher.files())
        if path in tracked_files:
            return
        self._watcher.addPath(path)

    def _remove_path_from_watcher(self, path: str) -> None:
        tracked_files = set(self._watcher.files())
        if path not in tracked_files:
            return
        self._watcher.removePath(path)

    def _detach_task_path(self, task_index: int, path: str) -> None:
        task_indices = self._tasks_by_path.get(path)
        if not task_indices:
            return
        task_indices.discard(task_index)
        if task_indices:
            return
        self._tasks_by_path.pop(path, None)
        self._remove_path_from_watcher(path)

    @staticmethod
    def _normalize_path(path: str) -> str:
        raw_path = str(path or "").strip()
        if not raw_path:
            return ""
        try:
            return os.path.normcase(os.path.abspath(raw_path))
        except Exception:
            return os.path.normcase(raw_path)

    @staticmethod
    def _should_ignore_path(path: str) -> bool:
        suffix = os.path.splitext(str(path or ""))[1].lower()
        return suffix in _IGNORED_TRACKING_SUFFIXES
