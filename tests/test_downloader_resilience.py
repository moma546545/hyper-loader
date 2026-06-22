import subprocess
from io import StringIO

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtWidgets import QApplication

from core.downloader import DownloadWorker


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _StubProcess:
    def __init__(self, returncode=1):
        self.stdout = StringIO("")
        self._returncode = returncode

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        return self._returncode

    def terminate(self):
        self._returncode = -15

    def kill(self):
        self._returncode = -9


def _make_worker():
    _ensure_qt_app()
    return DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )


def test_downloader_retry_policy_is_limited_to_retryable_errors():
    worker = _make_worker()

    assert worker._should_retry_download("HTTP Error 429: Too Many Requests", attempt=1) is True
    assert worker._should_retry_download("download timed out", attempt=1) is True
    assert worker._should_retry_download("Unsupported URL", attempt=1) is False
    assert worker._should_retry_download("HTTP Error 429: Too Many Requests", attempt=worker.retries) is False


def test_downloader_normalizes_sign_in_error_message():
    worker = _make_worker()

    message = worker._normalize_download_error("Sign in to confirm you're not a bot")

    assert "تسجيل الدخول" in message or "الكوكيز" in message


def test_downloader_run_subprocess_once_returns_idle_timeout_error(monkeypatch):
    worker = _make_worker()
    process = _StubProcess(returncode=1)
    monkeypatch.setattr("core.downloader.subprocess.Popen", lambda *args, **kwargs: process)

    def _fake_read_lines(stdout, process=None, idle_timeout=300.0):
        worker._set_last_runtime_error("انتهت مهلة القراءة من عملية التحميل")
        if False:
            yield ""

    monkeypatch.setattr(worker, "_read_lines_safely", _fake_read_lines)

    ok, cancelled, err = worker._run_subprocess_once(["yt-dlp"], {})

    assert ok is False
    assert cancelled is False
    assert err == "انتهت مهلة القراءة من عملية التحميل"


def test_downloader_gif_timeout_is_reported(monkeypatch, tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="gif",
    )
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")
    worker.downloaded_file_path = str(source)
    messages = []
    worker.log.connect(messages.append)
    monkeypatch.setattr("core.downloader.shutil.which", lambda name: "ffmpeg")
    monkeypatch.setattr(
        "core.downloader.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=300)),
    )

    worker._maybe_convert_to_gif()

    assert any("تحويل GIF timed out" in message for message in messages)


def test_downloader_audio_normalization_runs_when_postprocess_enabled(monkeypatch, tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir=str(tmp_path),
        mode="audio",
        quality="320kbps",
        fmt="mp3",
        normalize_audio_postprocess=True,
    )
    source = tmp_path / "sample.mp3"
    source.write_bytes(b"audio")
    worker.downloaded_file_path = str(source)
    messages = []
    worker.log.connect(messages.append)
    captured = {}

    def _fake_normalize_file(path, **kwargs):
        captured["path"] = path
        captured["target_lufs"] = kwargs.get("target_lufs")
        captured["in_place"] = kwargs.get("in_place")
        callback = kwargs.get("progress_callback")
        if callable(callback):
            callback("Normalizing sample.mp3")
        return True, "✅ Normalized"

    monkeypatch.setattr("core.audio_normalizer.normalize_file", _fake_normalize_file)

    worker._maybe_normalize_audio_output()

    assert captured["path"] == str(source)
    assert captured["in_place"] is True
    assert captured["target_lufs"] == -14.0
    assert any("Normalizing sample.mp3" in message for message in messages)
    assert any("✅ Normalized" in message for message in messages)


def test_downloader_retry_wait_seconds_uses_base_backoff_without_anti_detection(monkeypatch):
    worker = _make_worker()
    monkeypatch.setattr("core.downloader.random.uniform", lambda _a, _b: 1.0)
    worker.extra_args = []

    wait_attempt_1 = worker._compute_retry_wait_seconds(1)
    wait_attempt_3 = worker._compute_retry_wait_seconds(3)

    assert wait_attempt_1 == 2.0
    assert wait_attempt_3 == 8.0


def test_downloader_retry_wait_seconds_increases_with_cautious_profile(monkeypatch):
    worker = _make_worker()
    monkeypatch.setattr("core.downloader.random.uniform", lambda _a, _b: 1.0)
    worker.extra_args = [
        "--impersonate", "chrome",
        "--sleep-requests", "1.00",
        "--max-sleep-interval", "5.00",
    ]

    base = worker.retry_delay_seconds * (2 ** (2 - 1))
    wait_seconds = worker._compute_retry_wait_seconds(2)

    assert wait_seconds > float(base)


def test_downloader_cancel_requested_property_tracks_cancel_event():
    worker = _make_worker()

    assert worker.cancel_requested is False
    assert worker._cancel_event.is_set() is False

    worker.cancel_requested = True
    assert worker.cancel_requested is True
    assert worker._cancel_event.is_set() is True

    worker.cancel_requested = False
    assert worker.cancel_requested is False
    assert worker._cancel_event.is_set() is False


