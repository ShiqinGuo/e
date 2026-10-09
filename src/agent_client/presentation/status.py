from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static

from agent_client.domain.context_usage import ContextUsage
from agent_client.domain.presentation import WidgetID


@dataclass(frozen=True)
class StatusRendering:
    frame_seconds: float = 0.25
    kilo_tokens: int = 1000
    percent_scale: int = 100


DEFAULT_STATUS_RENDERING = StatusRendering()


class StatusStyle(StrEnum):
    MUTED = "#a8adb5"
    DIM = "dim"


class MotionMode(StrEnum):
    NONE = "none"


class WorkIndicator(Static):
    DEFAULT_CSS = "WorkIndicator { height: 1; padding: 0 1; color: $text-muted; }"
    FRAMES = ("·", "✧", "✦", "✧")

    def __init__(self):
        super().__init__("Idle", id=WidgetID.RUN_STATUS.value, markup=False)
        self.label = "Idle"
        self.suffix = ""
        self.working = False
        self.started = 0.0
        self.frame = 0
        self.timer: Timer | None = None

    def on_mount(self):
        self.timer = self.set_interval(
            DEFAULT_STATUS_RENDERING.frame_seconds, self.advance, pause=not self.working
        )

    def set_activity(self, label: str, suffix: str, *, working: bool):
        if working and (not self.working or label != self.label):
            self.started = monotonic()
            self.frame = 0
        self.label = label
        self.suffix = suffix
        self.working = working
        if self.timer is not None:
            match working:
                case True:
                    self.timer.resume()
                case False:
                    self.timer.pause()
        self.paint()

    def advance(self):
        if self.working:
            self.frame += 1
            self.paint()

    def paint(self):
        content = Text()
        if self.working:
            frame = 0 if self.app.animation_level == MotionMode.NONE else self.frame
            content.append(self.FRAMES[frame % len(self.FRAMES)] + " ", style=StatusStyle.MUTED)
            content.append(f"{self.label} · {int(monotonic() - self.started)}s")
        else:
            content.append(self.label)
        content.append(self.suffix, style=StatusStyle.DIM)
        self.update(content)


class ContextMeter(Static):
    DEFAULT_CSS = "ContextMeter { height: 1; padding: 0 1; color: $text-muted; }"

    def __init__(self, usage: ContextUsage):
        super().__init__(id=WidgetID.CONTEXT_USAGE.value, markup=False)
        self.show_usage(usage)

    def show_usage(self, usage: ContextUsage):
        capacity = f"{usage.context_window / DEFAULT_STATUS_RENDERING.kilo_tokens:g}k"
        match usage.used_tokens:
            case None:
                label = f"Context -- / {capacity} · unavailable"
            case used:
                label = f"Context {used / DEFAULT_STATUS_RENDERING.kilo_tokens:.1f}k / {capacity} · {usage.ratio * DEFAULT_STATUS_RENDERING.percent_scale:.1f}% · last input"
        self.update(label)
