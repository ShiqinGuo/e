import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import PydanticUndefined

from agent_client.domain.enums import MessageRole, NativeItemType


class ContentType(StrEnum):
    OUTPUT_TEXT = "output_text"
    REFUSAL = "refusal"
    INPUT_TEXT = "input_text"
    SUMMARY_TEXT = "summary_text"
    REASONING_TEXT = "reasoning_text"


class ProviderResponseStatus(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"
    QUEUED = "queued"
    CANCELLED = "cancelled"


class FunctionNamespace(StrEnum):
    FUNCTIONS = "functions"


class MessagePhase(StrEnum):
    COMMENTARY = "commentary"
    PARTIAL_ANSWER = "partial_answer"
    FINAL_ANSWER = "final_answer"


class IncompleteReason(StrEnum):
    MAX_OUTPUT_TOKENS = "max_output_tokens"
    CONTENT_FILTER = "content_filter"
    INSUFFICIENT_RESOURCE = "insufficient_system_resource"


class IncompleteDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: IncompleteReason


class AnnotationType(StrEnum):
    FILE_CITATION = "file_citation"
    URL_CITATION = "url_citation"
    CONTAINER_FILE_CITATION = "container_file_citation"
    FILE_PATH = "file_path"


class ProtocolModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, validate_default=True, allow_inf_nan=False
    )


class FileCitation(ProtocolModel):
    type: Literal[AnnotationType.FILE_CITATION] = AnnotationType.FILE_CITATION
    file_id: str
    filename: str
    index: int = Field(ge=0, strict=True)


class UrlCitation(ProtocolModel):
    type: Literal[AnnotationType.URL_CITATION] = AnnotationType.URL_CITATION
    start_index: int = Field(ge=0, strict=True)
    end_index: int = Field(ge=0, strict=True)
    title: str
    url: str


class ContainerFileCitation(ProtocolModel):
    type: Literal[AnnotationType.CONTAINER_FILE_CITATION] = AnnotationType.CONTAINER_FILE_CITATION
    container_id: str
    file_id: str
    filename: str
    start_index: int = Field(ge=0, strict=True)
    end_index: int = Field(ge=0, strict=True)


class FilePathAnnotation(ProtocolModel):
    type: Literal[AnnotationType.FILE_PATH] = AnnotationType.FILE_PATH
    file_id: str
    index: int = Field(ge=0, strict=True)


type NativeAnnotation = Annotated[
    FileCitation | UrlCitation | ContainerFileCitation | FilePathAnnotation,
    Field(discriminator="type"),
]


class TopLogprob(ProtocolModel):
    token: str
    bytes: list[Annotated[int, Field(ge=0, le=255, strict=True)]]
    logprob: float = Field(le=0)


class NativeLogprob(TopLogprob):
    top_logprobs: list[TopLogprob]


class NativeContent(ProtocolModel):
    type: ContentType
    text: str | None = None
    refusal: str | None = None
    annotations: list[NativeAnnotation] | None = None
    logprobs: list[NativeLogprob] | None = None

    @model_validator(mode="after")
    def validate_content(self):
        match self.type:
            case ContentType.REFUSAL:
                if self.refusal is None:
                    raise ValueError("Refusal content is required")
            case _:
                if self.text is None:
                    raise ValueError("Text content is required")
        return self


class NativeMessage(ProtocolModel):
    type: Literal[NativeItemType.MESSAGE] = NativeItemType.MESSAGE
    id: str | None = None
    status: ProviderResponseStatus = ProviderResponseStatus.COMPLETED
    role: MessageRole
    content: str | list[NativeContent]
    reasoning_content: str | None = None
    phase: MessagePhase | None = None


class NativeReasoning(ProtocolModel):
    type: Literal[NativeItemType.REASONING] = NativeItemType.REASONING
    id: str | None = None
    status: ProviderResponseStatus = ProviderResponseStatus.COMPLETED
    content: list[NativeContent] = Field(default_factory=list)
    summary: list[NativeContent] = Field(default_factory=list)
    encrypted_content: str | None = None


class NativeFunctionCall(ProtocolModel):
    type: Literal[NativeItemType.FUNCTION_CALL] = NativeItemType.FUNCTION_CALL
    id: str | None = None
    status: ProviderResponseStatus = ProviderResponseStatus.COMPLETED
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: str
    namespace: FunctionNamespace = FunctionNamespace.FUNCTIONS


