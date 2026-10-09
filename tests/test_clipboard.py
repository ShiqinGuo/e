import ctypes
import threading
from enum import IntEnum

import pytest
from textual import events
from textual.app import App
from textual.widgets import Static
from textual.widgets.text_area import Selection

from agent_client.bootstrap import ClientServices
from agent_client.domain.configuration import AppConfig
from agent_client.domain.presentation import WidgetID
from agent_client.presentation.clipboard import (
    ClipboardAccess,
    ClipboardApi,
    decode_clipboard,
    encode_clipboard,
    read_windows_clipboard,
    write_windows_clipboard,
)
from agent_client.presentation.composer import PromptInput
from agent_client.presentation.tui import AgentApp


class FakeHandle(IntEnum):
    MEMORY = 1
    WINDOW = 101


class MemoryClipboard:
    def __init__(self, text: str = ""):
        self.text = text
        self.read_threads: list[int] = []
        self.write_threads: list[int] = []
        self.copies: list[str] = []

    def read(self) -> str:
        self.read_threads.append(threading.get_ident())
        return self.text

    def write(self, text: str) -> None:
        self.write_threads.append(threading.get_ident())
        self.copies.append(text)
        self.text = text


class FakeClipboardApi(ClipboardApi):
    def __init__(self, payload: bytes = b"\x00\x00", *, transfer_failure: bool = False):
        self.user = self
        self.kernel = self
        self.payload = payload
        self.buffer = ctypes.create_string_buffer(payload, len(payload))
        self.transfer_failure = transfer_failure
        self.owners: list[int | None] = []
        self.closed = 0
        self.destroyed = 0
        self.freed = 0
        self.unlocked = 0
        self.emptied = 0
        self.transferred = False

    def open(self, owner: int | None = None):
        self.owners.append(owner)

    def IsClipboardFormatAvailable(self, format):
        return True

    def GetClipboardData(self, format):
        return FakeHandle.MEMORY

    def CloseClipboard(self):
        self.closed += 1
        return True

    def GlobalSize(self, memory):
        return len(self.payload)

    def GlobalLock(self, memory):
        return ctypes.addressof(self.buffer)

    def GlobalUnlock(self, memory):
        self.unlocked += 1
        return True

    def GlobalAlloc(self, flags, size):
        self.buffer = ctypes.create_string_buffer(size)
        return FakeHandle.MEMORY

    def GlobalFree(self, memory):
        self.freed += 1
        return None

    def CreateWindowExW(self, *args):
        return FakeHandle.WINDOW

    def DestroyWindow(self, owner):
        self.destroyed += 1
        return True

    def EmptyClipboard(self):
        self.emptied += 1
        return True

    def SetClipboardData(self, format, memory):
        if self.transfer_failure:
            raise OSError("Clipboard transfer failed")
        self.transferred = True
        self.payload = self.buffer.raw
        return memory


@pytest.fixture
async def clipboard_agent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    services = ClientServices.build(AppConfig(), tmp_path / "home")
    await services.open()
    app = AgentApp(services, workspace)
    memory = MemoryClipboard()
    app.clipboard_access = ClipboardAccess(windows=True, reader=memory.read, writer=memory.write)
    try:
        yield app, memory
    finally:
        await services.close()


@pytest.mark.parametrize("text", ["", "plain", "\u4f60\u597d \U0001f680\r\nnext"])
def test_unicode_clipboard_native_memory_ownership_and_roundtrip(text):
    api = FakeClipboardApi()
    write_windows_clipboard(text, api_factory=lambda: api)
    assert api.owners == [FakeHandle.WINDOW]
    assert api.closed == api.destroyed == api.unlocked == 1
    assert api.transferred and not api.freed
    assert decode_clipboard(api.payload) == text
    read_api = FakeClipboardApi(api.payload)
    assert read_windows_clipboard(api_factory=lambda: read_api) == text
    assert read_api.owners == [None]
    assert read_api.closed == read_api.unlocked == 1
    assert not read_api.freed


def test_failed_clipboard_transfer_releases_owned_memory_and_handles():
    api = FakeClipboardApi(transfer_failure=True)
    with pytest.raises(OSError, match="transfer failed"):
        write_windows_clipboard("text", api_factory=lambda: api)
    assert api.closed == api.destroyed == api.freed == 1
    assert not api.transferred


@pytest.mark.parametrize("payload", [b"x", b"A\x00", b"\x00\xd8\x00\x00"])
def test_malformed_clipboard_unicode_fails_at_boundary(payload):
    api = FakeClipboardApi(payload)
    with pytest.raises((ValueError, UnicodeError)):
        read_windows_clipboard(api_factory=lambda: api)
    assert api.closed == api.unlocked == 1


def test_embedded_nul_is_rejected_before_touching_native_clipboard():
    with pytest.raises(ValueError, match="NUL"):
        encode_clipboard("a\x00b")


