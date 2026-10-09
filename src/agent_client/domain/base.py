from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, validate_default=True, allow_inf_nan=False
    )


class Effect(StrEnum):
    READ = "read"
    WRITE = "write"
    PROCESS = "process"
    REMOTE = "remote"