class NativeFunctionOutput(ProtocolModel):
    type: Literal[NativeItemType.FUNCTION_CALL_OUTPUT] = NativeItemType.FUNCTION_CALL_OUTPUT
    call_id: str = Field(min_length=1)
    output: str


type NativeItem = Annotated[
    NativeMessage | NativeReasoning | NativeFunctionCall | NativeFunctionOutput,
    Field(discriminator="type"),
]

NATIVE_ITEM_ADAPTER = TypeAdapter(NativeItem)


class TokenDetails(ProtocolModel):
    cached_tokens: int | None = Field(default=None, ge=0, strict=True)
    reasoning_tokens: int | None = Field(default=None, ge=0, strict=True)
    cache_write_tokens: int | None = Field(default=None, ge=0, strict=True)


class AttributionCounts(ProtocolModel):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    cached_tokens: int = Field(ge=0, strict=True)
    cache_write_tokens: int = Field(ge=0, strict=True)


class AttributionItemCounts(AttributionCounts):
    content: list[AttributionCounts] | None = None


class AttributionItem(ProtocolModel):
    name: str = Field(min_length=1)
    usage: AttributionItemCounts


class AttributionRequestField(ProtocolModel):
    name: str = Field(min_length=1)
    usage: AttributionCounts


class AttributionWire(TypedDict):
    items: JsonValue
    request_fields: JsonValue


class TokenAttribution(ProtocolModel):
    items: list[AttributionItem]
    request_fields: list[AttributionRequestField]

    @model_validator(mode="after")
    def validate_entries(self):
        for entries in (self.items, self.request_fields):
            names = [entry.name for entry in entries]
            if len(names) != len(set(names)):
                raise ValueError("Repeated attribution identity")
            entries.sort(key=lambda entry: entry.name)
        return self

    @field_validator("items", mode="before")
    @classmethod
    def parse_items(cls, value: object):
        if isinstance(value, Mapping):
            return [
                AttributionItem(name=name, usage=AttributionItemCounts.model_validate(counts))
                for name, counts in value.items()
            ]
        return value

    @field_validator("request_fields", mode="before")
    @classmethod
    def parse_request_fields(cls, value: object):
        if isinstance(value, Mapping):
            return [
                AttributionRequestField(name=name, usage=AttributionCounts.model_validate(counts))
                for name, counts in value.items()
            ]
        return value

    @model_serializer
    def serialize_attribution(self) -> AttributionWire:
        item_json = (
            "{"
            + ",".join(
                json.dumps(entry.name) + ":" + entry.usage.model_dump_json(exclude_unset=True)
                for entry in self.items
            )
            + "}"
        )
        field_json = (
            "{"
            + ",".join(
                json.dumps(entry.name) + ":" + entry.usage.model_dump_json(exclude_unset=True)
                for entry in self.request_fields
            )
            + "}"
        )
        return AttributionWire(
            items=ProtocolObject(item_json).wire_value(),
            request_fields=ProtocolObject(field_json).wire_value(),
        )


class TokenUsage(ProtocolModel):
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)
    input_tokens_details: TokenDetails | None = None
    output_tokens_details: TokenDetails | None = None
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_tokens_details: TokenDetails | None = None
    completion_tokens_details: TokenDetails | None = None
    attribution: TokenAttribution | None = None


class ProtocolObject(RootModel[str]):
    root: str = "{}"

    @model_validator(mode="before")
    @classmethod
    def validate_object(cls, value: object) -> str:
        if value is PydanticUndefined:
            return "{}"
        if isinstance(value, cls):
            return value.root
        if isinstance(value, Mapping):
            return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        if not isinstance(value, str):
            raise ValueError("Protocol object must be a JSON object")

        def reject_constant(constant: str):
            raise ValueError("Nonfinite protocol number")

        def unique_pairs(pairs: list[tuple[str, JsonValue]]):
            names = [name for name, _ in pairs]
            if len(set(names)) != len(names):
                raise ValueError("Repeated protocol object field")
            return dict(pairs)

        parsed = json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
        if not isinstance(parsed, dict):
            raise ValueError("Protocol object must be a JSON object")
        TypeAdapter(JsonValue).validate_python(parsed)
        return json.dumps(parsed, ensure_ascii=False, allow_nan=False, sort_keys=True)

    @model_serializer
    def serialize_object(self) -> JsonValue:
        return json.loads(self.root)

    def wire_value(self) -> JsonValue:
        return json.loads(self.root)