def test_downloader_ytdlp_api_cancelled_from_progress_hook(monkeypatch):
    worker = _make_worker()
    worker.use_ytdlp_api = True
    worker.cancel_requested = True

    class _FakeYoutubeDL:
        def __init__(self, opts):
            self._opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, _urls):
            hook = self._opts["progress_hooks"][0]
            hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 10})
            return 0

    monkeypatch.setattr("core.downloader.YoutubeDL", _FakeYoutubeDL)

    ok, cancelled, err = worker._run_ytdlp_once()

    assert ok is False
    assert cancelled is True
    assert err == "تم إلغاء التحميل"


def test_downloader_ytdlp_api_tracks_tmpfilename_during_download(monkeypatch, tmp_path):
    worker = _make_worker()
    worker.use_ytdlp_api = True
    tmp_file = tmp_path / "video.mp4.part"
    final_file = tmp_path / "video.mp4"

    class _FakeYoutubeDL:
        def __init__(self, opts):
            self._opts = opts
            self._download_retcode = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, _urls):
            hook = self._opts["progress_hooks"][0]
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 5,
                    "total_bytes": 10,
                    "tmpfilename": str(tmp_file),
                }
            )
            hook({"status": "finished", "filename": str(final_file)})
            return 0

    monkeypatch.setattr("core.downloader.YoutubeDL", _FakeYoutubeDL)

    ok, cancelled, err = worker._run_ytdlp_once()

    assert ok is True
    assert cancelled is False
    assert err == ""
    assert worker.downloaded_file_path == str(final_file)


def test_downloader_ytdlp_api_relaxes_format_after_unavailable_error(monkeypatch):
    worker = _make_worker()
    worker.use_ytdlp_api = True
    formats_seen = []
    messages = []
    worker.log.connect(messages.append)

    class _FakeYoutubeDL:
        def __init__(self, opts):
            self._opts = opts
            self._download_retcode = 0
            formats_seen.append(opts.get("format"))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, _urls):
            if len(formats_seen) == 1:
                raise RuntimeError("ERROR: [youtube] abc123: Requested format is not available")
            return 0

    monkeypatch.setattr("core.downloader.DownloadError", RuntimeError)
    monkeypatch.setattr("core.downloader.YoutubeDL", _FakeYoutubeDL)

    ok, cancelled, err = worker._run_ytdlp_once()

    assert ok is True
    assert cancelled is False
    assert err == ""
    assert len(formats_seen) == 2
    assert formats_seen[1] == "bv*+ba/b"
    assert any("تخفيف القيود" in message for message in messages)


def test_downloader_subprocess_relaxes_format_even_when_retries_is_one(monkeypatch):
    worker = _make_worker()
    worker.use_ytdlp_api = False
    worker.use_aria2 = False
    worker.retries = 1
    commands_seen = []
    messages = []
    published = []
    worker.log.connect(messages.append)

    def _fake_run_download_attempt(cmd, _env):
        commands_seen.append(list(cmd))
        if len(commands_seen) == 1:
            return False, False, "ERROR: [youtube] abc123: Requested format is not available"
        return True, False, ""

    monkeypatch.setattr(worker, "_run_download_attempt", _fake_run_download_attempt)
    monkeypatch.setattr(worker, "_find_aria2", lambda: None)
    monkeypatch.setattr(worker, "_maybe_emit_resume_snapshot", lambda force=False: None)
    monkeypatch.setattr(worker, "_maybe_write_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_try_rename_output", lambda: None)
    monkeypatch.setattr(worker, "_maybe_run_whisper_fallback", lambda: None)
    monkeypatch.setattr(worker, "_maybe_hard_burn_subtitles", lambda: None)
    monkeypatch.setattr(worker, "_maybe_convert_to_gif", lambda: None)
    monkeypatch.setattr(worker, "_maybe_normalize_audio_output", lambda: None)
    monkeypatch.setattr(worker, "_maybe_scan_download_for_threats", lambda: {})
    monkeypatch.setattr(worker, "_compute_checksum", lambda: "")
    monkeypatch.setattr(worker, "_cleanup_checkpoint_file", lambda: None)
    monkeypatch.setattr(worker, "_cleanup", lambda: None)
    monkeypatch.setattr("core.downloader.event_bus.publish", published.append)

    worker.run()

    assert len(commands_seen) == 2
    assert commands_seen[0][commands_seen[0].index("-f") + 1] != "bv*+ba/b"
    assert commands_seen[1][commands_seen[1].index("-f") + 1] == "bv*+ba/b"
    assert worker._format_fallback_level == 1
    assert any("تخفيف القيود" in message for message in messages)
    assert published[-1].success is True


