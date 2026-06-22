import copy
from enum import Enum
from uuid import uuid4
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, Mapping


class _DataclassDictModel(dict):
    """Dict-compatible dataclass model used to add defaults without breaking old code."""

    def __post_init__(self):
        for data_field in fields(self):
            dict.__setitem__(self, data_field.name, copy.deepcopy(getattr(self, data_field.name)))

    def __setattr__(self, key: str, value):
        object.__setattr__(self, key, value)
        if key in self._field_names():
            dict.__setitem__(self, key, value)

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        if key in self._field_names():
            object.__setattr__(self, key, value)

    def update(self, *args, **kwargs):
        payload = dict(*args, **kwargs)
        for key, value in payload.items():
            self[key] = value

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        if key in self._field_names():
            data_field = next((f for f in fields(self) if f.name == key), None)
            if data_field is not None:
                if data_field.default_factory is not MISSING:
                    object.__setattr__(self, key, data_field.default_factory())
                elif data_field.default is not MISSING:
                    object.__setattr__(self, key, copy.deepcopy(data_field.default))
                else:
                    object.__setattr__(self, key, None)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None):
        instance = cls()
        if data:
            instance.update(dict(data))
        return instance

    def to_dict(self) -> dict[str, Any]:
        return dict(self)

    def copy(self):
        return type(self).from_mapping(self)

    def __deepcopy__(self, memo):
        return type(self).from_mapping(copy.deepcopy(dict(self), memo))

    @classmethod
    def _field_names(cls) -> set[str]:
        return {data_field.name for data_field in fields(cls)}


class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    MERGING = "merging"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SUCCESS = "success"
    COMPLETED = "completed"
    ERROR = "error"
    DELETED = "deleted"


class PostActionType(str, Enum):
    NONE = "none"
    OPEN_FOLDER = "open_folder"
    PLAY_SOUND = "play_sound"
    SHUTDOWN = "shutdown"
    RUN_SCRIPT = "run_script"
    TRANSCRIBE = "transcribe"


_TASK_STATUS_VALUES = frozenset(status.value for status in TaskStatus)
_POST_ACTION_VALUES = frozenset(action.value for action in PostActionType)

ACTIVE_TASK_STATUSES = frozenset(
    {
        TaskStatus.DOWNLOADING.value,
        TaskStatus.PROCESSING.value,
        TaskStatus.MERGING.value,
        TaskStatus.RUNNING.value,
    }
)
PENDING_TASK_STATUSES = frozenset({TaskStatus.PENDING.value, TaskStatus.QUEUED.value, ""})
SUCCESS_TASK_STATUSES = frozenset({TaskStatus.SUCCESS.value, TaskStatus.COMPLETED.value, "finished"})
ERROR_TASK_STATUSES = frozenset({TaskStatus.ERROR.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value})
TERMINAL_TASK_STATUSES = frozenset({*SUCCESS_TASK_STATUSES, *ERROR_TASK_STATUSES, TaskStatus.DELETED.value})


def _normalize_enum_value(value: Any, valid_values: set[str] | frozenset[str], default_value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in valid_values else default_value


def normalize_task_status(value: Any, default: TaskStatus | str = TaskStatus.PENDING) -> str:
    default_value = default.value if isinstance(default, TaskStatus) else str(default or TaskStatus.PENDING.value).strip().lower()
    default_value = _normalize_enum_value(default_value, _TASK_STATUS_VALUES, TaskStatus.PENDING.value)
    return _normalize_enum_value(value, _TASK_STATUS_VALUES, default_value)


def normalize_post_action(
    value: Any,
    default: PostActionType | str = PostActionType.NONE,
    *,
    allowed_values: set[str] | frozenset[str] | None = None,
    allow_unknown: bool = False,
) -> str:
    default_value = default.value if isinstance(default, PostActionType) else str(default or PostActionType.NONE.value).strip().lower()
    default_value = _normalize_enum_value(default_value, _POST_ACTION_VALUES, PostActionType.NONE.value)
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default_value
    if allow_unknown and allowed_values is None:
        return normalized
    valid_values = allowed_values if allowed_values is not None else _POST_ACTION_VALUES
    return normalized if normalized in valid_values else default_value


@dataclass
class MergeOptions(_DataclassDictModel):
    enabled: bool = False
    video_codec: str = "copy"
    video_crf: int = 23
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"


@dataclass
class ResumeSnapshot(_DataclassDictModel):
    partials_count: int = 0
    partials_total_bytes: int = 0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    status: str = ""
    eta: str = ""
    speed: str = ""


@dataclass
class DownloadHistoryEntry(_DataclassDictModel):
    timestamp: str = ""
    title: str = ""
    url: str = ""
    mode: str = "video"
    format: str = ""
    quality: str = ""
    size: str = "--"
    size_text: str = "--"
    size_bytes: int = 0
    thumbnail: str = ""
    file_path: str = ""
    status: str = ""
    message: str = ""
    attempts: int = 0
    error: str = ""


@dataclass
class StatsState(_DataclassDictModel):
    total_videos: int = 0
    total_audios: int = 0
    download_history: list[DownloadHistoryEntry] = field(default_factory=list)


@dataclass
class DownloadTask(_DataclassDictModel):
    task_uuid: str = field(default_factory=lambda: str(uuid4()))
    url: str = ""
    out_dir: str = ""
    mode: str = "video"
    quality: str = "1080p"
    format: str = "MP4"
    subtitle: str = "None"
    start_time: str = ""
    end_time: str = ""
    retries: int = 3
    auto_retry_delay_seconds: int = 4
    queue_retry_limit: int = 2
    retry_count: int = 0
    next_retry_at: float = 0.0
    scheduled_at: float = 0.0
    bandwidth_limit_kbps: int = 0
    use_aria2: bool = True
    title: str = ""
    thumbnail: str = ""
    cookies_from_browser: str = "none"
    duration_seconds: int = 0
    is_live: bool = False
    was_live: bool = False
    live_status: str = ""
    status: str = TaskStatus.PENDING.value
    category: str = ""
    schedule_repeat: str = "none"
    channel: str = ""
    file_path: str = ""
    last_output_path: str = ""
    error_msg: str = ""
    resume_json: str = ""
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    embed_subs: bool = True
    split_chapters: bool = False
    whisper_fallback: bool = False
    verify_checksum: bool = False
    virus_scan_after_download: bool = False
    sponsorblock_enabled: bool = False
    normalize_audio_postprocess: bool = False
    use_ytdlp_api: bool = False
    clean_metadata: bool = False
    rename_template: str = "Default"
    use_native_engine: bool = False
    merge_opts: MergeOptions = field(default_factory=MergeOptions)
    frozen_title: str = ""
    playlist_url: str = ""
    playlist_index: int = 0
    playlist_title: str = ""
    entry_id: str = ""
    source: str = ""
    trims: list[dict[str, Any]] = field(default_factory=list)
    resume: ResumeSnapshot | dict[str, Any] = field(default_factory=ResumeSnapshot)
    queue_index: int = -1
    size: str = "--"
    size_bytes: int = 0
    estimated_size_bytes: int = 0
    size_is_estimate: bool = True
    video_id: str = ""
    file_hash: str = ""
    post_action: str = "none"
    post_download_script: str = ""
    post_process_pipeline: list[dict[str, Any]] = field(default_factory=list)
