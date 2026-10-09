from agent_client.bootstrap import ClientServices
from agent_client.domain.configuration import AppConfig
from agent_client.domain.enums import (
    JournalEventType,
    MessageRole,
    ModelEventKind,
    NativeItemType,
    RunStatus,
    ToolExecutionState,
)
from agent_client.domain.models import ModelEvent, ModelResponse, ToolCall
from agent_client.domain.protocol import (
    ContentType,
    NativeContent,
    NativeFunctionCall,
    NativeMessage,
)
from agent_client.domain.tools import RunCommandArguments, ToolName, WriteFileArguments
from agent_client.domain.workspace import FileVersion


class CodingGateway:
    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        step = len(self.requests)
        match step:
            case 1:
                call = ToolCall(
                    id="write-integration",
                    name=ToolName.WRITE_FILE,
                    arguments=WriteFileArguments(
                        path="hello.py", content="print('hello')\n", before_hash=FileVersion.MISSING
                    ),
                )
            case 2:
                call = ToolCall(
                    id="command-integration",
                    name=ToolName.RUN_COMMAND,
                    arguments=RunCommandArguments(command="echo integration-ok", yield_seconds=10),
                )
            case _:
                yield ModelEvent(
                    kind=ModelEventKind.COMPLETED,
                    response=ModelResponse(
                        id="done",
                        text="Done",
                        output=[
                            NativeMessage(
                                type=NativeItemType.MESSAGE,
                                role=MessageRole.ASSISTANT,
                                content=[NativeContent(type=ContentType.OUTPUT_TEXT, text="Done")],
                            )
                        ],
                    ),
                )
                return
        yield ModelEvent(
            kind=ModelEventKind.COMPLETED,
            response=ModelResponse(
                id=f"r{step}",
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

    async def close(self):
        pass


async def test_real_tools_durable_dispatch_and_reopen_without_replaying(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AppConfig()
    config.runtime.allow_write = True
    config.runtime.allow_commands = True
    services = ClientServices.build(config, tmp_path / "home")
    gateway = CodingGateway()
    await services.model.close()
    services.model = gateway
    services.runtime.model = gateway

    async def observe(_event):
        pass

    try:
        await services.open()
        identity = await services.store.create_session(workspace)
        command_id = await services.runtime.enqueue(
            identity, "Create and check the example program"
        )
        result = await services.run(
            identity, "Create and check the example program", observe, command_id=command_id
        )
        assert result.status == RunStatus.COMPLETED
        assert (workspace / "hello.py").read_text() == "print('hello')\n"
        records = await services.store.read(identity)
        for call_id in ("write-integration", "command-integration"):
            dispatched = next(
                (
                    r.seq
                    for r in records
                    if r.type == JournalEventType.TOOL_CALL_STATE
                    and r.payload.call_id == call_id
                    and (r.payload.state == ToolExecutionState.DISPATCHING)
                )
            )
            completed = next(
                (
                    r
                    for r in records
                    if r.type == JournalEventType.TOOL_RESULT_COMMITTED
                    and r.payload.result.call_id == call_id
                )
            )
            assert dispatched < completed.seq
            assert not completed.payload.result.is_error
        assert any(
            (
                "integration-ok" in r.payload.model_dump_json()
                for r in records
                if r.type == JournalEventType.TOOL_RESULT_COMMITTED
            )
        )
        assert all(
            (request.cache_key == gateway.requests[0].cache_key for request in gateway.requests)
        )
    finally:
        await services.close()
    recovered = ClientServices.build(config, tmp_path / "home")
    try:
        await recovered.open(start_tools=False)
        await recovered.store.recover(identity)
        assert await recovered.runtime.pending_inputs(identity) == []
        assert len(await recovered.store.read(identity)) == len(records)
        assert (workspace / "hello.py").read_text() == "print('hello')\n"
    finally:
        await recovered.close()
