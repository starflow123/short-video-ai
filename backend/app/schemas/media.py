from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MediaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int | None = None
    shot_id: int | None = None
    kind: str
    source: str
    provider: str | None = None
    model: str | None = None
    filename: str
    url: str
    mime_type: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    task_id: int | None = None
    note: str | None = None
    created_at: datetime
