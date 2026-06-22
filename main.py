import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

try:
    from PySide6.QtCore import QCoreApplication, QTimer
except ImportError:
    from PyQt6.QtCore import QCoreApplication, QTimer

from core.event_bus import DownloadFinishedEvent, event_bus


logger = logging.getLogger("SnapDownloader.Main")
_HEADLESS_WORKER_ID = "headless_cli"


@dataclass
class HeadlessDownloadResult:
    success: bool = False
    message: str = ""
    data: dict = field(default_factory=dict)
    completed: bool = False


def _ensure_headless_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VidDownloader entrypoint. Defaults to GUI mode, or use --headless for CLI downloads."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run a single download without starting the GUI.",
    )
    parser.add_argument("--url", help="Media URL to download in headless mode.")
    parser.add_argument(
        "--url-file",
        help="Path to a text file containing one URL per line for headless batch mode.",
    )
    parser.add_argument("--out-dir", help="Output directory for headless mode.")
    parser.add_argument("--mode", choices=["video", "audio"], default="video")
    parser.add_argument("--quality", default="1080p")
    parser.add_argument("--format", dest="fmt", default="mp4")
    parser.add_argument("--subtitle", default="None")
    parser.add_argument("--start-time", default="")
    parser.add_argument("--end-time", default="")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=int, default=2)
    parser.add_argument("--cookies-file", default="")
    parser.add_argument("--rename-template", default="Default")
    parser.add_argument("--channel", default="")
    parser.add_argument("--bandwidth-limit-kbps", type=int, default=0)
    parser.add_argument("--verify-checksum", action="store_true")
    parser.add_argument("--virus-scan-after-download", action="store_true")
    parser.add_argument("--use-ytdlp-api", action="store_true")
    parser.add_argument(
        "--no-aria2",
        action="store_true",
        help="Disable aria2 integration for the headless download.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="In batch mode, stop immediately when one URL fails.",
    )
    parser.add_argument(
        "--report-json",
        help="Write a JSON report for headless execution (single or batch).",
    )
    return parser


def _validate_headless_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.headless:
        return
    if not str(args.out_dir or "").strip():
        parser.error("--out-dir is required with --headless")
    has_url = bool(str(args.url or "").strip())
    has_url_file = bool(str(args.url_file or "").strip())
    if not has_url and not has_url_file:
        parser.error("either --url or --url-file is required with --headless")


def _load_batch_urls(url_file_path: str) -> list[str]:
    urls: list[str] = []
    raw_path = str(url_file_path or "").strip()
    if raw_path == "-":
        raw_lines = sys.stdin.read().splitlines()
    else:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"URL file not found: {path}")
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    for raw in raw_lines:
        text = str(raw or "").strip()
        if not text or text.startswith("#"):
            continue
        urls.append(text)
    if not urls:
        raise ValueError("URL file is empty after filtering blank/comment lines.")
    return urls


def _build_headless_worker(args: argparse.Namespace):
    from core.downloader import DownloadWorker

    worker = DownloadWorker(
        target_url=str(args.url or "").strip(),
        out_dir=str(args.out_dir or "").strip(),
        mode=str(args.mode or "video"),
        quality=str(args.quality or "1080p"),
        fmt=str(args.fmt or "mp4"),
        subtitle_lang=str(args.subtitle or "None"),
        start_time=str(args.start_time or ""),
        end_time=str(args.end_time or ""),
        retries=max(1, int(args.retries or 1)),
        retry_delay_seconds=max(1, int(args.retry_delay or 1)),
        use_aria2=not bool(args.no_aria2),
        cookies_file=str(args.cookies_file or ""),
        rename_template=str(args.rename_template or "Default"),
        channel=str(args.channel or ""),
        verify_checksum=bool(args.verify_checksum),
        virus_scan_after_download=bool(args.virus_scan_after_download),
        bandwidth_limit_kbps=max(0, int(args.bandwidth_limit_kbps or 0)),
        use_ytdlp_api=bool(args.use_ytdlp_api),
    )
    worker.worker_id = _HEADLESS_WORKER_ID
    return worker


def _print_worker_log(line: str) -> None:
    text = str(line or "").rstrip()
    if text:
        print(text, flush=True)


def _print_worker_state(state: str) -> None:
    text = str(state or "").strip()
    if text:
        print(f"[state] {text}", flush=True)