def test_downloader_aria2_default_args_use_conservative_profile_for_youtube():
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://www.youtube.com/watch?v=abc123",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )

    args = worker._aria2_default_args()

    assert ["-x", "4", "-s", "4", "-j", "2"] == args[:6]


def test_downloader_aria2_default_args_use_default_profile_for_other_domains():
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/video.mp4",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )

    args = worker._aria2_default_args()

    assert ["-x", "8", "-s", "8", "-j", "4"] == args[:6]


def test_downloader_find_aria2_caches_scan_result(monkeypatch):
    worker = _make_worker()
    calls = {"which": 0, "glob": 0}
    discovered = "D:/tools/aria2-1.37.0/aria2c.exe"

    def _fake_which(_name):
        calls["which"] += 1
        return None

    def _fake_glob(_pattern):
        calls["glob"] += 1
        return [discovered]

    monkeypatch.setattr("core.downloader.shutil.which", _fake_which)
    monkeypatch.setattr("core.downloader.glob.glob", _fake_glob)
    monkeypatch.setattr("core.downloader.os.path.isfile", lambda p: str(p).replace("\\", "/") == discovered)

    first = worker._find_aria2()
    second = worker._find_aria2()

    assert first == discovered
    assert second == discovered
    assert calls["which"] == 1
    assert calls["glob"] == 2


def test_downloader_manifest_builders_enable_resilient_fragment_options():
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/live/playlist.m3u8",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )

    cmd = worker._build_command()
    opts = worker._build_ytdlp_options()

    assert "--fragment-retries" in cmd
    assert "--skip-unavailable-fragments" in cmd
    assert "--hls-use-mpegts" in cmd
    assert "--concurrent-fragments" in cmd
    assert cmd[cmd.index("--concurrent-fragments") + 1] == "5"
    assert opts["retries"] == 10
    assert opts["fragment_retries"] == 20
    assert opts["extractor_retries"] == 5
    assert opts["file_access_retries"] == 3
    assert opts["skip_unavailable_fragments"] is True
    assert opts["concurrent_fragment_downloads"] == 5
    assert opts["hls_use_mpegts"] is True


def test_downloader_live_metadata_hints_enable_live_mode_without_manifest_url():
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=live123",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
        is_live_hint=True,
        live_status_hint="is_live",
    )

    cmd = worker._build_command()
    opts = worker._build_ytdlp_options()

    assert "--live-from-start" in cmd
    assert "--wait-for-video" in cmd
    assert "--hls-use-mpegts" in cmd
    assert opts["live_from_start"] is True
    assert opts["hls_use_mpegts"] is True


def test_downloader_browser_cookies_are_forwarded_to_cli_and_api_builders():
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
        cookies_from_browser="firefox",
    )

    cmd = worker._build_command()
    opts = worker._build_ytdlp_options()

    assert "--cookies-from-browser" in cmd
    assert cmd[cmd.index("--cookies-from-browser") + 1] == "firefox"
    assert opts["cookiesfrombrowser"] == ("firefox",)


def test_downloader_all_subtitles_are_forwarded_to_cli_and_api_builders():
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
        subtitle_lang="All",
    )

    cmd = worker._build_command()
    opts = worker._build_ytdlp_options()

    assert "--sub-langs" in cmd
    assert cmd[cmd.index("--sub-langs") + 1] == "all"
    assert opts["subtitleslangs"] == ["all"]
    assert opts["embedsubtitles"] is True
    assert "all" in list(opts.get("compat_opts", []))


def test_downloader_multiple_subtitles_are_forwarded_to_cli_and_api_builders():
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
        subtitle_lang="English,ar",
    )

    cmd = worker._build_command()
    opts = worker._build_ytdlp_options()

    assert "--sub-langs" in cmd
    assert cmd[cmd.index("--sub-langs") + 1] == "en.*,ar.*"
    assert opts["subtitleslangs"] == ["en.*", "ar.*"]
    assert opts["embedsubtitles"] is True
    assert "all" in list(opts.get("compat_opts", []))


def test_downloader_audio_mode_uses_bestaudio_with_best_fallback():
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        mode="audio",
        quality="192",
        fmt="mp3",
    )

    cmd = worker._build_command()
    opts = worker._build_ytdlp_options()

    assert "-f" in cmd
    assert cmd[cmd.index("-f") + 1] == "bestaudio/best"
    assert opts["format"] == "bestaudio/best"


def test_downloader_audio_format_fallbacks_for_unsupported_targets():
    _ensure_qt_app()

    worker_aiff = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        mode="audio",
        quality="192",
        fmt="AIFF",
    )
    cmd_aiff = worker_aiff._build_command()
    assert "--audio-format" in cmd_aiff
    assert cmd_aiff[cmd_aiff.index("--audio-format") + 1] == "wav"

    worker_wma = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        mode="audio",
        quality="192",
        fmt="WMA",
    )
    cmd_wma = worker_wma._build_command()
    assert "--audio-format" in cmd_wma
    assert cmd_wma[cmd_wma.index("--audio-format") + 1] == "mp3"


