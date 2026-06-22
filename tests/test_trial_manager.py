from datetime import datetime

from core.trial_manager import TrialManager


class _ClockedTrialManager(TrialManager):
    def __init__(self, *args, now: datetime, **kwargs):
        self._current_now = now
        super().__init__(*args, **kwargs)

    def set_now(self, now: datetime):
        self._current_now = now

    def _now(self) -> datetime:
        return self._current_now


def _patch_storage(monkeypatch, tmp_path):
    monkeypatch.setattr("core.trial_manager.get_app_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("core.trial_manager.protect_text", lambda value: str(value or ""))
    monkeypatch.setattr("core.trial_manager.unprotect_text", lambda value: str(value or ""))


def test_trial_manager_migrates_legacy_state_and_persists_shadow(monkeypatch, tmp_path):
    _patch_storage(monkeypatch, tmp_path)
    manager = _ClockedTrialManager(
        enabled=True,
        total_days=14,
        load_settings=lambda: {"trial_started_at": "2026-05-01T08:00:00"},
        now=datetime(2026, 5, 3, 10, 0, 0),
    )

    state = manager.load_state()

    assert state.started_at == "2026-05-01T08:00:00"
    assert state.total_days == 14
    assert state.days_remaining == 12
    assert tmp_path.joinpath("trial_state.json").is_file()


def test_trial_manager_clock_rollback_does_not_extend_trial(monkeypatch, tmp_path):
    _patch_storage(monkeypatch, tmp_path)
    manager = _ClockedTrialManager(
        enabled=True,
        total_days=14,
        load_settings=lambda: {},
        now=datetime(2026, 5, 10, 9, 0, 0),
    )

    initial = manager.load_state()
    manager.set_now(datetime(2026, 5, 12, 9, 0, 0))
    progressed = manager.recompute(initial.started_at, 14)
    manager.set_now(datetime(2026, 5, 9, 9, 0, 0))
    rolled_back = manager.recompute(progressed.started_at, 14)

    assert progressed.days_remaining == 12
    assert rolled_back.days_remaining == 12
    assert rolled_back.started_at == initial.started_at


def test_trial_manager_ignores_tampered_later_start_when_shadow_exists(monkeypatch, tmp_path):
    _patch_storage(monkeypatch, tmp_path)
    initial_manager = _ClockedTrialManager(
        enabled=True,
        total_days=14,
        load_settings=lambda: {"trial_started_at": "2026-05-01T08:00:00"},
        now=datetime(2026, 5, 5, 9, 0, 0),
    )
    initial_state = initial_manager.load_state()
    assert initial_state.days_remaining == 10

    tampered_manager = _ClockedTrialManager(
        enabled=True,
        total_days=14,
        load_settings=lambda: {
            "trial_started_at": "2026-05-05T08:00:00",
            "trial_total_days": 999,
        },
        now=datetime(2026, 5, 6, 9, 0, 0),
    )

    state = tampered_manager.load_state()

    assert state.started_at == "2026-05-01T08:00:00"
    assert state.total_days == 14
    assert state.days_remaining == 9


def test_trial_manager_caps_total_days_to_configured_value(monkeypatch, tmp_path):
    _patch_storage(monkeypatch, tmp_path)
    manager = _ClockedTrialManager(
        enabled=True,
        total_days=14,
        load_settings=lambda: {},
        now=datetime(2026, 5, 1, 8, 0, 0),
    )

    state = manager.recompute(started_at="2026-05-01T08:00:00", total_days=500)

    assert state.total_days == 14
    assert state.days_remaining == 14
