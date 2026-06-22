import re

with open('core/queue_manager.py', 'r', encoding='utf-8') as f:
    text = f.read()

pattern = re.compile(r'def add_tasks\(.*?\):.*?with self._lock:.*?(?:try:\s*limit = max\(0, int\(max_tasks or 0\)\))', re.DOTALL)

replacement = '''def add_tasks(self, tasks: list[DownloadTask]):
        """Adds a list of tasks to the queue. Enforces MAX_QUEUE_SIZE."""
        tasks_copies = [DownloadTask.from_mapping(t if isinstance(t, dict) else {}) for t in tasks]
        with self._lock:
            seen_task_uuids = {
                str(item.get("task_uuid", "") or "").strip()
                for item in self.items
                if isinstance(item, dict) and str(item.get("task_uuid", "") or "").strip()
            }
            for task_copy in tasks_copies:
                _ensure_unique_task_identity(task_copy, seen_task_uuids)
            available = self.MAX_QUEUE_SIZE - len(self.items)
            accepted = tasks_copies[:available]
            rejected = len(tasks_copies) - len(accepted)
            if rejected > 0:
                logger.warning(f"[H-07] Queue capacity: accepted {len(accepted)}, rejected {rejected} tasks.")
            self.items.extend(accepted)
            if accepted:
                self._bump_change_token()
        if rejected > 0:
            self.queue_limit_exceeded.emit(rejected)
        logger.info(f"Adding {len(accepted)} tasks to queue.")
        self.queue_changed.emit()

    def start_queue(self):
        """Starts processing the queue."""
        should_start = False
        with self._lock:
            if self.is_running or not self.items:
                return
            logger.info("Starting queue processing.")
            self.is_running = True
            self.is_paused = False
            self.current_index = -1
            should_start = True
        if should_start:
            try:
                from core.system_keepalive import SystemKeepAlive
                SystemKeepAlive.prevent_sleep()
            except ImportError:
                pass
            self.queue_started.emit()

    def restore_stale_running_tasks(self, active_worker_ids: set[int] | None = None) -> int:
        """
        Reset queue items left in a running state when no live worker owns them.
        Returns the number of restored tasks.
        """
        normalized_active_ids = {
            int(idx)
            for idx in (active_worker_ids or set())
            if isinstance(idx, int) and int(idx) >= 0
        }
        restored = 0
        with self._lock:
            for idx, item in enumerate(self.items):
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", TaskStatus.PENDING.value) or TaskStatus.PENDING.value).lower()
                if status != TaskStatus.RUNNING.value or idx in normalized_active_ids:
                    continue
                item["status"] = TaskStatus.PENDING.value
                item["next_retry_at"] = 0
                restored += 1
            if restored > 0:
                self._bump_change_token()
        if restored > 0:
            self.queue_changed.emit()
        return restored

    def stop_queue(self):
        """Stops the queue and resets its state."""
        should_emit = False
        with self._lock:
            if not self.is_running:
                return
            logger.info("Stopping queue processing.")
            self.is_running = False
            self.is_paused = False
            self.current_index = -1
            should_emit = True
        if should_emit:
            try:
                from core.system_keepalive import SystemKeepAlive
                SystemKeepAlive.allow_sleep(force=True)
            except ImportError:
                pass
            self.queue_stopped.emit()

    def pause_queue(self):
        """Pauses the queue."""
        should_emit = False
        with self._lock:
            if not self.is_running or self.is_paused:
                return
            logger.info("Pausing queue.")
            self.is_paused = True
            should_emit = True
        if should_emit:
            self.queue_paused.emit()

    def resume_queue(self):
        """Resumes a paused queue."""
        should_resume = False
        with self._lock:
            if not self.is_running or not self.is_paused:
                return
            logger.info("Resuming queue.")
            self.is_paused = False
            should_resume = True
        if should_resume:
            self.queue_resumed.emit()

    def set_runtime_state(self, *, is_running: bool | None = None, is_paused: bool | None = None):
        """Synchronize orchestration state without triggering worker scheduling."""
        with self._lock:
            if is_running is not None:
                self.is_running = bool(is_running)
                if not self.is_running:
                    self.current_index = -1
            if is_paused is not None:
                requested_paused = bool(is_paused)
                self.is_paused = requested_paused if self.is_running else False

    def plan_parallel_start(
        self,
        max_tasks: int,
        *,
        active_worker_ids: set[int] | None = None,
        priority: str = "fifo",
        task_size_getter=None,
        now_ts: float | None = None,
        include_queue_items: bool = True,
    ) -> dict:
        """
        Build a single scheduling plan used by both the UI controller and the
        internal queue scheduler so domain throttling and readiness checks live
        in one place.
        """
        try:
            limit = max(0, int(max_tasks or 0))'''

match = pattern.search(text)
if match:
    new_text = text[:match.start()] + replacement + text[match.end():]
    with open('core/queue_manager.py', 'w', encoding='utf-8') as f:
        f.write(new_text)
    print('REGEX REPLACEMENT SUCCESS')
else:
    print('REGEX MATCH FAILED')
