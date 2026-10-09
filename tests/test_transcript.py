import asyncio
from time import monotonic

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

from agent_client.domain.enums import ReasoningChannel, RunStatus, ToolStatus
from agent_client.domain.mcp import McpContent, McpContentType, McpToolResult
from agent_client.domain.models import Effect, ReasoningBlock, ToolCall, ToolResult
from agent_client.domain.tools import ToolDispatchEvent, ToolName, ToolOutputRange
from agent_client.domain.workspace import FileWriteResult, ProcessResult
from agent_client.presentation.transcript import TaskTurn, bounded_text, render_diff


class TranscriptApp(App):
    def compose(self) -> ComposeResult:
        yield TaskTurn("command-1", "Change a file", 1)


class ScrollTranscriptApp(App):
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="transcript"):
            yield Static("Earlier content\n" * 30)
            yield TaskTurn("command-1", "Inspect output", 1)
            yield Static("Later content\n" * 30)


@pytest.mark.parametrize("body_offset", [(2, 0), (7, 3), (20, 7)])
async def test_tool_output_title_and_body_click_toggle_without_layout_gaps(body_offset):
    app = TranscriptApp()
    async with app.run_test(size=(80, 32)) as pilot:
        tool = await app.query_one(TaskTurn).tool_finished(
            ToolResult(
                call_id="read",
                content=ToolOutputRange(
                    total_characters=100, content="\n".join(f"Line {i}" for i in range(12))
                ),
            )
        )
        await pilot.pause()
        title = tool.details.query_one("CollapsibleTitle")
        assert tool.details.collapsed
        assert tool.details.region.height == title.region.height == 1
        assert title.region.y == tool.heading.region.bottom
        assert await pilot.click(title)
        await pilot.pause()
        assert not tool.details.collapsed
        assert tool.output.region.y == title.region.bottom
        assert tool.details.query_one(Collapsible.Contents).styles.padding.top == 0
        assert tool.details.query_one(Collapsible.Contents).styles.padding.bottom == 0
        assert await pilot.click(tool.output, offset=body_offset, button=3)
        assert not tool.details.collapsed
        assert await pilot.click(tool.output, offset=body_offset)
        await pilot.pause()
        assert tool.details.collapsed
        assert await pilot.click(title)
        assert not tool.details.collapsed
        assert await pilot.click(title)
        assert tool.details.collapsed


async def test_dragging_tool_output_preserves_expanded_text_selection():
    app = TranscriptApp()
    async with app.run_test(size=(80, 32)) as pilot:
        tool = await app.query_one(TaskTurn).tool_finished(
            ToolResult(
                call_id="read",
                content=ToolOutputRange(
                    total_characters=100, content="Selectable output text\nSecond line"
                ),
            )
        )
        await pilot.pause()
        await pilot.click(tool.details.query_one("CollapsibleTitle"))
        await pilot.mouse_down(tool.output, offset=(1, 0))
        await pilot.hover(tool.output, offset=(10, 1))
        await pilot.mouse_up(tool.output, offset=(10, 1))
        await pilot.pause()
        assert not tool.details.collapsed
        assert str(tool.output.render()).startswith("content:")
        assert app.screen.get_selected_text()


async def test_collapsing_long_output_returns_only_its_title_to_view():
    app = ScrollTranscriptApp()
    async with app.run_test(size=(80, 25)) as pilot:
        tool = await app.query_one(TaskTurn).tool_finished(
            ToolResult(
                call_id="read",
                content=ToolOutputRange(
                    total_characters=100, content="\n".join(f"Line {i}" for i in range(100))
                ),
            )
        )
        transcript = app.query_one("#transcript", VerticalScroll)
        title = tool.details.query_one("CollapsibleTitle")
        await pilot.pause()
        title.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(title)
        await pilot.pause()
        transcript.scroll_to(y=tool.output.virtual_region.y + 60, animate=False, immediate=True)
        await pilot.pause()
        assert title.region.y < transcript.content_region.y
        point = (tool.output.region.x + 5, transcript.content_region.y + 3)
        assert app.get_widget_at(*point)[0] is tool.output
        assert await pilot.click(offset=point)
        await pilot.pause()
        assert tool.details.collapsed
        assert transcript.content_region.contains(*title.region.offset)
        assert transcript.scroll_y > 0
        assert not transcript.is_vertical_scroll_end


