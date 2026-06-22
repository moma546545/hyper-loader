from types import SimpleNamespace

from core.window_controllers.download_controller import DownloadController


def test_normalize_task_applies_common_overrides():
    controller = DownloadController(SimpleNamespace())
    task = {"format": "MP4", "quality": "720p", "subtitle": "None", "out_dir": "D:/base"}

    normalized = controller.normalize_task(
        task,
        subtitle="ar",
        duration_seconds=125,
        retries=3,
        out_dir="D:/downloads",
        channel="My Channel",
        mode="video",
        is_live=True,
        was_live=False,
        live_status="is_live",
    )

    assert normalized is task
    assert normalized["subtitle"] == "ar"
    assert normalized["duration_seconds"] == 125
    assert normalized["retries"] == 3
    assert normalized["out_dir"] == "D:/downloads"
    assert normalized["channel"] == "My Channel"
    assert normalized["mode"] == "video"
    assert normalized["is_live"] is True
    assert normalized["was_live"] is False
    assert normalized["live_status"] == "is_live"
