from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from agent_client.domain.base import Contract


class ProcessChannel(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class PlatformKind(StrEnum):
    WINDOWS = "nt"
    POSIX = "posix"


class RipgrepRecordType(StrEnum):
    MATCH = "match"
    BEGIN = "begin"
    END = "end"
    SUMMARY = "summary"


class FileVersion(StrEnum):
    MISSING = "missing"


class FileReadResult(Contract):
    path: str
    sha256: str
    total_lines: int = Field(ge=0)
    content: str
    truncated: bool = False
    applicable_instructions: str | None = None


class FileWriteResult(Contract):
    path: str
    before_hash: str
    sha256: str
    diff: str
    before_artifact_id: str | None = None
    after_artifact_id: str | None = None
    applicable_instructions: str | None = None


class ProcessResult(Contract):
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    truncated: bool = False
    timed_out: bool = False
    stdout_bytes: int = Field(default=0, ge=0)
    stderr_bytes: int = Field(default=0, ge=0)
    full_output_truncated: bool = False
    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    process_handle: str | None = None
    tool_call_id: str | None = None
    running: bool = False
    cancelled: bool = False
    side_effect_result_unknown: bool = False
    error: str | None = None
    notice: str | None = None
    applicable_instructions: str | None = None


class StreamCapture(Contract):
    content: str
    truncated: bool
    total_bytes: int = Field(ge=0)
    disk_truncated: bool


class SearchPosition(Contract):
    text: str | None = None
    bytes: str | None = None

    @model_validator(mode="after")
    def validate_encoding(self):
        if (self.text is None) == (self.bytes is None):
            raise ValueError("Search position requires exactly one text or bytes value")
        return self


class SearchSubmatch(Contract):
    match: SearchPosition
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_span(self):
        if self.end < self.start:
            raise ValueError("Search submatch end must not precede start")
        return self


class TextMatch(Contract):
    path: SearchPosition
    lines: SearchPosition
    line_number: int = Field(ge=1)
    absolute_offset: int = Field(ge=0)
    submatches: list[SearchSubmatch]


class RipgrepMatchRecord(Contract):
    type: Literal[RipgrepRecordType.MATCH]
    data: TextMatch


class SearchDuration(Contract):
    secs: int = Field(ge=0)
    nanos: int = Field(ge=0)
    human: str


class SearchStats(Contract):
    elapsed: SearchDuration
    searches: int = Field(ge=0)
    searches_with_match: int = Field(ge=0)
    bytes_searched: int = Field(ge=0)
    bytes_printed: int = Field(ge=0)
    matched_lines: int = Field(ge=0)
    matches: int = Field(ge=0)


class SearchBegin(Contract):
    path: SearchPosition


class SearchEnd(SearchBegin):
    binary_offset: int | None = Field(default=None, ge=0)
    stats: SearchStats


class SearchSummary(Contract):
    elapsed_total: SearchDuration
    stats: SearchStats


class RipgrepBeginRecord(Contract):
    type: Literal[RipgrepRecordType.BEGIN]
    data: SearchBegin


class RipgrepEndRecord(Contract):
    type: Literal[RipgrepRecordType.END]
    data: SearchEnd


class RipgrepSummaryRecord(Contract):
    type: Literal[RipgrepRecordType.SUMMARY]
    data: SearchSummary


type RipgrepRecord = (
    RipgrepMatchRecord | RipgrepBeginRecord | RipgrepEndRecord | RipgrepSummaryRecord
)


class FileListResult(ProcessResult):
    files: list[str]


class TextSearchResult(ProcessResult):
    matches: list[TextMatch]


class InstructionDocument(Contract):
    path: Path
    sha256: str
    content: str


class InstructionSnapshot(Contract):
    documents: list[InstructionDocument] = Field(default_factory=list)

    def render(self) -> str:
        return "\n\n".join(
            f"Instructions: {document.path} (sha256={document.sha256})\n{document.content}"
            for document in self.documents
        )


class InstructionRules(Contract):
    content: str
    changed: bool
