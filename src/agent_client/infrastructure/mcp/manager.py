import asyncio
import hashlib
import json
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TypedDict

import httpx
from jsonschema import validate
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT
from pydantic import BaseModel, SecretStr

from agent_client.domain.configuration import McpConfig, McpServerConfig
from agent_client.domain.enums import McpTransport
from agent_client.domain.mcp import (
    McpCacheMode,
    McpConnectionStatus,
    McpNamedStatus,
    McpNegotiationMode,
    McpOperation,
    McpOperationResult,
    McpPromptPage,
    McpPromptResult,
    McpRequestArguments,
    McpResourcePage,
    McpResourceResult,
    McpToolEntry,
    McpToolResult,
)
from agent_client.domain.protocol import ProtocolObject
from agent_client.infrastructure.mcp.environment import MissingMcpEnvironment, resolve_environment


class AuthorizationHeaders(TypedDict, total=False):
    Authorization: str


@dataclass(frozen=True)
class DirectoryLimits:
    maximum_pages: int = 100


@dataclass
class Request:
    operation: McpOperation
    arguments: McpRequestArguments
    future: asyncio.Future[McpOperationResult]


@dataclass
class SecretBinding:
    reference: str
    secret: SecretStr


@dataclass
class ServerConnection:
    name: str
    config: McpServerConfig
    queue: asyncio.Queue[Request | None]
    task: asyncio.Task[None] | None = None
    status: McpNamedStatus | None = None
    secrets: list[SecretBinding] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SchemaChanged(ValueError):
    pass


