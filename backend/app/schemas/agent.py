from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int | None = None
    kind: str
    target_type: str | None = None
    target_id: int | None = None
    status: str | None = None
    model: str | None = None
    steps: int = 0
    stop_reason: str | None = None
    summary: dict | None = None
    trace: list[dict] = Field(default_factory=list)
    error: str | None = None
    advisory: bool = True
    created_at: datetime

    @field_validator("trace", mode="before")
    @classmethod
    def _trace_default_empty(cls, value):
        # 单次审查器只落结论、没有轨迹，库里是 NULL；不能因此让整张列表 500
        return value or []


class OrganizeRequest(BaseModel):
    """素材整理请求：只出建议，不写库。"""

    use_llm: bool = True
    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None


class OrganizeSuggestionRead(BaseModel):
    id: int
    kind: str
    severity: str
    title: str
    detail: str
    target_ids: list[int] = Field(default_factory=list)


class OrganizeRead(BaseModel):
    status: str
    project_id: int | None = None
    scan: dict = Field(default_factory=dict)
    suggestions: list[OrganizeSuggestionRead] = Field(default_factory=list)
    summary: str = ""
    model: str | None = None
    steps: int = 0
    trace: list[dict] = Field(default_factory=list)
    advisory: bool = True
