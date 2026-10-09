import pytest
from pydantic import ValidationError
from textual import events

from agent_client.presentation.windows_input import WindowsInputParser


def record(vk: int, character: int, state: int = 1, controls: int = 0, repeat: int = 1):
    return f"\x1b[{vk};28;{character};{state};{controls};{repeat}_"


def keys(data: str):
    return [event.key for event in WindowsInputParser().feed(data) if isinstance(event, events.Key)]


@pytest.mark.parametrize(
    ("controls", "character", "expected"),
    [
        (0, 13, "enter"),
        (16, 13, "shift+enter"),
        (8, 10, "ctrl+enter"),
        (24, 10, "ctrl+shift+enter"),
        (128 | 32, 13, "enter"),
        (2, 13, "alt+enter"),
    ],
)
def test_enter_modifiers(controls, character, expected):
    assert keys(record(13, character, controls=controls)) == [expected]


def test_ctrl_j_is_not_ctrl_enter():
    assert keys(record(74, 10, controls=8) + record(13, 10, controls=8)) == ["ctrl+j", "ctrl+enter"]


def test_key_up_and_modifier_records_are_not_dispatched():
    assert keys(record(16, 0, controls=16) + record(13, 13, state=0)) == []


def test_repeat_count_and_non_ascii_input():
    parsed = list(WindowsInputParser().feed(record(0, ord("\u4f60"), repeat=3)))
    assert [event.character for event in parsed] == ["\u4f60"] * 3


def test_surrogate_pair_across_chunks_with_intervening_keyup():
    parser = WindowsInputParser()
    assert list(parser.feed(record(0, 0xD83D))) == []
    assert list(parser.feed(record(0, 0xD83D, state=0))) == []
    assert [event.character for event in parser.feed(record(0, 0xDE00))] == ["\U0001f600"]


def test_altgr_and_shifted_printable_characters():
    parsed = list(
        WindowsInputParser().feed(record(69, 8364, controls=9) + record(65, 65, controls=16))
    )
    assert [event.character for event in parsed] == ["\u20ac", "A"]


@pytest.mark.parametrize(
    ("vk", "character", "expected"),
    [
        (38, 0, "up"),
        (9, 9, "tab"),
        (27, 27, "escape"),
        (8, 8, "backspace"),
        (112, 0, "f1"),
        (123, 0, "f12"),
        (124, 0, "f13"),
        (135, 0, "f24"),
    ],
)
def test_functional_keys(vk, character, expected):
    assert keys(record(vk, character)) == [expected]


def test_split_sequence_and_omitted_defaults():
    parser = WindowsInputParser()
    assert list(parser.feed("\x1b[13;28;13;1;")) == []
    assert [event.key for event in parser.feed("16_")] == ["shift+enter"]


def test_vt_mouse_focus_and_paste_remain_intact():
    parser = WindowsInputParser()
    text = "line one\r\nline two"
    parsed = list(parser.feed("\x1b[<0;3;4M\x1b[I\x1b[200~" + text + "\x1b[201~"))
    assert isinstance(parsed[0], events.MouseDown)
    assert isinstance(parsed[1], events.AppFocus)
    assert isinstance(parsed[2], events.Paste)
    assert parsed[2].text == text


def test_ctrl_space_and_alt_letter():
    assert keys(record(32, 0, controls=8) + record(65, 97, controls=2)) == ["ctrl+space", "alt+a"]


@pytest.mark.parametrize(
    "data", [record(13, 13, state=2), record(13, 13, repeat=0), record(13, 65536)]
)
def test_invalid_explicit_records_fail(data):
    with pytest.raises(ValidationError):
        list(WindowsInputParser().feed(data))


@pytest.mark.parametrize("data", [record(0, 0xDC00), record(0, 0xD800) + record(65, 65)])
def test_unpaired_surrogates_fail(data):
    with pytest.raises(ValueError):
        list(WindowsInputParser().feed(data))


def test_every_character_can_arrive_in_a_separate_chunk():
    parser = WindowsInputParser()
    parsed = []
    for character in record(13, 13, controls=16) + record(65, 97) + record(13, 13, state=0):
        parsed.extend(parser.feed(character))
    assert [event.key for event in parsed] == ["shift+enter", "a"]


def test_mismatched_surrogate_repeats_fail():
    parser = WindowsInputParser()
    with pytest.raises(ValueError):
        list(parser.feed(record(0, 0xD83D, repeat=2) + record(0, 0xDE00)))


