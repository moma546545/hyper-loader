import time

import qtawesome as qta
from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from core.i18n import _


class SchedulePicker(QFrame):
    def __init__(self, parent=None, *, title: str | None = None, compact: bool = False):
        super().__init__(parent)
        self.setObjectName("schedule_panel")
        self._compact = bool(compact)
        self._expanded = False
        self._build_ui(title or _("Download Schedule"))
        self._apply_state()

    def _build_ui(self, title: str):
        self.setStyleSheet(
            """
            QFrame#schedule_panel {
                background-color: rgba(15, 23, 42, 0.58);
                border: 1px solid rgba(34, 211, 238, 0.22);
                border-radius: 14px;
            }
            QLabel#schedule_title {
                color: #E2E8F0;
                font-size: 14px;
                font-weight: 900;
            }
            QLabel#schedule_hint {
                color: #94A3B8;
                font-size: 12px;
            }
            QPushButton#schedule_header_button {
                background-color: transparent;
                border: none;
                color: #E2E8F0;
                text-align: left;
                font-weight: 900;
                padding: 0;
            }
            QPushButton#schedule_preset {
                background-color: rgba(30, 41, 59, 0.65);
                color: #CBD5E1;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 10px;
                padding: 8px 12px;
                font-weight: 800;
            }
            QPushButton#schedule_preset:hover {
                border-color: rgba(34, 211, 238, 0.55);
                color: #FFFFFF;
            }
            QPushButton#schedule_preset:checked {
                background-color: rgba(34, 211, 238, 0.16);
                border-color: #22D3EE;
                color: #22D3EE;
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10 if self._compact else 12)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        self.enabled_checkbox = QCheckBox("")
        self.enabled_checkbox.setToolTip(_("Enable scheduling"))
        self.enabled_checkbox.toggled.connect(self._apply_state)
        self.title_label = QPushButton(title)
        self.title_label.setObjectName("schedule_header_button")
        self.title_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_label.clicked.connect(self.toggle_expanded)
        self.summary_label = QLabel(_("Start immediately"))
        self.summary_label.setObjectName("schedule_hint")
        header.addWidget(self.enabled_checkbox)
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.summary_label)
        root.addLayout(header)

        self.details_widget = QFrame()
        self.details_widget.setObjectName("schedule_details")
        details_layout = QVBoxLayout(self.details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(8)

        controls = QGridLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setHorizontalSpacing(10)
        controls.setVerticalSpacing(8)

        self.date_time_edit = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
        self.date_time_edit.setCalendarPopup(True)
        self.date_time_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.date_time_edit.setMinimumWidth(180)
        self.date_time_edit.dateTimeChanged.connect(self._refresh_summary)

        self.repeat_combo = QComboBox()
        self.repeat_combo.addItem(_("No repeat"), "none")
        self.repeat_combo.addItem(_("Repeat daily"), "daily")
        self.repeat_combo.addItem(_("Repeat weekly"), "weekly")
        self.repeat_combo.currentIndexChanged.connect(self._refresh_summary)

        controls.addWidget(QLabel(_("Start at:")), 0, 0)
        controls.addWidget(self.date_time_edit, 0, 1)
        controls.addWidget(QLabel(_("Repeat:")), 0, 2)
        controls.addWidget(self.repeat_combo, 0, 3)
        controls.setColumnStretch(1, 2)
        controls.setColumnStretch(3, 1)
        details_layout.addLayout(controls)

        presets = QHBoxLayout()
        presets.setContentsMargins(0, 0, 0, 0)
        presets.setSpacing(8)
        self.preset_buttons = []
        for label, seconds in (
            (_("In 1 hour"), 3600),
            (_("Tonight"), self._seconds_until(23, 0)),
            (_("Tomorrow"), self._seconds_until(9, 0, min_days=1)),
            (_("Next week"), self._seconds_until(9, 0, min_days=7)),
        ):
            btn = QPushButton(label)
            btn.setObjectName("schedule_preset")
            btn.setCheckable(False)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _checked=False, secs=seconds: self.set_relative_seconds(secs))
            self.preset_buttons.append(btn)
            presets.addWidget(btn)
        details_layout.addLayout(presets)
        root.addWidget(self.details_widget)

        try:
            self.enabled_checkbox.setIcon(qta.icon("fa5s.clock", color="#22D3EE"))
        except Exception:
            pass

    def _seconds_until(self, hour: int, minute: int, *, min_days: int = 0) -> int:
        now = QDateTime.currentDateTime()
        target = QDateTime(now.date(), now.time())
        target = target.addSecs(-target.time().hour() * 3600 - target.time().minute() * 60 - target.time().second())
        target = target.addSecs(int(hour) * 3600 + int(minute) * 60)
        if min_days:
            target = target.addDays(int(min_days))
        elif target <= now:
            target = target.addDays(1)
        return max(60, now.secsTo(target))

    def set_relative_seconds(self, seconds: int):
        self.enabled_checkbox.setChecked(True)
        self.set_expanded(True)
        self.date_time_edit.setDateTime(QDateTime.currentDateTime().addSecs(max(60, int(seconds or 60))))
        self._refresh_summary()

    def _apply_state(self):
        enabled = self.enabled_checkbox.isChecked()
        for widget in (self.date_time_edit, self.repeat_combo, *self.preset_buttons):
            widget.setEnabled(enabled)
        if enabled:
            self.set_expanded(True)
        else:
            self.set_expanded(False)
        self._refresh_summary()

    def toggle_expanded(self):
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool):
        self._expanded = bool(expanded)
        self.details_widget.setVisible(self._expanded)
        try:
            icon_name = "fa5s.chevron-up" if self._expanded else "fa5s.chevron-down"
            self.title_label.setIcon(qta.icon(icon_name, color="#94A3B8"))
        except Exception:
            pass

    def _refresh_summary(self):
        if not self.enabled_checkbox.isChecked():
            self.summary_label.setText(_("Start immediately"))
            return
        dt = self.date_time_edit.dateTime()
        when = dt.toString("yyyy-MM-dd HH:mm")
        repeat = str(self.repeat_combo.currentData() or "none")
        if repeat == "daily":
            self.summary_label.setText(_("Scheduled daily: {when}").format(when=when))
        elif repeat == "weekly":
            self.summary_label.setText(_("Scheduled weekly: {when}").format(when=when))
        else:
            self.summary_label.setText(_("Scheduled: {when}").format(when=when))

    def get_schedule_settings(self) -> dict:
        if not self.enabled_checkbox.isChecked():
            return {"scheduled_at": 0.0, "schedule_repeat": "none", "schedule_enabled": False}
        dt = self.date_time_edit.dateTime().toPython()
        timestamp = float(dt.timestamp())
        if timestamp <= time.time():
            timestamp = 0.0
        return {
            "scheduled_at": timestamp,
            "schedule_repeat": str(self.repeat_combo.currentData() or "none"),
            "schedule_enabled": timestamp > 0,
        }

    def set_schedule_enabled(self, enabled: bool):
        self.enabled_checkbox.setChecked(bool(enabled))
