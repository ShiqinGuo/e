from dataclasses import dataclass

from textual import events
from textual.binding import Binding
from textual.fuzzy import FuzzySearch
from textual.message import Message
from textual.widgets import OptionList, TextArea
from textual.widgets.option_list import Option

from agent_client.domain.presentation import ComposerKey, SlashCommand, SlashCommandHelp, WidgetID
from agent_client.presentation.clipboard import (
    ClipboardAccess,
    ClipboardAction,
    ClipboardKey,
    ClipboardMouseButton,
    ClipboardNotice,
)


@dataclass(frozen=True)
class ComposerLayout:
    maximum_lines: int = 8


DEFAULT_COMPOSER_LAYOUT = ComposerLayout()


COMMANDS = (
    SlashCommandHelp(
        command=SlashCommand.REASONING, description="Choose reasoning effort", arguments="[level]"
    ),
    SlashCommandHelp(
        command=SlashCommand.PERMISSIONS,
        description="Change approval policy",
        arguments="[ask|never|read_only]",
    ),
    SlashCommandHelp(command=SlashCommand.NEW, description="Start a new session"),
    SlashCommandHelp(
        command=SlashCommand.RESUME, description="Restore a session", arguments="<session_id>"
    ),
    SlashCommandHelp(command=SlashCommand.CONTINUE, description="Continue the unfinished task"),
    SlashCommandHelp(command=SlashCommand.SESSIONS, description="List saved sessions"),
    SlashCommandHelp(
        command=SlashCommand.LOGIN, description="Sign in with ChatGPT", arguments="[new]"
    ),
    SlashCommandHelp(command=SlashCommand.LOGOUT, description="Sign out of the account"),
    SlashCommandHelp(
        command=SlashCommand.MODEL, description="Show or change the model", arguments="[name]"
    ),
    SlashCommandHelp(
        command=SlashCommand.SKILLS, description="Find personal skills", arguments="[query]"
    ),
    SlashCommandHelp(command=SlashCommand.MCP, description="Show MCP connections"),
    SlashCommandHelp(command=SlashCommand.COMPACT, description="Summarize the current context"),
    SlashCommandHelp(
        command=SlashCommand.RESOLVE,
        description="Record a verified tool outcome",
        arguments="<call_id> failed|succeeded <evidence>",
    ),
    SlashCommandHelp(command=SlashCommand.STATUS, description="Show state, usage and log path"),
    SlashCommandHelp(command=SlashCommand.CONFIG, description="Show current configuration"),
    SlashCommandHelp(command=SlashCommand.REBUILD, description="Show projection rebuild steps"),
    SlashCommandHelp(
        command=SlashCommand.DELETE,
        description="Show session deletion steps",
        arguments="[session_id]",
    ),
)


def command_matches(text: str) -> tuple[SlashCommandHelp, ...]:
    if not text.startswith("/") or any(character.isspace() for character in text):
        return ()
    query = text[1:].casefold()
    if not query:
        return COMMANDS
    matcher = FuzzySearch()
    scored = [(entry, matcher.match(query, entry.command.value[1:])[0]) for entry in COMMANDS]
    scored = [item for item in scored if item[1] > 0]
    scored.sort(
        key=lambda item: (
            not item[0].command.value[1:].startswith(query),
            -item[1],
            item[0].command.value,
        )
    )
    return tuple(entry for entry, _ in scored)


class CommandMenu(OptionList, can_focus=False):
    DEFAULT_CSS = """
    CommandMenu { height: auto; max-height: 8; border: round $accent; margin: 0; }
    """

    def __init__(self):
        super().__init__(id=WidgetID.COMMAND_MENU.value, markup=False, compact=True)
        self.display = False
        self.border_title = "Commands | Up/Down: select | Tab/Enter: complete | Esc: close"
        self.query_text = ""

    def update_query(self, text: str):
        if text == self.query_text:
            return
        self.query_text = text
        self.clear_options()
        if not text.startswith("/") or any(character.isspace() for character in text):
            self.display = False
            return
        matches = command_matches(text)
        self.add_options(
            Option(
                f"{entry.command.value} {entry.arguments}  {entry.description}",
                id=entry.command.value,
            )
            for entry in matches
        )
        if not matches:
            self.add_option(Option("No matching commands", disabled=True))
        self.highlighted = 0 if matches else None
        self.display = True

    @property
    def selected_command(self) -> SlashCommand | None:
        option = self.highlighted_option
        if not self.display or option is None or option.id is None:
            return None
        return SlashCommand(option.id)


