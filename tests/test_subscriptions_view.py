try:
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtWidgets import QApplication, QListView, QStyleOptionViewItem
except ImportError:
    from PyQt6.QtCore import QRect, Qt
    from PyQt6.QtWidgets import QApplication, QListView, QStyleOptionViewItem

import ui.subscriptions_view as subscriptions_view
from core.channel_subscriptions import ChannelSubscription


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _DummyManager:
    def __init__(self, subscriptions=None):
        self._subscriptions = list(subscriptions or [])
        self.callback = None
        self.started = False
        self.checked = []
        self.removed = []

    def set_callback(self, callback):
        self.callback = callback

    def start(self):
        self.started = True

    def get_all(self):
        return list(self._subscriptions)

    def add(self, **kwargs):
        sub = ChannelSubscription(kwargs)
        self._subscriptions.append(sub)
        return sub

    def remove(self, url):
        self.removed.append(url)
        self._subscriptions = [sub for sub in self._subscriptions if sub.url != url]

    def check_now(self, sub):
        self.checked.append(sub)


class _FakeMouseEvent:
    class _PointWrapper:
        def __init__(self, point):
            self._point = point

        def toPoint(self):
            return self._point

    def __init__(self, event_type, point):
        self._event_type = event_type
        self._point = point

    def type(self):
        return self._event_type

    def position(self):
        return self._PointWrapper(self._point)

    def pos(self):
        return self._point


class _FakeKeyEvent:
    def __init__(self, key):
        self._key = key
        self.accepted = False

    def key(self):
        return self._key

    def accept(self):
        self.accepted = True


def test_subscriptions_view_uses_list_view_model(monkeypatch):
    _ensure_qt_app()
    dummy_manager = _DummyManager(
        [
            ChannelSubscription(
                {
                    "url": "https://example.com/channel/a",
                    "name": "Alpha",
                    "format": "mp4",
                    "quality": "1080p",
                    "check_interval_h": 24,
                    "known_ids": ["1", "2"],
                }
            )
        ]
    )
    monkeypatch.setattr(subscriptions_view, "subscription_manager", dummy_manager)

    view = subscriptions_view.SubscriptionsView()

    assert dummy_manager.started is True
    assert isinstance(view.subs_list, QListView)
    assert view.subs_model.rowCount() == 1
    assert view.subs_model.data(view.subs_model.index(0, 0), subscriptions_view.SUBSCRIPTION_ROLE).name == "Alpha"
    view.update_theme("Midnight Neon")
    assert view.subs_delegate._theme_name == "Midnight Neon"


def test_subscription_delegate_emits_check_and_remove_actions():
    _ensure_qt_app()
    sub = ChannelSubscription(
        {
            "url": "https://example.com/channel/remove-me",
            "name": "Delegate Test",
            "format": "mkv",
            "quality": "720p",
            "check_interval_h": 12,
            "known_ids": ["1"],
        }
    )
    model = subscriptions_view.SubscriptionListModel()
    model.set_subscriptions([sub])
    delegate = subscriptions_view.SubscriptionItemDelegate()
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 520, subscriptions_view.SubscriptionListModel.ITEM_ROW_HEIGHT)
    index = model.index(0, 0)

    check_hits = []
    remove_hits = []
    delegate.checkRequested.connect(lambda item: check_hits.append(item))
    delegate.removeRequested.connect(lambda url: remove_hits.append(url))

    _, _, check_rect, remove_rect = delegate._layout_parts(option.rect)
    delegate.editorEvent(
        _FakeMouseEvent(subscriptions_view.QEvent.Type.MouseButtonRelease, check_rect.center()),
        model,
        option,
        index,
    )
    delegate.editorEvent(
        _FakeMouseEvent(subscriptions_view.QEvent.Type.MouseButtonRelease, remove_rect.center()),
        model,
        option,
        index,
    )

    assert check_hits == [sub]
    assert remove_hits == [sub.url]


def test_subscription_delegate_button_at_and_hover_state():
    _ensure_qt_app()
    delegate = subscriptions_view.SubscriptionItemDelegate()
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 520, subscriptions_view.SubscriptionListModel.ITEM_ROW_HEIGHT)

    _, _, check_rect, remove_rect = delegate._layout_parts(option.rect)

    assert delegate.button_at(option.rect, check_rect.center()) == "check"
    assert delegate.button_at(option.rect, remove_rect.center()) == "remove"

    delegate.set_hover_state(3, "remove")
    assert delegate._hovered_row == 3
    assert delegate._hovered_button == "remove"
    assert delegate.tooltip_for_button("check")
    assert delegate.tooltip_for_button("remove")


def test_subscriptions_view_enter_on_empty_state_focuses_add_form(monkeypatch):
    _ensure_qt_app()
    dummy_manager = _DummyManager([])
    monkeypatch.setattr(subscriptions_view, "subscription_manager", dummy_manager)
    view = subscriptions_view.SubscriptionsView()

    hits = []
    view.focus_add_form = lambda: hits.append("focus")
    event = _FakeKeyEvent(Qt.Key.Key_Return)

    view.subs_list.keyPressEvent(event)

    assert hits == ["focus"]
    assert event.accepted is True


def test_subscriptions_view_keyboard_shortcuts_check_and_remove(monkeypatch):
    _ensure_qt_app()
    sub = ChannelSubscription(
        {
            "url": "https://example.com/channel/kbd",
            "name": "Keyboard Test",
            "format": "mp4",
            "quality": "1080p",
            "check_interval_h": 24,
        }
    )
    dummy_manager = _DummyManager([sub])
    monkeypatch.setattr(subscriptions_view, "subscription_manager", dummy_manager)
    monkeypatch.setattr(subscriptions_view.ErrorHandler, "confirm", staticmethod(lambda *args, **kwargs: True))
    view = subscriptions_view.SubscriptionsView()
    view.subs_list.setCurrentIndex(view.subs_model.index(0, 0))

    enter_event = _FakeKeyEvent(Qt.Key.Key_Return)
    delete_event = _FakeKeyEvent(Qt.Key.Key_Delete)
    view.subs_list.keyPressEvent(enter_event)
    view.subs_list.keyPressEvent(delete_event)

    assert dummy_manager.checked == [sub]
    assert dummy_manager.removed == [sub.url]
    assert enter_event.accepted is True
    assert delete_event.accepted is True
