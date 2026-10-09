import asyncio
import logging
import logging.handlers
import queue
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import get_current_span

from agent_client.domain.context_usage import ContextUsage
from agent_client.domain.enums import RuntimeEventKind
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.observability import DiagnosticDetails, DiagnosticRecord
from agent_client.domain.runtime import (
    EpochChanged,
    ErrorOccurred,
    ModelCompleted,
    ModelRequestMetadata,
    RunFinished,
    ToolFinished,
    UserMessage,
)
from agent_client.domain.tools import ToolDispatchEvent


@dataclass(frozen=True)
class TelemetryDefaults:
    queue_capacity: int = 1000
    log_bytes: int = 5242880
    log_backups: int = 3


class LogExtra(TypedDict):
    session_id: str
    run_id: str
    trace_id: str | None
    details: DiagnosticDetails


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return DiagnosticRecord(
            time=datetime.now(UTC),
            level=record.levelname,
            event=RuntimeEventKind(record.getMessage()),
            session_id=record.session_id,
            run_id=record.run_id,
            trace_id=record.trace_id,
            details=record.details,
        ).model_dump_json(exclude_unset=True)


class BoundedQueueHandler(logging.handlers.QueueHandler):
    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass


class Telemetry:
    def __init__(self, home: Path):
        self.home = home
        self.defaults = TelemetryDefaults()
        self.logger = logging.Logger("agent-client-events", logging.INFO)
        self.queue: queue.Queue = queue.Queue(maxsize=self.defaults.queue_capacity)
        self.listener: logging.handlers.QueueListener | None = None
        self.provider = TracerProvider()
        self.tracer = self.provider.get_tracer("agent-client")

    async def start(self) -> None:
        directory = self.home / "logs"
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)

        def configure():
            handler = logging.handlers.RotatingFileHandler(
                directory / "runtime.jsonl",
                maxBytes=self.defaults.log_bytes,
                backupCount=self.defaults.log_backups,
                encoding="utf-8",
            )
            handler.setFormatter(JsonFormatter())
            self.listener = logging.handlers.QueueListener(self.queue, handler)
            self.logger.addHandler(BoundedQueueHandler(self.queue))
            self.listener.start()

        await asyncio.to_thread(configure)

    async def emit(self, event: RuntimeEvent) -> None:
        if event.kind in {
            RuntimeEventKind.TEXT_DELTA,
            RuntimeEventKind.REASONING_DELTA,
            RuntimeEventKind.TOOL_OUTPUT,
            RuntimeEventKind.TOOL_OUTPUT_CHUNK,
        }:
            return
        match event.data:
            case ErrorOccurred() as payload:
                details = DiagnosticDetails(error_code=payload.code)
            case ToolFinished() as payload:
                details = DiagnosticDetails(
                    call_id=payload.result.call_id,
                    status=payload.result.status,
                    is_error=payload.result.is_error,
                )
            case ToolDispatchEvent() as payload:
                details = DiagnosticDetails(
                    call_id=payload.call_id, name=payload.name, effect=payload.effect
                )
            case RunFinished() as payload:
                details = DiagnosticDetails(status=payload.status, stop_reason=payload.stop_reason)
            case EpochChanged() as payload:
                details = DiagnosticDetails(epoch=payload.epoch)
            case ModelCompleted() as payload:
                details = DiagnosticDetails(
                    step_id=payload.step_id,
                    prefix_revision=payload.prefix_revision,
                    context_epoch=payload.context_epoch,
                    context_hash=payload.context_hash,
                    estimated_input_tokens=payload.estimated_input_tokens,
                    token_measurement=payload.token_measurement,
                    input_tokens=payload.input_tokens,
                    output_tokens=payload.output_tokens,
                    cached_input_tokens=payload.cached_input_tokens,
                    ttft_seconds=payload.ttft_seconds,
                    duration_seconds=payload.duration_seconds,
                )
            case ModelRequestMetadata() as payload:
                details = DiagnosticDetails(
                    step_id=payload.step_id,
                    prefix_revision=payload.prefix_revision,
                    context_epoch=payload.context_epoch,
                    context_hash=payload.context_hash,
                    estimated_input_tokens=payload.estimated_input_tokens,
                    token_measurement=payload.token_measurement,
                )
            case ContextUsage() as payload:
                details = DiagnosticDetails(token_measurement=payload.measurement)
            case UserMessage():
                details = DiagnosticDetails()
            case None:
                details = DiagnosticDetails()
            case _:
                raise ValueError("Unsupported diagnostic event payload")
        context = get_current_span().get_span_context()
        extra = LogExtra(
            session_id=event.session_id,
            run_id=event.run_id,
            details=details,
            trace_id=f"{context.trace_id:032x}" if context.is_valid else None,
        )
        self.logger.info(event.kind, extra=extra)

    async def close(self) -> None:
        if self.listener:

            def stop():
                try:
                    self.listener.stop()
                except queue.Full:
                    self.queue.get_nowait()
                    self.listener.stop()
                for handler in self.listener.handlers:
                    handler.close()

            await asyncio.to_thread(stop)
            self.listener = None
        await asyncio.to_thread(self.provider.shutdown)
