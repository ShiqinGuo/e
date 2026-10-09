import asyncio

import pytest
from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Button, Markdown, OptionList, Static, TextArea

from agent_client.bootstrap import ClientServices
from agent_client.domain.configuration import AppConfig, NamedMcpServer
from agent_client.domain.context_usage import ContextUsage
from agent_client.domain.enums import (
    ApprovalMode,
    ErrorCode,
    JournalEventType,
    McpTransport,
    MessageRole,
    ModelEventKind,
    NativeItemType,
    ReasoningChannel,
    ReasoningEffort,
    RunStatus,
    RuntimeEventKind,
    TokenMeasurement,
)
from agent_client.domain.errors import AgentError
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.mcp import McpConnectionStatus, McpNamedStatus
from agent_client.domain.models import (
    ApprovalRequest,
    ModelEvent,
    ModelResponse,
    ReasoningBlock,
    ToolCall,
)
from agent_client.domain.presentation import SlashCommand
from agent_client.domain.protocol import (
    ContentType,
    NativeContent,
    NativeFunctionCall,
    NativeMessage,
)
from agent_client.domain.runtime import ModelCompleted
from agent_client.domain.tools import ToolName, WriteFileArguments
from agent_client.infrastructure.mcp.manager import ServerConnection
from agent_client.presentation.composer import COMMANDS, CommandMenu, PromptInput
from agent_client.presentation.status import ContextMeter, WorkIndicator
from agent_client.presentation.transcript import AssistantResponse, TaskTurn, ToolBlock
from agent_client.presentation.tui import AgentApp


class Gateway:
    def __init__(self, *, wait=False):
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.wait = wait
        self.count = 0

    async def stream(self, request):
        self.count += 1
        self.started.set()
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="hello")
        if self.wait:
            await self.gate.wait()
        output = [
            {
                "type": NativeItemType.MESSAGE,
                "role": MessageRole.ASSISTANT,
                "content": [{"type": ContentType.OUTPUT_TEXT, "text": "hello"}],
            }
        ]
        yield ModelEvent(
            kind=ModelEventKind.COMPLETED,
            response=ModelResponse(id=f"r{self.count}", text="hello", output=output),
        )

    async def close(self):
        pass


class ReasoningGateway(Gateway):
    async def stream(self, request):
        self.count += 1
        blocks = [
            ReasoningBlock(
                item_id="reason1", index=0, channel=ReasoningChannel.SUMMARY, text="Read "
            ),
            ReasoningBlock(
                item_id="reason1", index=0, channel=ReasoningChannel.SUMMARY, text="source"
            ),
            ReasoningBlock(
                item_id="reason1", index=1, channel=ReasoningChannel.SUMMARY, text="Check result"
            ),
            ReasoningBlock(
                item_id="reason1", index=0, channel=ReasoningChannel.TEXT, text="Read source"
            ),
        ]
        for block in blocks:
            yield ModelEvent(kind=ModelEventKind.REASONING_DELTA, reasoning=block)
        self.started.set()
        await self.gate.wait()
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="Answer")
        reasoning = [blocks[0].model_copy(update={"text": "Read source"}), *blocks[2:]]
        yield ModelEvent(
            kind=ModelEventKind.COMPLETED,
            response=ModelResponse(
                id="reasoned-answer",
                text="Answer",
                reasoning=reasoning,
                output=[
                    {
                        "type": NativeItemType.MESSAGE,
                        "role": MessageRole.ASSISTANT,
                        "content": [{"type": ContentType.OUTPUT_TEXT, "text": "Answer"}],
                    }
                ],
            ),
        )


class RecoverableGateway(Gateway):
    def __init__(self, *, authorization_failure: bool):
        super().__init__()
        self.authorization_failure = authorization_failure
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        self.started.set()
        if len(self.requests) == 1:
            if self.authorization_failure:
                raise AgentError(ErrorCode.REAUTH_REQUIRED, "Sign in before continuing")
            call = ToolCall(
                id="read-first", name=ToolName.READ_FILE, arguments={"path": "task.txt"}
            )
            yield ModelEvent(
                kind=ModelEventKind.COMPLETED,
                response=ModelResponse(
                    id="tool-first",
                    calls=[call],
                    output=[
                        {
                            "type": NativeItemType.FUNCTION_CALL,
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": '{"path":"task.txt"}',
                        }
                    ],
                ),
            )
            return
        async for event in super().stream(request):
            yield event


