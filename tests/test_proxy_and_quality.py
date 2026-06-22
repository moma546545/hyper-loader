from core.config import (
    estimate_file_size_bytes,
    normalize_video_quality_label,
    video_quality_to_height,
)
from core.proxy_manager import ProxyManager, proxy_manager


def test_quality_helpers_normalize_labels_and_heights():
    assert normalize_video_quality_label("2160p") == "4K"
    assert normalize_video_quality_label("4320") == "8K"
    assert normalize_video_quality_label("720") == "720p"
    assert video_quality_to_height("4K") == "2160"
    assert video_quality_to_height("1080p") == "1080"


def test_estimate_file_size_bytes_distinguishes_audio_and_video():
    audio_size = estimate_file_size_bytes(180, "audio", "320kbps")
    video_size = estimate_file_size_bytes(180, "video", "1080p")

    assert audio_size > 0
    assert video_size > audio_size


def test_estimate_file_size_bytes_returns_zero_for_invalid_duration():
    assert estimate_file_size_bytes("bad-input", "video", "1080p") == 0
    assert estimate_file_size_bytes(0, "video", "1080p") == 0


def test_proxy_manager_add_proxy_avoids_duplicates():
    manager = ProxyManager(config_path=":memory:", protector=None)

    manager.add_proxy("http://127.0.0.1:8080")
    manager.add_proxy("http://127.0.0.1:8080")

    assert manager.config["proxies"] == ["http://127.0.0.1:8080"]


def test_proxy_manager_returns_ytdlp_flag_for_current_proxy():
    manager = ProxyManager(config_path=":memory:", protector=None)
    manager.config["enabled"] = True
    manager.config["proxies"] = ["socks5://127.0.0.1:1080"]

    assert manager.get_yt_dlp_flag() == ["--proxy", "socks5://127.0.0.1:1080"]


def test_proxy_manager_rotate_advances_current_index():
    manager = ProxyManager(config_path=":memory:", protector=None)
    manager.config["enabled"] = True
    manager.config["proxies"] = [
        "http://127.0.0.1:8080",
        "http://127.0.0.2:8080",
    ]

    rotated = manager.rotate()

    assert rotated == "http://127.0.0.2:8080"
    assert manager.config["current_index"] == 1


def test_proxy_manager_randomized_rotate_prefers_healthier_proxy(monkeypatch):
    manager = ProxyManager(config_path=":memory:", protector=None)
    manager.config["enabled"] = True
    manager.config["proxies"] = [
        "http://127.0.0.1:8080",
        "http://127.0.0.2:8080",
        "http://127.0.0.3:8080",
    ]
    manager.config["current_index"] = 0
    manager._fail_counts = {
        "http://127.0.0.2:8080": 3,
        "http://127.0.0.3:8080": 0,
    }
    monkeypatch.setattr("core.proxy_manager.random.choice", lambda values: values[0])

    rotated = manager.rotate(randomize=True)

    assert rotated == "http://127.0.0.3:8080"
    assert manager.config["current_index"] == 2


def test_proxy_manager_failure_can_engage_kill_switch():
    manager = ProxyManager(config_path=":memory:", protector=None)
    manager.config["enabled"] = True
    manager.config["proxies"] = ["http://127.0.0.1:8080"]
    manager.config["failure_threshold"] = 1
    manager.config["kill_switch_enabled"] = True

    manager.on_failure("http://127.0.0.1:8080")

    assert manager.config["enabled"] is False


def test_proxy_manager_can_rotate_requires_ready_alternative():
    manager = ProxyManager(config_path=":memory:", protector=None)
    manager.config["enabled"] = True
    manager.config["proxies"] = [
        "http://127.0.0.1:8080",
        "http://127.0.0.2:8080",
    ]
    manager.config["current_index"] = 0

    assert manager.can_rotate() is True

    manager._disabled_until = {
        "http://127.0.0.2:8080": 9999999999.0,
    }
    assert manager.can_rotate() is False


def test_proxy_manager_get_current_proxy_applies_rotation_interval(tmp_path, monkeypatch):
    manager = ProxyManager(config_path=str(tmp_path / "proxy_config.json"), protector=None)
    manager.config["enabled"] = True
    manager.config["proxies"] = [
        "http://127.0.0.1:8080",
        "http://127.0.0.2:8080",
    ]
    manager.config["current_index"] = 0
    manager.config["rotate_interval_minutes"] = 1
    manager._last_rotate_time = 0.0
    monkeypatch.setattr("core.proxy_manager.time.time", lambda: 120.0)
    monkeypatch.setattr("core.proxy_manager.random.choice", lambda values: values[0])

    selected = manager.get_current_proxy()

    assert selected == "http://127.0.0.2:8080"
    assert manager.config["current_index"] == 1


def test_proxy_manager_default_constructor_reuses_shared_singleton():
    first = ProxyManager()
    second = ProxyManager()

    assert first is proxy_manager
    assert second is proxy_manager