def test_excess_win32_fields_fail():
    with pytest.raises(ValueError):
        list(WindowsInputParser().feed("\x1b[13;28;13;1;0;1;2_"))


def test_bracketed_paste_encoded_as_win32_character_records():
    parser = WindowsInputParser()
    start = "".join(record(0, ord(character)) for character in "\x1b[200~")
    end = "".join(record(0, ord(character)) for character in "\x1b[201~")
    body = record(65, 97) + record(13, 13) + record(66, 98)
    parsed = list(parser.feed(start + body + end + record(13, 13)))
    assert isinstance(parsed[0], events.Paste)
    assert parsed[0].text == "a\rb"
    assert isinstance(parsed[1], events.Key)
    assert parsed[1].key == "enter"


def test_monitor_input_error_exits_with_visible_message(monkeypatch):
    import sys
    from types import SimpleNamespace

    if sys.platform != "win32":
        pytest.skip("Windows console driver")
    from agent_client.presentation.windows_input import WindowsInputMonitor

    failures = []
    app = SimpleNamespace(exit=lambda **kwargs: failures.append(kwargs))
    loop = SimpleNamespace(call_soon_threadsafe=lambda callback: callback())
    monitor = WindowsInputMonitor(loop, app, object(), lambda _: None)

    def fail():
        raise ValueError("Invalid keyboard record")

    monkeypatch.setattr(monitor, "_read_input", fail)
    monitor.run()
    assert failures == [
        {"return_code": 1, "message": "Windows terminal input failed: Invalid keyboard record"}
    ]


def test_monitor_decodes_utf16_split_between_console_batches(monkeypatch):
    import sys
    from ctypes import POINTER, cast
    from types import SimpleNamespace

    if sys.platform != "win32":
        pytest.skip("Windows console driver")
    from agent_client.presentation import windows_input

    batches = [0xD83D, 0xDE00]
    parsed = []
    exit_event = SimpleNamespace(is_set=lambda: not batches)
    monitor = windows_input.WindowsInputMonitor(object(), object(), exit_event, parsed.append)
    monkeypatch.setattr(windows_input.win32, "GetStdHandle", lambda _: 1)
    monkeypatch.setattr(windows_input.win32, "wait_for_handles", lambda *args: 1)

    def read_input(handle, records, capacity, count):
        target = cast(records, POINTER(windows_input.win32.INPUT_RECORD))
        target[0].EventType = 1
        target[0].Event.KeyEvent.bKeyDown = 1
        target[0].Event.KeyEvent.uChar.UnicodeChar = chr(batches.pop(0))
        count._obj.value = 1
        return 1

    monkeypatch.setattr(windows_input.win32.KERNEL32, "ReadConsoleInputW", read_input)
    monitor._read_input()
    assert [event.character for event in parsed] == ["\U0001f600"]


def test_driver_scopes_input_mode_to_application(monkeypatch):
    import sys
    from types import SimpleNamespace

    if sys.platform != "win32":
        pytest.skip("Windows console driver")
    from agent_client.presentation import windows_input

    writes = []
    lifecycle = []
    monitor = SimpleNamespace(start=lambda: lifecycle.append("monitor"))
    writer = SimpleNamespace(start=lambda: lifecycle.append("writer"))
    monkeypatch.setattr(windows_input.win32, "enable_application_mode", lambda: lambda: None)
    monkeypatch.setattr(windows_input, "WriterThread", lambda _: writer)
    monkeypatch.setattr(windows_input, "WindowsInputMonitor", lambda *args: monitor)
    monkeypatch.setattr(windows_input.asyncio, "get_running_loop", lambda: object())
    monkeypatch.setattr(
        windows_input.WindowsDriver, "stop_application_mode", lambda _: writes.append("stop")
    )
    driver = windows_input.WindowsInputDriver.__new__(windows_input.WindowsInputDriver)
    driver._file = object()
    driver._app = object()
    driver.exit_event = object()
    driver.write = writes.append
    driver.flush = lambda: None
    driver._enable_mouse_support = lambda: None
    driver._enable_bracketed_paste = lambda: None
    driver.start_application_mode()
    assert "\x1b[?9001h" in "".join(writes)
    assert lifecycle == ["writer", "monitor"]
    writes.clear()
    driver.stop_application_mode()
    assert writes == ["\x1b[?9001l", "stop"]
