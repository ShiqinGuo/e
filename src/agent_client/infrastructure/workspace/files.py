import difflib
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_client.application.instructions import scoped_path
from agent_client.domain.workspace import FileReadResult, FileVersion, FileWriteResult


@dataclass(frozen=True)
class FileLimits:
    maximum_bytes: int = 8388608
    maximum_line_span: int = 2000


DEFAULT_FILE_LIMITS = FileLimits()


def read_file(root: Path, relative: str, start: int = 1, end: int = 400) -> FileReadResult:
    path = scoped_path(root, relative)
    if start > end or end - start > DEFAULT_FILE_LIMITS.maximum_line_span:
        raise ValueError("Invalid line range; maximum 2001 lines")
    if path.stat().st_size > DEFAULT_FILE_LIMITS.maximum_bytes:
        raise ValueError("File exceeds 8 MiB")
    raw = path.read_bytes()
    if len(raw) > DEFAULT_FILE_LIMITS.maximum_bytes:
        raise ValueError("File exceeds 8 MiB")
    lines = raw.decode("utf-8").splitlines()
    return FileReadResult(
        path=relative,
        sha256=hashlib.sha256(raw).hexdigest(),
        total_lines=len(lines),
        content="\n".join(
            f"{number}: {line}" for number, line in enumerate(lines[start - 1 : end], start)
        ),
        truncated=end < len(lines),
    )


def write_file(root: Path, relative: str, content: str, before_hash: str) -> FileWriteResult:
    path = scoped_path(root, relative)
    old = path.read_bytes() if path.exists() else b""
    actual = hashlib.sha256(old).hexdigest() if path.exists() else FileVersion.MISSING
    if actual != before_hash:
        raise ValueError(f"File conflict: expected {before_hash}, actual {actual}")
    new = content.encode("utf-8")
    if len(new) > DEFAULT_FILE_LIMITS.maximum_bytes:
        raise ValueError("File write exceeds 8 MiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode)
        current = path.read_bytes() if path.exists() else None
        if current != (old if actual != FileVersion.MISSING else None):
            raise ValueError("File changed during write preparation")
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    diff = "".join(
        difflib.unified_diff(
            old.decode("utf-8").splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=relative,
            tofile=relative,
        )
    )
    return FileWriteResult(
        path=relative, before_hash=actual, sha256=hashlib.sha256(new).hexdigest(), diff=diff
    )


def apply_patch(
    root: Path, relative: str, before_hash: str, old_text: str, new_text: str
) -> FileWriteResult:
    body = scoped_path(root, relative).read_text(encoding="utf-8")
    if not old_text or body.count(old_text) != 1:
        raise ValueError("Patch old_text must match exactly once")
    return write_file(root, relative, body.replace(old_text, new_text, 1), before_hash)
