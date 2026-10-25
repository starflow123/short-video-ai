from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------- 项目 ----------------
class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    aspect_ratio: str = "9:16"
    style: str | None = None
    visual_style: str | None = None
    video_style: str | None = None
    seed: int | None = None
    style_lock: bool = False


class ProjectUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    aspect_ratio: str | None = None
    style: str | None = None
    status: str | None = None
    visual_style: str | None = None
    video_style: str | None = None
    seed: int | None = None
    style_lock: bool | None = None


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None = None
    aspect_ratio: str
    style: str | None = None
    status: str
    visual_style: str | None = None
    video_style: str | None = None
    seed: int | None = None
    style_lock: bool = False
    created_at: datetime
    episode_count: int = 0
    shot_count: int = 0
    # 卡片上的任务统计（待确认 / 已就绪 / 生成中）
    shots_pending: int = 0
    shots_ready: int = 0
    shots_running: int = 0
    progress: int = 0


class ProjectSuggestRequest(BaseModel):
    """新建项目时的 AI 智能补全：粗主题 → 标题/简介/风格/标签。"""

    idea: str = Field(min_length=1, max_length=1000)
    aspect_ratio: str = "9:16"
    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None


class ProjectSuggestRead(BaseModel):
    title: str
    description: str
    visual_style: str = ""
    video_style: str = ""
    style: str
    aspect_ratio: str
    tags: list[str] = Field(default_factory=list)


# ---------------- 分集 ----------------
class EpisodeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    synopsis: str | None = None
    script_text: str | None = None
    order_index: int | None = None


class EpisodeUpdate(BaseModel):
    title: str | None = None
    synopsis: str | None = None
    script_text: str | None = None
    order_index: int | None = None
    status: str | None = None


class EpisodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    order_index: int
    title: str
    synopsis: str | None = None
    script_text: str | None = None
    plan: dict | None = None
    status: str
    created_at: datetime
    shot_count: int = 0


# ---------------- 先规划后拆镜 ----------------
class ShotPlanBeatRead(BaseModel):
    name: str
    shots: int = 0
    seconds: int = 0
    summary: str | None = None


class ShotPlanRequest(BaseModel):
    """只出拆解方案，不动分镜：用户确认后再真正拆解。"""

    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None
    shot_count: int | None = Field(default=None, ge=1, le=60)
    target_seconds: int | None = Field(default=None, ge=5, le=900)
    style: str | None = None


class ShotPlanRead(BaseModel):
    reasoning: str = ""
    shot_count: int = 0
    shot_seconds: int = 8
    target_seconds: int = 0
    beats: list[ShotPlanBeatRead] = Field(default_factory=list)


# ---------------- 分镜 ----------------
class ShotSubject(BaseModel):
    """镜头里的一个主体：谁、在哪、在做什么、什么神态、手里拿什么。"""

    name: str
    asset_id: int | None = None
    position: str | None = None
    action: str | None = None
    expression: str | None = None
    gaze: str | None = None
    holds: str | None = None
    appearance: str | None = None
    costume: str | None = None
    is_primary: bool = False


class ShotProp(BaseModel):
    name: str
    asset_id: int | None = None
    owner: str | None = None
    state: str | None = None
    description: str | None = None


class ShotActionBeat(BaseModel):
    start: float = 0
    end: float = 0
    action: str | None = None
    expression: str | None = None
    gaze: str | None = None
    camera: str | None = None


class ShotCreate(BaseModel):
    title: str | None = None
    description: str | None = None
    dialogue: str | None = None
    camera: str | None = None
    duration_seconds: float = 5.0
    resolution: str | None = None
    image_prompt: str | None = None
    video_prompt: str | None = None
    directives: dict | None = None
    subjects: list[ShotSubject] | None = None
    props: list[ShotProp] | None = None
    action_timeline: list[ShotActionBeat] | None = None
    start_state: dict | None = None
    end_state: dict | None = None
    order_index: int | None = None


class ShotUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    dialogue: str | None = None
    camera: str | None = None
    duration_seconds: float | None = None
    resolution: str | None = None
    image_prompt: str | None = None
    video_prompt: str | None = None
    directives: dict | None = None
    subjects: list[ShotSubject] | None = None
    props: list[ShotProp] | None = None
    action_timeline: list[ShotActionBeat] | None = None
    start_state: dict | None = None
    end_state: dict | None = None
    order_index: int | None = None
    status: str | None = None
    # 人工确认：审阅完该镜头的提示词后置 True，供「批量生图」判断是否可派发
    prompt_confirmed: bool | None = None


class ShotKeyframeCreate(BaseModel):
    """在某个分镜下新增一张关键帧（media_id 留空表示先占位，稍后再生成）。"""

    media_id: int | None = None
    label: str | None = Field(default=None, max_length=100)
    prompt: str | None = None
    order_index: int | None = None
    is_primary: bool = False


class ShotKeyframeUpdate(BaseModel):
    label: str | None = None
    prompt: str | None = None
    media_id: int | None = None
    order_index: int | None = None
    is_primary: bool | None = None


class ShotKeyframeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shot_id: int
    media_id: int | None = None
    order_index: int
    label: str | None = None
    prompt: str | None = None
    is_primary: bool
    created_at: datetime
    url: str | None = None


class ShotFieldSuggestRequest(BaseModel):
    """分镜单字段的 AI 生成：画面描述 / 关键帧提示词 / 视频提示词。"""

    field: str = Field(description="description / image_prompt / video_prompt")
    hint: str | None = Field(default=None, max_length=300, description="本张提示词的补充要求，如「起幅：交代环境」")
    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None


class ShotFieldSuggestRead(BaseModel):
    field: str
    value: str


class PromptPreviewRequest(BaseModel):
    """拼装预览：只看最终会发出去的提示词与各层字数，不真正调用模型。"""

    kind: str = Field(default="image", description="image / video")
    prompt: str | None = Field(default=None, description="临时覆盖正文，用于先试写再保存")


class PromptBlockRead(BaseModel):
    tag: str
    label: str
    chars: int


class PromptPreviewRead(BaseModel):
    kind: str
    prompt: str
    chars: int
    max_chars: int | None = None
    blocks: list[PromptBlockRead] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    aspect_ratio: str | None = None
    project_title: str | None = None


class ShotNeighborRead(BaseModel):
    """相邻镜头的简要信息，用于前端展示「上下文衔接」。"""

    id: int
    order_index: int
    title: str | None = None
    summary: str | None = None


class ShotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    episode_id: int
    order_index: int
    title: str | None = None
    description: str | None = None
    dialogue: str | None = None
    camera: str | None = None
    duration_seconds: float
    resolution: str | None = None
    image_prompt: str | None = None
    video_prompt: str | None = None
    directives: dict | None = None
    subjects: list[ShotSubject] = Field(default_factory=list)
    props: list[ShotProp] = Field(default_factory=list)
    action_timeline: list[ShotActionBeat] = Field(default_factory=list)
    start_state: dict | None = None
    end_state: dict | None = None
    status: str
    prompt_confirmed: bool = False
    video_media_id: int | None = None
    video_url: str | None = None
    keyframes: list[ShotKeyframeRead] = Field(default_factory=list)
    # 前后镜头（生成提示词时会自动带入，用于保障画面连续性）
    prev_shot: ShotNeighborRead | None = None
    next_shot: ShotNeighborRead | None = None

    @field_validator("subjects", "props", "action_timeline", mode="before")
    @classmethod
    def _none_to_list(cls, value):
        # 旧分镜 / 手工建的分镜这些列是 NULL，序列化成空数组方便前端直接遍历
        return value or []
