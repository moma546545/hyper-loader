import logging

from .task_types import TaskStatus


logger = logging.getLogger("SnapDownloader.QueueAI")


class SmartQueueAI:
    @staticmethod
    def calculate_priority_score(task: dict, current_network_speed_kbps: int = 1000) -> float:
        score = 100.0

        format_type = str(task.get("format", "mp4") or "mp4").lower()
        if format_type in ["mp3", "m4a", "wav"]:
            score += 50.0

        estimated_size = int(task.get("estimated_size_bytes", 0) or 0)
        if estimated_size > 0:
            if estimated_size < (50 * 1024 * 1024):
                score += 40.0
            elif estimated_size > (1024 * 1024 * 1024):
                score -= 30.0

        retry_count = int(task.get("retry_count", 0) or 0)
        if retry_count > 0:
            score -= 15.0 * retry_count

        limit_kbps = int(task.get("bandwidth_limit_kbps", 0) or 0)
        if limit_kbps > 0 and limit_kbps < current_network_speed_kbps:
            score -= 10.0

        # Prefer tasks with partial downloaded data to maximize resume efficiency.
        resume = task.get("resume")
        if isinstance(resume, dict):
            partial_bytes = int(resume.get("partials_total_bytes", 0) or 0)
            if partial_bytes > 0:
                score += 20.0

        # Time-aware boost for scheduled items that are already due.
        scheduled_at = float(task.get("scheduled_at", 0) or 0)
        if scheduled_at > 0:
            import time
            if scheduled_at <= time.time():
                score += 15.0

        return score

    @staticmethod
    def optimize_queue(queue_items: list[dict]) -> list[dict]:
        active_statuses = {TaskStatus.RUNNING.value, TaskStatus.DOWNLOADING.value}
        pending_statuses = {TaskStatus.QUEUED.value, TaskStatus.PAUSED.value, TaskStatus.PENDING.value}
        completed_statuses = {
            TaskStatus.COMPLETED.value,
            TaskStatus.ERROR.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        }
        active_tasks = [item for item in queue_items if str(item.get("status", "")).lower() in active_statuses]
        pending_tasks = [item for item in queue_items if str(item.get("status", "")).lower() in pending_statuses]
        completed_tasks = [item for item in queue_items if str(item.get("status", "")).lower() in completed_statuses]

        pending_tasks.sort(key=lambda t: SmartQueueAI.calculate_priority_score(t), reverse=True)

        logger.info("[Queue AI] Queue has been dynamically reordered based on priority heuristics.")

        return active_tasks + pending_tasks + completed_tasks
