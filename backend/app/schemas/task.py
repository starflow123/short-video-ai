from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int | None = None
    episode_id: int | None = None
    shot_id: int | None = None
    kind: str
    provider: str | None = None
    model: str | None = None
    status: str
    progress: int
    params: dict | None = None
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class CancelActiveRead(BaseModel):
    """批量取消进行中任务的结果。"""

    canceled: int
    task_ids: list[int] = Field(default_factory=list)


class TaskBatchDeleteIn(BaseModel):
    """批量删除任务记录。"""

    task_ids: list[int] = Field(min_length=1, max_length=500)


class TaskBatchDeleteRead(BaseModel):
    """批量删除结果：进行中的任务会跳过，由前端提示先取消。"""

    deleted: int
    skipped_ids: list[int] = Field(default_factory=list)


class GenerationRequest(BaseModel):
    """图片 / 视频 / 文本通用生成请求。"""

    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None
    prompt: str | None = None
    size: str | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    n: int = 1
    duration: int | None = None
    image_media_id: int | None = None
    # 图片任务：指定 keyframe_id 表示重绘某张已有关键帧；否则按 keyframe_label 新建一张。
    keyframe_id: int | None = None
    keyframe_label: str | None = None
    # 追加模式：新增一张并承接上一张的结尾状态，不替换该镜头当前画面（逐镜确认的主路径）。
    append: bool = False
    # 逐张关键帧提示词：n>1 批量生成时，第 i 张用 keyframe_prompts[i]，
    # 让「起幅 / 中段 / 落幅」各用各自的提示词，而不是共用同一句再叠加构图提示。
    keyframe_prompts: list[str] | None = None
    # 关联资产（角色 / 场景 / 道具 / 服装）：后端把其外观描述拼进提示词，保证跨镜头一致。
    asset_ids: list[int] | None = None
    # 负面提示词：传空串表示不追加，传 None 用系统内置默认值。
    negative_prompt: str | None = None
    # 提示词模板：给定模板时用渲染后的内容作为主体提示词。
    template_id: int | None = None
    template_variables: dict[str, str] | None = None


class RepairRequest(BaseModel):
    """关键帧修复 agent 请求：用视觉初审的失败项驱动重绘。"""

    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None
    # 最多重绘几轮；留空用服务端配置，硬上限 3。
    max_rounds: int | None = Field(default=None, ge=1, le=3)


class DiagnoseRequest(BaseModel):
    """失败诊断 agent 请求；默认只分析，不重跑。"""

    # 打开后 agent 才能用另一套凭证重跑一次（会真实产生费用）
    allow_retry: bool = False
    # 诊断决策用的文本凭证；留空自动选择
    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None
    # 重跑时希望使用的凭证 / 模型；留空自动选择
    retry_provider: str | None = None
    retry_credential_id: int | None = None
    retry_model: str | None = None


class DiagnosisRead(BaseModel):
    """某条失败任务最近一次诊断结论（来自 agent_runs）。"""

    task_id: int
    found: bool = False
    status: str | None = None
    model: str | None = None
    steps: int = 0
    summary: dict | None = None
    trace: list[dict] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime | None = None


class CutRepairRequest(BaseModel):
    """切点修复 agent 请求；默认只给修正建议。"""

    # 打开后 agent 才能重拍下一镜（会替换现有视频并产生费用）
    allow_rerender: bool = False
    # 决策用的文本凭证
    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None
    # 重拍使用的视频凭证；留空自动选择
    video_provider: str | None = None
    video_credential_id: int | None = None
    video_model: str | None = None


class BreakdownRequest(BaseModel):
    """剧本拆解请求。"""

    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None
    # 留空表示由 AI 先规划再决定镜头数；填了则作为硬约束。
    shot_count: int | None = Field(default=None, ge=1, le=60)
    target_seconds: int | None = Field(default=None, ge=5, le=900)
    style: str | None = None
    skills: list[str] | None = None
    # 用户在「先规划」阶段确认过的方案；带上后拆解必须严格按它展开。
    plan: dict | None = None
    # 拆解完成后自动补定妆照 + 逐镜关键帧，省去用户逐个点生成。
    auto_images: bool = False


class ForgeRequest(BaseModel):
    """AI 一键生成：主题/世界观 → 剧本 → 分镜指令。"""

    provider: str | None = None
    credential_id: int | None = None
    model: str | None = None
    premise: str = Field(min_length=1, description="主题 / 世界观 / 设定")
    style: str | None = None
    shot_count: int = Field(default=8, ge=1, le=60)
    target_seconds: int = Field(default=60, ge=15, le=600)
    extra: str | None = None
    skills: list[str] | None = None


class ExportRequest(BaseModel):
    pass