def test_downloader_hard_burn_subtitles_disables_api_embed_subtitles(monkeypatch):
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
        subtitle_lang="English",
        hard_burn_subs=True,
        embed_subs=True,
    )

    opts = worker._build_ytdlp_options()

    assert opts["subtitleslangs"] == ["en.*"]
    assert "embedsubtitles" not in opts
    assert "compat_opts" not in opts or "all" not in list(opts.get("compat_opts", []))


def test_downloader_selector_honors_mp4_h264_compatibility_first():
    worker = _make_worker()

    selector = worker._video_format_selector()
    cmd = worker._build_command()
    opts = worker._build_ytdlp_options()

    assert "[fps>=50][vcodec^=av01]" in selector
    assert "[fps>=50][vcodec^=hev1]" in selector
    assert selector.index("[fps>=50][vcodec^=avc1]") < selector.index("[fps>=50][vcodec^=h264]")
    assert selector.index("[fps>=50][vcodec^=h264]") < selector.index("[fps>=50][vcodec^=vp09]")
    assert "-S" in cmd
    assert cmd[cmd.index("-S") + 1] == "-hdr,-res,-fps,-size,-br"
    assert opts["format_sort"] == ["-hdr", "-res", "-fps", "-size", "-br"]


def test_downloader_selector_prefers_webm_native_codecs_before_hevc():
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="webm",
    )

    selector = worker._video_format_selector()

    assert selector.index("[fps>=50][vcodec^=vp09]") < selector.index("[fps>=50][vcodec^=av01]")
    assert selector.index("[fps>=50][vcodec^=av01]") < selector.index("[fps>=50][vcodec^=hev1]")


def test_downloader_uses_injected_format_decision_engine():
    _ensure_qt_app()

    class _FakeEngine:
        def build_format_selector(self, _profile):
            return "bestvideo[height<=720]+bestaudio/best"

        def build_format_sort_spec(self, _profile):
            return "res,fps"

    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
        format_decision_engine=_FakeEngine(),
    )

    assert worker._video_format_selector() == "bestvideo[height<=720]+bestaudio/best"
    assert worker._video_format_sort_spec() == "res,fps"
    cmd = worker._build_command()
    assert cmd[cmd.index("-S") + 1] == "res,fps"


def test_merge_codec_plan_force_reencode_converts_copy_settings(monkeypatch):
    worker = _make_worker()
    worker.merge_opts = {
        "video_codec": "copy",
        "audio_codec": "copy",
        "force_reencode": True,
    }
    monkeypatch.setattr(worker, "_probe_media_info", lambda _path: {"streams": [{"codec_type": "video", "codec_name": "vp9"}]})
    monkeypatch.setattr(worker, "_load_ffmpeg_encoders", lambda _bin: {"libx264", "aac"})

    plan = worker._build_merge_codec_plan("ffmpeg", "video.webm", "audio.webm", "mp4")

    assert plan["video_codec"] == "libx264"
    assert plan["audio_codec"] == "aac"
    assert any("إعادة الترميز الإجباري" in note for note in plan["notes"])


def test_merge_codec_plan_falls_back_when_requested_encoder_is_missing(monkeypatch):
    worker = _make_worker()
    worker.merge_opts = {
        "video_codec": "h264_nvenc",
        "audio_codec": "opus",
        "force_reencode": False,
    }
    monkeypatch.setattr(worker, "_probe_media_info", lambda _path: {"streams": [{"codec_type": "video", "codec_name": "h264"}]})
    monkeypatch.setattr(worker, "_load_ffmpeg_encoders", lambda _bin: {"libx264", "aac"})

    plan = worker._build_merge_codec_plan("ffmpeg", "video.mp4", "audio.opus", "mp4")

    assert plan["video_codec"] == "libx264"
    assert plan["audio_codec"] == "aac"
    assert any("غير متاح" in note for note in plan["notes"])