async def test_nonwindows_clipboard_uses_textual_internal_state_without_native_calls():
    app = App()
    memory = MemoryClipboard("OS content")
    access = ClipboardAccess(windows=False, reader=memory.read, writer=memory.write)
    await access.write(app, "local content")
    assert await access.read(app) == "local content"
    assert not memory.read_threads and not memory.write_threads


async def test_ctrl_v_uses_os_clipboard_unicode_and_does_not_submit(clipboard_agent):
    app, memory = clipboard_agent
    memory.text = "\u4f60\u597d \U0001f680\r\nsecond line"
    async with app.run_test(size=(90, 30)) as pilot:
        await app.ready.wait()
        await pilot.pause()
        composer = app.query_one(PromptInput)
        composer.load_text("old")
        composer.selection = Selection((0, 0), (0, 3))
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert composer.text == "\u4f60\u597d \U0001f680\nsecond line"
        assert len(memory.read_threads) == 1
        assert memory.read_threads[0] != threading.get_ident()
        assert not app.pending and not app.processing
        assert composer.region.height == 2


async def test_right_click_pastes_once_preserving_existing_input_selection(clipboard_agent):
    app, memory = clipboard_agent
    memory.text = "replacement"
    async with app.run_test(size=(90, 30)) as pilot:
        await app.ready.wait()
        await pilot.pause()
        composer = app.query_one(PromptInput)
        composer.load_text("prefix target suffix")
        composer.selection = Selection((0, 7), (0, 13))
        await pilot.click(composer, offset=(0, 0), button=3)
        await pilot.pause()
        assert composer.text == "prefix replacement suffix"
        assert len(memory.read_threads) == 1
        assert not app.pending


async def test_terminal_paste_is_inserted_once_without_reading_os_clipboard(clipboard_agent):
    app, memory = clipboard_agent
    memory.text = "should not be read"
    async with app.run_test() as pilot:
        await app.ready.wait()
        await pilot.pause()
        composer = app.query_one(PromptInput)
        app.post_message(events.Paste("terminal\npaste"))
        await pilot.pause()
        assert composer.text == "terminal\npaste"
        assert not memory.read_threads
        assert not app.pending


@pytest.mark.parametrize("key", ["ctrl+c", "ctrl+shift+c"])
async def test_copy_input_selection_writes_os_clipboard_and_does_not_exit(clipboard_agent, key):
    app, memory = clipboard_agent
    async with app.run_test() as pilot:
        await app.ready.wait()
        await pilot.pause()
        composer = app.query_one(PromptInput)
        composer.load_text("copy selected input")
        composer.selection = Selection((0, 5), (0, 13))
        await pilot.press(key)
        await pilot.pause()
        assert memory.copies == ["selected"]
        assert memory.write_threads[0] != threading.get_ident()
        assert composer.text == "copy selected input"
        assert app.is_running
        assert app.clipboard == "selected"


async def test_copy_selected_transcript_does_not_require_composer_selection(clipboard_agent):
    app, memory = clipboard_agent
    async with app.run_test(size=(90, 30)) as pilot:
        await app.ready.wait()
        composer = app.query_one(PromptInput)
        composer.load_text("stale input selection")
        composer.selection = Selection((0, 0), (0, 5))
        await app.add_message("Copy this output")
        await pilot.pause()
        transcript = app.query_one(WidgetID.TRANSCRIPT.selector)
        output = next(
            widget
            for widget in transcript.children
            if isinstance(widget, Static) and str(widget.render()) == "Copy this output"
        )
        await pilot.mouse_down(output, offset=(0, 0))
        await pilot.hover(output, offset=(9, 0))
        await pilot.mouse_up(output, offset=(9, 0))
        await pilot.pause()
        selected = app.screen.get_selected_text()
        assert selected
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert memory.copies == [selected]
        assert app.is_running


async def test_copy_without_selection_is_noop_and_preserves_os_clipboard(clipboard_agent):
    app, memory = clipboard_agent
    memory.text = "untouched"
    async with app.run_test() as pilot:
        await app.ready.wait()
        await pilot.pause()
        await pilot.press("ctrl+c")
        assert not memory.copies
        assert memory.text == "untouched"
        assert app.is_running


async def test_failed_os_paste_preserves_input_and_never_uses_stale_internal_clipboard(
    clipboard_agent,
):
    app, memory = clipboard_agent

    def fail_read() -> str:
        raise OSError("Clipboard is unavailable")

    app.clipboard_access.reader = fail_read
    async with app.run_test() as pilot:
        await app.ready.wait()
        await pilot.pause()
        composer = app.query_one(PromptInput)
        composer.load_text("keep this input")
        app.copy_to_clipboard("stale local text")
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert composer.text == "keep this input"
        assert not app.pending
        assert not memory.copies


async def test_read_only_input_rejects_ctrl_v_without_opening_clipboard(clipboard_agent):
    app, memory = clipboard_agent
    async with app.run_test() as pilot:
        await app.ready.wait()
        await pilot.pause()
        composer = app.query_one(PromptInput)
        composer.load_text("read only")
        composer.read_only = True
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert composer.text == "read only"
        assert not memory.read_threads
