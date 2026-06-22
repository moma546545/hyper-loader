"""
Tests for the DownloadProviderRegistry and routing logic.
Uses a mock HTTP server via http.server to test real byte-range downloads.
"""
from __future__ import annotations
import os
import sys
import threading
import time
import tempfile
import http.server
import socketserver
from contextlib import contextmanager

# Ensure the project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.download_providers_oop.base import DownloadProviderRegistry
from core.download_providers_oop.ytdlp_provider import YtDlpProvider, _url_is_direct, _url_needs_extractor
from core.download_providers_oop.segmented_provider import SegmentedProvider


# ── URL routing tests ────────────────────────────────────────────────────────

class TestUrlRouting:
    def test_direct_mp4_url_is_detected(self):
        assert _url_is_direct("https://example.com/video.mp4") is True

    def test_youtube_url_is_not_direct(self):
        assert _url_is_direct("https://www.youtube.com/watch?v=abc123") is False

    def test_direct_mp3_url_is_detected(self):
        assert _url_is_direct("https://cdn.example.com/audio.mp3") is True

    def test_unknown_url_without_extension_is_not_direct(self):
        assert _url_is_direct("https://example.com/api/v1/video") is False

    def test_youtube_needs_extractor(self):
        assert _url_needs_extractor("https://www.youtube.com/watch?v=test") is True

    def test_vimeo_needs_extractor(self):
        assert _url_needs_extractor("https://vimeo.com/123456789") is True

    def test_direct_file_does_not_need_extractor(self):
        assert _url_needs_extractor("https://cdn.example.com/file.mp4") is False

    def test_twitter_needs_extractor(self):
        assert _url_needs_extractor("https://twitter.com/user/status/123") is True


class TestProviderRegistry:
    def test_segmented_provider_handles_direct_url(self):
        provider_cls = DownloadProviderRegistry.get_provider(
            "https://files.example.com/movie.mp4", is_direct=True
        )
        assert provider_cls is SegmentedProvider

    def test_ytdlp_provider_handles_youtube(self):
        provider_cls = DownloadProviderRegistry.get_provider(
            "https://www.youtube.com/watch?v=test", is_direct=False
        )
        assert provider_cls is YtDlpProvider

    def test_ytdlp_provider_handles_unknown_domain(self):
        provider_cls = DownloadProviderRegistry.get_provider(
            "https://somesite.example.com/watch?v=abc", is_direct=False
        )
        assert provider_cls is YtDlpProvider

    def test_segmented_provider_can_handle_direct_flag(self):
        assert SegmentedProvider.can_handle("https://example.com/api/resource", is_direct=True) is True

    def test_ytdlp_provider_cannot_handle_direct(self):
        assert YtDlpProvider.can_handle("https://example.com/video.mp4", is_direct=True) is False


# ── Mock HTTP server for real download tests ─────────────────────────────────

_DUMMY_CONTENT = b"A" * (512 * 1024)   # 512 KB of dummy data


def _local_task(url: str, tmp_path, **extra) -> dict:
    task = {
        "url": url,
        "out_dir": str(tmp_path),
        "cookies_file": "",
        "bandwidth_limit_kbps": 0,
        "allow_private_hosts": True,
    }
    task.update(extra)
    return task