def test_diff_preserves_real_changes_and_bounds_preview():
    rendered = render_diff("--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n+new\n context")
    assert "Changes: +1 / -1" in rendered.plain
    assert (
        next(
            rendered.plain[span.start : span.end]
            for span in rendered.spans
            if str(span.style) == "red"
        )
        == "-old\n"
    )
    assert (
        next(
            rendered.plain[span.start : span.end]
            for span in rendered.spans
            if str(span.style) == "green"
        )
        == "+new\n"
    )
    assert any(
        "context" in rendered.plain[span.start : span.end]
        for span in rendered.spans
        if str(span.style) == "dim"
    )
    assert "    1       │ -old" in rendered.plain
    assert "          1 │ +new" in rendered.plain
    assert "Preview truncated" in bounded_text("x\n" * 121)


@pytest.mark.asyncio
async def test_turn_groups_distinct_responses_and_tool_results():
    app = TranscriptApp()
    async with app.run_test():
        turn = app.query_one(TaskTurn)
        first = await turn.add_response("step-1")
        first.set_text("First response")
        first.finish()
        assert await turn.add_response("step-1") is first
        second = await turn.add_response("step-2")
        assert first is not second
        assert first.number == 1
        assert second.number == 2
        tool = await turn.tool_started(
            ToolDispatchEvent(call_id="call-1", name=ToolName.APPLY_PATCH, effect=Effect.WRITE)
        )
        await turn.tool_finished(
            ToolResult(
                call_id="call-1",
                content=FileWriteResult(
                    before_hash="missing",
                    sha256="test-hash",
                    path="example.py",
                    diff="--- a/example.py\n+++ b/example.py\n-old\n+new",
                ),
            )
        )
        assert tool.tool_name == "apply_patch"
        assert tool.details.collapsed
        assert tool.summary.display
        assert "-old" in str(tool.summary.render())
        assert tool.has_class("success")
        turn.set_status(RunStatus.COMPLETED)
        assert not turn.border_title
        assert "completed" in str(turn.status_line.render())
        assert "writing" not in str(second.heading.render())
        assert str(second.heading.render()) == "•"


@pytest.mark.asyncio
async def test_proposed_tools_wait_until_their_own_dispatch():
    app = TranscriptApp()
    async with app.run_test():
        turn = app.query_one(TaskTurn)
        first = await turn.ensure_tool("first", ToolName.CALL_MCP_TOOL)
        second = await turn.ensure_tool("second", ToolName.CALL_MCP_TOOL)
        assert "queued" in str(first.heading.render())
        assert "queued" in str(second.heading.render())
        started = await turn.tool_started(
            ToolDispatchEvent(call_id="first", name=ToolName.CALL_MCP_TOOL, effect=Effect.REMOTE)
        )
        assert started is first
        assert "working" in str(first.heading.render())
        assert "queued" in str(second.heading.render())


@pytest.mark.asyncio
async def test_unknown_outcome_and_artifacts_are_visible():
    app = TranscriptApp()
    async with app.run_test():
        turn = app.query_one(TaskTurn)
        tool = await turn.tool_finished(
            ToolResult(
                call_id="unknown",
                status=ToolStatus.UNKNOWN,
                content=ProcessResult(
                    error="Connection lost", stdout_artifact_id="saved-output", truncated=True
                ),
                artifact_id="full-result",
            )
        )
        assert tool.details.collapsed
        assert "Connection lost" in str(tool.summary.render())
        assert "saved-output" not in str(tool.summary.render())
        rendered = tool.output.render()
        assert "Connection lost" in str(rendered)
        assert "saved-output" in str(rendered)
        assert "full-result" in str(rendered)


