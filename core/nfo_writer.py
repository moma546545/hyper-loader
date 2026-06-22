import os
import logging
import tempfile
import xml.etree.ElementTree as ET


logger = logging.getLogger("SnapDownloader.NFO")


def write_nfo_for_download(file_path: str, task: dict, payload: dict):
    path = os.path.abspath(str(file_path or "").strip())
    if not path or not os.path.isfile(path):
        return
    nfo_path = os.path.splitext(path)[0] + ".nfo"
    try:
        root = ET.Element("movie")
        ET.SubElement(root, "title").text = str((task or {}).get("title", "") or "")
        ET.SubElement(root, "source_url").text = str((task or {}).get("url", "") or "")
        ET.SubElement(root, "channel").text = str((task or {}).get("channel", "") or "")
        ET.SubElement(root, "format").text = str((task or {}).get("format", "") or "")
        ET.SubElement(root, "quality").text = str((task or {}).get("quality", "") or "")
        ET.SubElement(root, "video_id").text = str((task or {}).get("video_id", "") or (task or {}).get("entry_id", "") or "")
        ET.SubElement(root, "sha256").text = str((payload or {}).get("checksum", "") or (task or {}).get("file_hash", "") or "")
        ET.SubElement(root, "downloaded_at").text = str((payload or {}).get("timestamp", "") or "")

        directory = os.path.dirname(nfo_path) or os.getcwd()
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(nfo_path)}.",
            suffix=".tmp",
            dir=directory,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                tree = ET.ElementTree(root)
                tree.write(handle, encoding="utf-8", xml_declaration=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, nfo_path)
        finally:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    except Exception as exc:
        logger.warning(f"[NFO] Failed to write NFO for {path}: {exc}")
