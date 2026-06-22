from core.media_size import apply_estimated_size, estimate_media_size_bytes, format_size_label


def test_estimate_media_size_prefers_matching_video_and_audio_formats():
    payload = {
        "duration_seconds": 180,
        "formats": [
            {"format_id": "18", "height": 360, "vcodec": "avc1", "acodec": "mp4a", "filesize": 20_000_000},
            {"format_id": "137", "height": 1080, "vcodec": "avc1", "acodec": "none", "filesize": 95_000_000},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 5_000_000},
        ],
    }

    size, exact = estimate_media_size_bytes(
        payload,
        duration_seconds=180,
        mode="video",
        quality="1080p",
        fmt="MP4",
    )

    assert size == 100_000_000
    assert exact is True


def test_apply_estimated_size_marks_duration_fallback_as_estimate():
    task = {"duration_seconds": 120, "mode": "audio", "quality": "320kbps", "format": "MP3"}

    apply_estimated_size(task)

    assert task["estimated_size_bytes"] > 0
    assert task["size_bytes"] == task["estimated_size_bytes"]
    assert task["size_is_estimate"] is True
    assert task["size"].startswith("~")
    assert format_size_label(task["estimated_size_bytes"]) == task["size"]
