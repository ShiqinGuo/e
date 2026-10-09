import asyncio
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from agent_client.application.runtime import AgentRuntime
from agent_client.application.tools import ToolService
from agent_client.domain.configuration import AppConfig
from agent_client.domain.enums import (
    JournalEventType,
    ModelEventKind,
    NativeItemType,
    ToolExecutionState,
)
from agent_client.domain.events import JournalRecord, RuntimeEvent
from agent_client.domain.models import ModelEvent, ModelResponse, ToolCall
from agent_client.domain.protocol import NativeFunctionCall
from agent_client.domain.runtime import ToolStateChange
from agent_client.domain.tools import ToolName, WriteFileArguments
from agent_client.domain.workspace import FileVersion
from agent_client.infrastructure.persistence.store import SessionStore


class CrashPoint(StrEnum):
    BEFORE_EFFECT = "before_effect"
    AFTER_EFFECT = "after_effect"
    AFTER_RESULT = "after_result"


class PausingStore(SessionStore):
    def __init__(self, home: Path, point: CrashPoint):
        super().__init__(home)
        self.point = point

    async def pause(self, session_id: str) -> None:
        print(session_id, flush=True)
        await asyncio.Event().wait()

    async def append(
        self,
        session_id: str,
        type: JournalEventType,
        payload: BaseModel,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> JournalRecord:
        if self.point == CrashPoint.AFTER_EFFECT and type == JournalEventType.TOOL_RESULT_COMMITTED:
            await self.pause(session_id)
        record = await super().append(session_id, type, payload, run_id, event_id)
        match self.point:
            case CrashPoint.BEFORE_EFFECT if isinstance(payload, ToolStateChange):
                if payload.state == ToolExecutionState.DISPATCHING:
                    await self.pause(session_id)
            case CrashPoint.AFTER_RESULT if type == JournalEventType.TOOL_RESULT_COMMITTED:
                await self.pause(session_id)
        return record


class WriteModel:
    async def stream(self, request):
        call = ToolCall(
            id="crash-write",
            name=ToolName.WRITE_FILE,
            arguments=WriteFileArguments(
                path="proof.txt", content="written exactly once", before_hash=FileVersion.MISSING
            ),
        )
        yield ModelEvent(
            kind=ModelEventKind.COMPLETED,
            response=ModelResponse(
                id="write-response",
                calls=[call],
                output=[
                    NativeFunctionCall(
                        type=NativeItemType.FUNCTION_CALL,
                        call_id=call.id,
                        name=call.name,
                        arguments=call.arguments.model_dump_json(),
                    )
                ],
            ),
        )


async def quiet(event: RuntimeEvent) -> None:
    pass


async def main() -> None:
    home, workspace = (Path(sys.argv[1]), Path(sys.argv[2]))
    store = PausingStore(home, CrashPoint(sys.argv[3]))
    config = AppConfig()
    config.runtime.allow_write = True
    tools = ToolService(config, store)
    try:
        await store.open()
        await tools.start()
        identity = await store.create_session(workspace)
        runtime = AgentRuntime(config, store, WriteModel(), tools)
        await runtime.run(identity, "Write the proof file", quiet)
    finally:
        await tools.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
