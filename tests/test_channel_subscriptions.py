import core.channel_subscriptions as channel_subscriptions


class _DummyThread:
    def __init__(self, alive=True):
        self._alive = bool(alive)
        self.join_calls = []

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        self._alive = False


def test_collect_new_video_ids_prefers_last_seen_boundary():
    fetched = ["new3", "new2", "new1", "old2", "old1"]
    known = {"old2", "old1"}

    result = channel_subscriptions.SubscriptionManager._collect_new_video_ids(
        fetched,
        known,
        last_seen_video_id="old2",
        max_downloads=10,
    )

    assert result == ["new3", "new2", "new1"]


def test_collect_new_video_ids_falls_back_to_known_ids_when_boundary_missing():
    fetched = ["v5", "v4", "v3", "v2", "v1"]
    known = {"v2", "v1"}

    result = channel_subscriptions.SubscriptionManager._collect_new_video_ids(
        fetched,
        known,
        last_seen_video_id="unknown",
        max_downloads=2,
    )

    assert result == ["v5", "v4"]


def test_subscription_state_roundtrip_uses_sqlite(tmp_path, monkeypatch):
    subs_json = tmp_path / "subscriptions.json"
    subs_db = tmp_path / "subscriptions_state.db"
    monkeypatch.setattr(channel_subscriptions, "SUBS_PATH", str(subs_json))
    monkeypatch.setattr(channel_subscriptions, "SUBS_DB_PATH", str(subs_db))

    manager = channel_subscriptions.SubscriptionManager()
    manager._save_state_to_db(
        "https://example.com/channel/abc",
        last_seen_video_id="vid_500",
        snapshot_hash="hash_500",
        fetched_count=500,
    )

    state = manager._load_state_from_db("https://example.com/channel/abc")

    assert state["last_seen_video_id"] == "vid_500"
    assert state["snapshot_hash"] == "hash_500"
    assert state["fetched_count"] == 500


def test_subscription_manager_stop_joins_background_threads(tmp_path, monkeypatch):
    subs_json = tmp_path / "subscriptions.json"
    subs_db = tmp_path / "subscriptions_state.db"
    monkeypatch.setattr(channel_subscriptions, "SUBS_PATH", str(subs_json))
    monkeypatch.setattr(channel_subscriptions, "SUBS_DB_PATH", str(subs_db))

    manager = channel_subscriptions.SubscriptionManager()
    watcher = _DummyThread(alive=True)
    checker = _DummyThread(alive=True)
    manager._thread = watcher
    manager._check_threads = {checker}
    manager._running = True

    manager.stop(join_timeout=1.5)

    assert manager._running is False
    assert watcher.join_calls == [1.5]
    assert checker.join_calls == [1.0]
