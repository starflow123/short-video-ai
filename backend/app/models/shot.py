from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

# Shot.status 取值：pending / prepared / generating / ready / failed
# 参照 Jellyfish：区分「已就绪(prepared)」与「正在生成(generating)」


class Shot(Base, TimestampMixin):
    __tablename__ = "shots"

    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), index=True, nullable=False
    )

    order_index: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    dialogue: Mapped[str | None] = mapped_column(Text, nullable=True)
    camera: Mapped[str | None] = mapped_column(String(200), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    # 逐镜生成分辨率：拆解时由 AI 按镜头重要程度选 720p / 1080p，视频生成默认采用
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)

    image_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 镜头指令（参照 Jellyfish 的 ShotFramePromptInput）：
    # shot_size / camera_angle / camera_movement / atmosphere / mood_tags /
    # action_beats / composition / script_excerpt
    directives: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 结构化镜头上下文：生成时按这些事实选择参考图、拼装提示词。
    # subjects: [{"name","position","action","expression","gaze","holds","asset_id"}]
    # props:    [{"name","owner","state","description","asset_id"}]
    # action_timeline: [{"start","end","action","expression","gaze","camera"}]
    subjects: Mapped[list | None] = mapped_column(JSON, nullable=True)
    props: Mapped[list | None] = mapped_column(JSON, nullable=True)
    action_timeline: Mapped[list | None] = mapped_column(JSON, nullable=True)
    start_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    end_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    # 人工确认位：AI 出分镜后先停下来，用户逐个镜头审阅 / 修改提示词，
    # 确认后置 True；「批量生图」只对已确认的镜头派发任务。
    prompt_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # 指向 media_assets.id，为避免与 media_assets.shot_id 形成循环外键，
    # 此处仅存整数引用，由业务层保证一致性。
    # 关键帧（可多张）存放在 shot_keyframes 表，不再用单列存 ID。
    video_media_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    episode: Mapped["Episode"] = relationship(back_populates="shots")  # noqa: F821