@pytest.mark.asyncio
async def test_nested_protocol_content_uses_visible_raw_details():
    app = TranscriptApp()
    async with app.run_test():
        turn = app.query_one(TaskTurn)
        tool = await turn.ensure_tool("mcp", ToolName.CALL_MCP_TOOL)
        tool.set_call(
            ToolCall(
                id="mcp",
                name=ToolName.RUN_COMMAND,
                arguments={"command": "rg 'test' src", "cwd": "src"},
            )
        )
        assert "rg 'test' src" in str(tool.heading.render())
        assert "src" in str(tool.heading.render())
        tool.set_waiting()
        await turn.tool_finished(
            ToolResult(
                call_id="mcp",
                content=McpToolResult(content=[McpContent(type=McpContentType.TEXT, text="Reply")]),
            )
        )
        rendered = str(tool.output.render())
        assert "Raw protocol details" in rendered
        assert "Reply" in rendered
        assert tool.details.collapsed
        assert not tool.summary.display


@pytest.mark.asyncio
async def test_default_diff_and_failure_are_compact_without_metadata():
    app = TranscriptApp()
    async with app.run_test():
        turn = app.query_one(TaskTurn)
        diff = "--- a/file\n+++ b/file\n@@ -1,20 +1,20 @@\n" + "-old\n+new\n" * 20
        tool = await turn.ensure_tool("edit", ToolName.APPLY_PATCH)
        await turn.tool_finished(
            ToolResult(
                call_id="edit",
                content=FileWriteResult(
                    before_hash="missing",
                    sha256="test-hash",
                    path="file",
                    diff=diff,
                    after_artifact_id="private-metadata",
                ),
            )
        )
        assert "Edit file" in str(tool.heading.render())
        summary = str(tool.summary.render())
        assert len(summary.splitlines()) <= 14
        assert "private-metadata" not in summary
        assert "More changes in details" in summary
        failed = await turn.tool_finished(
            ToolResult(
                call_id="fail",
                is_error=True,
                content=ProcessResult(
                    stderr="\n".join(f"line-{i}" for i in range(100)), exit_code=1
                ),
            )
        )
        assert len(str(failed.summary.render()).splitlines()) <= 3
        assert failed.details.collapsed


def test_diff_treats_triple_signs_inside_hunk_as_source_lines():
    diff = "--- a.txt\n+++ a.txt\n@@ -1 +1 @@\n--- old\n+++ new\n"
    rendered = render_diff(diff)
    assert "Changes: +1 / -1" in rendered.plain
    assert any(
        span.style == "green" and "+ new" in rendered.plain[span.start : span.end]
        for span in rendered.spans
    )
    assert any(
        span.style == "red" and "- old" in rendered.plain[span.start : span.end]
        for span in rendered.spans
    )


async def test_stream_appends_without_replacing_completed_markdown_blocks(monkeypatch):
    app = TranscriptApp()
    async with app.run_test() as pilot:
        response = await app.query_one(TaskTurn).add_response("stream")
        response.set_text("# Completed heading\n\nFirst paragraph.\n\n")
        await response.wait_render()
        heading = response.markdown.query_one("MarkdownH1")
        updates = []
        original = response.markdown.update

        def capture(text):
            updates.append(text)
            return original(text)

        monkeypatch.setattr(response.markdown, "update", capture)
        for fragment in ("Second ", "paragraph", ".\n\n", "- Item one\n", "- Item two"):
            response.set_text(response.text + fragment)
            await response.wait_render()
        await pilot.pause()
        assert response.markdown.query_one("MarkdownH1") is heading
        assert not updates
        assert response.markdown.source == response.text


