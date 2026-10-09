import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass

from textual import constants
from textual._xterm_parser import XTermParser
from textual.message import Message

from agent_client.domain.windows_input import (
    ConsoleEventType,
    ControlKeyState,
    KeyState,
    VirtualKey,
    WindowsKeyRecord,
)


@dataclass(frozen=True)
class KeyboardEncoding:
    maximum_sequence: int = 128
    high_surrogate_first: int = 0xD800
    high_surrogate_last: int = 0xDBFF
    low_surrogate_first: int = 0xDC00
    low_surrogate_last: int = 0xDFFF
    supplementary_start: int = 0x10000
    surrogate_shift: int = 10
    alternate_modifier: int = 2
    control_modifier: int = 4
    printable_start: int = 32
    capital_first: int = 65
    capital_last: int = 90
    lowercase_offset: int = 32
    function_extension: int = 57376
    function_count: int = 12
    function_codes: tuple[int, ...] = (11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 23, 24)
    console_records: int = 1024
    console_poll_milliseconds: int = 100


DEFAULT_KEYBOARD_ENCODING = KeyboardEncoding()


class WindowsInputParser:
    _record_pattern = re.compile(r"\x1b\[((?:\d*;){0,5}\d*)_")
    _modifier_keys = frozenset(
        (
            VirtualKey.SHIFT,
            VirtualKey.CONTROL,
            VirtualKey.ALT,
            VirtualKey.LEFT_WINDOWS,
            VirtualKey.RIGHT_WINDOWS,
            VirtualKey.LEFT_SHIFT,
            VirtualKey.RIGHT_SHIFT,
            VirtualKey.LEFT_CONTROL,
            VirtualKey.RIGHT_CONTROL,
            VirtualKey.LEFT_ALT,
            VirtualKey.RIGHT_ALT,
        )
    )

    def __init__(self, debug: bool = False):
        self._parser = XTermParser(debug=debug)
        self._sequence = ""
        self._started = 0.0
        self._paste = False
        self._encoded_paste = False
        self._converted_tail = ""
        self._surrogate: WindowsKeyRecord | None = None

    def feed(self, data: str) -> Iterable[Message]:
        output: list[str] = []
        for character in data:
            if not self._sequence:
                if character == "\x1b":
                    self._sequence = character
                    self._started = time.monotonic()
                else:
                    output.append(character)
                continue
            self._sequence += character
            if self._sequence == "\x1b[":
                continue
            if len(self._sequence) == 2 or "@" <= character <= "~":
                sequence = self._sequence
                self._sequence = ""
                match sequence:
                    case "\x1b[200~":
                        self._paste = True
                    case "\x1b[201~":
                        self._paste = False
                if not self._paste and (match := self._record_pattern.fullmatch(sequence)):
                    values = match[1].split(";")
                    defaults = (0, 0, 0, 0, 0, 1)
                    numbers = [
                        int(value) if value else defaults[index]
                        for index, value in enumerate(values)
                    ]
                    numbers.extend(defaults[len(numbers) :])
                    record = WindowsKeyRecord(
                        virtual_key=numbers[0],
                        scan_code=numbers[1],
                        character=numbers[2],
                        state=numbers[3],
                        control_state=numbers[4],
                        repeat=numbers[5],
                    )
                    converted = self._convert(record)
                    for converted_character in converted:
                        self._converted_tail = (self._converted_tail + converted_character)[-6:]
                        match self._converted_tail:
                            case "\x1b[200~":
                                self._encoded_paste = True
                            case "\x1b[201~":
                                self._encoded_paste = False
                    output.append(converted)
                else:
                    if not self._paste and sequence.startswith("\x1b[") and sequence.endswith("_"):
                        raise ValueError("Invalid Win32 keyboard input sequence")
                    output.append(sequence)
            elif len(self._sequence) > DEFAULT_KEYBOARD_ENCODING.maximum_sequence:
                raise ValueError("Input escape sequence exceeds 128 characters")
        if translated := "".join(output):
            yield from self._parser.feed(translated)

    def tick(self) -> Iterable[Message]:
        if self._sequence and time.monotonic() - self._started >= constants.ESCAPE_DELAY:
            sequence = self._sequence
            self._sequence = ""
            yield from self._parser.feed(sequence)
        yield from self._parser.tick()

    def _convert(self, record: WindowsKeyRecord) -> str:
        if record.state is KeyState.UP or record.virtual_key in self._modifier_keys:
            return ""
        if self._surrogate is not None:
            first = self._surrogate
            self._surrogate = None
            if (
                not DEFAULT_KEYBOARD_ENCODING.low_surrogate_first
                <= record.character
                <= DEFAULT_KEYBOARD_ENCODING.low_surrogate_last
            ):
                raise ValueError("Unpaired UTF-16 high surrogate in keyboard input")
            if first.repeat != record.repeat or first.control_state != record.control_state:
                raise ValueError("Mismatched UTF-16 surrogate keyboard records")
            codepoint = (
                DEFAULT_KEYBOARD_ENCODING.supplementary_start
                + (
                    (first.character - DEFAULT_KEYBOARD_ENCODING.high_surrogate_first)
                    << DEFAULT_KEYBOARD_ENCODING.surrogate_shift
                )
                + record.character
                - DEFAULT_KEYBOARD_ENCODING.low_surrogate_first
            )
            return chr(codepoint) * record.repeat
        if (
            DEFAULT_KEYBOARD_ENCODING.high_surrogate_first
            <= record.character
            <= DEFAULT_KEYBOARD_ENCODING.high_surrogate_last
        ):
            self._surrogate = record
            return ""
        if (
            DEFAULT_KEYBOARD_ENCODING.low_surrogate_first
            <= record.character
            <= DEFAULT_KEYBOARD_ENCODING.low_surrogate_last
        ):
            raise ValueError("Unpaired UTF-16 low surrogate in keyboard input")
        if self._encoded_paste:
            return chr(record.character) * record.repeat if record.character else ""
        state = record.control_state
        shift = bool(state & ControlKeyState.SHIFT)
        ctrl = bool(state & (ControlKeyState.LEFT_CTRL | ControlKeyState.RIGHT_CTRL))
        alt = bool(state & (ControlKeyState.LEFT_ALT | ControlKeyState.RIGHT_ALT))
        modifier = (
            1
            + shift
            + DEFAULT_KEYBOARD_ENCODING.alternate_modifier * alt
            + DEFAULT_KEYBOARD_ENCODING.control_modifier * ctrl
        )
        match record.virtual_key:
            case VirtualKey.BACKSPACE:
                return f"\x1b[127;{modifier}u" * record.repeat
            case VirtualKey.TAB:
                return f"\x1b[9;{modifier}u" * record.repeat
            case VirtualKey.ENTER:
                return f"\x1b[13;{modifier}u" * record.repeat
            case VirtualKey.ESCAPE:
                return f"\x1b[27;{modifier}u" * record.repeat
            case VirtualKey.PAGE_UP:
                return f"\x1b[5;{modifier}~" * record.repeat
            case VirtualKey.PAGE_DOWN:
                return f"\x1b[6;{modifier}~" * record.repeat
            case VirtualKey.END:
                return f"\x1b[1;{modifier}F" * record.repeat
            case VirtualKey.HOME:
                return f"\x1b[1;{modifier}H" * record.repeat
            case VirtualKey.LEFT:
                return f"\x1b[1;{modifier}D" * record.repeat
            case VirtualKey.UP:
                return f"\x1b[1;{modifier}A" * record.repeat
            case VirtualKey.RIGHT:
                return f"\x1b[1;{modifier}C" * record.repeat
            case VirtualKey.DOWN:
                return f"\x1b[1;{modifier}B" * record.repeat
            case VirtualKey.INSERT:
                return f"\x1b[2;{modifier}~" * record.repeat
            case VirtualKey.DELETE:
                return f"\x1b[3;{modifier}~" * record.repeat
        if VirtualKey.F1 <= record.virtual_key <= VirtualKey.F24:
            offset = record.virtual_key - VirtualKey.F1
            codepoint, suffix = (
                (DEFAULT_KEYBOARD_ENCODING.function_codes[offset], "~")
                if offset < DEFAULT_KEYBOARD_ENCODING.function_count
                else (
                    DEFAULT_KEYBOARD_ENCODING.function_extension
                    + offset
                    - DEFAULT_KEYBOARD_ENCODING.function_count,
                    "u",
                )
            )
            return f"\x1b[{codepoint};{modifier}{suffix}" * record.repeat
        if record.character >= DEFAULT_KEYBOARD_ENCODING.printable_start and (not ctrl or alt):
            character = chr(record.character)
            if alt and not ctrl:
                return (
                    f"\x1b[{record.character};{1 + DEFAULT_KEYBOARD_ENCODING.alternate_modifier * alt}u"
                    * record.repeat
                )
            return character * record.repeat
        if (
            ctrl
            and DEFAULT_KEYBOARD_ENCODING.capital_first
            <= record.virtual_key
            <= DEFAULT_KEYBOARD_ENCODING.capital_last
        ):
            return (
                f"\x1b[{record.virtual_key + DEFAULT_KEYBOARD_ENCODING.lowercase_offset};{modifier}u"
                * record.repeat
            )
        if ctrl and record.virtual_key == VirtualKey.SPACE:
            return f"\x1b[32;{modifier}u" * record.repeat
        if record.character:
            return chr(record.character) * record.repeat
        return ""


