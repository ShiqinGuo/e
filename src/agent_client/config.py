import os
import tomllib
from pathlib import Path

from agent_client.domain.configuration import AppConfig


def agent_home() -> Path:
    configured = (
        Path(os.environ["AGENT_HOME"])
        if "AGENT_HOME" in os.environ
        else Path.home() / ".agent-client"
    )
    return configured.expanduser().resolve()


def load_config(path: Path | None = None, *, home: Path | None = None) -> AppConfig:
    config_path = path or (home or agent_home()) / "config.toml"
    if path is None and not config_path.exists():
        return AppConfig()
    return AppConfig.model_validate(tomllib.loads(config_path.read_text(encoding="utf-8")))
