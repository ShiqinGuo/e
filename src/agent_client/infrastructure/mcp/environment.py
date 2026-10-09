import asyncio
import os
import re

from pydantic import SecretStr

from agent_client.domain.workspace import PlatformKind


class MissingMcpEnvironment(ValueError):
    def __init__(self, reference: str):
        super().__init__(f"MCP environment variable is missing or empty: {reference}")


def windows_environment(reference: str) -> str | None:
    if os.name != PlatformKind.WINDOWS:
        return None
    import winreg

    for root, path in (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    ):
        try:
            with winreg.OpenKey(root, path) as key:
                value, kind = winreg.QueryValueEx(key, reference)
        except FileNotFoundError:
            continue
        if kind not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(value, str):
            raise ValueError(f"MCP environment variable must contain text: {reference}")
        return value
    return None


async def resolve_environment(reference: str) -> SecretStr:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", reference):
        raise ValueError("MCP environment variable name is invalid")
    value = (
        os.environ[reference]
        if reference in os.environ
        else await asyncio.to_thread(windows_environment, reference)
    )
    if value is None or not value.strip():
        raise MissingMcpEnvironment(reference)
    return SecretStr(value)