class PromptInput(TextArea):
    BINDINGS = [
        Binding(ClipboardKey.PASTE, ClipboardAction.PASTE, "Paste", show=False, priority=True),
        Binding(
            ClipboardKey.PASTE_ALTERNATE, ClipboardAction.PASTE, "Paste", show=False, priority=True
        ),
        Binding(ComposerKey.ENTER, "send", "Send", priority=True),
        Binding(ComposerKey.SHIFT_ENTER, "newline", "Newline", priority=True),
        Binding(ComposerKey.CTRL_J, "newline", "Newline", show=False, priority=True),
        Binding(ComposerKey.CTRL_ENTER, "send", "Send", show=False, priority=True),
    ]

    class Submitted(Message):
        def __init__(self, *, queue: bool = False):
            super().__init__()
            self.queue = queue

    def __init__(self, menu: CommandMenu, clipboard_access: ClipboardAccess | None = None):
        super().__init__(
            id=WidgetID.COMPOSER.value,
            soft_wrap=True,
            tab_behavior="focus",
            compact=True,
            placeholder="Type a task or /command",
        )
        self.menu = menu
        self.clipboard_access = clipboard_access or ClipboardAccess()
        self.run_active = False

    async def action_paste(self):
        if self.read_only or self.disabled:
            return
        try:
            text = await self.clipboard_access.read(self.app)
        except (OSError, UnicodeError, ValueError) as error:
            self.app.notify(f"Clipboard paste failed: {error}", severity=ClipboardNotice.ERROR)
            return
        if text:
            result = self.replace(text, *self.selection, maintain_selection_offset=False)
            self.move_cursor(result.end_location)
            self.focus()

    async def action_copy(self):
        if self.selected_text:
            try:
                await self.clipboard_access.write(self.app, self.selected_text)
            except (OSError, UnicodeError, ValueError) as error:
                self.app.notify(f"Clipboard copy failed: {error}", severity=ClipboardNotice.ERROR)

    async def _on_mouse_down(self, event: events.MouseDown):
        if event.button == ClipboardMouseButton.RIGHT:
            event.stop()
            event.prevent_default()
            await self.action_paste()
            return
        await super()._on_mouse_down(event)

    def resize_to_content(self):
        self.styles.height = min(
            DEFAULT_COMPOSER_LAYOUT.maximum_lines, max(1, self.wrapped_document.height)
        )

    def on_resize(self):
        self.call_after_refresh(self.resize_to_content)

    def complete_command(self, command: SlashCommand):
        self.load_text(command.value + " ")
        self.move_cursor(self.document.end)
        self.menu.display = False
        self.focus()

    def action_send(self):
        self.menu.update_query(self.text)
        selected = self.menu.selected_command
        exact = any(self.text == command.value for command in SlashCommand)
        if selected is not None and not exact:
            self.complete_command(selected)
            return
        self.menu.display = False
        self.post_message(self.Submitted())

    def action_newline(self):
        self.replace("\n", *self.selection, maintain_selection_offset=False)

    async def _on_key(self, event: events.Key):
        if self.menu.display:
            match event.key:
                case ComposerKey.UP:
                    self.menu.action_cursor_up()
                case ComposerKey.DOWN:
                    self.menu.action_cursor_down()
                case ComposerKey.TAB:
                    if selected := self.menu.selected_command:
                        self.complete_command(selected)
                case _:
                    await super()._on_key(event)
                    return
            event.stop()
            event.prevent_default()
            return
        if event.key == ComposerKey.TAB and self.run_active and self.text.strip():
            self.post_message(self.Submitted(queue=True))
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)
