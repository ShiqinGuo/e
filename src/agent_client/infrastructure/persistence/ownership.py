import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from agent_client.domain.enums import ErrorCode
from agent_client.domain.errors import AgentError


@asynccontextmanager
async def maintenance_gate(home: Path) -> AsyncIterator[None]:
    await asyncio.to_thread(home.mkdir, parents=True, exist_ok=True)
    lock = FileLock(str(home / "maintenance.lock"), thread_local=False)
    try:
        await asyncio.to_thread(lock.acquire, timeout=0)
    except Timeout as error:
        raise AgentError(
            ErrorCode.SESSION_BUSY, "Client maintenance is already in progress"
        ) from error
    try:
        yield
    finally:
        await asyncio.to_thread(lock.release)


async def acquire_client_lease(home: Path) -> FileLock:
    directory = home / "clients"
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    lease = FileLock(str(directory / f"{uuid4().hex}.lock"), thread_local=False)
    await asyncio.to_thread(lease.acquire, timeout=0)
    return lease


@asynccontextmanager
async def offline_maintenance(home: Path) -> AsyncIterator[None]:
    async with maintenance_gate(home):
        locks: list[FileLock] = []
        paths = await asyncio.to_thread(
            lambda: (
                list((home / "clients").glob("*.lock"))
                + list((home / "sessions").glob("*/execution.lock"))
            )
        )
        try:
            for path in sorted(paths):
                lock = FileLock(str(path), thread_local=False)
                try:
                    await asyncio.to_thread(lock.acquire, timeout=0)
                except Timeout as error:
                    raise AgentError(
                        ErrorCode.SESSION_BUSY,
                        "Close all active clients and running sessions before maintenance",
                    ) from error
                if path.parent == home / "clients":
                    locks.append(lock)
                else:
                    await asyncio.to_thread(lock.release)
            yield
        finally:
            for lock in reversed(locks):
                await asyncio.to_thread(lock.release)