def test_custom_merge_embeds_external_subtitles_for_mp4(monkeypatch, tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
        embed_subs=True,
    )
    worker.merge_opts = {
        "enabled": True,
        "video_codec": "copy",
        "audio_codec": "copy",
        "force_reencode": False,
    }
    worker.custom_merge = True
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.m4a"
    subtitle_path = tmp_path / "video.en.srt"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")
    subtitle_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    worker.downloaded_separate_files = [str(video_path), str(audio_path), str(subtitle_path)]

    monkeypatch.setattr("core.downloader.shutil.which", lambda _name: "ffmpeg")
    monkeypatch.setattr(worker, "_find_ffmpeg", lambda: "")
    monkeypatch.setattr(worker, "_pick_merge_inputs", lambda: (str(video_path), str(audio_path)))
    monkeypatch.setattr(worker, "_get_duration_seconds", lambda _path: 0.0)
    monkeypatch.setattr(worker, "_probe_stream_types", lambda path: {"video"} if str(path) == str(video_path) else {"audio"})
    monkeypatch.setattr(
        worker,
        "_build_merge_codec_plan",
        lambda *_args, **_kwargs: {
            "video_codec": "copy",
            "audio_codec": "copy",
            "src_video_codec": "h264",
            "video_stream": {},
            "notes": [],
        },
    )

    captured = {}

    class _MergeProcess:
        def __init__(self, cmd):
            captured["cmd"] = list(cmd)
            output_path = cmd[-1]
            with open(output_path, "wb") as handle:
                handle.write(b"merged")
            self.stdout = StringIO("")
            self._returncode = 0

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            return self._returncode

    monkeypatch.setattr("core.downloader.subprocess.Popen", lambda cmd, **kwargs: _MergeProcess(cmd))
    monkeypatch.setattr(worker, "_read_lines_safely", lambda *args, **kwargs: iter(()))

    ok, err = worker._run_custom_merge()

    assert ok is True
    assert err == ""
    assert subtitle_path.as_posix().replace("/", "\\") in [str(part).replace("/", "\\") for part in captured["cmd"]]
    assert "-c:s" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-c:s") + 1] == "mov_text"
    assert "-disposition:s:0" in captured["cmd"]
    disposition_values = [
        captured["cmd"][idx + 1]
        for idx, part in enumerate(captured["cmd"][:-1])
        if part == "-disposition:s:0"
    ]
    assert disposition_values
    assert disposition_values[-1] == "default"
    assert "-metadata:s:s:0" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-metadata:s:s:0") + 1] == "language=eng"
    assert captured["cmd"][captured["cmd"].index("-metadata:s:s:0", captured["cmd"].index("-metadata:s:s:0") + 1) + 1] == "title=English"
    assert "2:0" in captured["cmd"]
    assert worker.downloaded_file_path.endswith(".mp4")


def test_custom_merge_filters_external_subtitles_by_selected_language(monkeypatch, tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
        subtitle_lang="English",
        embed_subs=True,
    )
    worker.merge_opts = {
        "enabled": True,
        "video_codec": "copy",
        "audio_codec": "copy",
        "force_reencode": False,
    }
    worker.custom_merge = True
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.m4a"
    subtitle_en = tmp_path / "video.en.srt"
    subtitle_ar = tmp_path / "video.ar.srt"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")
    subtitle_en.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    subtitle_ar.write_text("1\n00:00:00,000 --> 00:00:01,000\nمرحبا\n", encoding="utf-8")
    worker.downloaded_separate_files = [str(video_path), str(audio_path), str(subtitle_en), str(subtitle_ar)]

    monkeypatch.setattr("core.downloader.shutil.which", lambda _name: "ffmpeg")
    monkeypatch.setattr(worker, "_find_ffmpeg", lambda: "")
    monkeypatch.setattr(worker, "_pick_merge_inputs", lambda: (str(video_path), str(audio_path)))
    monkeypatch.setattr(worker, "_get_duration_seconds", lambda _path: 0.0)
    monkeypatch.setattr(worker, "_probe_stream_types", lambda path: {"video"} if str(path) == str(video_path) else {"audio"})
    monkeypatch.setattr(
        worker,
        "_build_merge_codec_plan",
        lambda *_args, **_kwargs: {
            "video_codec": "copy",
            "audio_codec": "copy",
            "src_video_codec": "h264",
            "video_stream": {},
            "notes": [],
        },
    )

    captured = {}

    class _MergeProcess:
        def __init__(self, cmd):
            captured["cmd"] = list(cmd)
            output_path = cmd[-1]
            with open(output_path, "wb") as handle:
                handle.write(b"merged")
            self.stdout = StringIO("")
            self._returncode = 0

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            return self._returncode

    monkeypatch.setattr("core.downloader.subprocess.Popen", lambda cmd, **kwargs: _MergeProcess(cmd))
    monkeypatch.setattr(worker, "_read_lines_safely", lambda *args, **kwargs: iter(()))

    ok, err = worker._run_custom_merge()

    assert ok is True
    assert err == ""
    cmd_parts = [str(part).replace("/", "\\") for part in captured["cmd"]]
    assert str(subtitle_en).replace("/", "\\") in cmd_parts
    assert str(subtitle_ar).replace("/", "\\") not in cmd_parts