def _run_headless_download(args: argparse.Namespace) -> int:
    _ensure_headless_logging()
    app = QCoreApplication.instance() or QCoreApplication([sys.argv[0]])
    result = HeadlessDownloadResult()
    worker = _build_headless_worker(args)

    if hasattr(worker, "log"):
        worker.log.connect(_print_worker_log)
    if hasattr(worker, "state"):
        worker.state.connect(_print_worker_state)

    def _on_finished(event: DownloadFinishedEvent) -> None:
        if getattr(event, "worker_id", None) != _HEADLESS_WORKER_ID:
            return
        result.success = bool(event.success)
        result.message = str(event.message or "").strip()
        result.data = dict(getattr(event, "data", {}) or {})
        result.completed = True
        stream = sys.stdout if result.success else sys.stderr
        if result.message:
            print(result.message, file=stream, flush=True)
        QTimer.singleShot(0, app.quit)

    event_bus.subscribe(DownloadFinishedEvent, _on_finished)
    try:
        worker.start()
        app.exec()
    finally:
        event_bus.unsubscribe(DownloadFinishedEvent, _on_finished)
        try:
            if hasattr(worker, "isRunning") and worker.isRunning():
                if hasattr(worker, "stop"):
                    worker.stop()
                else:
                    worker.requestInterruption()
                    worker.quit()
        except Exception as exc:
            logger.debug(f"Headless worker shutdown failed: {exc}")
        try:
            if hasattr(worker, "wait_for_stop"):
                worker.wait_for_stop(2000)
            else:
                worker.wait(2000)
        except Exception as exc:
            logger.debug(f"Headless worker wait failed: {exc}")

    if not result.completed:
        print("Headless download did not report a completion event.", file=sys.stderr, flush=True)
        return 1
    return 0 if result.success else 1


def _run_headless_batch(args: argparse.Namespace, urls: list[str]) -> int:
    results: list[dict] = []
    failures = 0
    total = len(urls)
    for index, url in enumerate(urls, start=1):
        print(f"[headless] ({index}/{total}) starting: {url}", flush=True)
        item_args = argparse.Namespace(**vars(args))
        item_args.url = str(url).strip()
        exit_code = _run_headless_download(item_args)
        item_success = exit_code == 0
        results.append(
            {
                "index": index,
                "url": str(url).strip(),
                "success": item_success,
                "exit_code": int(exit_code),
            }
        )
        if exit_code != 0:
            failures += 1
            if bool(getattr(args, "fail_fast", False)):
                print("[headless] fail-fast triggered; stopping batch.", file=sys.stderr, flush=True)
                break
    print(f"[headless] completed: total={total}, failures={failures}", flush=True)
    _maybe_write_report(
        report_path=str(getattr(args, "report_json", "") or ""),
        payload={
            "mode": "headless_batch",
            "total_requested": total,
            "executed": len(results),
            "failures": failures,
            "success": failures == 0 and len(results) == total,
            "results": results,
        },
    )
    return 0 if failures == 0 else 1


def _maybe_write_report(report_path: str, payload: dict) -> None:
    target = str(report_path or "").strip()
    if not target:
        return
    out_path = Path(target).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload or {}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _validate_headless_args(parser, args)
    if bool(args.headless):
        has_url = bool(str(args.url or "").strip())
        has_url_file = bool(str(args.url_file or "").strip())
        if has_url_file and not has_url:
            try:
                urls = _load_batch_urls(args.url_file)
            except (OSError, ValueError) as exc:
                parser.error(str(exc))
            return _run_headless_batch(args, urls)
        if has_url_file and has_url:
            try:
                urls = [str(args.url).strip(), *_load_batch_urls(args.url_file)]
            except (OSError, ValueError) as exc:
                parser.error(str(exc))
            return _run_headless_batch(args, urls)
        code = _run_headless_download(args)
        _maybe_write_report(
            report_path=str(getattr(args, "report_json", "") or ""),
            payload={
                "mode": "headless_single",
                "url": str(args.url or "").strip(),
                "success": code == 0,
                "exit_code": int(code),
            },
        )
        return code
    from app import main as gui_main

    gui_result = gui_main()
    if gui_result is None:
        return 0
    try:
        return int(gui_result)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
