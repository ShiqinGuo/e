from pathlib import Path

from pydantic import Field

from agent_client.domain.base import Contract
from agent_client.domain.protocol import ProtocolObject


class SkillMetadata(Contract):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: str | None = Field(default=None, alias="allowed-tools")
    metadata: ProtocolObject | None = None


class SkillEntry(Contract):
    id: str
    name: str = ""
    description: str = ""
    path: Path | None = None
    hash: str | None = None
    error: str | None = None


class SkillLoadResult(Contract):
    id: str
    hash: str
    already_loaded: bool
    content: str
    snapshot_artifact_id: str | None = None


class SkillResourceResult(Contract):
    content: str
    sha256: str
    snapshot_artifact_id: str | None = None
