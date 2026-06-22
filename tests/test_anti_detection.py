from core.anti_detection import AntiDetectionEngine
from core.downloader import DownloadWorker


def test_anti_detection_generates_expected_safe_args():
    engine = AntiDetectionEngine()
    engine._allow_impersonation = True
    engine._impersonation_supported = lambda _target: True

    opts = engine.get_yt_dlp_options()

    assert "--impersonate" in opts
    assert "--sleep-interval" in opts
    assert "--max-sleep-interval" in opts
    assert "--sleep-requests" in opts
    assert "--extractor-args" not in opts
    assert "--add-header" in opts
    assert any("Accept-Language:" in value for value in opts if isinstance(value, str))


def test_anti_detection_switches_to_cautious_on_rate_limit():
    engine = AntiDetectionEngine()
    engine.strategy = "normal"

    should_retry = engine.on_error("HTTP Error 429: Too Many Requests")

    assert should_retry is True
    assert engine.strategy == "cautious"


def test_downloader_allows_safe_headers():
    worker = DownloadWorker(
        target_url="https://example.com/watch",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    worker.extra_args = [
        "--add-header", "Accept-Language:en-US,en;q=0.9",
    ]

    safe_args, options = worker._parse_safe_extra_args()

    assert safe_args == ["--add-header", "Accept-Language:en-US,en;q=0.9"]
    assert options["http_headers"] == {"Accept-Language": "en-US,en;q=0.9"}


def test_anti_detection_uses_impersonate_in_cautious_mode():
    engine = AntiDetectionEngine()
    engine.strategy = "cautious"
    engine._allow_impersonation = True
    engine._impersonation_supported = lambda _target: True

    opts = engine.get_yt_dlp_options()

    assert "--impersonate" in opts
    idx = opts.index("--impersonate")
    assert idx + 1 < len(opts)
    assert opts[idx + 1] in {"chrome", "edge", "safari"}
    assert "--sleep-requests" in opts
    assert "--add-header" in opts


def test_anti_detection_analysis_options_include_headers():
    engine = AntiDetectionEngine()

    opts = engine.get_yt_dlp_analysis_options()

    assert "--add-header" in opts
    assert any("Accept-Language:" in value for value in opts if isinstance(value, str))


def test_anti_detection_escalates_sleep_profile_with_error_streak():
    engine = AntiDetectionEngine()
    engine.strategy = "normal"
    engine._allow_impersonation = True
    engine._impersonation_supported = lambda _target: True

    assert engine.on_error("HTTP Error 429: Too Many Requests") is True
    assert engine.on_error("HTTP Error 429: Too Many Requests") is True
    assert engine.on_error("HTTP Error 403: forbidden") is True
    opts = engine.get_yt_dlp_options()

    assert "--impersonate" in opts
    assert "--user-agent" not in opts
    assert "--add-header" in opts
    sleep_idx = opts.index("--sleep-interval") + 1
    max_sleep_idx = opts.index("--max-sleep-interval") + 1
    request_sleep_idx = opts.index("--sleep-requests") + 1
    assert float(opts[sleep_idx]) >= 2.2
    assert float(opts[max_sleep_idx]) >= 5.0
    assert float(opts[request_sleep_idx]) >= 1.4


def test_downloader_refreshes_anti_detection_profile_after_403(monkeypatch):
    worker = DownloadWorker(
        target_url="https://example.com/watch",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    logs = []
    worker.log.connect(logs.append)
    monkeypatch.setattr("core.downloader.anti_detection_engine.on_error", lambda text: "403" in str(text))
    monkeypatch.setattr(
        "core.downloader.anti_detection_engine.get_yt_dlp_options",
        lambda: ["--user-agent", "EscalatedUA/1.0", "--add-header", "Accept-Language:en-US"],
    )
    monkeypatch.setattr(
        "core.downloader.anti_detection_engine.get_transport_fingerprint",
        lambda: {"transport_impersonate": "chrome124"},
    )

    changed = worker._refresh_anti_detection_after_error("HTTP Error 403: Forbidden")

    assert changed is True
    assert worker.extra_args == ["--user-agent", "EscalatedUA/1.0", "--add-header", "Accept-Language:en-US"]
    assert worker.tls_transport_profile == "chrome124"
    assert any("تم تحديث بصمة الطلبات" in line for line in logs)


def test_anti_detection_falls_back_to_user_agent_when_impersonation_unavailable():
    engine = AntiDetectionEngine()
    engine._impersonation_supported = lambda _target: False

    opts = engine.get_yt_dlp_options()

    assert "--impersonate" not in opts
    assert "--user-agent" in opts


def test_anti_detection_cools_down_to_normal_after_non_blocking_errors():
    engine = AntiDetectionEngine()
    engine.on_error("HTTP Error 429: Too Many Requests")
    assert engine.strategy == "cautious"

    assert engine.on_error("temporary DNS issue") is False
    assert engine.strategy == "cautious"
    assert engine.on_error("connection reset by peer") is False
    assert engine.strategy == "normal"


def test_downloader_allows_safe_impersonate_and_request_sleep_options():
    worker = DownloadWorker(
        target_url="https://example.com/watch",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    worker.extra_args = [
        "--impersonate", "chrome",
        "--sleep-requests", "0.75",
        "--sleep-interval", "1.25",
    ]

    safe_args, options = worker._parse_safe_extra_args()

    assert "--impersonate" in safe_args
    assert "chrome" in safe_args
    assert options["impersonate"] == "chrome"
    assert options["sleep_interval_requests"] == 0.75
    assert options["sleep_interval"] == 1.25


def test_downloader_allows_safe_youtube_extractor_args():
    worker = DownloadWorker(
        target_url="https://example.com/watch",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    worker.extra_args = ["--extractor-args", "youtube:player_client=web,android,web"]

    safe_args, options = worker._parse_safe_extra_args()

    assert safe_args == ["--extractor-args", "youtube:player_client=web,android"]
    assert options["extractor_args"] == {"youtube": {"player_client": ["web", "android"]}}


def test_downloader_rejects_unsafe_youtube_extractor_args():
    worker = DownloadWorker(
        target_url="https://example.com/watch",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    worker.extra_args = ["--extractor-args", "youtube:player_client=web,badclient"]

    safe_args, options = worker._parse_safe_extra_args()

    assert safe_args == []
    assert "extractor_args" not in options


def test_downloader_retry_cooldown_uses_sleep_requests(monkeypatch):
    worker = DownloadWorker(
        target_url="https://example.com/watch",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    worker.extra_args = ["--sleep-requests", "1.00"]
    monkeypatch.setattr("core.downloader.random.uniform", lambda _a, _b: 1.0)

    first_attempt = worker._get_anti_detection_retry_cooldown_seconds(1)
    second_attempt = worker._get_anti_detection_retry_cooldown_seconds(2)

    assert first_attempt == 0.0
    assert second_attempt == 1.0


def test_downloader_retry_cooldown_waits_via_cancel_event(monkeypatch):
    worker = DownloadWorker(
        target_url="https://example.com/watch",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    worker.extra_args = ["--sleep-requests", "0.80"]
    monkeypatch.setattr("core.downloader.random.uniform", lambda _a, _b: 1.0)
    waits = []

    class _Event:
        def wait(self, timeout=0):
            waits.append(timeout)
            return False

        def is_set(self):
            return False

        def set(self):
            return None

    worker._cancel_event = _Event()
    worker._maybe_apply_retry_cooldown(2)

    assert waits == [0.8]


def test_downloader_rejects_newline_in_extra_arg_value():
    worker = DownloadWorker(
        target_url="https://example.com/watch",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    worker.extra_args = ["--referer", "https://example.com/\nInjected"]

    safe_args, options = worker._parse_safe_extra_args()

    assert safe_args == []
    assert "referer" not in options