async def test_fast_updates_coalesce_into_one_serial_renderer(monkeypatch):
    app = TranscriptApp()
    async with app.run_test():
        response = await app.query_one(TaskTurn).add_response("stream")
        started = asyncio.Event()
        released = asyncio.Event()
        fragments = []
        original = response.markdown.append

        async def slow_append(fragment):
            fragments.append(fragment)
            started.set()
            await released.wait()
            await original(fragment)

        monkeypatch.setattr(response.markdown, "append", slow_append)
        response.set_text("first")
        await asyncio.wait_for(started.wait(), 5)
        for index in range(1, 100):
            response.set_text("first" + "x" * index)
        released.set()
        await response.wait_render()
        assert fragments == ["first", "x" * 99]
        assert response.markdown.source == "first" + "x" * 99


async def test_reasoning_is_separate_muted_and_summary_preferred():
    app = TranscriptApp()
    async with app.run_test() as pilot:
        response = await app.query_one(TaskTurn).add_response("reasoning")
        response.append_reasoning(
            ReasoningBlock(
                item_id="r1", index=0, channel=ReasoningChannel.SUMMARY, text="Checking "
            )
        )
        response.append_reasoning(
            ReasoningBlock(
                item_id="r1", index=0, channel=ReasoningChannel.SUMMARY, text="the boundary."
            )
        )
        response.append_reasoning(
            ReasoningBlock(
                item_id="r1", index=0, channel=ReasoningChannel.TEXT, text="Alternate provider text"
            )
        )
        response.flush_reasoning()
        response.set_text("The answer.")
        await response.wait_render()
        await pilot.pause()
        assert response.reasoning_view.display
        assert str(response.reasoning_view.render()) == "Checking the boundary."
        assert response.text == "The answer."
        assert response.markdown.source == "The answer."
        assert response.reasoning_view.styles.color.hex == "#A8ADB5"
        assert not app.query_one(TaskTurn).border_title


async def test_animated_burst_reveals_intermediate_unicode_frames_and_finishes_promptly(
    monkeypatch,
):
    app = TranscriptApp()
    async with app.run_test():
        response = await app.query_one(TaskTurn).add_response("animated")
        cluster = "你👨‍👩‍👧‍👦🇨🇳e\u0301👍🏽"
        target = cluster * 180
        boundaries = {
            len(cluster) * index + suffix for index in range(180) for suffix in (1, 8, 10, 12, 14)
        }
        frames: list[str] = []
        original = response.markdown.append

        async def capture(fragment):
            await original(fragment)
            frames.append(response.markdown.source)

        monkeypatch.setattr(response.markdown, "append", capture)
        started = monotonic()
        response.set_text(target, animate=True)
        await asyncio.wait_for(response.wait_render(), 0.8)
        assert monotonic() - started < 0.8
        assert len(frames) > 2
        assert 0 < len(frames[0]) < len(target)
        assert all(target.startswith(frame) and len(frame) in boundaries for frame in frames)
        assert response.markdown.source == response.rendered_text == target


async def test_live_body_and_reasoning_growth_share_serial_renderer_without_lost_text(monkeypatch):
    app = TranscriptApp()
    async with app.run_test():
        response = await app.query_one(TaskTurn).add_response("growing")
        original = response.markdown.append
        concurrent = 0
        maximum = 0
        frames: list[str] = []

        async def capture(fragment):
            nonlocal concurrent, maximum
            concurrent += 1
            maximum = max(maximum, concurrent)
            await asyncio.sleep(0.008)
            await original(fragment)
            frames.append(response.markdown.source)
            concurrent -= 1

        monkeypatch.setattr(response.markdown, "append", capture)
        text = ""
        reasoning = ""
        for index in range(80):
            text += f"答案{index}🙂 "
            reasoning += f"步骤{index} "
            response.set_text(text, animate=True)
            response.append_reasoning(
                ReasoningBlock(
                    item_id="growing",
                    index=0,
                    channel=ReasoningChannel.SUMMARY,
                    text=f"步骤{index} ",
                )
            )
            response.flush_reasoning(animate=True)
            await asyncio.sleep(0.001)
        await asyncio.wait_for(response.wait_render(), 0.8)
        assert maximum == 1
        assert frames == sorted(frames, key=len)
        assert all(text.startswith(frame) for frame in frames)
        assert response.markdown.source == response.rendered_text == text
        assert response.rendered_reasoning == reasoning
        assert str(response.reasoning_view.render()) == reasoning


