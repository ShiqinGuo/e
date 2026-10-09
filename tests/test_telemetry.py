import json

from agent_client.domain.enums import ErrorCode, RuntimeEventKind, ToolStatus
from agent_client.domain.events import RuntimeEvent
from agent_client.domain.models import ToolResult
from agent_client.domain.runtime import ToolFinished
from agent_client.domain.tools import ToolErrorContent
from agent_client.infrastructure.observability.telemetry import Telemetry


async def test_diagnostic_log_omits_model_text_arguments_and_credentials(tmp_path):
    telemetry = Telemetry(tmp_path)
    await telemetry.start()
    await telemetry.emit(
        RuntimeEvent(
            kind=RuntimeEventKind.TOOL_FINISHED,
            session_id="session",
            run_id="run",
            data=ToolFinished(
                result=ToolResult(
                    call_id="call", content=ToolErrorContent(error="private source secret")
                )
            ),
        )
    )
    await telemetry.emit(
        RuntimeEvent(kind=RuntimeEventKind.TEXT_DELTA, data={"text": "private content"})
    )
    await telemetry.close()
    lines = (tmp_path / "logs" / "runtime.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["details"] == {
        "call_id": "call",
        "status": ToolStatus.SUCCEEDED,
        "is_error": False,
    }
    assert "private" not in lines[0] and "secret" not in lines[0]


async def test_error_category_is_retained_without_private_message(tmp_path):
    telemetry = Telemetry(tmp_path)
    await telemetry.start()
    await telemetry.emit(
        RuntimeEvent(
            kind=RuntimeEventKind.ERROR,
            data={"code": ErrorCode.UNKNOWN_OUTCOME, "message": "private command contents"},
        )
    )
    await telemetry.close()
    content = (tmp_path / "logs" / "runtime.jsonl").read_text(encoding="utf-8")
    assert json.loads(content)["details"]["error_code"] == ErrorCode.UNKNOWN_OUTCOME
    assert "private" not in content
