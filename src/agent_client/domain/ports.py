from collections.abc import AsyncIterator
from typing import Protocol

from agent_client.domain.models import ModelEvent, ModelRequest


class ModelGateway(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...

    async def close(self) -> None: ...