if sys.platform == "win32":
    import asyncio
    import codecs
    from ctypes import byref, wintypes
    from functools import partial

    from textual._parser import ParseError
    from textual.drivers import win32
    from textual.drivers._writer_thread import WriterThread
    from textual.drivers.windows_driver import WindowsDriver

    class WindowsInputMonitor(win32.EventMonitor):
        def run(self):
            try:
                self._read_input()
            except (OSError, ValueError, ParseError) as error:
                self.loop.call_soon_threadsafe(
                    partial(
                        self.app.exit,
                        return_code=1,
                        message=f"Windows terminal input failed: {error}",
                    )
                )

        def _read_input(self):
            parser = WindowsInputParser(debug=constants.DEBUG)
            decoder = codecs.getincrementaldecoder("utf-16-le")()
            handle = win32.GetStdHandle(win32.STD_INPUT_HANDLE)
            records = (win32.INPUT_RECORD * DEFAULT_KEYBOARD_ENCODING.console_records)()
            count = wintypes.DWORD(0)
            while not self.exit_event.is_set():
                for event in parser.tick():
                    self.process_event(event)
                if (
                    win32.wait_for_handles(
                        [handle], DEFAULT_KEYBOARD_ENCODING.console_poll_milliseconds
                    )
                    is None
                ):
                    continue
                if not win32.KERNEL32.ReadConsoleInputW(
                    handle, byref(records), DEFAULT_KEYBOARD_ENCODING.console_records, byref(count)
                ):
                    raise OSError("ReadConsoleInputW failed")
                characters: list[str] = []
                for record in records[: count.value]:
                    match record.EventType:
                        case ConsoleEventType.KEY:
                            key = record.Event.KeyEvent
                            if key.bKeyDown and not (
                                key.dwControlKeyState and key.wVirtualKeyCode == 0
                            ):
                                characters.append(key.uChar.UnicodeChar)
                        case ConsoleEventType.WINDOW_SIZE:
                            size = record.Event.WindowBufferSizeEvent.dwSize
                            self.on_size_change(size.X, size.Y)
                if characters:
                    data = decoder.decode("".join(characters).encode("utf-16-le", "surrogatepass"))
                    if data:
                        for event in parser.feed(data):
                            self.process_event(event)
            decoder.decode(b"", final=True)

    class WindowsInputDriver(WindowsDriver):
        def start_application_mode(self):
            self._restore_console = win32.enable_application_mode()
            self._writer_thread = WriterThread(self._file)
            self._writer_thread.start()
            self.write("\x1b[?1049h")
            self._enable_mouse_support()
            self.write("\x1b[?25l\x1b[?1004h\x1b[>1u\x1b[?9001h")
            self._enable_bracketed_paste()
            self.flush()
            self._event_thread = WindowsInputMonitor(
                asyncio.get_running_loop(),
                self._app,
                self.exit_event,
                self.process_message,
            )
            self._event_thread.start()

        def stop_application_mode(self):
            self.write("\x1b[?9001l")
            super().stop_application_mode()
