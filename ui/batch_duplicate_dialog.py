"""
ui/batch_duplicate_dialog.py — Batch Duplicate Review Dialog
Replaces per-item QMessageBox storms with a single table dialog that lets
the user review every detected duplicate and decide per-row (or all at once)
whether to skip or download anyway.
"""
from __future__ import annotations

import os
import logging
from typing import List, Tuple

try:
    from PySide6.QtCore import Qt, Signal
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
        QWidget, QAbstractItemView, QFrame, QSizePolicy,
    )
    from PySide6.QtGui import QColor, QFont
except ImportError:
    from PyQt6.QtCore import Qt, pyqtSignal as Signal
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
        QWidget, QAbstractItemView, QFrame, QSizePolicy,
    )
    from PyQt6.QtGui import QColor, QFont

logger = logging.getLogger("SnapDownloader.BatchDuplicateDialog")

# Each entry: (task_dict, report_dict)
# report_dict keys: url_duplicate, local_files, visual_duplicate, is_duplicate
DuplicateEntry = Tuple[dict, dict]


class BatchDuplicateDialog(QDialog):
    """
    Shows all pre-detected duplicate tasks in a table.
    The user marks each row as 'Skip' or 'Download Anyway'.
    Returns via .exec():
        - QDialog.DialogCode.Accepted  → apply choices
        - QDialog.DialogCode.Rejected  → cancel the entire batch
    After accept, call .get_allowed_tasks() for the approved list.
    """

    def __init__(self, entries: List[DuplicateEntry], parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚠️ Duplicate Detection — Batch Review")
        self.setMinimumSize(820, 480)
        self.setModal(True)
        self._entries = list(entries or [])
        # Per-row decision: True = download anyway, False = skip
        self._decisions: List[bool] = [False] * len(self._entries)
        self._build_ui()
        self._apply_stylesheet()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        # ── Title row
        title_lbl = QLabel("🔍  Duplicate Items Detected")
        title_lbl.setObjectName("dup_dialog_title")
        sub_lbl = QLabel(
            f"{len(self._entries)} item(s) were already downloaded or exist locally.\n"
            "Review each row and choose Skip or Download Anyway, then press Apply."
        )
        sub_lbl.setObjectName("dup_dialog_sub")
        sub_lbl.setWordWrap(True)
        root.addWidget(title_lbl)
        root.addWidget(sub_lbl)

        # ── Top action row
        top_bar = QHBoxLayout()
        self._skip_all_btn = QPushButton("⛔  Skip All")
        self._skip_all_btn.setObjectName("dup_btn_skip_all")
        self._skip_all_btn.clicked.connect(self._on_skip_all)
        self._dl_all_btn = QPushButton("✅  Download All Anyway")
        self._dl_all_btn.setObjectName("dup_btn_dl_all")
        self._dl_all_btn.clicked.connect(self._on_dl_all)
        top_bar.addWidget(self._skip_all_btn)
        top_bar.addWidget(self._dl_all_btn)
        top_bar.addStretch(1)
        root.addLayout(top_bar)

        # ── Table
        self._table = QTableWidget(len(self._entries), 4, self)
        self._table.setObjectName("dup_table")
        self._table.setHorizontalHeaderLabels(["Title", "Reason", "Skip", "Download Anyway"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setFrameShape(QFrame.Shape.NoFrame)

        self._skip_cbs: List[QCheckBox] = []
        self._dl_cbs: List[QCheckBox] = []

        for row_idx, (task, report) in enumerate(self._entries):
            self._table.setRowHeight(row_idx, 52)
            # ── Title
            title = str((task or {}).get("title", "") or "").strip() or str((task or {}).get("url", "") or "")[:60]
            title_item = QTableWidgetItem(title)
            title_item.setToolTip(str((task or {}).get("url", "") or ""))
            self._table.setItem(row_idx, 0, title_item)

            # ── Reason
            reason = self._build_reason_text(report)
            reason_item = QTableWidgetItem(reason)
            reason_item.setForeground(QColor("#F59E0B"))
            self._table.setItem(row_idx, 1, reason_item)

            # ── Skip checkbox (default: checked = will skip)
            skip_cb = QCheckBox()
            skip_cb.setChecked(True)  # default: skip duplicates
            skip_cb.toggled.connect(lambda checked, r=row_idx: self._on_skip_toggled(r, checked))
            self._skip_cbs.append(skip_cb)
            self._table.setCellWidget(row_idx, 2, self._centered_widget(skip_cb))

            # ── Download Anyway checkbox
            dl_cb = QCheckBox()
            dl_cb.setChecked(False)
            dl_cb.toggled.connect(lambda checked, r=row_idx: self._on_dl_toggled(r, checked))
            self._dl_cbs.append(dl_cb)
            self._table.setCellWidget(row_idx, 3, self._centered_widget(dl_cb))

        root.addWidget(self._table, 1)

        # ── Summary row
        self._summary_lbl = QLabel()
        self._summary_lbl.setObjectName("dup_summary")
        self._refresh_summary()
        root.addWidget(self._summary_lbl)

        # ── Footer buttons
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("border: 1px solid rgba(255,255,255,0.07);")
        root.addWidget(sep)

        footer = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("dup_btn_cancel")
        cancel_btn.clicked.connect(self.reject)
        self._apply_btn = QPushButton("✔  Apply Choices")
        self._apply_btn.setObjectName("dup_btn_apply")
        self._apply_btn.clicked.connect(self.accept)
        footer.addWidget(cancel_btn)
        footer.addStretch(1)
        footer.addWidget(self._apply_btn)
        root.addLayout(footer)

    @staticmethod
    def _centered_widget(widget: QWidget) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(widget)
        return container

    @staticmethod
    def _build_reason_text(report: dict) -> str:
        parts = []
        url_dup = report.get("url_duplicate") or {}
        local_files = report.get("local_files") or []
        visual_dup = report.get("visual_duplicate") or {}
        if isinstance(url_dup, dict) and url_dup:
            ts = str(url_dup.get("timestamp", "") or "")[:10]
            parts.append(f"In history ({ts})" if ts else "In history")
        if local_files:
            parts.append(f"{len(local_files)} local file(s)")
        if isinstance(visual_dup, dict) and visual_dup:
            dist = int(visual_dup.get("distance", 0) or 0)
            parts.append(f"Visual match (d={dist})")
        return " | ".join(parts) if parts else "Duplicate"

    # ── Interaction handlers ──────────────────────────────────────────────────

    def _on_skip_toggled(self, row_idx: int, checked: bool):
        """Skip toggled → if enabled, uncheck Download Anyway for that row."""
        if checked:
            self._decisions[row_idx] = False
            dl_cb = self._dl_cbs[row_idx]
            dl_cb.blockSignals(True)
            dl_cb.setChecked(False)
            dl_cb.blockSignals(False)
        else:
            # unchecking skip → auto-enable download
            self._decisions[row_idx] = True
            dl_cb = self._dl_cbs[row_idx]
            dl_cb.blockSignals(True)
            dl_cb.setChecked(True)
            dl_cb.blockSignals(False)
        self._refresh_summary()

    def _on_dl_toggled(self, row_idx: int, checked: bool):
        """Download Anyway toggled → mirror to skip checkbox."""
        self._decisions[row_idx] = bool(checked)
        skip_cb = self._skip_cbs[row_idx]
        skip_cb.blockSignals(True)
        skip_cb.setChecked(not checked)
        skip_cb.blockSignals(False)
        self._refresh_summary()

    def _on_skip_all(self):
        for i in range(len(self._entries)):
            self._decisions[i] = False
            self._skip_cbs[i].blockSignals(True)
            self._skip_cbs[i].setChecked(True)
            self._skip_cbs[i].blockSignals(False)
            self._dl_cbs[i].blockSignals(True)
            self._dl_cbs[i].setChecked(False)
            self._dl_cbs[i].blockSignals(False)
        self._refresh_summary()

    def _on_dl_all(self):
        for i in range(len(self._entries)):
            self._decisions[i] = True
            self._skip_cbs[i].blockSignals(True)
            self._skip_cbs[i].setChecked(False)
            self._skip_cbs[i].blockSignals(False)
            self._dl_cbs[i].blockSignals(True)
            self._dl_cbs[i].setChecked(True)
            self._dl_cbs[i].blockSignals(False)
        self._refresh_summary()

    def _refresh_summary(self):
        download_count = sum(1 for d in self._decisions if d)
        skip_count = len(self._decisions) - download_count
        self._summary_lbl.setText(
            f"Will download: <b>{download_count}</b> &nbsp;|&nbsp; Will skip: <b>{skip_count}</b>"
        )

    # ── Result API ────────────────────────────────────────────────────────────

    def get_allowed_tasks(self) -> List[dict]:
        """Return only the tasks the user approved to download."""
        return [
            task
            for (task, _report), decision in zip(self._entries, self._decisions)
            if decision
        ]

    def get_skipped_tasks(self) -> List[dict]:
        """Return tasks the user chose to skip."""
        return [
            task
            for (task, _report), decision in zip(self._entries, self._decisions)
            if not decision
        ]

    # ── Stylesheet ────────────────────────────────────────────────────────────

    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #09090B;
                color: #FAFAFA;
                font-family: 'Segoe UI', system-ui, sans-serif;
            }
            QLabel#dup_dialog_title {
                font-size: 18px;
                font-weight: 900;
                color: #FAFAFA;
            }
            QLabel#dup_dialog_sub {
                font-size: 13px;
                color: #A1A1AA;
            }
            QLabel#dup_summary {
                font-size: 13px;
                color: #A1A1AA;
                padding: 4px 0;
            }
            QTableWidget#dup_table {
                background-color: #121214;
                color: #FAFAFA;
                border: none;
                border-radius: 10px;
                font-size: 13px;
                alternate-background-color: #1C1C1F;
                gridline-color: transparent;
            }
            QTableWidget#dup_table QHeaderView::section {
                background-color: #27272A;
                color: #A1A1AA;
                font-weight: 700;
                padding: 8px 10px;
                border: none;
                font-size: 12px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 5px;
                border: 2px solid #3F3F46;
                background: #27272A;
            }
            QCheckBox::indicator:checked {
                background-color: #6366F1;
                border-color: #6366F1;
            }
            QPushButton#dup_btn_skip_all {
                background-color: #27272A;
                color: #F43F5E;
                border: 1px solid rgba(244,63,94,0.4);
                border-radius: 10px;
                padding: 8px 18px;
                font-weight: 700;
                font-size: 13px;
            }
            QPushButton#dup_btn_skip_all:hover {
                background-color: rgba(244,63,94,0.15);
            }
            QPushButton#dup_btn_dl_all {
                background-color: #27272A;
                color: #10B981;
                border: 1px solid rgba(16,185,129,0.4);
                border-radius: 10px;
                padding: 8px 18px;
                font-weight: 700;
                font-size: 13px;
            }
            QPushButton#dup_btn_dl_all:hover {
                background-color: rgba(16,185,129,0.15);
            }
            QPushButton#dup_btn_apply {
                background-color: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #6366F1,stop:1 #8B5CF6);
                color: #FFFFFF;
                border: none;
                border-radius: 10px;
                padding: 10px 28px;
                font-weight: 800;
                font-size: 14px;
            }
            QPushButton#dup_btn_apply:hover {
                background-color: #8B5CF6;
            }
            QPushButton#dup_btn_cancel {
                background-color: #27272A;
                color: #A1A1AA;
                border: 1px solid rgba(255,255,255,0.1);
                border-radius: 10px;
                padding: 10px 20px;
                font-weight: 600;
                font-size: 14px;
            }
            QPushButton#dup_btn_cancel:hover {
                color: #FAFAFA;
                background-color: #3F3F46;
            }
            QScrollBar:vertical {
                background: #121214;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #3F3F46;
                border-radius: 4px;
                min-height: 30px;
            }
        """)
