import copy

from core.task_types import (
    ACTIVE_TASK_STATUSES,
    DownloadTask,
    PostActionType,
    StatsState,
    TaskStatus,
    normalize_post_action,
    normalize_task_status,
)


def test_download_task_stays_dict_compatible():
    task = DownloadTask(url="https://example.com/watch", format="MP4")

    assert isinstance(task, dict)
    assert task["url"] == "https://example.com/watch"
    assert task["format"] == "MP4"

    task["mode"] = "audio"
    assert task.mode == "audio"

    task.quality = "320kbps"
    assert task["quality"] == "320kbps"


def test_download_task_deepcopy_preserves_model_type():
    task = DownloadTask(url="https://example.com/watch", trims=[{"start": "00:01"}])

    cloned = copy.deepcopy(task)

    assert isinstance(cloned, DownloadTask)
    assert cloned["url"] == task["url"]
    assert cloned["trims"] == [{"start": "00:01"}]


def test_stats_state_defaults_are_isolated():
    left = StatsState()
    right = StatsState()

    left["download_history"].append({"url": "https://example.com"})

    assert right["download_history"] == []


def test_normalize_task_status_uses_known_values_and_fallback():
    assert normalize_task_status("RUNNING") == TaskStatus.RUNNING.value
    assert normalize_task_status("unknown") == TaskStatus.PENDING.value
    assert normalize_task_status("", default=TaskStatus.FAILED) == TaskStatus.FAILED.value


def test_normalize_post_action_supports_known_and_extension_safe_values():
    assert normalize_post_action("PLAY_SOUND") == PostActionType.PLAY_SOUND.value
    assert normalize_post_action("custom_hook", allow_unknown=True) == "custom_hook"
    assert normalize_post_action("custom_hook") == PostActionType.NONE.value


def test_active_statuses_include_runtime_worker_states():
    assert TaskStatus.RUNNING.value in ACTIVE_TASK_STATUSES
    assert TaskStatus.DOWNLOADING.value in ACTIVE_TASK_STATUSES
    assert TaskStatus.PROCESSING.value in ACTIVE_TASK_STATUSES