def test_custom_merge_orders_multiple_subtitles_by_preferred_language(monkeypatch, tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
        subtitle_lang="Arabic,English",
        embed_subs=True,
    )
    worker.merge_opts = {
        "enabled": True,
        "video_codec": "copy",
        "audio_codec": "copy",
        "force_reencode": False,
    }
    worker.custom_merge = True
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.m4a"
    subtitle_ar = tmp_path / "video.ar.srt"
    subtitle_en = tmp_path / "video.en.srt"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")
    subtitle_ar.write_text("1\n00:00:00,000 --> 00:00:01,000\nمرحبا\n", encoding="utf-8")
    subtitle_en.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    # intentionally reversed to validate sorting by preferred language
    worker.downloaded_separate_files = [str(video_path), str(audio_path), str(subtitle_en), str(subtitle_ar)]

    monkeypatch.setattr("core.downloader.shutil.which", lambda _name: "ffmpeg")
    monkeypatch.setattr(worker, "_find_ffmpeg", lambda: "")
    monkeypatch.setattr(worker, "_pick_merge_inputs", lambda: (str(video_path), str(audio_path)))
    monkeypatch.setattr(worker, "_get_duration_seconds", lambda _path: 0.0)
    monkeypatch.setattr(worker, "_probe_stream_types", lambda path: {"video"} if str(path) == str(video_path) else {"audio"})
    monkeypatch.setattr(
        worker,
        "_build_merge_codec_plan",
        lambda *_args, **_kwargs: {
            "video_codec": "copy",
            "audio_codec": "copy",
            "src_video_codec": "h264",
            "video_stream": {},
            "notes": [],
        },
    )

    captured = {}

    class _MergeProcess:
        def __init__(self, cmd):
            captured["cmd"] = list(cmd)
            output_path = cmd[-1]
            with open(output_path, "wb") as handle:
                handle.write(b"merged")
            self.stdout = StringIO("")
            self._returncode = 0

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            return self._returncode

    monkeypatch.setattr("core.downloader.subprocess.Popen", lambda cmd, **kwargs: _MergeProcess(cmd))
    monkeypatch.setattr(worker, "_read_lines_safely", lambda *args, **kwargs: iter(()))

    ok, err = worker._run_custom_merge()

    assert ok is True
    assert err == ""
    cmd_parts = [str(part).replace("/", "\\") for part in captured["cmd"]]
    first_sub_input_idx = cmd_parts.index("-i", cmd_parts.index(str(audio_path).replace("/", "\\")) + 1) + 1
    second_sub_input_idx = cmd_parts.index("-i", first_sub_input_idx + 1) + 1
    assert cmd_parts[first_sub_input_idx] == str(subtitle_ar).replace("/", "\\")
    assert cmd_parts[second_sub_input_idx] == str(subtitle_en).replace("/", "\\")
    assert "language=ara" in cmd_parts
    assert "title=Arabic" in cmd_parts
    assert "language=eng" in cmd_parts
    assert "title=English" in cmd_parts


def test_custom_merge_prefers_external_language_as_default_when_embedded_exists(monkeypatch, tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
        subtitle_lang="English,Arabic",
        embed_subs=True,
    )
    worker.merge_opts = {
        "enabled": True,
        "video_codec": "copy",
        "audio_codec": "copy",
        "force_reencode": False,
    }
    worker.custom_merge = True
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.m4a"
    subtitle_en = tmp_path / "video.en.srt"
    subtitle_ar = tmp_path / "video.ar.srt"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")
    subtitle_en.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    subtitle_ar.write_text("1\n00:00:00,000 --> 00:00:01,000\nمرحبا\n", encoding="utf-8")
    worker.downloaded_separate_files = [str(video_path), str(audio_path), str(subtitle_en), str(subtitle_ar)]

    monkeypatch.setattr("core.downloader.shutil.which", lambda _name: "ffmpeg")
    monkeypatch.setattr(worker, "_find_ffmpeg", lambda: "")
    monkeypatch.setattr(worker, "_pick_merge_inputs", lambda: (str(video_path), str(audio_path)))
    monkeypatch.setattr(worker, "_get_duration_seconds", lambda _path: 0.0)
    monkeypatch.setattr(worker, "_probe_stream_types", lambda path: {"video"} if str(path) == str(video_path) else {"audio"})
    monkeypatch.setattr(
        worker,
        "_probe_subtitle_stream_descriptors",
        lambda _path: [{"language": "ara", "codec_name": "mov_text"}],
    )
    monkeypatch.setattr(
        worker,
        "_build_merge_codec_plan",
        lambda *_args, **_kwargs: {
            "video_codec": "copy",
            "audio_codec": "copy",
            "src_video_codec": "h264",
            "video_stream": {},
            "notes": [],
        },
    )

    captured = {}

    class _MergeProcess:
        def __init__(self, cmd):
            captured["cmd"] = list(cmd)
            output_path = cmd[-1]
            with open(output_path, "wb") as handle:
                handle.write(b"merged")
            self.stdout = StringIO("")
            self._returncode = 0

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            return self._returncode

    monkeypatch.setattr("core.downloader.subprocess.Popen", lambda cmd, **kwargs: _MergeProcess(cmd))
    monkeypatch.setattr(worker, "_read_lines_safely", lambda *args, **kwargs: iter(()))

    ok, err = worker._run_custom_merge()

    assert ok is True
    assert err == ""
    cmd_parts = [str(part).replace("/", "\\") for part in captured["cmd"]]
    assert "-metadata:s:s:0" in cmd_parts
    assert "language=ara" in cmd_parts
    assert "title=Arabic" in cmd_parts
    # one embedded subtitle already exists, so external metadata starts from index 1
    assert "-metadata:s:s:1" in cmd_parts
    assert "language=eng" in cmd_parts
    assert "title=English" in cmd_parts
    assert "-metadata:s:s:2" in cmd_parts
    assert "language=ara" in cmd_parts
    assert "title=Arabic" in cmd_parts
    # default should follow preferred language (English), which is output subtitle index 1
    assert "-disposition:s:1" in cmd_parts
    disposition_values = [
        cmd_parts[idx + 1]
        for idx, part in enumerate(cmd_parts[:-1])
        if part == "-disposition:s:1"
    ]
    assert disposition_values
    assert disposition_values[-1] == "default"


