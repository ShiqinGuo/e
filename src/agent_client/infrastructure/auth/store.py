import asyncio
import ctypes
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from filelock import FileLock, Timeout

from agent_client.domain.auth import AuthRecord
from agent_client.domain.enums import ErrorCode
from agent_client.domain.errors import AgentError
from agent_client.domain.workspace import PlatformKind


@dataclass(frozen=True)
class CredentialStorageDefaults:
    directory_mode: int = 448
    lock_poll_seconds: float = 0.05
    no_interactive_protection: int = 1
    host_uuid_version: int = 4


def protect(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != PlatformKind.WINDOWS:
        return data

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    if not function(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        CredentialStorageDefaults().no_interactive_protection,
        ctypes.byref(result),
    ):
        raise AgentError(ErrorCode.CREDENTIAL_STORE, "Credential protection failed")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


class CredentialStore:
    def __init__(self, home: Path):
        self.directory = home / "auth"
        self.path = self.directory / "credentials.bin"

    def initialize(self):
        self.directory.mkdir(
            parents=True, exist_ok=True, mode=CredentialStorageDefaults().directory_mode
        )
        if os.name != PlatformKind.WINDOWS:
            self.directory.chmod(CredentialStorageDefaults().directory_mode)

    @asynccontextmanager
    async def locked(self):
        await asyncio.to_thread(self.initialize)
        lock = FileLock(self.directory / "account.lock", thread_local=False)
        while True:
            acquisition = asyncio.create_task(asyncio.to_thread(lock.acquire, timeout=0))
            try:
                await asyncio.shield(acquisition)
                break
            except asyncio.CancelledError:
                try:
                    await acquisition
                except Timeout:
                    pass
                else:
                    await asyncio.to_thread(lock.release)
                raise
            except Timeout:
                await asyncio.sleep(CredentialStorageDefaults().lock_poll_seconds)
        try:
            yield
        finally:
            await asyncio.to_thread(lock.release)

    def read(self) -> AuthRecord | None:
        if not self.path.exists():
            return None
        try:
            return AuthRecord.model_validate_json(protect(self.path.read_bytes(), decrypt=True))
        except (OSError, ValueError):
            raise AgentError(
                ErrorCode.CREDENTIAL_STORE, "Credential store cannot be read"
            ) from None

    def write(self, data: AuthRecord):
        self.initialize()
        raw = protect(data.model_dump_json().encode())
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=".credentials-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            if os.name != PlatformKind.WINDOWS:
                self.path.chmod(0o600)
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def host_id(self) -> str:
        self.initialize()
        path = self.directory / "host-id"
        if not path.exists():
            with path.open("x", encoding="utf-8") as stream:
                stream.write(f"urn:uuid:{uuid4()}")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != PlatformKind.WINDOWS:
                path.chmod(0o600)
        value = path.read_text(encoding="utf-8").strip()
        try:
            parsed = UUID(value.removeprefix("urn:uuid:"))
            if (
                value != f"urn:uuid:{parsed}"
                or parsed.version != CredentialStorageDefaults().host_uuid_version
            ):
                raise ValueError
        except ValueError:
            raise AgentError(
                ErrorCode.HOST_ID_INVALID, "Saved authorization host identity is invalid"
            ) from None
        return value