class ToolStepGateway(Gateway):
    async def stream(self, request):
        if self.count:
            async for event in super().stream(request):
                yield event
            return
        self.count += 1
        self.started.set()
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="Reading the file first.")
        await self.gate.wait()
        call = ToolCall(id="read-first", name=ToolName.READ_FILE, arguments={"path": "task.txt"})
        yield ModelEvent(
            kind=ModelEventKind.COMPLETED,
            response=ModelResponse(
                id="first-step",
                text="Reading the file first.",
                calls=[call],
                output=[
                    {
                        "type": NativeItemType.MESSAGE,
                        "role": MessageRole.ASSISTANT,
                        "content": [
                            {"type": ContentType.OUTPUT_TEXT, "text": "Reading the file first."}
                        ],
                    },
                    {
                        "type": NativeItemType.FUNCTION_CALL,
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": '{"path":"task.txt"}',
                    },
                ],
            ),
        )


async def services_for(tmp_path, gateway):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    services = ClientServices.build(AppConfig(), tmp_path / "home")
    await services.model.close()
    services.model = gateway
    services.runtime.model = gateway
    await services.open()
    return (services, workspace)


async def test_default_window_is_new_despite_unrelated_invalid_history(tmp_path):
    services, workspace = await services_for(tmp_path, Gateway())
    old_session = await services.store.create_session(workspace)
    old_journal = services.store.home / "sessions" / old_session / "rollout.jsonl"
    old_journal.write_bytes(b"invalid historical record\n")
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(90, 32)) as pilot:
            await app.ready.wait()
            await pilot.pause()
            assert app.session_id != old_session
            assert app.context_usage.used_tokens is None
            assert app.focused is app.query_one("#composer", TextArea)
            assert "Loading" not in str(app.query_one("#account", Static).render())
            assert old_journal.read_bytes() == b"invalid historical record\n"
    finally:
        await services.close()


async def test_streaming_steer_is_durable_and_keeps_same_run_after_restore(tmp_path):
    gateway = Gateway(wait=True)
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(90, 32)) as pilot:
            await app.ready.wait()
            app.query_one("#composer", TextArea).load_text("first\nmultiline task")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), timeout=10)
            await pilot.pause()
            assert app.query(Markdown)
            app.query_one("#composer", TextArea).load_text("second")
            await pilot.press("enter")
            await pilot.pause()
            queued = await services.runtime.pending_inputs(app.session_id)
            assert [p.prompt for p in queued] == ["second"]
            assert queued[0].target_run_id == app.active_run_id
            assert "steer pending" in str(list(app.query(TaskTurn))[1].status_line.render())
            records = await services.store.read(app.session_id)
            assert len([r for r in records if r.type == JournalEventType.PENDING_INPUT]) == 2
            gateway.gate.set()
            await pilot.pause()
            for _ in range(50):
                if not app.processing:
                    break
                await asyncio.sleep(0.02)
            assert gateway.count == 2
            assert await services.runtime.pending_inputs(app.session_id) == []
            records = await services.store.read(app.session_id)
            users = [r for r in records if r.type == JournalEventType.USER_MESSAGE]
            assert len(users) == 2
            assert users[0].run_id == users[1].run_id
            first, second = list(app.query(TaskTurn))
            assert len(first.responses) == len(second.responses) == 1
            identity = app.session_id
        restored = AgentApp(services, workspace, identity)
        async with restored.run_test(size=(90, 32)):
            await restored.ready.wait()
            first, second = list(restored.query(TaskTurn))
            assert len(first.responses) == len(second.responses) == 1
            assert first.has_class(RunStatus.COMPLETED.value)
            assert second.has_class(RunStatus.COMPLETED.value)
    finally:
        await services.close()


async def test_cancelled_run_preserves_pending_steer_for_continue(tmp_path):
    gateway = Gateway(wait=True)
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(90, 32)) as pilot:
            await app.ready.wait()
            composer = app.query_one(PromptInput)
            composer.load_text("first")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            composer.load_text("finish with the revised scope")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            assert [item.prompt for item in app.pending] == ["finish with the revised scope"]
            assert list(app.pending) == await services.runtime.pending_inputs(app.session_id)
            gateway.gate.set()
            composer.load_text("/continue ")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert not app.pending
            records = await services.store.read(app.session_id)
            users = [
                record.payload for record in records if record.type == JournalEventType.USER_MESSAGE
            ]
            assert len(users) == 2
            assert gateway.count == 2
    finally:
        await services.close()


async def test_steer_after_final_race_falls_back_to_new_run(tmp_path):
    gateway = Gateway()
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(90, 32)) as pilot:
            await app.ready.wait()
            composer = app.query_one(PromptInput)
            composer.load_text("first")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            finished_run_id = app.run_turns[0].run_id
            app.active_run_id = finished_run_id
            composer.load_text("arrived after final commit")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert not composer.text
            assert not app.pending
            records = await services.store.read(app.session_id)
            users = [record for record in records if record.type == JournalEventType.USER_MESSAGE]
            assert len(users) == 2
            assert users[0].run_id != users[1].run_id
            assert gateway.count == 2
    finally:
        await services.close()


