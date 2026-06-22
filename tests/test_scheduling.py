import os

import pytest

from core import smart_rename
from core.smart_rename import build_filename, get_output_path


def test_build_filename_sanitizes_traversal_components():
    name = build_filename("Default", "..", "mp4")
    assert ".." not in name
    assert "/" not in name
    assert "\\" not in name


def test_build_filename_sanitizes_encoded_traversal_components():
    name = build_filename("Channel - Title", "%2e%2e\\evil", "mp4", channel="%EF%BC%8E%EF%BC%8E/chan")
    assert ".." not in name
    assert "%2e%2e" not in name.lower()
    assert "/" not in name
    assert "\\" not in name


def test_get_output_path_stays_inside_out_dir(tmp_path):
    out_dir = tmp_path / "downloads"
    out_dir.mkdir()

    path = get_output_path(
        str(out_dir),
        "../../etc/{title}.{ext}",
        title="../evil/../video",
        ext="mp4",
        channel="x",
        quality="1080p",
    )

    full = os.path.realpath(str(path))
    base = os.path.realpath(str(out_dir))
    assert full == base or full.startswith(base + os.sep)


def test_get_output_path_limits_collision_attempts(tmp_path, monkeypatch):
    out_dir = tmp_path / "downloads"
    out_dir.mkdir()

    monkeypatch.setattr(smart_rename, "_MAX_COLLISION_ATTEMPTS", 3)
    monkeypatch.setattr(smart_rename.os.path, "exists", lambda _path: True)

    with pytest.raises(RuntimeError, match="Too many filename collision attempts"):
        get_output_path(
            str(out_dir),
            "Default",
            title="video",
            ext="mp4",
        )
