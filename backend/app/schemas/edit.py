from pydantic import BaseModel, Field


class EditClipIn(BaseModel):
    media_id: int
    note: str | None = Field(default=None, max_length=200)


class EditCreateIn(BaseModel):
    clips: list[EditClipIn] = Field(min_length=1, max_length=30)
    target_duration: int = Field(default=30, ge=5, le=300)
    aspect_ratio: str = Field(default="9:16")
    pace: str = Field(default="medium")
    instruction: str | None = Field(default=None, max_length=500)
