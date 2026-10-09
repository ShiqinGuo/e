from pydantic import Field, model_validator

from agent_client.domain.enums import TokenMeasurement
from agent_client.domain.models import Contract


class ContextUsage(Contract):
    used_tokens: int | None = Field(default=None, ge=0)
    context_window: int = Field(gt=0)
    input_budget: int = Field(gt=0)
    measurement: TokenMeasurement = TokenMeasurement.UNAVAILABLE

    @model_validator(mode="after")
    def validate_usage(self):
        if self.input_budget > self.context_window:
            raise ValueError("Input budget cannot exceed the context window")
        if (self.used_tokens is None) != (self.measurement == TokenMeasurement.UNAVAILABLE):
            raise ValueError("Context usage availability must match its measurement")
        return self

    @property
    def ratio(self) -> float | None:
        return self.used_tokens / self.context_window if self.used_tokens is not None else None
