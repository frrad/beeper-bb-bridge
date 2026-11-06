"""Validated data at the Beeper, SQLite, and bb process boundaries."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Source(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    target: str = Field(min_length=1)
    chatID: str = Field(min_length=1)
    messageID: str = Field(min_length=1)
    timestamp: str = Field(min_length=1)
    repliedToMessageID: str | None = None


class Job(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    instruction: str = Field(min_length=1)
    source: Source


class MessagePayload(BaseModel):
    # External APIs may add fields. Validate the fields we actually consume.
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    id: str = ""
    chatID: str = ""
    timestamp: str | None = None
    isSender: bool | None = None
    isDeleted: bool = False
    isHidden: bool = False
    text: str | None = Field(default=None, max_length=32_000)
    linkedMessageID: str | None = None


class UpsertPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    type: Literal["message.upserted"]
    chatID: str = Field(min_length=1)
    entries: list[object] = Field(default_factory=list)
    ids: list[str] = Field(default_factory=list)


class BeeperResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    success: Literal[True]
    data: object


class SpawnResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    id: str = Field(min_length=1)


class CLIError(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    code: str
    message: str


class CLIErrorResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    ok: Literal[False]
    error: CLIError
