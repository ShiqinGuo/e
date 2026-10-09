import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent_client.application.instructions import scoped_path
from agent_client.domain.skills import (
    SkillEntry,
    SkillLoadResult,
    SkillMetadata,
    SkillResourceResult,
)


def skill_metadata(body: str) -> SkillMetadata:
    if not body.startswith("---\n"):
        raise ValueError("Skill metadata is missing")
    try:
        metadata = SkillMetadata.model_validate(yaml.safe_load(body.split("---", 2)[1]))
    except yaml.YAMLError as error:
        raise ValueError(f"Malformed skill metadata: {error}") from error
    return metadata


@dataclass
class LoadedSkill:
    identity: str
    digest: str


@dataclass(frozen=True)
class SkillLimits:
    depth: int = 5
    count: int = 1000
    document_bytes: int = 262144
    resource_bytes: int = 1048576


class SkillCatalog:
    def __init__(self, roots: list[Path]):
        self.roots = roots
        self.entries: list[SkillEntry] = []
        self.loaded: list[LoadedSkill] = []
        self.limits = SkillLimits()

    def require_entry(self, identity: str) -> SkillEntry:
        for entry in self.entries:
            if entry.id == identity:
                return entry
        raise KeyError(identity)

    def _discover(self) -> list[SkillEntry]:
        entries: list[SkillEntry] = []
        for index, configured_root in enumerate(self.roots):
            root = configured_root.expanduser().resolve()
            if not root.is_dir():
                continue
            paths: list[Path] = []
            for directory, children, names in os.walk(root, followlinks=False):
                children[:] = sorted(
                    child
                    for child in children
                    if child not in {".git", "node_modules", ".venv", "__pycache__"}
                )
                if len(Path(directory).relative_to(root).parts) >= self.limits.depth:
                    children.clear()
                if "SKILL.md" in names:
                    paths.append(Path(directory) / "SKILL.md")
                if len(paths) >= self.limits.count:
                    break
            for discovered in sorted(paths):
                relative = discovered.relative_to(root)
                if len(relative.parts) > self.limits.depth + 1 or len(entries) >= self.limits.count:
                    continue
                identity = f"r{index}/{relative.as_posix()}"
                try:
                    path = scoped_path(root, str(relative))
                    if path.stat().st_size > self.limits.document_bytes:
                        raise ValueError("Skill exceeds 256 KiB")
                    body = path.read_text(encoding="utf-8")
                    metadata = skill_metadata(body)
                    entries.append(
                        SkillEntry(
                            id=identity,
                            name=metadata.name,
                            description=metadata.description,
                            path=path,
                            hash=hashlib.sha256(body.encode()).hexdigest(),
                        )
                    )
                except (OSError, UnicodeError, ValueError) as error:
                    entries.append(SkillEntry(id=identity, error=str(error)))
        return entries

    async def scan(self) -> None:
        self.entries = await asyncio.to_thread(self._discover)

    def search(self, query: str) -> list[SkillEntry]:
        words = query.casefold().split()
        return [
            entry
            for entry in sorted(self.entries, key=lambda item: item.id)
            if all(
                word in f"{entry.id} {entry.name} {entry.description}".casefold() for word in words
            )
        ]

    async def load(self, identity: str) -> SkillLoadResult:
        entry = self.require_entry(identity)
        if entry.error or entry.path is None:
            raise ValueError(entry.error or "Skill path is unavailable")
        path = entry.path
        if await asyncio.to_thread(lambda: path.stat().st_size) > self.limits.document_bytes:
            raise ValueError("Skill exceeds 256 KiB")
        body = await asyncio.to_thread(path.read_text, encoding="utf-8")
        metadata = skill_metadata(body)
        digest = hashlib.sha256(body.encode()).hexdigest()
        entry.name = metadata.name
        entry.description = metadata.description
        entry.hash = digest
        loaded = next((item for item in self.loaded if item.identity == identity), None)
        repeated = loaded is not None and loaded.digest == digest
        if loaded is None:
            self.loaded.append(LoadedSkill(identity, digest))
        else:
            loaded.digest = digest
        return SkillLoadResult(id=identity, hash=digest, already_loaded=repeated, content=body)

    async def resource(self, identity: str, relative: str) -> SkillResourceResult:
        entry = self.require_entry(identity)
        if entry.path is None:
            raise ValueError("Skill path is unavailable")
        path = scoped_path(entry.path.parent, relative)
        if await asyncio.to_thread(lambda: path.stat().st_size) > self.limits.resource_bytes:
            raise ValueError("Resource exceeds 1 MiB")
        body = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return SkillResourceResult(content=body, sha256=hashlib.sha256(body.encode()).hexdigest())