async def test_reasoning_stream_is_gray_separate_from_answer_and_restored(tmp_path):
    gateway = ReasoningGateway()
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(90, 32)) as pilot:
            await app.ready.wait()
            app.query_one(PromptInput).load_text("Inspect source")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            await app.flush_stream()
            response = app.query_one(AssistantResponse)
            assert response.text == ""
            assert not response.body.display
            assert response.reasoning_view.display
            assert str(response.reasoning_view.render()) == "Read source\n\nCheck result"
            assert response.reasoning_view.styles.color.hex == "#A8ADB5"
            gateway.gate.set()
            await app.workers.wait_for_complete()
            await response.wait_render()
            assert response.text == response.rendered_text == "Answer"
            assert "Read source" not in response.text
            assert len(response.reasoning) == 3
            assert len(app.metrics.reasoning) == 3
            identity = app.session_id
        restored = AgentApp(services, workspace, identity)
        async with restored.run_test(size=(90, 32)) as pilot:
            await restored.ready.wait()
            response = restored.query_one(AssistantResponse)
            await response.wait_render()
            assert response.text == response.rendered_text == "Answer"
            assert str(response.reasoning_view.render()) == "Read source\n\nCheck result"
            assert response.reasoning_view.styles.color.hex == "#A8ADB5"
            assert gateway.count == 1
    finally:
        await services.close()


async def test_tool_step_and_final_step_reasoning_remain_separate_after_restore(tmp_path):
    class MultiStepReasoningGateway(Gateway):
        async def stream(self, request):
            self.count += 1
            self.started.set()
            first = self.count == 1
            block = ReasoningBlock(
                item_id=f"reason-{self.count}",
                index=0,
                channel=ReasoningChannel.SUMMARY,
                text="Inspect the file" if first else "Explain the result",
            )
            yield ModelEvent(kind=ModelEventKind.REASONING_DELTA, reasoning=block)
            response = ModelResponse(
                id=f"response-{self.count}",
                reasoning=[block],
                output=[
                    {
                        "type": NativeItemType.REASONING,
                        "id": block.item_id,
                        "summary": [{"type": ContentType.SUMMARY_TEXT, "text": block.text}],
                    }
                ],
            )
            if first:
                call = ToolCall(
                    id="inspect-file", name=ToolName.READ_FILE, arguments={"path": "task.txt"}
                )
                response.calls = [call]
                response.output.append(
                    NativeFunctionCall(
                        call_id=call.id, name=call.name, arguments='{"path":"task.txt"}'
                    )
                )
            else:
                yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="Final answer")
                response.text = "Final answer"
                response.output.append(
                    NativeMessage(
                        role=MessageRole.ASSISTANT,
                        content=[NativeContent(type=ContentType.OUTPUT_TEXT, text=response.text)],
                    )
                )
            yield ModelEvent(kind=ModelEventKind.COMPLETED, response=response)

    gateway = MultiStepReasoningGateway()
    services, workspace = await services_for(tmp_path, gateway)
    await asyncio.to_thread((workspace / "task.txt").write_text, "File contents", encoding="utf-8")
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(90, 32)) as pilot:
            await app.ready.wait()
            app.query_one(PromptInput).load_text("Read task.txt and explain")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            await app.workers.wait_for_complete()
            responses = list(app.query(AssistantResponse))
            assert len(responses) == 2
            assert [str(response.reasoning_view.render()) for response in responses] == [
                "Inspect the file",
                "Explain the result",
            ]
            assert [response.text for response in responses] == ["", "Final answer"]
            assert all(response.reasoning_view.display for response in responses)
            assert len(app.query(ToolBlock)) == 1
            step_ids = [response.step_id for response in responses]
            assert len(set(step_ids)) == 2
            identity = app.session_id
        restored = AgentApp(services, workspace, identity)
        async with restored.run_test(size=(90, 32)) as pilot:
            await restored.ready.wait()
            responses = list(restored.query(AssistantResponse))
            assert [response.step_id for response in responses] == step_ids
            assert [str(response.reasoning_view.render()) for response in responses] == [
                "Inspect the file",
                "Explain the result",
            ]
            assert [response.text for response in responses] == ["", "Final answer"]
            assert all(response.reasoning_view.display for response in responses)
            assert len(restored.query(ToolBlock)) == 1
            assert gateway.count == 2
    finally:
        await services.close()


