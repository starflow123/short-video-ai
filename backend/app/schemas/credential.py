from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CredentialCreate(BaseModel):
    provider: str = Field(description="厂商 key，如 deepseek / agnes / magichour")
    name: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=4)
    base_url: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    is_default: bool = False


class CredentialUpdate(BaseModel):
    name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    capabilities: list[str] | None = None
    is_default: bool | None = None


class CredentialRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    name: str
    base_url: str | None = None
    capabilities: str
    is_default: bool
    masked_key: str = ""
    last_checked_at: datetime | None = None
    last_check_ok: bool | None = None
    last_check_message: str | None = None
    created_at: datetime


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
