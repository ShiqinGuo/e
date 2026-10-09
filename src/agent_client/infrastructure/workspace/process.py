import asyncio
import codecs
import os
import re
import signal
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import TypedDict

from agent_client.domain.workspace import PlatformKind, ProcessChannel, ProcessResult, StreamCapture

type ProcessOutputSink = Callable[[ProcessChannel, str], Awaitable[None]]


async def await_process_completion[T](task: asyncio.Future[T]) -> T:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class ProcessOptions(TypedDict, total=False):
    creationflags: int
    start_new_session: bool


@dataclass(frozen=True)
class CaptureLimits:
    chunk_bytes: int = 8192
    disk_bytes: int = 67108864
    printable_start: int = 32


class ProcessCreationFlag(IntEnum):
    SUSPENDED = 4


class ProcessRunner:
    def __init__(self, limits: CaptureLimits = CaptureLimits()):
        self.limits = limits

    async def run(
        self,
        args: list[str],
        cwd: Path,
        timeout: float = 60,
        limit: int = 1048576,
        on_output: ProcessOutputSink | None = None,
        capture_dir: Path | None = None,
    ) -> ProcessResult:
        started = False

        async def owned() -> ProcessResult:
            nonlocal started
            started = True
            return await self._owned_run(args, cwd, timeout, limit, on_output, capture_dir)

        task = asyncio.create_task(owned())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            if not started:
                await asyncio.gather(task, return_exceptions=True)
                return ProcessResult(
                    cancelled=True, notice="Command cancelled before process launch"
                )
            return await await_process_completion(task)

    async def _owned_run(
        self,
        args: list[str],
        cwd: Path,
        timeout: float = 60,
        limit: int = 1048576,
        on_output: ProcessOutputSink | None = None,
        capture_dir: Path | None = None,
    ) -> ProcessResult:
        job = None
        if PlatformKind(os.name) == PlatformKind.WINDOWS:
            from agent_client.infrastructure.workspace.windows_job import WindowsJob

            job = WindowsJob()
        options: ProcessOptions = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | ProcessCreationFlag.SUSPENDED}
            if job
            else {"start_new_session": True}
        )
        launching = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **options,
            )
        )
        cancelled = False
        try:
            process = await asyncio.shield(launching)
        except asyncio.CancelledError:
            process = await launching
            if job:
                try:
                    await asyncio.to_thread(job.assign_and_resume, process.pid, False)
                except BaseException:
                    job.close()
                    await self.terminate(process)
                    raise
            cancelled = True
        except Exception:
            if job:
                job.close()
            raise
        if job and not cancelled:
            assignment = asyncio.create_task(asyncio.to_thread(job.assign_and_resume, process.pid))
            try:
                await asyncio.shield(assignment)
            except asyncio.CancelledError:
                await await_process_completion(assignment)
                cancelled = True
            except BaseException:
                job.close()
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise

        async def drain(stream: asyncio.StreamReader, channel: ProcessChannel) -> StreamCapture:
            chunks = []
            size = 0
            truncated = False
            total = 0
            captured = 0
            disk_truncated = False
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            spool = (
                await asyncio.to_thread((capture_dir / channel).open, "wb") if capture_dir else None
            )
            try:
                while chunk := await stream.read(self.limits.chunk_bytes):
                    remaining = limit - size
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                    size += min(len(chunk), max(0, remaining))
                    truncated |= len(chunk) > remaining
                    text = decoder.decode(chunk)
                    if spool and captured + len(text.encode("utf-8")) > self.limits.disk_bytes:
                        disk_truncated = True
                    if spool and captured < self.limits.disk_bytes:
                        encoded = text.encode("utf-8")
                        remaining_disk = self.limits.disk_bytes - captured
                        saved = (
                            encoded[:remaining_disk]
                            .decode("utf-8", errors="ignore")
                            .encode("utf-8")
                        )
                        await asyncio.to_thread(spool.write, saved)
                        captured += len(encoded)
                    total += len(chunk)
                    if on_output:
                        text = re.sub(
                            r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", text
                        )
                        text = "".join(
                            char
                            for char in text
                            if char in "\n\r\t" or ord(char) >= self.limits.printable_start
                        )
                        await on_output(channel, text)
                tail = decoder.decode(b"", final=True)
                if tail and spool and captured < self.limits.disk_bytes:
                    await asyncio.to_thread(spool.write, tail.encode("utf-8"))
                    captured += len(tail.encode("utf-8"))
                return StreamCapture(
                    content=b"".join(chunks).decode("utf-8", errors="replace"),
                    truncated=truncated,
                    total_bytes=total,
                    disk_truncated=disk_truncated,
                )
            finally:
                if spool:
                    await asyncio.to_thread(spool.close)

        readers = [
            asyncio.create_task(drain(process.stdout, ProcessChannel.STDOUT)),
            asyncio.create_task(drain(process.stderr, ProcessChannel.STDERR)),
        ]
        completion = asyncio.gather(process.wait(), *readers)
        timed_out = False
        try:
            if cancelled:
                if job:
                    job.close()
                await self.terminate(process)
            async with asyncio.timeout(timeout):
                results = await asyncio.shield(completion)
                streams = results[1:]
        except (TimeoutError, asyncio.CancelledError) as error:
            if job:
                job.close()
            await self.terminate(process)
            streams = await asyncio.gather(*readers)
            await completion
            if isinstance(error, asyncio.CancelledError):
                cancelled = True
            else:
                timed_out = True
        except Exception:
            if job:
                job.close()
            await self.terminate(process)
            await asyncio.gather(*readers, return_exceptions=True)
            raise
        finally:
            if job:
                job.close()
        return ProcessResult(
            stdout=streams[0].content,
            stderr=streams[1].content,
            exit_code=process.returncode,
            truncated=any(stream.truncated for stream in streams),
            timed_out=timed_out,
            cancelled=cancelled,
            notice="Command cancelled; any effects before termination remain"
            if cancelled
            else None,
            stdout_bytes=streams[0].total_bytes,
            stderr_bytes=streams[1].total_bytes,
            full_output_truncated=any(stream.disk_truncated for stream in streams),
        )

    async def terminate(self, process):
        match PlatformKind(os.name):
            case PlatformKind.WINDOWS:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            case _:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await process.wait()
