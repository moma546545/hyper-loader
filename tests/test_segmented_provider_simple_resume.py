import json
from types import SimpleNamespace

from core.download_providers_oop.segmented_provider import SegmentedProvider


class _FakeResponse:
    def __init__(self, chunks, *, status=200, url="https://example.com/file.bin", headers=None):
        self._chunks = list(chunks or [])
        self._index = 0
        self.status = int(status)
        self.url = str(url)
        self.headers = dict(headers or {})

    def getcode(self):
        return self.status

    def read(self, _size: int = -1):
        if self._index >= len(self._chunks):
            return b""
        value = self._chunks[self._index]
        self._index += 1
        return value

    def close(self):
        return None


def _safe_snapshot(*_args, **_kwargs):
    return SimpleNamespace(allowed_ips=("93.184.216.34",))


def test_simple_download_cancel_keeps_resume_artifacts(monkeypatch, tmp_path):
    out_path = tmp_path / "file.bin"
    task = {"url": "https://example.com/file.bin", "out_dir": str(tmp_path)}
    provider = SegmentedProvider(task, worker=None)

    monkeypatch.setattr("core.download_providers_oop.segmented_provider.resolve_safe_host_snapshot", _safe_snapshot)
    monkeypatch.setattr("core.download_providers_oop.segmented_provider.extract_response_peer_ip", lambda _resp: "")
    monkeypatch.setattr(
        "core.download_providers_oop.segmented_provider.urllib.request.urlopen",
        lambda *_args, **_kwargs: _FakeResponse(
            [b"abc", b"def"],
            status=200,
            headers={"Content-Length": "6"},
        ),
    )
    monkeypatch.setattr(
        provider,
        "_apply_bandwidth_throttle",
        lambda *_args, **_kwargs: provider._cancel_event.set(),
    )

    ok, err = provider._download_simple("https://example.com/file.bin", str(out_path))

    assert ok is False
    assert "إلغاء" in err
    tmp_resume = tmp_path / "file.bin.sdtmp"
    meta_resume = tmp_path / "file.bin.sdtmp.meta.json"
    assert tmp_resume.exists()
    assert meta_resume.exists()
    payload = json.loads(meta_resume.read_text(encoding="utf-8"))
    assert payload.get("mode") == "simple"
    assert int(payload.get("downloaded_bytes", 0)) > 0


def test_simple_download_resumes_from_existing_temp_with_range(monkeypatch, tmp_path):
    out_path = tmp_path / "file.bin"
    tmp_resume = tmp_path / "file.bin.sdtmp"
    meta_resume = tmp_path / "file.bin.sdtmp.meta.json"
    tmp_resume.write_bytes(b"abc")
    meta_resume.write_text(
        json.dumps(
            {
                "mode": "simple",
                "url": "https://example.com/file.bin",
                "downloaded_bytes": 3,
                "total_bytes": 6,
            }
        ),
        encoding="utf-8",
    )

    task = {"url": "https://example.com/file.bin", "out_dir": str(tmp_path)}
    provider = SegmentedProvider(task, worker=None)

    monkeypatch.setattr("core.download_providers_oop.segmented_provider.resolve_safe_host_snapshot", _safe_snapshot)
    monkeypatch.setattr("core.download_providers_oop.segmented_provider.extract_response_peer_ip", lambda _resp: "")

    captured = {"range": ""}

    def _fake_urlopen(req, **_kwargs):
        captured["range"] = str(req.headers.get("Range", "") or "")
        return _FakeResponse(
            [b"def"],
            status=206,
            headers={
                "Content-Length": "3",
                "Content-Range": "bytes 3-5/6",
            },
        )

    monkeypatch.setattr(
        "core.download_providers_oop.segmented_provider.urllib.request.urlopen",
        _fake_urlopen,
    )

    ok, err = provider._download_simple("https://example.com/file.bin", str(out_path))

    assert ok is True
    assert err == ""
    assert captured["range"] == "bytes=3-"
    assert out_path.read_bytes() == b"abcdef"
    assert not tmp_resume.exists()
    assert not meta_resume.exists()
