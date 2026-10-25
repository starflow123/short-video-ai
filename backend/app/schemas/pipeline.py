from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PipelineStartRequest(BaseModel):
    """启动制作流水线：建角色档案 → AI 出分镜（随后暂停等人工确认）。"""

    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None
    shot_count: int = Field(default=8, ge=1, le=60)
    style: str | None = None
    skills: list[str] | None = None
    # 是否重新抽取角色档案（已有档案时置 False 可复用，加快出分镜）
    rebuild_characters: bool = True


class PipelineConfirmRequest(BaseModel):
    """人工确认后批量生图：可只指定部分镜头（=只重跑这些镜头）。"""

    shot_ids: list[int] | None = Field(
        default=None, description="指定则只生成这些镜头；不传则生成全部已确认的镜头"
    )
    credential_id: int | None = None
    provider: str | None = None
    model: str | None = None
    asset_ids: list[int] | None = Field(
        default=None, description="额外关联的资产；本项目的角色档案会自动带上"
    )
    negative_prompt: str | None = None
    include_character_assets: bool = True


class PipelineVideoRequest(BaseModel):
    """批量生视频：为已有关键帧的镜头生成视频，全部完成后自动导出成片。"""

    shot_ids: list[int] | None = Field(
        default=None, description="指定则只生成这些镜头；不传则补齐所有有图无视频的镜头"
    )
    credential_id: int | None = None
    provider: str | None = None
    model: str | None = None
    asset_ids: list[int] | None = None
    negative_prompt: str | None = None
    duration: int | None = Field(default=None, ge=1, le=30)


class CharacterProfileRead(BaseModel):
    name: str
    age: str | None = None
    role: str | None = None
    appearance: str | None = None
    costume: str | None = None
    traits: str | None = None
    tags: list[str] = Field(default_factory=list)
    asset_id: int | None = None


class PipelineRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int | None = None
    episode_id: int | None = None
    status: str
    stage: str
    params: dict | None = None
    characters: list[CharacterProfileRead] | None = None
    error: str | None = None
    task_id: int | None = None
    created_at: datetime
    updated_at: datetime
    # 冗余统计：便于前端展示「已确认 x / 共 y」
    total_shots: int = 0
    confirmed_shots: int = 0
    generated_shots: int = 0
    generated_videos: int = 0
