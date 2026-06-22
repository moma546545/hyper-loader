from .anti_detection import anti_detection_engine
from .constants import AUDIO_FORMATS, VIDEO_FORMATS
from .i18n import _
from .workers import FormatProbeWorker


def on_mode_changed(window) -> None:
    mode_index = window.search_view.current_mode_index()
    if mode_index == 0:
        window.search_view.quality_stack.setCurrentIndex(0)
        window.search_view.format_combo.clear()
        window.search_view.format_combo.addItems(VIDEO_FORMATS)
    elif mode_index == 1:
        window.search_view.quality_stack.setCurrentIndex(1)
        window.search_view.format_combo.clear()
        window.search_view.format_combo.addItems(AUDIO_FORMATS)
    elif mode_index == 2:
        window.search_view.quality_stack.setCurrentIndex(2)
        window.search_view.format_combo.clear()
        window.search_view.format_combo.addItem("GIF")


def toggle_advanced_options(window) -> None:
    if window.adv_container.isVisible():
        window.adv_container.hide()
        window.adv_toggle_btn.setText("⚙ Show advanced settings")
    else:
        window.adv_container.show()
        window.adv_toggle_btn.setText("⚙ Hide advanced settings")


def toggle_trim_options(window) -> None:
    window.analyze_controller.toggle_trim_options()


def on_formats_requested(window, url: str) -> None:
    text = str(url or "").strip()
    if not text:
        window._warn(_("يرجى إدخال رابط أولاً"))
        return
    if window._formats_worker is not None and window._formats_worker.isRunning():
        window._warn(_("يوجد فحص صيغ جارٍ بالفعل"))
        return
    window._append_log(_("جاري جلب الصيغ المتاحة..."))
    worker = FormatProbeWorker(
        text,
        window,
        cookies_file=window.cookies_path,
        extra_args=anti_detection_engine.get_yt_dlp_analysis_options(),
    )
    worker.finished.connect(window._on_formats_probe_finished)
    window._formats_worker = worker
    worker.start()


def on_formats_probe_finished(window, success: bool, output: str, error: str) -> None:
    worker = window._formats_worker
    window._formats_worker = None
    if worker is not None:
        try:
            worker.deleteLater()
        except RuntimeError:
            pass
    if not success:
        window._warn(error or _("تعذر جلب الصيغ من yt-dlp"))
        return
    text = str(output or "").strip()
    if not text:
        window._warn(_("لم يتم استرجاع أي صيغ من yt-dlp"))
        return
    framed = f"{'=' * 28}\nyt-dlp -F\n{'=' * 28}\n{text}\n{'=' * 28}"
    window.search_view.log_text.append(framed)
    window._info(_("تم عرض الصيغ المتاحة في سجل التحليل"))
