
"""
ui/models.py — Custom Qt Models for SnapDownloader
Implements efficient data handling for large lists (playlist, downloads).
"""
try:
    from PySide6.QtCore import QAbstractListModel, Qt, QModelIndex, QSize
except ImportError:
    from PyQt6.QtCore import QAbstractListModel, Qt, QModelIndex, QSize

from core.utils import redact_url

EMPTY_STATE_SENTINEL = {"_empty": True, "_type": "empty_state"}
EMPTY_ROW_HEIGHT = 340
NORMAL_ROW_HEIGHT = 112

class DownloadListModel(QAbstractListModel):
    def __init__(self, items=None):
        super().__init__()
        self._items = items or []

    def rowCount(self, parent=QModelIndex()):
        return len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return str(item.get("title", "") or redact_url(item.get("url", "")) or "")
        if role == Qt.ItemDataRole.SizeHintRole:
            if item.get("_empty"):
                return QSize(0, EMPTY_ROW_HEIGHT)
            return QSize(0, NORMAL_ROW_HEIGHT)
        if role == Qt.ItemDataRole.UserRole:
            return item
        return None

    def update_items(self, new_items, preserve_rows: bool = False):
        next_items = list(new_items or [])
        if preserve_rows and len(next_items) == len(self._items):
            changed_rows = [
                row for row, (old_item, new_item) in enumerate(zip(self._items, next_items))
                if old_item != new_item
            ]
            self._items = next_items
            if changed_rows:
                first = min(changed_rows)
                last = max(changed_rows)
                top_left = self.index(first, 0)
                bottom_right = self.index(last, 0)
                self.dataChanged.emit(
                    top_left,
                    bottom_right,
                    [
                        Qt.ItemDataRole.DisplayRole,
                        Qt.ItemDataRole.SizeHintRole,
                        Qt.ItemDataRole.UserRole,
                    ],
                )
            return
        self.beginResetModel()
        self._items = next_items
        self.endResetModel()

    def get_item(self, row):
        if 0 <= row < len(self._items):
            return self._items[row]
        return None


class PlaylistListModel(QAbstractListModel):
    def __init__(self, items=None):
        super().__init__()
        self._items = items if isinstance(items, list) else list(items or [])

    def rowCount(self, parent=QModelIndex()):
        return len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return str(item.get("title", "") or redact_url(item.get("url", "")) or "")
        if role == Qt.ItemDataRole.SizeHintRole:
            return QSize(0, 104)
        if role == Qt.ItemDataRole.UserRole:
            return item
        return None

    def update_items(self, new_items):
        self.beginResetModel()
        self._items = new_items if isinstance(new_items, list) else list(new_items or [])
        self.endResetModel()

    def append_items(self, new_items):
        next_items = list(new_items or [])
        if not next_items:
            return
        start = len(self._items)
        end = start + len(next_items) - 1
        self.beginInsertRows(QModelIndex(), start, end)
        self._items.extend(next_items)
        self.endInsertRows()

    def remove_rows(self, row_indexes):
        indexes = sorted({int(idx) for idx in (row_indexes or []) if int(idx) >= 0})
        if not indexes:
            return 0
        max_index = len(self._items) - 1
        valid = [idx for idx in indexes if idx <= max_index]
        if not valid:
            return 0

        # Remove contiguous ranges from tail to head to keep indices stable.
        ranges = []
        start = prev = valid[0]
        for idx in valid[1:]:
            if idx == prev + 1:
                prev = idx
                continue
            ranges.append((start, prev))
            start = prev = idx
        ranges.append((start, prev))

        removed = 0
        for first, last in reversed(ranges):
            self.beginRemoveRows(QModelIndex(), first, last)
            del self._items[first:last + 1]
            self.endRemoveRows()
            removed += (last - first + 1)
        return removed



