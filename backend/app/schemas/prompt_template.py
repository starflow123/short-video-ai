from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TemplateCreate(BaseModel):
    category: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    preview: str | None = Field(default=None, max_length=255)
    content: str = Field(min_length=1)
    is_default: bool = False


class TemplateUpdate(BaseModel):
    category: str | None = None
    name: str | None = Field(default=None, max_length=200)
    preview: str | None = Field(default=None, max_length=255)
    content: str | None = None
    is_default: bool | None = None


class TemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    name: str
    preview: str | None = None
    content: str
    variables: list[str] | None = None
    is_system: bool
    is_default: bool
    created_at: datetime
    category_label: str | None = None


class TemplateRenderRequest(BaseModel):
    """按给定变量渲染模板（缺省变量渲染为空串）。"""

    variables: dict[str, str] = Field(default_factory=dict)


class TemplateRenderRead(BaseModel):
    content: str
    variables: list[str] = Field(default_factory=list)


class TemplateCategoryRead(BaseModel):
    key: str
    label: str
    categories: list[dict]
