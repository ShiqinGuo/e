import asyncio
import os
import sys
from enum import StrEnum
from pathlib import Path

import pytest

from agent_client.application.runtime import AgentRuntime
from agent_client.application.tools import ToolService
from agent_client.domain.configuration import AppConfig
from agent_client.domain.enums import (
    ErrorCode,
    JournalEventType,
    MessageRole,
    ModelEventKind,
    NativeItemType,
    RunStatus,
    StopReason,
    ToolStatus,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.models import ModelEvent, ModelResponse
from agent_client.domain.protocol import ContentType, NativeContent, NativeMessage
from agent_client.domain.runtime import ToolResultCommitted
from agent_client.infrastructure.persistence.maintenance import ProjectionMaintenance
from agent_client.infrastructure.persistence.store import SessionStore


class CrashPoint(StrEnum):
    BEFORE_EFFECT = "before_effect"
    AFTER_EFFECT = "after_effect"
    AFTER_RESULT = "after_result"


class InspectModel:
    def __init__(self):
        self.calls = 0

    async def stream(self, request):
        self.calls += 1
        yield ModelEvent(
            kind=ModelEventKind.COMPLETED,
            response=ModelResponse(
                id="inspection",
                text="Inspected",
                output=[
                    NativeMessage(
                        type=NativeItemType.MESSAGE,
                        role=MessageRole.ASSISTANT,
                        content=[NativeContent(type=ContentType.OUTPUT_TEXT, text="Inspected")],
                    )
                ],
            ),
        )


async def quiet(event: RuntimeEvent) -> None:
    pass


@pytest.mark.parametrize("point", list(CrashPoint))
async def test_forced_process_death_never_replays_a_dispatched_write(tmp_path, point):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    helper = Path(__file__).parent / "helpers" / "crash_client.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        str(helper),
        str(home),
        str(workspace),
        point.value,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        identity = (await asyncio.wait_for(process.stdout.readline(), timeout=30)).decode().strip()
        if not identity:
            pytest.fail((await process.stderr.read()).decode())
        proof = workspace / "proof.txt"
        assert proof.exists() == (point != CrashPoint.BEFORE_EFFECT)
        if point == CrashPoint.BEFORE_EFFECT:
            with pytest.raises(AgentError) as rejected:
                await ProjectionMaintenance(home).rebuild()
            assert rejected.value.code == ErrorCode.SESSION_BUSY
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    config = AppConfig()
    store = SessionStore(home)
    tools = ToolService(config, store)
    model = InspectModel()
    try:
        await store.open()
        await tools.start()
        runtime = AgentRuntime(config, store, model, tools)
        result = await runtime.run(identity, "Inspect the recovered state", quiet)
        records = await store.read(identity)
        results = [
            ToolResultCommitted.model_validate(record.payload).result
            for record in records
            if record.type == JournalEventType.TOOL_RESULT_COMMITTED
        ]
        assert len(results) == 1
        match point:
            case CrashPoint.AFTER_RESULT:
                assert result.status == RunStatus.COMPLETED
                assert model.calls == 1
                assert results[0].status == ToolStatus.SUCCEEDED
            case CrashPoint.BEFORE_EFFECT | CrashPoint.AFTER_EFFECT:
                assert result.stop_reason == StopReason.COMPLETED
                assert model.calls == 1
                assert results[0].status == ToolStatus.UNKNOWN
        assert proof.exists() == (point != CrashPoint.BEFORE_EFFECT)
        if proof.exists():
            assert proof.read_text() == "written exactly once"
    finally:
        await tools.close()
        await store.close()