class _RangeRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass   # silence server logs during tests

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(_DUMMY_CONTENT)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            start, end = rng[6:].split("-")
            start = int(start)
            end = int(end) if end else len(_DUMMY_CONTENT) - 1
            chunk = _DUMMY_CONTENT[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(_DUMMY_CONTENT)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(_DUMMY_CONTENT)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(_DUMMY_CONTENT)


class _CookieProtectedRangeRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _authorized(self) -> bool:
        return self.headers.get("Cookie", "") == "auth=ok"

    def do_HEAD(self):
        if not self._authorized():
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(_DUMMY_CONTENT)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            self.send_response(403)
            self.end_headers()
            return
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            start, end = rng[6:].split("-")
            start = int(start)
            end = int(end) if end else len(_DUMMY_CONTENT) - 1
            chunk = _DUMMY_CONTENT[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(_DUMMY_CONTENT)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(_DUMMY_CONTENT)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(_DUMMY_CONTENT)


class _FakeRangeRequestHandler(http.server.BaseHTTPRequestHandler):
    """Claims range support in HEAD, but ignores Range on GET."""

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(_DUMMY_CONTENT)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(_DUMMY_CONTENT)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(_DUMMY_CONTENT)


@contextmanager
def _mock_server():
    server = socketserver.TCPServer(("127.0.0.1", 0), _RangeRequestHandler)
    server.allow_reuse_address = True
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/file.bin"
    finally:
        server.shutdown()


@contextmanager
def _mock_cookie_server():
    server = socketserver.TCPServer(("127.0.0.1", 0), _CookieProtectedRangeRequestHandler)
    server.allow_reuse_address = True
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/file.bin"
    finally:
        server.shutdown()


@contextmanager
def _mock_fake_range_server():
    server = socketserver.TCPServer(("127.0.0.1", 0), _FakeRangeRequestHandler)
    server.allow_reuse_address = True
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/file.bin"
    finally:
        server.shutdown()


class TestSegmentedProviderDownload:
    def test_segmented_download_produces_correct_file(self, tmp_path):
        with _mock_server() as url:
            task = _local_task(url, tmp_path)
            provider = SegmentedProvider(task, worker=None)
            results = {}

            def on_done(success, error):
                results["success"] = success
                results["error"] = error

            provider.on_done = on_done

            provider.start()

            assert results.get("success") is True, f"Expected success, got error: {results.get('error')}"
            out_files = list(tmp_path.iterdir())
            assert len(out_files) == 1
            assert out_files[0].read_bytes() == _DUMMY_CONTENT

    def test_segmented_download_emits_progress(self, tmp_path):
        with _mock_server() as url:
            task = _local_task(url, tmp_path)
            provider = SegmentedProvider(task, worker=None)
            progress_values = []

            provider.on_progress = lambda pct, speed, eta: progress_values.append(pct)
            provider.on_done = lambda s, e: None
            provider.start()

            assert any(p > 0 for p in progress_values)

    def test_cancel_stops_download(self, tmp_path):
        with _mock_server() as url:
            task = _local_task(url, tmp_path)
            provider = SegmentedProvider(task, worker=None)
            results = {}

            def on_done(success, error):
                results["success"] = success

            provider.on_done = on_done

            original_download_chunk = provider._download_chunk

            def slow_chunk(*args, **kwargs):
                provider.stop()   # cancel mid-way
                return original_download_chunk(*args, **kwargs)

            provider._download_chunk = slow_chunk
            provider.start()

            assert results.get("success") is False

    def test_segmented_provider_reuses_output_path_when_resume_temp_exists(self, tmp_path):
        with _mock_server() as url:
            task = _local_task(url, tmp_path)
            expected_out = tmp_path / "file.bin"
            temp_path = tmp_path / "file.bin.sdtmp"
            meta_path = tmp_path / "file.bin.sdtmp.meta.json"

            first_provider = SegmentedProvider(task, worker=None)
            first_results = {}

            def _fake_first_segmented(_url, out_path, _total_bytes, **_kwargs):
                assert out_path == str(expected_out)
                temp_path.write_bytes(b"partial")
                meta_path.write_text("{}", encoding="utf-8")
                first_provider.is_cancelled = True
                return False, "تم إلغاء التحميل"

            first_provider._download_segmented = _fake_first_segmented
            first_provider.on_done = lambda success, error: first_results.update(success=success, error=error)

            first_provider.start()

            assert first_results.get("success") is False
            assert not expected_out.exists()
            assert temp_path.exists()
            assert meta_path.exists()

            second_provider = SegmentedProvider(task, worker=None)
            seen_paths = {}
            second_provider.on_path = lambda path: seen_paths.setdefault("path", path)

            def _fake_second_segmented(_url, out_path, _total_bytes, **_kwargs):
                seen_paths["segmented_path"] = out_path
                second_provider.is_cancelled = True
                return False, "تم إلغاء التحميل"

            second_provider._download_segmented = _fake_second_segmented
            second_provider.on_done = lambda *_args: None

            second_provider.start()

            assert seen_paths.get("path") == str(expected_out)
            assert seen_paths.get("segmented_path") == str(expected_out)
            assert not (tmp_path / "file_1.bin").exists()

    def test_pause_and_resume_download(self, tmp_path):
        """Verify that pausing and then resuming completes the download successfully."""
        with _mock_server() as url:
            task = _local_task(url, tmp_path)
            provider = SegmentedProvider(task, worker=None)
            results = {}
            provider.on_done = lambda s, e: results.__setitem__("success", s)

            # Pause for 0.1 second then resume
            def _delayed_resume():
                time.sleep(0.1)
                provider.resume()

            provider.pause()
            t = threading.Thread(target=_delayed_resume, daemon=True)
            t.start()
            provider.start()
            t.join(timeout=10)

            assert results.get("success") is True

    def test_segmented_download_uses_cookie_file_for_protected_resources(self, tmp_path):
        cookies_file = tmp_path / "cookies.txt"
        cookies_file.write_text(
            "# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tauth\tok\n",
            encoding="utf-8",
        )

        with _mock_cookie_server() as url:
            task = _local_task(url, tmp_path, cookies_file=str(cookies_file))
            provider = SegmentedProvider(task, worker=None)
            results = {}
            provider.on_done = lambda success, error: results.update(success=success, error=error)

            provider.start()

            assert results.get("success") is True, results.get("error")
            out_files = [path for path in tmp_path.iterdir() if path.name != "cookies.txt"]
            assert len(out_files) == 1
            assert out_files[0].read_bytes() == _DUMMY_CONTENT
            assert not any(path.suffix == ".sdtmp" for path in tmp_path.iterdir())

    def test_segmented_provider_falls_back_to_simple_download_when_segmented_fails(self, tmp_path, monkeypatch):
        with _mock_server() as url:
            task = _local_task(url, tmp_path)
            provider = SegmentedProvider(task, worker=None)
            calls = {"segmented": 0, "simple": 0}
            results = {}

            def _fake_segmented(*_args, **_kwargs):
                calls["segmented"] += 1
                return False, "segmented failed"

            def _fake_simple(*_args, **_kwargs):
                calls["simple"] += 1
                out_path = _args[1]
                with open(out_path, "wb") as handle:
                    handle.write(_DUMMY_CONTENT)
                return True, ""

            monkeypatch.setattr(provider, "_download_segmented", _fake_segmented)
            monkeypatch.setattr(provider, "_download_simple", _fake_simple)
            provider.on_done = lambda success, error: results.update(success=success, error=error)

            provider.start()

            assert calls["segmented"] == 1
            assert calls["simple"] == 1
            assert results.get("success") is True

    def test_segmented_provider_falls_back_when_server_ignores_range_get(self, tmp_path):
        with _mock_fake_range_server() as url:
            task = _local_task(url, tmp_path)
            provider = SegmentedProvider(task, worker=None)
            results = {}

            provider.on_done = lambda success, error: results.update(success=success, error=error)
            provider.start()

            assert results.get("success") is True, results.get("error")
            out_files = list(tmp_path.iterdir())
            assert len(out_files) == 1
            assert out_files[0].read_bytes() == _DUMMY_CONTENT

    def test_segmented_resume_metadata_writes_are_batched(self, tmp_path, monkeypatch):
        task = _local_task("https://example.com/file.bin", tmp_path)
        provider = SegmentedProvider(task, worker=None)
        out_path = tmp_path / "file.bin"
        saved_counts = []

        monkeypatch.setattr("core.download_providers_oop.segmented_provider._RESUME_META_FLUSH_EVERY_CHUNKS", 4)
        monkeypatch.setattr("core.download_providers_oop.segmented_provider._RESUME_META_FLUSH_INTERVAL_SECONDS", 9999.0)
        monkeypatch.setattr(provider, "_compute_connections", lambda _total: 2)
        monkeypatch.setattr(provider, "_compute_chunk_size", lambda _total, _n: 100)
        monkeypatch.setattr(provider, "_download_chunk", lambda chunk, *_args, **_kwargs: setattr(chunk, "done", True) or True)
        monkeypatch.setattr(
            provider,
            "_save_resume_chunk_indices",
            lambda **kwargs: saved_counts.append(len(set(kwargs.get("done_indices", set())))),
        )

        ok, error = provider._download_segmented(
            "https://example.com/file.bin",
            str(out_path),
            1000,
            cookies_path="",
            bandwidth_limit_kbps=0,
            allow_private_hosts=True,
        )

        assert ok is True, error
        assert saved_counts == [4, 8, 10]

    def test_segmented_resume_metadata_forces_flush_on_failure(self, tmp_path, monkeypatch):
        task = _local_task("https://example.com/file.bin", tmp_path)
        provider = SegmentedProvider(task, worker=None)
        out_path = tmp_path / "file.bin"
        saved_counts = []

        monkeypatch.setattr("core.download_providers_oop.segmented_provider._RESUME_META_FLUSH_EVERY_CHUNKS", 50)
        monkeypatch.setattr("core.download_providers_oop.segmented_provider._RESUME_META_FLUSH_INTERVAL_SECONDS", 9999.0)
        monkeypatch.setattr(provider, "_compute_connections", lambda _total: 1)
        monkeypatch.setattr(provider, "_compute_chunk_size", lambda _total, _n: 100)

        def _chunk_worker(chunk, *_args, **_kwargs):
            if chunk.index >= 2:
                chunk.error = "chunk failed"
                return False
            chunk.done = True
            return True

        monkeypatch.setattr(provider, "_download_chunk", _chunk_worker)
        monkeypatch.setattr(
            provider,
            "_save_resume_chunk_indices",
            lambda **kwargs: saved_counts.append(len(set(kwargs.get("done_indices", set())))),
        )

        ok, error = provider._download_segmented(
            "https://example.com/file.bin",
            str(out_path),
            500,
            cookies_path="",
            bandwidth_limit_kbps=0,
            allow_private_hosts=True,
        )

        assert ok is False
        assert error
        assert saved_counts == [2]

    def test_segmented_fallback_cleans_segmented_resume_artifacts_before_simple(self, tmp_path, monkeypatch):
        with _mock_server() as url:
            task = _local_task(url, tmp_path)
            provider = SegmentedProvider(task, worker=None)
            observed = {}

            def _fake_segmented(_url, out_path, _total_bytes, **_kwargs):
                tmp_file = out_path + ".sdtmp"
                meta_file = tmp_file + ".meta.json"
                with open(tmp_file, "wb") as handle:
                    handle.write(b"segmented-partial")
                with open(meta_file, "w", encoding="utf-8") as handle:
                    handle.write("{}")
                return False, "range mismatch"

            def _fake_simple(_url, out_path, **_kwargs):
                observed["tmp_exists_before_simple"] = os.path.exists(out_path + ".sdtmp")
                observed["meta_exists_before_simple"] = os.path.exists(out_path + ".sdtmp.meta.json")
                with open(out_path, "wb") as handle:
                    handle.write(_DUMMY_CONTENT)
                return True, ""

            monkeypatch.setattr(provider, "_download_segmented", _fake_segmented)
            monkeypatch.setattr(provider, "_download_simple", _fake_simple)
            provider.on_done = lambda *_args: None

            provider.start()

            assert observed["tmp_exists_before_simple"] is False
            assert observed["meta_exists_before_simple"] is False
