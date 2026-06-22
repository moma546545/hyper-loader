
import os
import logging
import re
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLineEdit, QPushButton,
    QLabel, QComboBox, QFileDialog, QTextEdit, QScrollArea, QGridLayout, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtGui import QPixmap
os.environ.setdefault("QT_API", "pyside6")
import qtawesome as qta

logger = logging.getLogger("ToolsView")

from ui.views.base_view import BaseView
from core.i18n import _
from core.error_handler import ErrorHandler
from core.smart_rename import TEMPLATES as RENAME_TEMPLATES, build_filename
from core.workers import ConversionWorker, ThumbnailExtractWorker


class _BulkRenameWorker(QThread):
    finished = Signal(int)
    failed = Signal(str)

    def __init__(self, folder: str, template: str, parent=None):
        super().__init__(parent)
        self._folder = folder
        self._template = template

    def run(self):
        renamed_count = 0
        try:
            for root, _, files in os.walk(self._folder):
                if self.isInterruptionRequested():
                    break
                for name in files:
                    if self.isInterruptionRequested():
                        break
                    full = os.path.join(root, name)
                    if not os.path.isfile(full):
                        continue
                    base, ext = os.path.splitext(name)
                    if not ext:
                        continue
                    new_rel = build_filename(self._template, title=base, ext=ext.lstrip("."), channel="", quality="")
                    new_full = os.path.join(root, new_rel)
                    if os.path.abspath(full) == os.path.abspath(new_full):
                        continue
                    os.makedirs(os.path.dirname(new_full), exist_ok=True)
                    try:
                        os.rename(full, new_full)
                        renamed_count += 1
                    except Exception as exc:
                        logger.warning(f"Bulk rename failed for {full}: {exc}")
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(renamed_count)


