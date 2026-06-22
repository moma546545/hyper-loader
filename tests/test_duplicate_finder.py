import hashlib
import os

from core import duplicate_finder


def test_get_file_hash_returns_full_sha256(tmp_path):
    payload = (b"hello-world-" * 4096) + b"tail"
    file_path = tmp_path / "sample.bin"
    file_path.write_bytes(payload)

    expected = hashlib.sha256(payload).hexdigest()

    assert duplicate_finder.get_file_hash(str(file_path)) == expected


def test_scan_directory_for_duplicates_confirms_fast_hash_matches_with_full_hash(tmp_path):
    head = b"A" * duplicate_finder._FAST_HASH_HEAD_SIZE
    tail = b"Z" * duplicate_finder._FAST_HASH_TAIL_SIZE
    original = head + (b"1" * 64) + tail
    same_fast_hash_different_content = head + (b"2" * 64) + tail
    true_duplicate = original

    (tmp_path / "a.bin").write_bytes(original)
    (tmp_path / "b.bin").write_bytes(same_fast_hash_different_content)
    (tmp_path / "c.bin").write_bytes(true_duplicate)

    duplicates = duplicate_finder.scan_directory_for_duplicates(str(tmp_path))
    normalized = {(os.path.basename(left), os.path.basename(right)) for left, right in duplicates}

    assert normalized == {("c.bin", "a.bin")}


def test_check_url_duplicate_returns_history_record_only_when_file_still_exists(monkeypatch, tmp_path):
    existing_file = tmp_path / "video.mp4"
    existing_file.write_bytes(b"ok")

    monkeypatch.setattr(
        "core.database.url_exists_in_history",
        lambda _url: {"url": "https://example.com/watch?v=1", "file_path": str(existing_file)},
    )

    result = duplicate_finder.check_url_duplicate("https://example.com/watch?v=1")

    assert result is not None
    assert result["file_path"] == str(existing_file)


def test_check_url_duplicate_ignores_history_record_when_file_was_deleted(monkeypatch, tmp_path):
    missing_file = tmp_path / "missing.mp4"

    monkeypatch.setattr(
        "core.database.url_exists_in_history",
        lambda _url: {"url": "https://example.com/watch?v=1", "file_path": str(missing_file)},
    )

    result = duplicate_finder.check_url_duplicate("https://example.com/watch?v=1")

    assert result is None


def test_build_duplicate_report_does_not_flag_visual_match_alone_as_duplicate(monkeypatch):
    monkeypatch.setattr(duplicate_finder, "check_url_duplicate", lambda _url: None)
    monkeypatch.setattr(duplicate_finder, "scan_for_local_duplicates", lambda _directory, _title: [])
    monkeypatch.setattr(
        duplicate_finder,
        "find_best_visual_duplicate",
        lambda *_args, **_kwargs: {"title": "Old Video", "distance": 1},
    )
    monkeypatch.setattr(duplicate_finder, "Image", object())
    monkeypatch.setattr(duplicate_finder, "imagehash", object())

    report = duplicate_finder.build_duplicate_report(
        url="https://example.com/watch?v=1",
        out_dir="D:/downloads",
        title="Video Title",
        thumbnail="https://example.com/thumb.jpg",
        visual_candidates=[],
    )

    assert report["visual_duplicate"] == {"title": "Old Video", "distance": 1}
    assert report["is_duplicate"] is False
