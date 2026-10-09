from pathlib import Path
from typing import TypedDict

from agent_client.application.runtime import AgentRuntime
from agent_client.application.tools import ToolService
from agent_client.domain.configuration import AppConfig
from agent_client.domain.enums import ProviderKind
from agent_client.domain.errors import AgentError
from agent_client.domain.events import EventSink, RuntimeEvent
from agent_client.domain.models import ApprovalHandler, RunResult
from agent_client.domain.ports import ModelGateway
from agent_client.infrastructure.auth import AuthService
from agent_client.infrastructure.models import ResponsesGateway
from agent_client.infrastructure.models.access import ProviderAccess
from agent_client.infrastructure.models.chat import ChatCompletionsGateway
from agent_client.infrastructure.observability.telemetry import Telemetry
from agent_client.infrastructure.persistence.store import SessionStore


class SpanAttributes(TypedDict):
    session_id: str
    model: str


class ClientServices:
    def __init__(
        self,
        config: AppConfig,
        store: SessionStore,
        auth: AuthService,
        model: ModelGateway,
        tools: ToolService,
        runtime: AgentRuntime,
        telemetry: Telemetry,
    ):
        self.config = config
        self.store = store
        self.auth = auth
        self.model = model
        self.tools = tools
        self.runtime = runtime
        self.telemetry = telemetry

    @classmethod
    def build(cls, config: AppConfig, home: Path) -> "ClientServices":
        store = SessionStore(home)
        auth = AuthService(home)
        match config.model.provider:
            case ProviderKind.OPENAI_RESPONSES:
                model = ResponsesGateway(config.model, auth)
            case ProviderKind.OPENAI_CHAT_COMPLETIONS:
                model = ChatCompletionsGateway(config.model)
        tools = ToolService(config, store)
        runtime = AgentRuntime(config, store, model, tools)
        return cls(config, store, auth, model, tools, runtime, Telemetry(home))

    @property
    def access(self) -> ProviderAccess:
        return ProviderAccess(self.config.model, self.auth)

    async def open(self, *, start_tools: bool = True) -> "ClientServices":
        await self.store.open()
        await self.telemetry.start()
        if start_tools:
            await self.tools.start()
        return self

    async def close(self) -> None:
        try:
            await self.tools.close()
        finally:
            try:
                await self.model.close()
            finally:
                try:
                    await self.auth.close()
                finally:
                    try:
                        await self.store.close()
                    finally:
                        await self.telemetry.close()

    async def run(
        self,
        session_id: str,
        prompt: str,
        emit: EventSink,
        approve: ApprovalHandler | None = None,
        command_id: str | None = None,
    ) -> RunResult:
        async def observe(event: RuntimeEvent):
            await self.telemetry.emit(event)
            await emit(event)

        attributes = SpanAttributes(session_id=session_id, model=self.config.model.model)
        with self.telemetry.tracer.start_as_current_span(
            "agent.run",
            record_exception=False,
            set_status_on_exception=False,
            attributes=attributes,
        ) as span:
            try:
                result = await self.runtime.run(session_id, prompt, observe, approve, command_id)
                span.set_attribute("run.status", result.status)
                span.set_attribute("run.stop_reason", result.stop_reason)
                return result
            except AgentError as error:
                span.set_attribute("error.code", error.code)
                raise

    async def compact(self, session_id: str, emit: EventSink) -> None:
        async def observe(event: RuntimeEvent):
            await self.telemetry.emit(event)
            await emit(event)

        await self.runtime.compact(session_id, observe)