class ToolsView(BaseView):
    fetch_channel_requested = Signal(str)
    export_queue_requested = Signal()
    import_queue_requested = Signal()
    retry_failed_requested = Signal()
    optimize_queue_requested = Signal()

    def __init__(self, main_window=None, parent=None):
        super().__init__(main_window, parent)
        self._conversion_worker = None
        self._thumbnail_worker = None
        self._rename_worker = None
        self._last_extract_btn = None
        self.setup_ui()

    def _media_tools_controller(self):
        return getattr(self.main_window, "media_tools_controller", None)

    def _create_tool_card(self, title_text, icon_name, desc_text=""):
        card = QFrame()
        card.setObjectName("tool_card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(15)
        
        # Card styling with glassmorphism
        card.setStyleSheet("""
            QFrame#tool_card {
                background-color: rgba(30, 30, 35, 180);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 12px;
            }
            QFrame#tool_card:hover {
                background-color: rgba(45, 45, 55, 200);
                border: 1px solid rgba(99, 102, 241, 0.4);
            }
        """)

        # Apply drop shadow to the card
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 80))
        shadow.setOffset(0, 5)
        card.setGraphicsEffect(shadow)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        
        icon_lbl = QLabel()
        icon_lbl.setPixmap(qta.icon(icon_name, color="#6366F1").pixmap(24, 24))
        icon_lbl.setStyleSheet("background: transparent; border: none;")
        
        title_lbl = QLabel(title_text)
        title_lbl.setStyleSheet("font-size: 16px; font-weight: bold; color: #E5E7EB; background: transparent; border: none;")
        
        header_layout.addWidget(icon_lbl)
        header_layout.addWidget(title_lbl)
        header_layout.addStretch(1)
        
        card_layout.addLayout(header_layout)
        
        if desc_text:
            desc_lbl = QLabel(desc_text)
            desc_lbl.setStyleSheet("color: #9CA3AF; font-size: 12px; background: transparent; border: none;")
            desc_lbl.setWordWrap(True)
            card_layout.addWidget(desc_lbl)
            
        # Add a subtle separator line
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("border: none; background-color: rgba(255, 255, 255, 0.05); max-height: 1px; margin-top: 5px; margin-bottom: 5px;")
        card_layout.addWidget(line)

        return card, card_layout

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Scroll area setup
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background-color: transparent; }")
        
        main_content = QWidget()
        main_layout = QVBoxLayout(main_content)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(25)

        title = QLabel(_("Advanced Tools"))
        title.setObjectName("single_title")
        main_layout.addWidget(title)

        # Grid Layout for Cards
        grid = QGridLayout()
        grid.setSpacing(20)
        
        # 1. Converter Section
        conv_frame, conv_layout = self._create_tool_card(
            _("Video/Audio Converter"), 
            "fa5s.sync-alt",
            _("Convert media files to MP3, MP4, MKV, AVI, or GIF format.")
        )
        
        self.conv_input = QLineEdit()
        self.conv_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.conv_input.setPlaceholderText(_("Select file to convert..."))
        browse_btn = QPushButton(_("Browse"))
        controller = self._media_tools_controller()
        browse_btn.clicked.connect(controller.pick_conv_file if controller is not None else self._pick_conv_file)
        
        input_row = QHBoxLayout()
        input_row.addWidget(self.conv_input)
        input_row.addWidget(browse_btn)
        
        self.conv_fmt = QComboBox()
        self.conv_fmt.addItems(["MP3", "MP4", "MKV", "AVI", "GIF"])
        self.conv_fmt.setMinimumWidth(100)
        
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel(_("Output Format:")))
        fmt_row.addWidget(self.conv_fmt)
        fmt_row.addStretch(1)
        
        start_conv = QPushButton(_("Start Conversion"))
        start_conv.setObjectName("action_download")
        start_conv.clicked.connect(controller.start_conversion if controller is not None else self._start_conversion)
        
        conv_layout.addLayout(input_row)
        conv_layout.addLayout(fmt_row)
        conv_layout.addStretch(1)
        conv_layout.addWidget(start_conv)
        
        # 2. Channel Downloader Section
        chan_frame, chan_layout = self._create_tool_card(
            _("Channel Downloader"), 
            "fa5s.list-ul",
            _("Fetch all videos from a channel or playlist URL to batch download.")
        )
        
        self.chan_url = QLineEdit()
        self.chan_url.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.chan_url.setPlaceholderText(_("Enter Channel or Playlist URL..."))
        
        start_chan = QPushButton(_("Fetch Channel Videos"))
        start_chan.setObjectName("action_schedule")
        start_chan.clicked.connect(controller.fetch_channel if controller is not None else self._fetch_channel)
        
        chan_layout.addWidget(self.chan_url)
        chan_layout.addStretch(1)
        chan_layout.addWidget(start_chan)

        # 3. Subtitle Editor Section
        sub_frame, sub_layout = self._create_tool_card(
            _("Subtitle Editor"), 
            "fa5s.closed-captioning",
            _("View, edit and save subtitle files (.srt, .vtt).")
        )

        self.sub_input = QLineEdit()
        self.sub_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.sub_input.setPlaceholderText(_("Select subtitle file..."))
        sub_browse = QPushButton(_("Browse"))
        sub_browse.clicked.connect(self._pick_sub_file)

        sub_row = QHBoxLayout()
        sub_row.addWidget(self.sub_input)
        sub_row.addWidget(sub_browse)

        self.sub_text = QTextEdit()
        self.sub_text.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.sub_text.setMinimumHeight(120)

        sub_actions = QHBoxLayout()
        load_btn = QPushButton(_("Load"))
        save_btn = QPushButton(_("Save"))
        load_btn.clicked.connect(self._load_subtitle)
        save_btn.clicked.connect(self._save_subtitle)
        sub_actions.addWidget(load_btn)
        sub_actions.addWidget(save_btn)
        sub_actions.addStretch(1)

        sub_layout.addLayout(sub_row)
        sub_layout.addWidget(self.sub_text)
        sub_layout.addLayout(sub_actions)

        # 4. Bulk Rename Downloads
        rename_frame, rename_layout = self._create_tool_card(
            _("Bulk Rename Downloads"), 
            "fa5s.edit",
            _("Rename multiple downloaded files based on naming templates.")
        )

        self.rename_folder_input = QLineEdit()
        self.rename_folder_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.rename_folder_input.setPlaceholderText(_("Select folder containing downloads..."))
        rename_browse = QPushButton(_("Browse"))
        rename_browse.clicked.connect(self._pick_rename_folder)

        rename_row = QHBoxLayout()
        rename_row.addWidget(self.rename_folder_input)
        rename_row.addWidget(rename_browse)

        self.rename_template_combo = QComboBox()
        self.rename_template_combo.addItems(list(RENAME_TEMPLATES.keys()))

        template_row = QHBoxLayout()
        template_row.addWidget(QLabel(_("Template:")))
        template_row.addWidget(self.rename_template_combo)
        template_row.addStretch(1)

        apply_rename = QPushButton(_("Apply Rename"))
        apply_rename.setObjectName("action_schedule")
        apply_rename.clicked.connect(self._apply_bulk_rename)

        rename_layout.addLayout(rename_row)
        rename_layout.addLayout(template_row)
        rename_layout.addStretch(1)
        rename_layout.addWidget(apply_rename)

        # 5. Thumbnail Extractor
        thumb_frame, thumb_layout = self._create_tool_card(
            _("Thumbnail Extractor"), 
            "fa5s.image",
            _("Extract a high-quality frame from a video at a specific time.")
        )

        self.thumb_input = QLineEdit()
        self.thumb_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.thumb_input.setPlaceholderText(_("Select video file..."))
        thumb_browse = QPushButton(_("Browse"))
        thumb_browse.clicked.connect(self._pick_thumb_file)

        thumb_row = QHBoxLayout()
        thumb_row.addWidget(self.thumb_input)
        thumb_row.addWidget(thumb_browse)

        time_row = QHBoxLayout()
        self.thumb_time_input = QLineEdit("00:10")
        self.thumb_time_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.thumb_time_input.setPlaceholderText(_("Time (HH:MM:SS)"))
        self.thumb_time_input.setFixedWidth(100)
        time_row.addWidget(QLabel(_("Capture at:"), alignment=Qt.AlignmentFlag.AlignVCenter))
        time_row.addWidget(self.thumb_time_input)
        time_row.addStretch(1)

        extract_btn = QPushButton(_("Extract Thumbnail"))
        extract_btn.setObjectName("action_download")
        extract_btn.clicked.connect(self._extract_thumbnail)

        self.thumb_status_label = QLabel(_("جاهز لاستخراج الصورة المصغرة"))
        self.thumb_status_label.setWordWrap(True)
        self.thumb_status_label.setStyleSheet("color: #9CA3AF; font-size: 12px; background: transparent; border: none;")

        self.thumb_preview = QLabel()
        self.thumb_preview.setFixedSize(220, 124)
        self.thumb_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_preview.setText(_("لا توجد معاينة بعد"))
        self.thumb_preview.setStyleSheet(
            "background-color: rgba(0, 0, 0, 0.2);"
            "border: 1px solid rgba(255, 255, 255, 0.06);"
            "border-radius: 10px;"
            "color: #9CA3AF;"
        )

        thumb_layout.addLayout(thumb_row)
        thumb_layout.addLayout(time_row)
        thumb_layout.addWidget(extract_btn)
        thumb_layout.addWidget(self.thumb_status_label)
        thumb_layout.addWidget(self.thumb_preview, alignment=Qt.AlignmentFlag.AlignLeft)
        thumb_layout.addStretch(1)

        # 6. Queue Tools
        queue_frame, queue_layout = self._create_tool_card(
            _("Queue Tools"), 
            "fa5s.tasks",
            _("Manage your download queue: export, import, or retry failed items.")
        )

        export_queue_btn = QPushButton(_("Export Queue"))
        export_queue_btn.setObjectName("action_trim")
        export_queue_btn.clicked.connect(self.export_queue_requested.emit)
        
        import_queue_btn = QPushButton(_("Import Queue"))
        import_queue_btn.setObjectName("action_schedule")
        import_queue_btn.clicked.connect(self.import_queue_requested.emit)
        
        retry_failed_btn = QPushButton(_("Retry Failed"))
        retry_failed_btn.setObjectName("action_download")
        retry_failed_btn.clicked.connect(self.retry_failed_requested.emit)

        optimize_queue_btn = QPushButton(_("Auto-Optimize Queue"))
        optimize_queue_btn.setObjectName("action_schedule")
        optimize_queue_btn.clicked.connect(self.optimize_queue_requested.emit)
        
        # Make buttons expand
        export_queue_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        import_queue_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        retry_failed_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        optimize_queue_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        queue_layout.addWidget(export_queue_btn)
        queue_layout.addWidget(import_queue_btn)
        queue_layout.addWidget(retry_failed_btn)
        queue_layout.addWidget(optimize_queue_btn)
        queue_layout.addStretch(1)

        # Add cards to Grid (2 columns)
        grid.addWidget(conv_frame, 0, 0)
        grid.addWidget(chan_frame, 0, 1)
        grid.addWidget(sub_frame, 1, 0)
        grid.addWidget(rename_frame, 1, 1)
        grid.addWidget(thumb_frame, 2, 0)
        grid.addWidget(queue_frame, 2, 1)
        
        main_layout.addLayout(grid)
        main_layout.addStretch(1)

        scroll.setWidget(main_content)
        layout.addWidget(scroll)

    def _pick_conv_file(self):
        controller = self._media_tools_controller()
        if controller is not None:
            controller.pick_conv_file()
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            _("Select Media File"),
            "",
            _("Media Files (*.mp4 *.mkv *.avi *.mp3 *.wav);;All Files (*)"),
        )
        if path:
            self.conv_input.setText(path)

    def _start_conversion(self):
        controller = self._media_tools_controller()
        if controller is not None:
            controller.start_conversion()
            return
        if self._conversion_worker is not None and self._conversion_worker.isRunning():
            ErrorHandler.show_info(self, _("Busy"), _("A conversion is already in progress."))
            return
        input_path = self.conv_input.text()
        if not input_path or not os.path.exists(input_path):
            ErrorHandler.show_warning(self, _("Error"), _("Please select a valid file."))
            return
        
        fmt = self.conv_fmt.currentText().lower()
        output_path = os.path.splitext(input_path)[0] + f"_converted.{fmt}"
        worker = ConversionWorker(input_path, fmt, output_path, self)
        self._conversion_worker = worker

        def _on_done(success: bool, final_path: str, error: str):
            self._conversion_worker = None
            if success:
                ErrorHandler.show_info(
                    self,
                    _("Done"),
                    _("Conversion finished successfully! Output: {path}").format(path=final_path),
                )
                return
            msg = str(error or _("FFmpeg conversion failed.")).strip()
            ErrorHandler.show_error(self, _("Error"), msg)

        worker.finished.connect(_on_done)
        worker.start()
        ErrorHandler.show_info(
            self,
            _("Started"),
            _("Conversion started! Output: {path}").format(path=output_path),
        )
        logger.info(f"Conversion started: {input_path} -> {output_path}")

    def _fetch_channel(self):
        controller = self._media_tools_controller()
        if controller is not None:
            controller.fetch_channel()
            return
        url = self.chan_url.text().strip()
        if not url: return
        self.fetch_channel_requested.emit(url)

    def _pick_sub_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            _("Select Subtitle File"),
            "",
            _("Subtitle Files (*.srt *.vtt);;All Files (*)"),
        )
        if path:
            self.sub_input.setText(path)

    def _load_subtitle(self):
        path = self.sub_input.text().strip()
        if not path or not os.path.exists(path):
            ErrorHandler.show_warning(self, _("Error"), _("Please select a valid subtitle file."))
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                self.sub_text.setPlainText(f.read())
        except Exception as exc:
            ErrorHandler.show_error(self, _("Error"), _("Failed to load subtitle: {err}").format(err=str(exc)), exc=exc)

    def _save_subtitle(self):
        path = self.sub_input.text().strip()
        if not path:
            ErrorHandler.show_warning(self, _("Error"), _("Please select a valid subtitle file."))
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.sub_text.toPlainText())
            ErrorHandler.show_info(self, _("Success"), _("Subtitle saved: {path}").format(path=path))
        except Exception as exc:
            ErrorHandler.show_error(self, _("Error"), _("Failed to save subtitle: {err}").format(err=str(exc)), exc=exc)

    def _pick_rename_folder(self):
        path = QFileDialog.getExistingDirectory(self, _("Select Folder"), "")
        if path:
            self.rename_folder_input.setText(path)

    def _apply_bulk_rename(self):
        folder = self.rename_folder_input.text().strip()
        if not folder or not os.path.isdir(folder):
            ErrorHandler.show_warning(self, _("Error"), _("Please select a valid folder."))
            return
        if self._rename_worker is not None and self._rename_worker.isRunning():
            ErrorHandler.show_warning(self, _("Info"), _("Bulk rename is already running."))
            return
        template = self.rename_template_combo.currentText().strip()
        self._rename_worker = _BulkRenameWorker(folder, template, self)
        self._rename_worker.finished.connect(self._on_bulk_rename_finished)
        self._rename_worker.failed.connect(self._on_bulk_rename_failed)
        self._rename_worker.start()
        ErrorHandler.show_info(self, _("Started"), _("Bulk rename started."))

    def _on_bulk_rename_finished(self, count: int):
        self._rename_worker = None
        if int(count) > 0:
            ErrorHandler.show_info(self, _("Success"), _("Renamed {count} files.").format(count=count))
        else:
            ErrorHandler.show_warning(self, _("Info"), _("No files were renamed."))

    def _on_bulk_rename_failed(self, error_text: str):
        self._rename_worker = None
        ErrorHandler.show_error(self, _("Error"), _("Bulk rename failed: {err}").format(err=error_text))

    def _pick_thumb_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            _("Select Video File"),
            "",
            _("Video Files (*.mp4 *.mkv *.avi *.mov);;All Files (*)"),
        )
        if path:
            self.thumb_input.setText(path)

    def _extract_thumbnail(self):
        input_path = self.thumb_input.text().strip()
        if not input_path or not os.path.exists(input_path):
            ErrorHandler.show_warning(self, _("Error"), _("Please select a valid video file."))
            return
        time_value = self.thumb_time_input.text().strip() or "00:10"
        if not self._is_valid_thumbnail_time(time_value):
            self._set_thumbnail_status(_("صيغة الوقت غير صالحة. استخدم MM:SS أو HH:MM:SS"), tone="error")
            ErrorHandler.show_warning(self, _("Error"), _("Invalid time format. Use MM:SS or HH:MM:SS."))
            return
        base, _ = os.path.splitext(input_path)
        output_path = base + "_thumb.jpg"
        if self._thumbnail_worker is not None and self._thumbnail_worker.isRunning():
            ErrorHandler.show_warning(self, _("Info"), _("Thumbnail extraction is already running."))
            return
            
        # Add loading state to the button
        extract_btn = self.sender()
        if isinstance(extract_btn, QPushButton):
            extract_btn.setEnabled(False)
            extract_btn.setText(_("Extracting..."))
            self._last_extract_btn = extract_btn
        else:
            self._last_extract_btn = None

        self._set_thumbnail_status(_("جاري استخراج الصورة المصغرة..."), tone="busy")
        self.thumb_preview.clear()
        self.thumb_preview.setText(_("جاري إنشاء المعاينة..."))
        self._thumbnail_worker = ThumbnailExtractWorker(input_path, time_value, output_path, self)
        self._thumbnail_worker.finished.connect(self._on_thumbnail_extracted)
        self._thumbnail_worker.start()
        ErrorHandler.show_info(
            self,
            _("Started"),
            _("Thumbnail extraction started: {path}").format(path=output_path),
        )
        logger.info(f"Thumbnail extraction started: {input_path} -> {output_path}")

    def _on_thumbnail_extracted(self, success: bool, output_path: str, error: str):
        self._thumbnail_worker = None
        
        # Reset button state
        if hasattr(self, "_last_extract_btn") and self._last_extract_btn:
            self._last_extract_btn.setEnabled(True)
            self._last_extract_btn.setText(_("Extract Thumbnail"))
            self._last_extract_btn = None
            
        if success:
            pixmap = QPixmap(output_path)
            if not pixmap.isNull():
                self.thumb_preview.setPixmap(
                    pixmap.scaled(
                        self.thumb_preview.width(),
                        self.thumb_preview.height(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                self.thumb_preview.setText(_("تم الحفظ لكن تعذر تحميل المعاينة"))
            self._set_thumbnail_status(_("تم استخراج الصورة المصغرة بنجاح"), tone="success")
            ErrorHandler.show_info(
                self,
                _("Success"),
                _("Thumbnail saved to: {path}").format(path=output_path),
            )
            logger.info(f"Thumbnail extraction completed: {output_path}")
            return
        ErrorHandler.show_error(
            self,
            _("Error"),
            _("Thumbnail extraction failed: {err}").format(err=error or _("Unknown error")),
        )
        self.thumb_preview.setText(_("فشل إنشاء المعاينة"))
        self._set_thumbnail_status(_("فشل استخراج الصورة المصغرة"), tone="error")
        logger.warning(f"Thumbnail extraction failed: {error}")

    def _set_thumbnail_status(self, text: str, tone: str = "muted"):
        if not hasattr(self, "thumb_status_label"):
            return
        colors = {
            "muted": "#9CA3AF",
            "busy": "#F59E0B",
            "success": "#10B981",
            "error": "#EF4444",
        }
        color = colors.get(str(tone or "").strip().lower(), colors["muted"])
        self.thumb_status_label.setText(str(text or ""))
        self.thumb_status_label.setStyleSheet(
            f"color: {color}; font-size: 12px; background: transparent; border: none;"
        )

    def _is_valid_thumbnail_time(self, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return bool(re.fullmatch(r"(?:(?:\d{1,2}):)?\d{1,2}:\d{2}", text))