async def test_reasoning_burst_has_multiple_frames_and_unchanged_targets_do_not_redraw(monkeypatch):
    app = TranscriptApp()
    async with app.run_test():
        response = await app.query_one(TaskTurn).add_response("thinking")
        target = "检查边界与结果🙂 " * 120
        frames: list[str] = []
        original = response.reasoning_view.update

        def capture(value):
            frames.append(value)
            return original(value)

        monkeypatch.setattr(response.reasoning_view, "update", capture)
        block = ReasoningBlock(
            item_id="thinking", index=0, channel=ReasoningChannel.SUMMARY, text=target
        )
        response.set_reasoning([block], animate=True)
        await asyncio.wait_for(response.wait_render(), 0.8)
        assert len(frames) > 2
        assert 0 < len(frames[0]) < len(target)
        assert frames[-1] == target
        previous_frames = list(frames)
        response.flush_reasoning(animate=True)
        response.set_reasoning([block], animate=False)
        await response.wait_render()
        assert frames == previous_frames


async def test_static_restore_and_cancel_catch_up_without_replaying_characters(monkeypatch):
    app = TranscriptApp()
    async with app.run_test():
        response = await app.query_one(TaskTurn).add_response("restore")
        target = "完整恢复内容🙂 " * 100
        appended: list[str] = []
        original = response.markdown.append

        async def capture(fragment):
            appended.append(fragment)
            await original(fragment)

        monkeypatch.setattr(response.markdown, "append", capture)
        response.set_text(target)
        await response.wait_render()
        assert appended == [target]
        target += "继续流式内容 " * 100
        response.set_text(target, animate=True)
        await asyncio.sleep(0.02)
        response.set_text(target, animate=False)
        await asyncio.wait_for(response.wait_render(), 0.2)
        assert response.markdown.source == target


async def test_slow_markdown_drawing_catches_up_without_an_unbounded_frame_queue(monkeypatch):
    app = TranscriptApp()
    async with app.run_test():
        response = await app.query_one(TaskTurn).add_response("slow")
        original = response.markdown.append
        frames: list[str] = []

        async def slow(fragment):
            await asyncio.sleep(0.04)
            await original(fragment)
            frames.append(response.markdown.source)

        monkeypatch.setattr(response.markdown, "append", slow)
        target = "Long buffered output " * 200
        started = monotonic()
        response.set_text(target, animate=True)
        await asyncio.wait_for(response.wait_render(), 0.65)
        assert monotonic() - started < 0.65
        assert 2 <= len(frames) <= 9
        assert response.markdown.source == target


async def test_nonprefix_correction_replaces_animated_target_without_stale_suffix(monkeypatch):
    app = TranscriptApp()
    async with app.run_test():
        response = await app.query_one(TaskTurn).add_response("correction")
        original = response.markdown.append
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed(fragment):
            started.set()
            await release.wait()
            await original(fragment)

        monkeypatch.setattr(response.markdown, "append", delayed)
        response.set_text("Old buffered text " * 80, animate=True)
        await asyncio.wait_for(started.wait(), 2)
        response.set_text("Corrected answer🙂", animate=True)
        release.set()
        await asyncio.wait_for(response.wait_render(), 0.3)
        assert response.markdown.source == "Corrected answer🙂"
