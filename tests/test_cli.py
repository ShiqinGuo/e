import asyncio
import json
from pathlib import Path

from agent_client.domain.auth import AuthState
from agent_client.domain.enums import JournalEventType, RunStatus
from agent_client.domain.persistence import (
    MaintenanceState,
    ProjectionRebuildResult,
    SessionDeletionResult,
)
from agent_client.infrastructure.persistence.store import SessionStore
from agent_client.presentation.cli import main, parser


async def create_saved_session(home: Path, workspace: Path) -> str:
    store = await SessionStore(home).open()
    try:
        return await store.create_session(workspace)
    finally:
        await store.close()


def test_auth_status_without_credentials_does_not_start_login(tmp_path, capsys):
    assert main(["--home", str(tmp_path / "home"), "auth", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {"status": AuthState.SIGNED_OUT}
    assert not (tmp_path / "home" / "auth" / "credentials.bin").exists()


def test_sessions_and_diagnostics_are_usable_without_auth(tmp_path, capsys):
    home = str(tmp_path / "home")
    assert main(["--home", home, "sessions"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["--home", home, "diagnostics"]) == 0
    diagnostics = json.loads(capsys.readouterr().out)
    assert diagnostics["account"]["status"] == AuthState.SIGNED_OUT
    assert diagnostics["credentials_included"] is False
    assert diagnostics["usage"] is None


def test_default_command_opens_tui_and_resume_has_optional_prompt():
    assert parser().parse_args([]).command is None
    args = parser().parse_args(["resume", "session"])
    assert args.session_id == "session" and args.prompt is None
    assert parser().parse_args(["continue", "session"]).session_id == "session"


def test_continue_cli_idle_does_not_start_model_request(tmp_path, capsys):
    home = tmp_path / "home"
    session = asyncio.run(create_saved_session(home, tmp_path))
    assert main(["--home", str(home), "continue", session]) == 0
    assert "No task to continue" in capsys.readouterr().out
    records = [
        json.loads(line)
        for line in (home / "sessions" / session / "rollout.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert not any(record["type"] == JournalEventType.RUN_STARTED for record in records)


def test_continue_cli_failed_auth_creates_new_run_without_replaying_prior_run(tmp_path, capsys):
    home = tmp_path / "home"
    assert main(["--home", str(home), "--workspace", str(tmp_path), "run", "Finish work"]) == 1
    capsys.readouterr()
    journal = next(home.glob("sessions/*/rollout.jsonl"))
    assert main(["--home", str(home), "continue", journal.parent.name]) == 1
    captured = capsys.readouterr()
    assert AuthState.REAUTH_REQUIRED in captured.err
    records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    terminals = [record for record in records if record["type"] == JournalEventType.RUN_FINISHED]
    assert len(terminals) == 2
    assert terminals[-1]["payload"]["status"] == RunStatus.FAILED
    assert terminals[0]["run_id"] != terminals[-1]["run_id"]


def test_headless_missing_auth_records_failed_run_and_returns_nonzero(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    assert main(["--home", str(home), "--workspace", str(workspace), "run", "Read project"]) == 1
    captured = capsys.readouterr()
    assert AuthState.REAUTH_REQUIRED in captured.err
    journals = list(home.glob("sessions/*/rollout.jsonl"))
    records = [json.loads(line) for line in journals[0].read_text(encoding="utf-8").splitlines()]
    assert records[-1]["type"] == JournalEventType.RUN_FINISHED
    assert records[-1]["payload"]["stop_reason"] == AuthState.REAUTH_REQUIRED


def test_rebuild_cli_recovers_corrupt_database_before_normal_open(tmp_path, capsys):
    home = tmp_path / "home"
    session = asyncio.run(create_saved_session(home, tmp_path))
    (home / "state.sqlite").write_bytes(b"corrupt projection")
    assert main(["--home", str(home), "rebuild"]) == 0
    result = ProjectionRebuildResult.model_validate_json(capsys.readouterr().out)
    assert result.state == MaintenanceState.COMPLETED
    assert result.session_count == 1
    assert result.backup_directory.is_dir()
    assert main(["--home", str(home), "sessions"]) == 0
    assert [row["id"] for row in json.loads(capsys.readouterr().out)] == [session]


def test_delete_cli_requires_explicit_command_and_preserves_trash(tmp_path, capsys):
    home = tmp_path / "home"
    session = asyncio.run(create_saved_session(home, tmp_path))
    assert main(["--home", str(home), "delete", session]) == 0
    result = SessionDeletionResult.model_validate_json(capsys.readouterr().out)
    assert result.state == MaintenanceState.COMPLETED
    assert result.trash_directory.is_dir()
    assert main(["--home", str(home), "sessions"]) == 0
    assert json.loads(capsys.readouterr().out) == []


async def test_delete_cli_refuses_live_client_without_removing_session(tmp_path, capsys):
    home = tmp_path / "home"
    store = await SessionStore(home).open()
    try:
        session = await store.create_session(tmp_path)
        result = await asyncio.to_thread(main, ["--home", str(home), "delete", session])
        assert result == 1
        assert "session_busy" in capsys.readouterr().err
        assert (await store.read(session))[0].type == JournalEventType.SESSION_CREATED
    finally:
        await store.close()