async def test_cancel_flushes_pending_reasoning(tmp_path):
    gateway = ReasoningGateway()
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(90, 32)) as pilot:
            await app.ready.wait()
            app.query_one(PromptInput).load_text("Inspect source")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            await app.flush_stream()
            response = app.query_one(AssistantResponse)
            pending = ReasoningBlock(
                item_id="reason1", index=1, channel=ReasoningChannel.SUMMARY, text=" before cancel"
            )
            await app.emit(
                RuntimeEvent(
                    kind=RuntimeEventKind.REASONING_DELTA,
                    run_id=app.run_turns[0].run_id,
                    data=pending,
                )
            )
            assert app.dirty
            assert "before cancel" not in str(response.reasoning_view.render())
            app.action_cancel_run()
            await app.workers.wait_for_complete()
            assert not app.processing
            assert not app.dirty
            assert (
                str(response.reasoning_view.render()) == "Read source\n\nCheck result before cancel"
            )
            assert response.text == ""
            records = await services.store.read(app.session_id)
            assert (
                next(
                    record
                    for record in reversed(records)
                    if record.type == JournalEventType.RUN_FINISHED
                ).payload.status
                == RunStatus.CANCELLED
            )
    finally:
        await services.close()


async def test_streaming_retains_user_scroll_then_follows_when_returned_to_bottom(tmp_path):
    gateway = Gateway(wait=True)
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(80, 26)) as pilot:
            await app.ready.wait()
            app.query_one(PromptInput).load_text("Stream a long response")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            run_id = app.run_turns[0].run_id

            async def append_chunk(start, stop):
                await app.emit(
                    RuntimeEvent(
                        kind=RuntimeEventKind.TEXT_DELTA,
                        run_id=run_id,
                        data={
                            "text": "\n\n"
                            + "\n\n".join(f"Paragraph {index}" for index in range(start, stop))
                        },
                    )
                )
                await app.flush_stream()
                await app.query_one(AssistantResponse).wait_render()
                await pilot.pause()

            await append_chunk(0, 60)
            transcript = app.query_one("#transcript", VerticalScroll)
            assert transcript.max_scroll_y > 40
            assert transcript.is_vertical_scroll_end
            transcript.scroll_to(y=10, animate=False, immediate=True)
            await pilot.pause()
            retained_y = transcript.scroll_y
            old_maximum = transcript.max_scroll_y
            assert not transcript.is_vertical_scroll_end
            await append_chunk(60, 80)
            assert transcript.max_scroll_y > old_maximum
            assert transcript.scroll_y == retained_y
            transcript.scroll_to(y=transcript.max_scroll_y, animate=False, immediate=True)
            await pilot.pause()
            assert transcript.is_vertical_scroll_end
            old_maximum = transcript.max_scroll_y
            await append_chunk(80, 100)
            assert transcript.max_scroll_y > old_maximum
            assert transcript.is_vertical_scroll_end
            app.action_cancel_run()
            await app.workers.wait_for_complete()
    finally:
        await services.close()


async def test_live_deltas_and_input_are_not_blocked_by_markdown_rendering(tmp_path, monkeypatch):
    gateway = Gateway(wait=True)
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    render_started = asyncio.Event()
    release_render = asyncio.Event()
    try:
        async with app.run_test(size=(80, 26)) as pilot:
            await app.ready.wait()
            composer = app.query_one(PromptInput)
            composer.load_text("Stream a response")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            response = app.query_one(AssistantResponse)
            await response.wait_render()
            original_append = response.markdown.append

            async def slow_append(fragment):
                render_started.set()
                await release_render.wait()
                await original_append(fragment)

            monkeypatch.setattr(response.markdown, "append", slow_append)
            run_id = app.run_turns[0].run_id

            async def emit_text(value):
                await app.emit(
                    RuntimeEvent(
                        kind=RuntimeEventKind.TEXT_DELTA,
                        run_id=run_id,
                        data={"text": value},
                    )
                )

            try:
                await asyncio.wait_for(emit_text(" first burst" * 30), 1)
                await asyncio.wait_for(render_started.wait(), 5)
                await asyncio.wait_for(emit_text(" second burst"), 1)
                assert response.text.endswith(" second burst")
                await pilot.press("x")
                assert composer.text == "x"
                assert not release_render.is_set()
            finally:
                release_render.set()
            app.action_cancel_run()
            await app.workers.wait_for_complete()
            assert response.markdown.source == response.text
            assert response.text.endswith(" second burst")
    finally:
        release_render.set()
        await services.close()


async def test_escape_cancels_active_run_and_persists_terminal_state(tmp_path):
    gateway = Gateway(wait=True)
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(70, 24)) as pilot:
            await app.ready.wait()
            app.query_one("#composer", TextArea).load_text("wait for cancellation")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), timeout=10)
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            await pilot.pause()
            records = await services.store.read(app.session_id)
            assert [r for r in records if r.type == JournalEventType.RUN_FINISHED][
                -1
            ].payload.status == RunStatus.CANCELLED
            assert not app.processing
    finally:
        await services.close()


