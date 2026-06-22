from types import SimpleNamespace

from core.window_controllers.queue_transfer_controller import QueueTransferController


class _DummyWindow:
    def __init__(self):
        self.info_messages = []
        self.warn_messages = []
        self.logs = []

    def _info(self, message):
        self.info_messages.append(message)

    def _warn(self, message):
        self.warn_messages.append(message)

    def _append_log(self, message):
        self.logs.append(message)


def test_infer_export_format_prefers_extension_over_filter():
    assert QueueTransferController._infer_export_format("queue.csv", "JSON Files (*.json)") == "csv"
    assert QueueTransferController._infer_export_format("queue.txt", "JSON Files (*.json)") == "txt"
    assert QueueTransferController._infer_export_format("queue", "CSV Files (*.csv)") == "csv"
    assert QueueTransferController._infer_export_format("queue", "Text Files (*.txt)") == "txt"
    assert QueueTransferController._infer_export_format("queue", "") == "json"


def test_export_queue_csv_writes_tabular_payload(tmp_path):
    window = _DummyWindow()
    controller = QueueTransferController(window)
    out_path = tmp_path / "queue.csv"

    controller._export_queue_csv(
        str(out_path),
        [
            {
                "url": "https://example.com/watch?v=1",
                "title": "Video 1",
                "status": "pending",
                "mode": "video",
                "format": "MP4",
                "quality": "1080p",
                "out_dir": "D:/Downloads",
            }
        ],
    )

    content = out_path.read_text(encoding="utf-8")
    assert "url,title,status,mode,format,quality,out_dir" in content
    assert "https://example.com/watch?v=1,Video 1,pending,video,MP4,1080p,D:/Downloads" in content
    assert window.warn_messages == []


def test_export_queue_txt_writes_urls_only(tmp_path):
    window = _DummyWindow()
    controller = QueueTransferController(window)
    out_path = tmp_path / "queue.txt"

    controller._export_queue_txt(
        str(out_path),
        [
            {"url": "https://example.com/watch?v=1", "title": "Video 1"},
            {"url": "   ", "title": "Skip"},
            SimpleNamespace(),
        ],
    )

    assert out_path.read_text(encoding="utf-8") == "https://example.com/watch?v=1\n"
    assert window.warn_messages == []
