from types import SimpleNamespace

from core.window_controllers.media_tools_controller import MediaToolsController


class _DummyText:
    def __init__(self, value: str = ""):
        self._value = value

    def text(self) -> str:
        return self._value

    def setText(self, value: str):
        self._value = str(value)


def test_media_tools_fetch_channel_switches_view_and_starts_analyze():
    switch_calls = []
    analyze_calls = []
    url_input = _DummyText("")
    window = SimpleNamespace(
        tools_view=SimpleNamespace(chan_url=_DummyText("https://example.com/channel")),
        search_view=SimpleNamespace(url_input=url_input),
        _switch_view=lambda key: switch_calls.append(key),
        _start_analyze=lambda: analyze_calls.append(True),
    )
    controller = MediaToolsController(window)

    controller.fetch_channel()

    assert url_input.text() == "https://example.com/channel"
    assert switch_calls == ["search"]
    assert analyze_calls == [True]


def test_media_tools_normalize_downloads_folder_rejects_parallel_runs():
    warns = []
    window = SimpleNamespace(
        _normalize_folder_running=True,
        _warn=lambda msg: warns.append(str(msg)),
    )
    controller = MediaToolsController(window)

    controller.normalize_downloads_folder()

    assert warns