async def test_waiting_indicator_animates_without_estimating_context_from_output(tmp_path):
    class SilentGateway(Gateway):
        async def stream(self, request):
            self.started.set()
            await self.gate.wait()
            async for event in super().stream(request):
                yield event

    gateway = SilentGateway()
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(80, 26)) as pilot:
            await app.ready.wait()
            meter = app.query_one(ContextMeter)
            assert app.context_usage.context_window == 256000
            assert "256k" in str(meter.render())
            assert app.context_usage.used_tokens is None
            app.query_one(PromptInput).load_text("Wait for a response")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            indicator = app.query_one(WorkIndicator)
            assert indicator.working
            assert "Waiting for model" in str(indicator.render())
            first_frame = indicator.frame
            async with asyncio.timeout(10):
                while indicator.frame <= first_frame:
                    await asyncio.sleep(0.025)
            assert indicator.frame > first_frame
            run_id = app.run_turns[0].run_id
            await app.emit(
                RuntimeEvent(
                    kind=RuntimeEventKind.CONTEXT_USAGE,
                    run_id=run_id,
                    data=ContextUsage(
                        used_tokens=4000,
                        context_window=256000,
                        input_budget=services.runtime.context.input_limit,
                        measurement=TokenMeasurement.PROVIDER_INPUT_TOKENS,
                    ),
                )
            )
            base = app.context_usage.used_tokens
            await app.emit(
                RuntimeEvent(
                    kind=RuntimeEventKind.TEXT_DELTA,
                    run_id=run_id,
                    data={"text": "Growing context " * 100},
                )
            )
            assert app.context_usage.used_tokens == base
            assert "%" in str(meter.render())
            assert "~" not in str(meter.render())
            assert "last input" in str(meter.render())
            app.action_cancel_run()
            await app.workers.wait_for_complete()
            assert not indicator.working
            final_frame = indicator.frame
            await asyncio.sleep(0.3)
            assert indicator.frame == final_frame
            assert app.context_usage.used_tokens is None
            await app.command("/new")
            assert app.context_usage.used_tokens is None
    finally:
        await services.close()


async def test_restart_reconciles_pending_input_once(tmp_path):
    gateway = Gateway()
    services, workspace = await services_for(tmp_path, gateway)
    identity = await services.store.create_session(workspace)
    command_id = await services.runtime.enqueue(identity, "accepted but not executed")
    app = AgentApp(services, workspace, identity)
    try:
        async with app.run_test(size=(60, 25)) as pilot:
            await app.ready.wait()
            await pilot.pause()
            assert gateway.count == 0
            assert len(await services.runtime.pending_inputs(identity)) == 1
            app.query_one("#composer", TextArea).load_text("/continue")
            await pilot.press("enter")
            await pilot.pause()
            for _ in range(50):
                if gateway.count and (not app.processing):
                    break
                await asyncio.sleep(0.02)
            records = await services.store.read(identity)
            assert (
                len(
                    [
                        r
                        for r in records
                        if r.type == JournalEventType.USER_MESSAGE
                        and r.payload.command_id == command_id
                    ]
                )
                == 1
            )
            assert gateway.count == 1
    finally:
        await services.close()


@pytest.mark.parametrize("deny_key", ["escape", "enter"])
async def test_approval_defaults_to_deny_and_keyboard_rejects(tmp_path, deny_key):
    services, workspace = await services_for(tmp_path, Gateway())
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(80, 30)) as pilot:
            await app.ready.wait()
            task = asyncio.create_task(
                app.approve(
                    ApprovalRequest(
                        id="a",
                        call_id="c",
                        tool="write_file",
                        description="Replace a file",
                        arguments=WriteFileArguments(
                            path="a.txt", content="x", before_hash="missing"
                        ),
                    )
                )
            )
            await pilot.pause()
            choices = app.query_one("#approval-choices", OptionList)
            assert app.query_one("#approval").display
            assert choices.has_focus
            assert choices.highlighted_option.id == "deny"
            assert not app.query(Button)
            await pilot.press(deny_key)
            assert await task is False
    finally:
        await services.close()


async def test_narrow_screen_keeps_keyboard_input_and_cancel_usable(tmp_path):
    gateway = Gateway(wait=True)
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(38, 24)) as pilot:
            await app.ready.wait()
            await pilot.pause()
            assert not app.query(Button)
            assert app.query_one("#composer", TextArea).region.right <= 38
            assert app.query_one("#composer", TextArea).region.height == 1
            app.query_one("#composer", TextArea).load_text("narrow screen \u4f60\u597d")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            assert not app.processing
    finally:
        await services.close()


