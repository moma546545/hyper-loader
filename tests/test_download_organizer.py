from core.download_organizer import organize_download_output


def test_organize_download_output_moves_media_and_sidecars_by_mode(tmp_path):
    source_dir = tmp_path / "downloads"
    source_dir.mkdir()
    media = source_dir / "clip.mp4"
    sidecar = source_dir / "clip.en.srt"
    info = source_dir / "clip.info.json"
    media.write_bytes(b"video")
    sidecar.write_text("subtitle", encoding="utf-8")
    info.write_text("{}", encoding="utf-8")

    result = organize_download_output(
        str(media),
        "mode",
        {"mode": "video"},
    )

    target_dir = source_dir / "Videos"
    assert result["moved"] is True
    assert result["target_dir"] == str(target_dir)
    assert result["file_path"] == str(target_dir / "clip.mp4")
    assert (target_dir / "clip.mp4").is_file()
    assert (target_dir / "clip.en.srt").is_file()
    assert (target_dir / "clip.info.json").is_file()
    assert not media.exists()
    assert not sidecar.exists()


def test_organize_download_output_handles_extension_collisions(tmp_path):
    source_dir = tmp_path / "downloads"
    source_dir.mkdir()
    media = source_dir / "song.mp3"
    sidecar = source_dir / "song.txt"
    media.write_bytes(b"audio")
    sidecar.write_text("lyrics", encoding="utf-8")

    target_dir = source_dir / "MP3"
    target_dir.mkdir()
    (target_dir / "song.mp3").write_bytes(b"existing")

    result = organize_download_output(
        str(media),
        "extension",
        {"mode": "audio"},
    )

    assert result["moved"] is True
    assert result["file_path"].endswith("song (1).mp3")
    assert (target_dir / "song (1).mp3").is_file()
    assert (target_dir / "song (1).txt").is_file()
