import asyncio
import hashlib
from pathlib import Path

from agent_client.domain.workspace import InstructionDocument, InstructionSnapshot


def scoped_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path escapes the allowed root")
    return path


class InstructionResolver:
    def __init__(self, personal: Path | None = None):
        self.personal = personal

    async def resolve(self, workspace: Path, target: Path | None = None) -> InstructionSnapshot:
        root = workspace.resolve()
        destination = (target or root).resolve()
        if not destination.is_relative_to(root):
            raise ValueError("Instruction target outside workspace")
        directory = destination if destination.is_dir() else destination.parent
        directories = [root]
        if directory != root:
            directories.extend(
                reversed(
                    [
                        parent
                        for parent in directory.parents
                        if parent.is_relative_to(root) and parent != root
                    ]
                )
            )
            directories.append(directory)
        paths = ([self.personal] if self.personal else []) + [
            directory / "AGENTS.md" for directory in directories
        ]
        documents: list[InstructionDocument] = []
        for path in paths:
            if path and await asyncio.to_thread(path.is_file):
                body = await asyncio.to_thread(path.read_text, encoding="utf-8")
                digest = hashlib.sha256(body.encode()).hexdigest()
                documents.append(InstructionDocument(path=path, sha256=digest, content=body))
        return InstructionSnapshot(documents=documents)