async def test_missing_mcp_connection_is_visible_at_startup(tmp_path):
    services, workspace = await services_for(tmp_path, Gateway())
    services.config.mcp.servers.append(
        NamedMcpServer(
            name="mining-news",
            transport=McpTransport.STREAMABLE_HTTP,
            url="http://127.0.0.1:12345/mcp/news/",
            token_env="MINING_SERVICE_TOKEN",
        )
    )
    services.tools.mcp.connections.append(
        ServerConnection(
            name="mining-news",
            config=services.config.mcp.servers[-1],
            queue=asyncio.Queue(),
            status=McpNamedStatus(
                name="mining-news",
                status=McpConnectionStatus.DISCONNECTED,
                error="MCP environment variable is missing or empty: MINING_SERVICE_TOKEN",
            ),
        )
    )
    app = AgentApp(services, workspace)
    try:
        async with app.run_test() as pilot:
            await app.ready.wait()
            await pilot.pause()
            messages = "\n".join(str(widget.render()) for widget in app.query(Static))
            assert "MCP unavailable: mining-news" in messages
            assert "MINING_SERVICE_TOKEN" in messages
            assert app.query_one(PromptInput).has_focus
    finally:
        await services.close()


async def test_usage_missing_stays_unavailable_and_status_shows_logs(tmp_path):
    services, workspace = await services_for(tmp_path, Gateway())
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(60, 25)) as pilot:
            await app.ready.wait()
            metadata = ModelCompleted(
                step_id="step",
                step=0,
                context_epoch=0,
                prefix_revision="revision",
                context_hash="hash",
                estimated_input_tokens=100,
                cache_key="key",
            )
            await app.emit(RuntimeEvent(kind=RuntimeEventKind.MODEL_COMPLETED, data=metadata))
            assert app.metrics.input_tokens is None
            assert app.metrics.cached_input_tokens is None
            assert "unavailable" in str(app.query_one("#usage", Static).content)
            await app.command("/status")
            await pilot.pause()
            transcript = "\n".join(str(widget.content) for widget in app.query(Static))
            assert "runtime.jsonl" in transcript
            assert '"input_tokens":null' in transcript
    finally:
        await services.close()


@pytest.mark.parametrize("authorization_failure", [True, False])
async def test_continue_resumes_consumed_failed_or_budget_limited_input(
    tmp_path, authorization_failure
):
    gateway = RecoverableGateway(authorization_failure=authorization_failure)
    services, workspace = await services_for(tmp_path, gateway)
    await asyncio.to_thread((workspace / "task.txt").write_text, "saved input", encoding="utf-8")
    services.config.runtime.max_model_steps = 1
    identity = await services.store.create_session(workspace)
    command_id = await services.runtime.enqueue(identity, "Finish the original task")

    async def quiet(event: RuntimeEvent):
        pass

    result = await services.run(identity, "Finish the original task", quiet, command_id=command_id)
    assert result.status == (RunStatus.FAILED if authorization_failure else RunStatus.PARTIAL)
    assert await services.runtime.pending_inputs(identity) == []
    app = AgentApp(services, workspace, identity)
    try:
        async with app.run_test(size=(60, 25)) as pilot:
            await app.ready.wait()
            await pilot.pause()
            assert len(gateway.requests) == 1
            services.config.runtime.max_model_steps = 2
            app.query_one("#composer", TextArea).load_text("/continue")
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert len(gateway.requests) == 2
            records = await services.store.read(identity)
            terminal = [
                record for record in records if record.type == JournalEventType.RUN_FINISHED
            ]
            assert terminal[-1].payload.status == RunStatus.COMPLETED
            assert terminal[0].run_id != terminal[-1].run_id
            continued = [
                record for record in records if record.type == JournalEventType.PENDING_INPUT
            ][-1]
            assert continued.payload.continuation_of == terminal[0].run_id
            if not authorization_failure:
                assert any(
                    item.type == NativeItemType.FUNCTION_CALL_OUTPUT
                    for item in gateway.requests[-1].items
                )
            app.query_one("#composer", TextArea).load_text("/continue")
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert len(gateway.requests) == 2
            assert any("No task to continue" in str(widget.content) for widget in app.query(Static))
    finally:
        await services.close()


