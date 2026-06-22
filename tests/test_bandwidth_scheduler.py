from datetime import time

from core.bandwidth_scheduler import _time_in_range


def test_time_in_range_uses_end_exclusive_boundary_for_adjacent_rules():
    assert _time_in_range(time(0, 0), time(8, 0), time(8, 0)) is False
    assert _time_in_range(time(8, 0), time(12, 0), time(8, 0)) is True


def test_time_in_range_keeps_midnight_wrap_intervals_end_exclusive():
    assert _time_in_range(time(22, 0), time(6, 0), time(23, 30)) is True
    assert _time_in_range(time(22, 0), time(6, 0), time(5, 59)) is True
    assert _time_in_range(time(22, 0), time(6, 0), time(6, 0)) is False
