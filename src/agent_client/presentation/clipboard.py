import asyncio
import ctypes
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from textual.app import App


class ClipboardKey(StrEnum):
    COPY = "ctrl+c"
    COPY_ALTERNATE = "ctrl+shift+c"
    PASTE = "ctrl+v"
    PASTE_ALTERNATE = "super+v"


class ClipboardAction(StrEnum):
    COPY_SELECTION = "copy_selection"
    COPY = "copy"
    PASTE = "paste"


class ClipboardNotice(StrEnum):
    ERROR = "error"


class ClipboardMouseButton(IntEnum):
    RIGHT = 3


class ClipboardFormat(IntEnum):
    UNICODE_TEXT = 13


class MemoryFlag(IntEnum):
    MOVEABLE = 2


class ClipboardWindow(IntEnum):
    MESSAGE_ONLY = -3


class ClipboardLibrary(StrEnum):
    USER = "user32"
    KERNEL = "kernel32"


class ClipboardWindowClass(StrEnum):
    STATIC = "STATIC"


class ClipboardPlatform(StrEnum):
    WINDOWS = "win32"


class ClipboardEncoding(StrEnum):
    UTF16 = "utf-16-le"


@dataclass(frozen=True)
class ClipboardLimits:
    attempts: int = 10
    retry_seconds: float = 0.02
    maximum_bytes: int = 16777216
    code_unit_bytes: int = 2


DEFAULT_CLIPBOARD_LIMITS = ClipboardLimits()


class ClipboardApi:
    def __init__(self):
        self.user = ctypes.WinDLL(ClipboardLibrary.USER, use_last_error=True)
        self.kernel = ctypes.WinDLL(ClipboardLibrary.KERNEL, use_last_error=True)
        self.user.OpenClipboard.argtypes = [wintypes.HWND]
        self.user.OpenClipboard.restype = wintypes.BOOL
        self.user.CloseClipboard.argtypes = []
        self.user.CloseClipboard.restype = wintypes.BOOL
        self.user.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        self.user.IsClipboardFormatAvailable.restype = wintypes.BOOL
        self.user.GetClipboardData.argtypes = [wintypes.UINT]
        self.user.GetClipboardData.restype = wintypes.HANDLE
        self.user.EmptyClipboard.argtypes = []
        self.user.EmptyClipboard.restype = wintypes.BOOL
        self.user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self.user.SetClipboardData.restype = wintypes.HANDLE
        self.user.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        self.user.CreateWindowExW.restype = wintypes.HWND
        self.user.DestroyWindow.argtypes = [wintypes.HWND]
        self.user.DestroyWindow.restype = wintypes.BOOL
        self.kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self.kernel.GlobalAlloc.restype = wintypes.HGLOBAL
        self.kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalFree.restype = wintypes.HGLOBAL
        self.kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalLock.restype = wintypes.LPVOID
        self.kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalUnlock.restype = wintypes.BOOL
        self.kernel.GlobalSize.argtypes = [wintypes.HGLOBAL]
        self.kernel.GlobalSize.restype = ctypes.c_size_t

    def open(self, owner: int | None = None):
        for attempt in range(DEFAULT_CLIPBOARD_LIMITS.attempts):
            if self.user.OpenClipboard(owner):
                return
            if attempt + 1 < DEFAULT_CLIPBOARD_LIMITS.attempts:
                time.sleep(DEFAULT_CLIPBOARD_LIMITS.retry_seconds)
        raise ctypes.WinError(ctypes.get_last_error())


def decode_clipboard(payload: bytes) -> str:
    limits = DEFAULT_CLIPBOARD_LIMITS
    if not payload or len(payload) > limits.maximum_bytes or len(payload) % limits.code_unit_bytes:
        raise ValueError("Clipboard text has an invalid UTF-16 buffer size")
    terminator = next(
        (
            offset
            for offset in range(0, len(payload), limits.code_unit_bytes)
            if payload[offset : offset + limits.code_unit_bytes] == b"\x00\x00"
        ),
        None,
    )
    if terminator is None:
        raise ValueError("Clipboard text is missing its UTF-16 terminator")
    return payload[:terminator].decode(ClipboardEncoding.UTF16)


def encode_clipboard(text: str) -> bytes:
    if "\x00" in text:
        raise ValueError("Clipboard text cannot contain a NUL character")
    payload = text.encode(ClipboardEncoding.UTF16) + b"\x00\x00"
    if len(payload) > DEFAULT_CLIPBOARD_LIMITS.maximum_bytes:
        raise ValueError("Clipboard text exceeds the configured byte limit")
    return payload


def read_windows_clipboard(*, api_factory: Callable[[], ClipboardApi] = ClipboardApi) -> str:
    api = api_factory()
    api.open()
    try:
        if not api.user.IsClipboardFormatAvailable(ClipboardFormat.UNICODE_TEXT):
            return ""
        memory = api.user.GetClipboardData(ClipboardFormat.UNICODE_TEXT)
        if not memory:
            raise ctypes.WinError(ctypes.get_last_error())
        size = api.kernel.GlobalSize(memory)
        if size > DEFAULT_CLIPBOARD_LIMITS.maximum_bytes:
            raise ValueError("Clipboard text exceeds the configured byte limit")
        pointer = api.kernel.GlobalLock(memory)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return decode_clipboard(ctypes.string_at(pointer, size))
        finally:
            api.kernel.GlobalUnlock(memory)
    finally:
        api.user.CloseClipboard()


def write_windows_clipboard(
    text: str, *, api_factory: Callable[[], ClipboardApi] = ClipboardApi
) -> None:
    payload = encode_clipboard(text)
    api = api_factory()
    memory = api.kernel.GlobalAlloc(MemoryFlag.MOVEABLE, len(payload))
    if not memory:
        raise ctypes.WinError(ctypes.get_last_error())
    owner = None
    opened = False
    transferred = False
    try:
        pointer = api.kernel.GlobalLock(memory)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(pointer, payload, len(payload))
        finally:
            api.kernel.GlobalUnlock(memory)
        owner = api.user.CreateWindowExW(
            0,
            ClipboardWindowClass.STATIC,
            None,
            0,
            0,
            0,
            0,
            0,
            wintypes.HWND(ClipboardWindow.MESSAGE_ONLY),
            None,
            None,
            None,
        )
        if not owner:
            raise ctypes.WinError(ctypes.get_last_error())
        api.open(owner)
        opened = True
        if not api.user.EmptyClipboard():
            raise ctypes.WinError(ctypes.get_last_error())
        if not api.user.SetClipboardData(ClipboardFormat.UNICODE_TEXT, memory):
            raise ctypes.WinError(ctypes.get_last_error())
        transferred = True
    finally:
        if opened:
            api.user.CloseClipboard()
        if owner:
            api.user.DestroyWindow(owner)
        if not transferred:
            api.kernel.GlobalFree(memory)


class ClipboardAccess:
    def __init__(
        self,
        *,
        windows: bool = sys.platform == ClipboardPlatform.WINDOWS,
        reader: Callable[[], str] = read_windows_clipboard,
        writer: Callable[[str], None] = write_windows_clipboard,
    ):
        self.windows = windows
        self.reader = reader
        self.writer = writer

    async def read(self, app: App) -> str:
        match self.windows:
            case True:
                return await asyncio.to_thread(self.reader)
            case False:
                return app.clipboard

    async def write(self, app: App, text: str) -> None:
        if self.windows:
            await asyncio.to_thread(self.writer, text)
        app.copy_to_clipboard(text)