@pytest.mark.parametrize("send_key", ["enter", "ctrl+enter"])
async def test_composer_sends_multiline_text_once_and_preserves_paste(tmp_path, send_key):
    gateway = Gateway(wait=True)
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(80, 30)) as pilot:
            await app.ready.wait()
            composer = app.query_one(PromptInput)
            await pilot.press("a", "shift+enter", "b", "ctrl+j")
            app.post_message(events.Paste("pasted\nsecond line"))
            await pilot.pause()
            assert composer.text == "a\nb\npasted\nsecond line"
            assert gateway.count == 0
            await pilot.press(send_key)
            await asyncio.wait_for(gateway.started.wait(), timeout=10)
            await pilot.pause()
            assert composer.text == ""
            records = await services.store.read(app.session_id)
            accepted = [r for r in records if r.type == JournalEventType.PENDING_INPUT]
            assert len(accepted) == 1
            assert accepted[0].payload.prompt == "a\nb\npasted\nsecond line"
            await pilot.press("enter")
            assert gateway.count == 1
            await pilot.press("escape")
            await app.workers.wait_for_complete()
    finally:
        await services.close()


async def test_slash_menu_filters_completes_and_executes_without_model_call(tmp_path):
    gateway = Gateway()
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(80, 30)) as pilot:
            await app.ready.wait()
            composer = app.query_one(PromptInput)
            menu = app.query_one(CommandMenu)
            await pilot.press("/")
            assert menu.display
            assert menu.option_count == len(SlashCommand)
            await pilot.press("down", "tab")
            assert composer.text == COMMANDS[1].command.value + " "
            assert not menu.display
            assert composer.has_focus
            composer.load_text("/")
            composer.move_cursor(composer.document.end)
            await pilot.press("s", "t", "t", "s")
            assert menu.selected_command == SlashCommand.STATUS
            assert menu.option_count == 1
            await pilot.press("enter")
            assert composer.text == "/status "
            assert gateway.count == 0
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert composer.text == ""
            assert any("runtime.jsonl" in str(widget.content) for widget in app.query(Static))
            composer.load_text("/config")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert composer.text == ""
            assert gateway.count == 0
    finally:
        await services.close()


async def test_slash_menu_empty_results_arguments_and_narrow_layout(tmp_path):
    services, workspace = await services_for(tmp_path, Gateway())
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(38, 24)) as pilot:
            await app.ready.wait()
            composer = app.query_one(PromptInput)
            menu = app.query_one(CommandMenu)
            composer.load_text("/zzzz")
            await pilot.pause()
            assert menu.display
            assert menu.selected_command is None
            assert menu.options[0].disabled
            assert composer.region.height == 1
            assert composer.region.bottom <= 24
            assert not app.query(Button)
            await pilot.press("escape")
            assert not menu.display
            assert composer.text == "/zzzz"
            composer.load_text("/model model-name")
            await pilot.pause()
            assert not menu.display
            composer.load_text("ordinary /status text")
            await pilot.pause()
            assert not menu.display
            composer.load_text("/sta")
            composer.move_cursor(composer.document.end)
            await pilot.pause()
            assert menu.selected_command == SlashCommand.STATUS
            await pilot.press("backspace", "backspace")
            assert menu.option_count > 1
            composer.load_text("")
            await pilot.pause()
            assert not menu.display
    finally:
        await services.close()


async def test_escape_closes_menu_before_cancelling_active_run(tmp_path):
    gateway = Gateway(wait=True)
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(80, 30)) as pilot:
            await app.ready.wait()
            app.query_one(PromptInput).load_text("wait")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            await pilot.press("/")
            assert app.command_menu.display
            await pilot.press("escape")
            assert not app.command_menu.display
            assert app.processing
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            assert not app.processing
    finally:
        await services.close()


async def test_composer_grows_with_content_and_shrinks_after_clear(tmp_path):
    services, workspace = await services_for(tmp_path, Gateway())
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(80, 30)) as pilot:
            await app.ready.wait()
            composer = app.query_one(PromptInput)
            await pilot.pause()
            assert composer.region.height == 1
            await pilot.press("a", "shift+enter", "b")
            await pilot.pause()
            assert composer.region.height == 2
            composer.load_text("\n".join("line" for _ in range(20)))
            await pilot.pause()
            assert composer.region.height == 8
            composer.load_text("")
            await pilot.pause()
            assert composer.region.height == 1
    finally:
        await services.close()


async def test_inline_approval_allows_by_keyboard_and_restores_composer(tmp_path):
    services, workspace = await services_for(tmp_path, Gateway())
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(80, 30)) as pilot:
            await app.ready.wait()
            task = asyncio.create_task(
                app.approve(
                    ApprovalRequest(
                        id="allow",
                        call_id="write",
                        tool="write_file",
                        description="Write task.txt",
                        arguments=WriteFileArguments(
                            path="task.txt", content="x", before_hash="missing"
                        ),
                    )
                )
            )
            await pilot.pause()
            approval = app.query_one("#approval")
            composer = app.query_one(PromptInput)
            assert approval.region.bottom <= composer.region.y
            assert composer.disabled
            assert not app.query(Button)
            await pilot.press("down", "enter")
            assert await asyncio.wait_for(task, 10)
            await pilot.pause()
            assert not approval.display
            assert not composer.disabled
            assert composer.has_focus
    finally:
        await services.close()