def test_custom_merge_sets_fallback_title_for_unknown_external_subtitle_language(monkeypatch, tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
        subtitle_lang="All",
        embed_subs=True,
    )
    worker.merge_opts = {
        "enabled": True,
        "video_codec": "copy",
        "audio_codec": "copy",
        "force_reencode": False,
    }
    worker.custom_merge = True
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.m4a"
    subtitle_unknown = tmp_path / "video.cc.srt"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")
    subtitle_unknown.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    worker.downloaded_separate_files = [str(video_path), str(audio_path), str(subtitle_unknown)]

    monkeypatch.setattr("core.downloader.shutil.which", lambda _name: "ffmpeg")
    monkeypatch.setattr(worker, "_find_ffmpeg", lambda: "")
    monkeypatch.setattr(worker, "_pick_merge_inputs", lambda: (str(video_path), str(audio_path)))
    monkeypatch.setattr(worker, "_get_duration_seconds", lambda _path: 0.0)
    monkeypatch.setattr(worker, "_probe_stream_types", lambda path: {"video"} if str(path) == str(video_path) else {"audio"})
    monkeypatch.setattr(
        worker,
        "_build_merge_codec_plan",
        lambda *_args, **_kwargs: {
            "video_codec": "copy",
            "audio_codec": "copy",
            "src_video_codec": "h264",
            "video_stream": {},
            "notes": [],
        },
    )

    captured = {}

    class _MergeProcess:
        def __init__(self, cmd):
            captured["cmd"] = list(cmd)
            output_path = cmd[-1]
            with open(output_path, "wb") as handle:
                handle.write(b"merged")
            self.stdout = StringIO("")
            self._returncode = 0

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            return self._returncode

    monkeypatch.setattr("core.downloader.subprocess.Popen", lambda cmd, **kwargs: _MergeProcess(cmd))
    monkeypatch.setattr(worker, "_read_lines_safely", lambda *args, **kwargs: iter(()))

    ok, err = worker._run_custom_merge()

    assert ok is True
    assert err == ""
    cmd_parts = [str(part).replace("/", "\\") for part in captured["cmd"]]
    assert "-metadata:s:s:0" in cmd_parts
    assert "title=CC" in cmd_parts
    assert "language=cc" not in cmd_parts


def test_custom_merge_sets_embedded_title_when_embedded_language_is_unknown(monkeypatch, tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
        subtitle_lang="All",
        embed_subs=True,
    )
    worker.merge_opts = {
        "enabled": True,
        "video_codec": "copy",
        "audio_codec": "copy",
        "force_reencode": False,
    }
    worker.custom_merge = True
    video_path = tmp_path / "video.mp4"
    audio_path = tmp_path / "audio.m4a"
    subtitle_en = tmp_path / "video.en.srt"
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")
    subtitle_en.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    worker.downloaded_separate_files = [str(video_path), str(audio_path), str(subtitle_en)]

    monkeypatch.setattr("core.downloader.shutil.which", lambda _name: "ffmpeg")
    monkeypatch.setattr(worker, "_find_ffmpeg", lambda: "")
    monkeypatch.setattr(worker, "_pick_merge_inputs", lambda: (str(video_path), str(audio_path)))
    monkeypatch.setattr(worker, "_get_duration_seconds", lambda _path: 0.0)
    monkeypatch.setattr(worker, "_probe_stream_types", lambda path: {"video"} if str(path) == str(video_path) else {"audio"})
    monkeypatch.setattr(
        worker,
        "_probe_subtitle_stream_descriptors",
        lambda _path: [{"language": "", "codec_name": "mov_text"}],
    )
    monkeypatch.setattr(
        worker,
        "_build_merge_codec_plan",
        lambda *_args, **_kwargs: {
            "video_codec": "copy",
            "audio_codec": "copy",
            "src_video_codec": "h264",
            "video_stream": {},
            "notes": [],
        },
    )

    captured = {}

    class _MergeProcess:
        def __init__(self, cmd):
            captured["cmd"] = list(cmd)
            output_path = cmd[-1]
            with open(output_path, "wb") as handle:
                handle.write(b"merged")
            self.stdout = StringIO("")
            self._returncode = 0

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            return self._returncode

    monkeypatch.setattr("core.downloader.subprocess.Popen", lambda cmd, **kwargs: _MergeProcess(cmd))
    monkeypatch.setattr(worker, "_read_lines_safely", lambda *args, **kwargs: iter(()))

    ok, err = worker._run_custom_merge()

    assert ok is True
    assert err == ""
    cmd_parts = [str(part).replace("/", "\\") for part in captured["cmd"]]
    assert "-metadata:s:s:0" in cmd_parts
    assert "title=Subtitle (mov_text)" in cmd_parts