class McpManager:
    def __init__(self, config: McpConfig):
        self.config = config
        self.connections: list[ServerConnection] = []
        self.tools: list[McpToolEntry] = []

    @property
    def status(self) -> list[McpNamedStatus]:
        return [
            connection.status for connection in self.connections if connection.status is not None
        ]

    def connection(self, name: str) -> ServerConnection:
        connection = next(
            (connection for connection in self.connections if connection.name == name), None
        )
        if connection is None:
            raise ValueError("MCP server is not configured")
        return connection

    def tool(self, identity: str) -> McpToolEntry:
        entry = next((tool for tool in self.tools if tool.id == identity), None)
        if entry is None:
            raise ValueError("MCP tool is not discovered; search tools again")
        return entry

    def replace_tools(self, server: str, entries: list[McpToolEntry]) -> None:
        self.tools = [tool for tool in self.tools if tool.server != server] + entries

    async def start(self) -> None:
        for config in self.config.servers:
            name = config.name
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("MCP server names allow only letters, digits, underscore and dash")
            ready = asyncio.get_running_loop().create_future()
            queue: asyncio.Queue[Request | None] = asyncio.Queue()
            connection = ServerConnection(name=name, config=config, queue=queue)
            self.connections.append(connection)
            connection.task = asyncio.create_task(self._owner(name, config, queue, ready))
            try:
                await asyncio.wait_for(asyncio.shield(ready), config.timeout_seconds)
            except Exception:
                ready.cancel()
                connection.task.cancel()
                await asyncio.gather(connection.task, return_exceptions=True)
                if connection.status is None:
                    connection.status = McpNamedStatus(
                        name=name,
                        status=McpConnectionStatus.DISCONNECTED,
                        error="connection_timeout",
                    )
                self.replace_tools(name, [])
                if config.required:
                    await self.close()
                    raise RuntimeError(
                        f"Required MCP server failed: {name}; {connection.status.error}"
                    ) from None

    async def _directory(self, client: Client, server: str) -> list[McpToolEntry]:
        cursor = None
        seen: set[str] = set()
        entries: list[McpToolEntry] = []
        for _ in range(DirectoryLimits().maximum_pages):
            page = await client.list_tools(cursor=cursor, cache_mode=McpCacheMode.REFRESH)
            for tool in page.tools:
                schema_hash = hashlib.sha256(
                    (
                        ProtocolObject.model_validate(tool.input_schema).root
                        + str(
                            tool.annotations is not None and tool.annotations.read_only_hint is True
                        )
                    ).encode()
                ).hexdigest()
                entries.append(
                    self._sanitize(
                        McpToolEntry(
                            id=f"{server}/{tool.name}",
                            server=server,
                            name=tool.name,
                            description=tool.description or "",
                            parameters=tool.input_schema,
                            schema_hash=schema_hash,
                            read_only_hint=tool.annotations is not None
                            and tool.annotations.read_only_hint is True,
                        ),
                        McpToolEntry,
                        self.connection(server).config,
                    )
                )
            cursor = page.next_cursor
            if cursor is None:
                return entries
            if cursor in seen:
                raise ValueError("MCP pagination repeated cursor")
            seen.add(cursor)
        raise ValueError("MCP pagination exceeds 100 pages")

    async def _owner(
        self,
        name: str,
        config: McpServerConfig,
        queue: asyncio.Queue[Request | None],
        ready: asyncio.Future[None],
    ) -> None:
        request: Request | None = None
        try:
            references = {binding.reference for binding in config.env}
            if config.token_env:
                references.add(config.token_env)
            environment: list[SecretBinding] = []
            for reference in sorted(references):
                environment.append(SecretBinding(reference, await resolve_environment(reference)))
            self.connection(name).secrets = environment
            async with AsyncExitStack() as stack:
                match config.transport:
                    case McpTransport.STDIO:
                        if config.command is None:
                            raise ValueError("MCP stdio command is required")
                        server = StdioServerParameters(
                            command=config.command,
                            args=config.args,
                            env={
                                binding.name: next(
                                    value.secret.get_secret_value()
                                    for value in environment
                                    if value.reference == binding.reference
                                )
                                for binding in config.env
                            },
                        )
                    case McpTransport.STREAMABLE_HTTP:
                        if config.url is None:
                            raise ValueError("MCP HTTP URL is required")
                        headers: AuthorizationHeaders = (
                            AuthorizationHeaders(
                                Authorization=f"Bearer {next(value.secret.get_secret_value() for value in environment if value.reference == config.token_env)}"
                            )
                            if config.token_env
                            else AuthorizationHeaders()
                        )
                        http = await stack.enter_async_context(
                            httpx.AsyncClient(headers=headers, timeout=config.timeout_seconds)
                        )
                        server = config.url
                        if headers:

                            @asynccontextmanager
                            async def transport():
                                async with streamable_http_client(
                                    config.url, http_client=http
                                ) as streams:
                                    yield streams[0], streams[1]

                            server = transport()
                client = await stack.enter_async_context(
                    Client(
                        server,
                        mode=McpNegotiationMode.AUTO,
                        read_timeout_seconds=config.timeout_seconds,
                    )
                )
                self.connection(name).status = McpNamedStatus(
                    name=name,
                    status=McpConnectionStatus.CONNECTED,
                    protocol_version=client.protocol_version,
                )
                self.replace_tools(name, await self._directory(client, name))
                ready.set_result(None)
                while request := await queue.get():
                    if request.future.cancelled():
                        continue
                    try:
                        async with asyncio.timeout(config.timeout_seconds):
                            result = await self._dispatch(client, name, request, config)
                        if not request.future.done():
                            request.future.set_result(result)
                    except Exception as error:
                        disconnected = (
                            isinstance(error, TimeoutError)
                            or isinstance(error, MCPError)
                            and error.code in {CONNECTION_CLOSED, REQUEST_TIMEOUT}
                        )
                        if disconnected:
                            self.connection(name).status = McpNamedStatus(
                                name=name,
                                status=McpConnectionStatus.DISCONNECTED,
                                error=type(error).__name__,
                            )
                        if not request.future.done():
                            failure = (
                                ValueError(str(error))
                                if isinstance(error, SchemaChanged)
                                else RuntimeError(f"MCP call failed ({type(error).__name__})")
                            )
                            request.future.set_exception(failure)
                        if disconnected:
                            raise
        except Exception as error:
            detail = (
                str(error) if isinstance(error, MissingMcpEnvironment) else type(error).__name__
            )
            self.connection(name).status = McpNamedStatus(
                name=name, status=McpConnectionStatus.DISCONNECTED, error=detail
            )
            if not ready.done():
                ready.set_exception(RuntimeError(f"MCP connection failed ({detail})"))
        finally:
            connection = self.connection(name)
            if (
                connection.status is None
                or connection.status.status == McpConnectionStatus.CONNECTED
            ):
                connection.status = McpNamedStatus(
                    name=name, status=McpConnectionStatus.DISCONNECTED, error="connection_closed"
                )
            if request is not None and not request.future.done():
                request.future.set_exception(RuntimeError("MCP connection closed during request"))
            while not queue.empty():
                pending = queue.get_nowait()
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(
                        ValueError("MCP connection closed before request dispatch")
                    )
            if not ready.done():
                ready.set_exception(RuntimeError("MCP connection closed before initialization"))

    async def _disconnect(self, connection: ServerConnection) -> None:
        task = connection.task
        if task is not None and not task.done():
            task.cancel()
            done, _ = await asyncio.wait([task], timeout=connection.config.timeout_seconds)
            if not done:
                raise RuntimeError("MCP connection did not close within its timeout")
        if task is not None and task.done():
            await asyncio.gather(task, return_exceptions=True)

    async def _connect(self, connection: ServerConnection) -> None:
        async with connection.lock:
            if connection.task is not None and not connection.task.done():
                if (
                    connection.status is not None
                    and connection.status.status == McpConnectionStatus.CONNECTED
                ):
                    return
                await self._disconnect(connection)
            ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            connection.queue = asyncio.Queue()
            connection.task = asyncio.create_task(
                self._owner(connection.name, connection.config, connection.queue, ready)
            )
            try:
                async with asyncio.timeout(connection.config.timeout_seconds):
                    await ready
            except (TimeoutError, RuntimeError):
                await self._disconnect(connection)
                raise ValueError("MCP server could not reconnect") from None
            except asyncio.CancelledError:
                await self._disconnect(connection)
                raise

    def _sanitize[T: BaseModel](
        self, result: BaseModel, model: type[T], config: McpServerConfig
    ) -> T:
        body = result.model_dump_json(by_alias=False)
        references = [binding.reference for binding in config.env] + (
            [config.token_env] if config.token_env else []
        )
        for connection in self.connections:
            for binding in connection.secrets:
                if binding.reference in references:
                    body = body.replace(
                        json.dumps(binding.secret.get_secret_value(), ensure_ascii=False)[1:-1],
                        "[REDACTED]",
                    )
        return model.model_validate_json(body)

    async def _dispatch(
        self, client: Client, server: str, request: Request, config: McpServerConfig
    ) -> McpOperationResult:
        arguments = request.arguments
        match request.operation:
            case McpOperation.CALL:
                directory = await self._directory(client, server)
                self.replace_tools(server, directory)
                refreshed = next(
                    (entry for entry in directory if entry.name == arguments.name), None
                )
                if refreshed is None:
                    raise SchemaChanged("MCP tool removed; search tools again")
                if refreshed.schema_hash != arguments.schema_hash:
                    raise SchemaChanged("MCP schema changed; search tools again")
                if request.future.cancelled():
                    raise SchemaChanged("MCP request cancelled before dispatch")
                result = await client.call_tool(refreshed.name, arguments.arguments.wire_value())
                return self._sanitize(result, McpToolResult, config)
            case McpOperation.LIST_RESOURCES:
                result = await client.list_resources(cursor=arguments.cursor)
                return self._sanitize(result, McpResourcePage, config)
            case McpOperation.READ_RESOURCE:
                if arguments.uri is None:
                    raise ValueError("Resource URI is required")
                result = await client.read_resource(arguments.uri)
                return self._sanitize(result, McpResourceResult, config)
            case McpOperation.LIST_PROMPTS:
                result = await client.list_prompts(cursor=arguments.cursor)
                return self._sanitize(result, McpPromptPage, config)
            case McpOperation.GET_PROMPT:
                if arguments.name is None:
                    raise ValueError("Prompt name is required")
                result = await client.get_prompt(arguments.name, arguments.arguments.wire_value())
                return self._sanitize(result, McpPromptResult, config)

    def search(self, query: str) -> list[McpToolEntry]:
        return [
            tool
            for tool in sorted(self.tools, key=lambda entry: entry.id)
            if all(
                word in f"{tool.id} {tool.description}".casefold()
                for word in query.casefold().split()
            )
        ]

    async def request(
        self, server: str, operation: McpOperation, arguments: McpRequestArguments
    ) -> McpOperationResult:
        connection = self.connection(server)
        await self._connect(connection)
        future: asyncio.Future[McpOperationResult] = asyncio.get_running_loop().create_future()
        await connection.queue.put(Request(operation=operation, arguments=arguments, future=future))
        try:
            async with asyncio.timeout(connection.config.timeout_seconds):
                return await future
        except (TimeoutError, asyncio.CancelledError):
            await self._disconnect(connection)
            raise

    async def call(self, identity: str, arguments: McpRequestArguments) -> McpToolResult:
        tool = self.tool(identity)
        if arguments.schema_hash != tool.schema_hash:
            raise ValueError("MCP schema changed; search tools again")
        validate(arguments.arguments.wire_value(), tool.parameters.wire_value())
        response = await self.request(
            tool.server,
            McpOperation.CALL,
            McpRequestArguments(
                name=tool.name,
                arguments=arguments.arguments,
                schema_hash=arguments.schema_hash,
                cursor=arguments.cursor,
                uri=arguments.uri,
            ),
        )
        if not isinstance(response, McpToolResult):
            raise RuntimeError("MCP call returned an incompatible result")
        return response

    async def close(self) -> None:
        for connection in self.connections:
            await self._disconnect(connection)
        for connection in self.connections:
            connection.secrets.clear()
            connection.task = None