async def test_permissions_picker_and_command_change_policy_without_model(tmp_path):
    gateway = Gateway()
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(80, 30)) as pilot:
            await app.ready.wait()
            composer = app.query_one(PromptInput)
            composer.load_text("/permissions")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            picker = app.query_one("#permissions", OptionList)
            assert picker.display
            assert picker.has_focus
            assert picker.highlighted_option.id == ApprovalMode.ASK.value
            await pilot.press("down", "enter")
            await pilot.pause()
            assert services.config.runtime.approval_mode == ApprovalMode.NEVER
            assert not picker.display
            assert composer.has_focus
            composer.load_text("/permissions read_only")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert services.config.runtime.approval_mode == ApprovalMode.READ_ONLY
            assert gateway.count == 0
    finally:
        await services.close()


async def test_reasoning_picker_and_command_change_actual_request_effort(tmp_path):
    gateway = RecoverableGateway(authorization_failure=False)
    services, workspace = await services_for(tmp_path, gateway)
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(80, 30)) as pilot:
            await app.ready.wait()
            app.query_one(PromptInput).load_text("/reasoning")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            picker = app.query_one("#reasoning-levels", OptionList)
            assert picker.display
            assert picker.has_focus
            await pilot.press("down", "enter")
            await app.workers.wait_for_complete()
            assert services.config.model.reasoning_effort == ReasoningEffort.HIGH
            assert not picker.display
            assert app.query_one(PromptInput).has_focus
            await app.command("/reasoning low")
            await app.workers.wait_for_complete()
            app.query_one(PromptInput).load_text("Respond briefly")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            await app.workers.wait_for_complete()
            assert gateway.requests[0].reasoning_effort == ReasoningEffort.LOW
    finally:
        await services.close()


async def test_model_steps_tools_and_queued_task_keep_boundaries_after_restore(tmp_path):
    gateway = ToolStepGateway()
    services, workspace = await services_for(tmp_path, gateway)
    await asyncio.to_thread((workspace / "task.txt").write_text, "File contents", encoding="utf-8")
    app = AgentApp(services, workspace)
    try:
        async with app.run_test(size=(90, 32)) as pilot:
            await app.ready.wait()
            composer = app.query_one(PromptInput)
            composer.load_text("Read task.txt and explain it")
            await pilot.press("enter")
            await asyncio.wait_for(gateway.started.wait(), 10)
            await pilot.pause()
            composer.load_text("Second task")
            await pilot.press("tab")
            await pilot.pause()
            assert len(app.query(TaskTurn)) == 2
            gateway.gate.set()
            await app.workers.wait_for_complete()
            assert gateway.count == 3
            first, second = list(app.query(TaskTurn))
            assert first.prompt == "Read task.txt and explain it"
            assert second.prompt == "Second task"
            assert len(first.responses) == 2
            assert len(second.responses) == 1
            assert [response.text for response in first.responses] == [
                "Reading the file first.",
                "hello",
            ]
            assert len(first.query(AssistantResponse)) == 2
            assert len(first.tools) == 1
            tool = next(tool for tool in first.tools if tool.call_id == "read-first")
            assert len(first.query(ToolBlock)) == 1
            assert tool.tool_name == ToolName.READ_FILE
            assert "File contents" in str(tool.output.render())
            identity = app.session_id
            boundaries = [
                (
                    turn.command_id,
                    tuple(response.step_id for response in turn.responses),
                    tuple(tool.call_id for tool in turn.tools),
                )
                for turn in (first, second)
            ]
        restored = AgentApp(services, workspace, identity)
        async with restored.run_test(size=(90, 32)) as pilot:
            await restored.ready.wait()
            await pilot.pause()
            turns = list(restored.query(TaskTurn))
            assert [
                (
                    turn.command_id,
                    tuple(response.step_id for response in turn.responses),
                    tuple(tool.call_id for tool in turn.tools),
                )
                for turn in turns
            ] == boundaries
            assert [response.text for response in turns[0].responses] == [
                "Reading the file first.",
                "hello",
            ]
            assert (
                next(tool for tool in turns[0].tools if tool.call_id == "read-first").tool_name
                == ToolName.READ_FILE
            )
            assert all(
                "writing" not in str(response.heading.render())
                for response in restored.query(AssistantResponse)
            )
            assert gateway.count == 3
    finally:
        await services.close()
