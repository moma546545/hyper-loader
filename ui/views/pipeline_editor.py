try:
    from PySide6.QtCore import Qt, Signal
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QComboBox,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    from PyQt6.QtCore import Qt, pyqtSignal as Signal
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QComboBox,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

from core.i18n import _


PIPELINE_ACTIONS = [
    ("convert_mp3", _("Convert to MP3")),
    ("transcribe", _("Whisper Auto-Transcription")),
    ("summarize", _("Summarize Transcript")),
    ("run_python", _("Run Python Script")),
    ("run_powershell", _("Run PowerShell Script")),
    ("open_folder", _("Open Download Folder")),
    ("play_sound", _("Play Notification Sound")),
]


class PipelineEditor(QWidget):
    pipelineChanged = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        hint = QLabel(_("Arrange post-download actions in execution order. Drag to reorder."))
        hint.setObjectName("single_sub")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.steps_list = QListWidget()
        self.steps_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.steps_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.steps_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.steps_list.model().rowsMoved.connect(lambda *_args: self._emit_change())
        self.steps_list.currentRowChanged.connect(self._load_selected_step)
        self.steps_list.setMinimumHeight(160)
        root.addWidget(self.steps_list)

        controls = QHBoxLayout()
        self.add_step_btn = QPushButton(_("Add Step"))
        self.add_step_btn.setObjectName("action_schedule")
        self.add_step_btn.clicked.connect(self._add_step)
        self.remove_step_btn = QPushButton(_("Remove Step"))
        self.remove_step_btn.setObjectName("action_trim")
        self.remove_step_btn.clicked.connect(self._remove_selected_step)
        controls.addWidget(self.add_step_btn)
        controls.addWidget(self.remove_step_btn)
        controls.addStretch(1)
        root.addLayout(controls)

        self.form_card = QFrame()
        self.form_card.setObjectName("playlist_header")
        form_layout = QGridLayout(self.form_card)
        form_layout.setContentsMargins(12, 12, 12, 12)
        form_layout.setHorizontalSpacing(10)
        form_layout.setVerticalSpacing(10)

        self.action_combo = QComboBox()
        for value, label in PIPELINE_ACTIONS:
            self.action_combo.addItem(label, value)
        self.action_combo.currentIndexChanged.connect(self._update_selected_step)

        self.label_input = QLineEdit()
        self.label_input.setPlaceholderText(_("Optional label"))
        self.label_input.textChanged.connect(self._update_selected_step)

        self.script_input = QLineEdit()
        self.script_input.setPlaceholderText(_("Trusted script path inside app scripts directory"))
        self.script_input.textChanged.connect(self._update_selected_step)

        self.args_input = QLineEdit()
        self.args_input.setPlaceholderText(_("Optional arguments or ffmpeg profile"))
        self.args_input.textChanged.connect(self._update_selected_step)

        form_layout.addWidget(QLabel(_("Action")), 0, 0)
        form_layout.addWidget(self.action_combo, 0, 1)
        form_layout.addWidget(QLabel(_("Label")), 1, 0)
        form_layout.addWidget(self.label_input, 1, 1)
        form_layout.addWidget(QLabel(_("Script")), 2, 0)
        form_layout.addWidget(self.script_input, 2, 1)
        form_layout.addWidget(QLabel(_("Args")), 3, 0)
        form_layout.addWidget(self.args_input, 3, 1)
        root.addWidget(self.form_card)

        self._set_form_enabled(False)

    def _set_form_enabled(self, enabled: bool):
        for widget in (self.action_combo, self.label_input, self.script_input, self.args_input, self.remove_step_btn):
            widget.setEnabled(bool(enabled))

    def _default_step(self) -> dict:
        return {
            "action": "convert_mp3",
            "label": "",
            "script_path": "",
            "args": "",
        }

    def pipeline(self) -> list[dict]:
        steps = []
        for row in range(self.steps_list.count()):
            item = self.steps_list.item(row)
            steps.append(dict(item.data(Qt.ItemDataRole.UserRole) or {}))
        return steps

    def set_pipeline(self, pipeline: list[dict], *, emit_signal: bool = False):
        self.steps_list.blockSignals(True)
        self.steps_list.clear()
        for raw_step in pipeline or []:
            step = self._normalize_step(raw_step)
            item = QListWidgetItem(self._step_text(step))
            item.setData(Qt.ItemDataRole.UserRole, step)
            self.steps_list.addItem(item)
        self.steps_list.blockSignals(False)
        if self.steps_list.count():
            self.steps_list.setCurrentRow(0)
        else:
            self._set_form_enabled(False)
            self._load_step_to_form(None)
        if emit_signal:
            self._emit_change()

    def _normalize_step(self, step: dict | None) -> dict:
        raw = dict(step or {})
        action = str(raw.get("action", "convert_mp3") or "convert_mp3").strip()
        allowed_values = {value for value, _label in PIPELINE_ACTIONS}
        if action not in allowed_values:
            action = "convert_mp3"
        return {
            "action": action,
            "label": str(raw.get("label", "") or "").strip(),
            "script_path": str(raw.get("script_path", "") or "").strip(),
            "args": str(raw.get("args", "") or "").strip(),
        }

    def _step_text(self, step: dict) -> str:
        action = str(step.get("action", "") or "").strip()
        label = str(step.get("label", "") or "").strip()
        action_label = next((text for value, text in PIPELINE_ACTIONS if value == action), action)
        return f"{action_label} - {label}" if label else action_label

    def _current_item(self):
        row = self.steps_list.currentRow()
        if row < 0:
            return None
        return self.steps_list.item(row)

    def _add_step(self):
        step = self._default_step()
        item = QListWidgetItem(self._step_text(step))
        item.setData(Qt.ItemDataRole.UserRole, step)
        self.steps_list.addItem(item)
        self.steps_list.setCurrentItem(item)
        self._emit_change()

    def _remove_selected_step(self):
        row = self.steps_list.currentRow()
        if row < 0:
            return
        self.steps_list.takeItem(row)
        if self.steps_list.count():
            self.steps_list.setCurrentRow(min(row, self.steps_list.count() - 1))
        else:
            self._set_form_enabled(False)
            self._load_step_to_form(None)
        self._emit_change()

    def _load_selected_step(self, row: int):
        item = self.steps_list.item(row) if row >= 0 else None
        step = dict(item.data(Qt.ItemDataRole.UserRole) or {}) if item is not None else None
        self._load_step_to_form(step)
        self._set_form_enabled(item is not None)

    def _load_step_to_form(self, step: dict | None):
        self.action_combo.blockSignals(True)
        self.label_input.blockSignals(True)
        self.script_input.blockSignals(True)
        self.args_input.blockSignals(True)
        action = str((step or {}).get("action", "convert_mp3") or "convert_mp3")
        index = self.action_combo.findData(action)
        self.action_combo.setCurrentIndex(max(0, index))
        self.label_input.setText(str((step or {}).get("label", "") or ""))
        self.script_input.setText(str((step or {}).get("script_path", "") or ""))
        self.args_input.setText(str((step or {}).get("args", "") or ""))
        self.action_combo.blockSignals(False)
        self.label_input.blockSignals(False)
        self.script_input.blockSignals(False)
        self.args_input.blockSignals(False)

    def _update_selected_step(self):
        item = self._current_item()
        if item is None:
            return
        step = {
            "action": str(self.action_combo.currentData() or "convert_mp3"),
            "label": str(self.label_input.text() or "").strip(),
            "script_path": str(self.script_input.text() or "").strip(),
            "args": str(self.args_input.text() or "").strip(),
        }
        item.setData(Qt.ItemDataRole.UserRole, step)
        item.setText(self._step_text(step))
        self._emit_change()

    def _emit_change(self):
        self.pipelineChanged.emit(self.pipeline())
