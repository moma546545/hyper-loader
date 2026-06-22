from dataclasses import dataclass
from typing import Optional


@dataclass
class MediaProfile:
    """
    Represents the requested or actual profile of a media stream.
    Used by FormatDecisionEngine to deterministically select the best streams.
    """

    resolution: str = "1080p"
    fps: int = 60
    dynamic_range: str = "SDR"
    preferred_video_codec: str = "avc1"
    preferred_audio_codec: str = "mp4a"
    target_container: str = "mp4"
    mode: str = "video"

    # Advanced / Constraints
    max_bitrate_kbps: Optional[int] = None
    force_hardware_decode_safe: bool = False
    allow_recode: bool = True

    @classmethod
    def from_task(cls, task_dict: dict) -> "MediaProfile":
        quality = str(task_dict.get("quality", "1080p")).lower()
        mode = str(task_dict.get("mode", "video")).lower()
        fmt = str(task_dict.get("format", "mp4")).lower()

        profile = cls(
            resolution=quality,
            mode=mode,
            target_container=fmt,
        )

        if quality in {"2160p", "4k", "8k", "4320p"}:
            profile.preferred_video_codec = "vp9"
            profile.fps = 60
        elif quality in {"1080p", "720p"}:
            profile.preferred_video_codec = "avc1"
            profile.fps = 60

        if fmt == "webm":
            profile.preferred_video_codec = "vp9"
            profile.preferred_audio_codec = "opus"
        elif fmt == "mkv":
            profile.preferred_video_codec = "av01"
            profile.preferred_audio_codec = "opus"

        if mode == "audio":
            if fmt == "mp3":
                profile.preferred_audio_codec = "mp3"
            elif fmt in {"m4a", "mp4"}:
                profile.preferred_audio_codec = "mp4a"
            elif fmt in {"ogg", "flac", "wav"}:
                profile.preferred_audio_codec = fmt
            else:
                profile.preferred_audio_codec = "opus"

        return profile