def test_downloader_resume_snapshot_skips_emit_when_payload_is_unchanged(monkeypatch, tmp_path):
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/watch?v=1",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    worker.downloaded_file_path = str(tmp_path / "video.mp4")
    emitted = []
    worker.resume_snapshot.connect(lambda payload: emitted.append(payload))

    timeline = iter([10.0, 10.0, 12.0, 12.0])
    monkeypatch.setattr("core.downloader.time.time", lambda: next(timeline))
    monkeypatch.setattr("core.downloader.os.path.isfile", lambda _p: False)
    monkeypatch.setattr(worker, "_list_resume_partial_candidates", lambda _base: [])

    worker._maybe_emit_resume_snapshot(force=False)
    worker._maybe_emit_resume_snapshot(force=False)

    assert len(emitted) == 1


def test_downloader_exposes_default_provider_registry_entries():
    worker = _make_worker()

    providers = worker.list_download_providers()

    assert "yt_dlp_api" in providers
    assert "yt_dlp_subprocess" in providers


def test_downloader_custom_provider_can_override_default_execution():
    worker = _make_worker()
    worker.url = "custom://resource"
    called = []
    provider_name = "test_custom_provider"

    def _can_handle(candidate):
        return str(candidate.url).startswith("custom://")

    def _run_once(candidate, cmd, env):
        called.append((candidate.url, list(cmd), dict(env)))
        return True, False, ""

    worker.register_download_provider(
        provider_name,
        can_handle=_can_handle,
        run_once=_run_once,
        priority=1,
    )
    try:
        ok, cancelled, err = worker._run_download_attempt(["yt-dlp"], {})
        assert ok is True
        assert cancelled is False
        assert err == ""
        assert len(called) == 1
    finally:
        worker.unregister_download_provider(provider_name)


def test_downloader_prefers_tls_fingerprint_provider_for_direct_links(monkeypatch):
    worker = _make_worker()
    worker.use_native_engine = True
    worker.url = "https://example.com/file.mp4"
    monkeypatch.setenv("VIDDOWNLOADER_ENABLE_TLS_FINGERPRINT_PROVIDER", "1")
    monkeypatch.setattr(worker, "_is_tls_transport_available", lambda: True)
    called = {"tls": 0}

    def _tls_once():
        called["tls"] += 1
        return True, False, ""

    monkeypatch.setattr(worker, "_run_tls_fingerprint_direct_once", _tls_once)

    ok, cancelled, err = worker._run_download_attempt(["yt-dlp"], {})

    assert ok is True
    assert cancelled is False
    assert err == ""
    assert called["tls"] == 1


def test_downloader_falls_back_to_native_segmented_when_tls_provider_is_unavailable(monkeypatch):
    worker = _make_worker()
    worker.use_native_engine = True
    worker.url = "https://example.com/file.mp4"
    monkeypatch.setenv("VIDDOWNLOADER_ENABLE_TLS_FINGERPRINT_PROVIDER", "1")
    monkeypatch.setattr(worker, "_is_tls_transport_available", lambda: False)
    called = {"native": 0}

    def _native_once():
        called["native"] += 1
        return True, False, ""

    monkeypatch.setattr(worker, "_run_native_segmented_once", _native_once)

    ok, cancelled, err = worker._run_download_attempt(["yt-dlp"], {})

    assert ok is True
    assert cancelled is False
    assert err == ""
    assert called["native"] == 1


def test_downloader_dynamic_bandwidth_limit_uses_scheduler_when_enabled(monkeypatch):
    worker = _make_worker()
    worker.bandwidth_limit_kbps = 777

    monkeypatch.setattr("core.downloader.scheduler.enabled", True)
    monkeypatch.setattr("core.downloader.scheduler.get_current_limit", lambda: 321)
    assert worker.get_dynamic_bandwidth_limit_kbps() == 321

    monkeypatch.setattr("core.downloader.scheduler.enabled", False)
    assert worker.get_dynamic_bandwidth_limit_kbps() == 777
