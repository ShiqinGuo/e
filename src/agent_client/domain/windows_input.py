from enum import IntEnum, IntFlag

from pydantic import ConfigDict, Field

from agent_client.domain.base import Contract


class KeyState(IntEnum):
    UP = 0
    DOWN = 1


class ConsoleEventType(IntEnum):
    KEY = 1
    WINDOW_SIZE = 4


class VirtualKey(IntEnum):
    BACKSPACE = 8
    TAB = 9
    ENTER = 13
    SHIFT = 16
    CONTROL = 17
    ALT = 18
    ESCAPE = 27
    SPACE = 32
    PAGE_UP = 33
    PAGE_DOWN = 34
    END = 35
    HOME = 36
    LEFT = 37
    UP = 38
    RIGHT = 39
    DOWN = 40
    INSERT = 45
    DELETE = 46
    LEFT_WINDOWS = 91
    RIGHT_WINDOWS = 92
    F1 = 112
    F24 = 135
    LEFT_SHIFT = 160
    RIGHT_SHIFT = 161
    LEFT_CONTROL = 162
    RIGHT_CONTROL = 163
    LEFT_ALT = 164
    RIGHT_ALT = 165


class ControlKeyState(IntFlag):
    RIGHT_ALT = 1
    LEFT_ALT = 2
    RIGHT_CTRL = 4
    LEFT_CTRL = 8
    SHIFT = 16
    NUM_LOCK = 32
    SCROLL_LOCK = 64
    CAPS_LOCK = 128
    ENHANCED = 256


class WindowsKeyRecord(Contract):
    model_config = ConfigDict(frozen=True)

    virtual_key: int = Field(ge=0, le=65535)
    scan_code: int = Field(ge=0, le=65535)
    character: int = Field(ge=0, le=65535)
    state: KeyState
    control_state: ControlKeyState = Field(ge=0, le=65535)
    repeat: int = Field(ge=1, le=65535)
