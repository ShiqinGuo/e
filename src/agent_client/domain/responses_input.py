from typing import Annotated, Literal

from pydantic import Field

from agent_client.domain.base import Contract
from agent_client.domain.enums import NativeItemType
from agent_client.domain.protocol import (
    NativeContent,
    NativeFunctionCall,
    NativeFunctionOutput,
    NativeMessage,
)


class ResponsesReasoningInput(Contract):
    type: Literal[NativeItemType.REASONING] = NativeItemType.REASONING
    id: str | None = Field(default=None, min_length=1)
    summary: list[NativeContent]
    content: list[NativeContent] | None = None
    encrypted_content: str | None = None


type ResponsesInput = Annotated[
    NativeMessage | ResponsesReasoningInput | NativeFunctionCall | NativeFunctionOutput,
    Field(discriminator="type"),
]
