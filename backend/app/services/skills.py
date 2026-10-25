"""轻量 skills 注册表：把镜头指令规范做成可注册、可选择的 skill 集合。

对应 Jellyfish 的 skills 概念：每个 skill 负责一组镜头指令字段的产出 / 约束，
``guidance`` 是其注入提示词的约束文本，``fields`` 是它负责的字段名。

设计目标：提示词里原来硬编码的「景别 / 机位 / 运镜 / 氛围 / 情绪标签 / 动作拍点 /
构图」等字段规范，改为按启用的 skill 动态组装，便于后续扩展与前端按需勾选。
注意：``fields`` 中的字段名必须与 ``generation._shot_directives`` / ``Shot.directives``
的既有结构保持一致，不要改名，否则会破坏已落库数据。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ShotSkill:
    """一个镜头指令 skill。

    - ``key``：唯一标识（前端 / 任务参数用它选择启用哪些 skill）；
    - ``label``：展示名；
    - ``description``：一句话说明；
    - ``fields``：该 skill 负责产出 / 约束的镜头指令字段名；
    - ``guidance``：注入提示词的中文约束文本。
    """

    key: str
    label: str
    description: str
    fields: tuple[str, ...]
    guidance: str


SHOT_SKILLS: dict[str, ShotSkill] = {
    "shot_language": ShotSkill(
        key="shot_language",
        label="镜头语言",
        description="为每个镜头约束景别 / 机位 / 运镜三项镜头语言。",
        fields=("shot_size", "camera_angle", "camera_movement"),
        guidance=(
            "每个镜头都要给出景别（shot_size）/ 机位角度（camera_angle）/ 运镜（camera_movement）"
            "三项镜头语言；画面描述与提示词按「景别 / 机位 / 运镜 → 场景环境 → 主体人物 / 关键对象 → "
            "动作状态 → 氛围情绪 → 风格收束」组织。"
        ),
    ),
    "emotion": ShotSkill(
        key="emotion",
        label="氛围与情绪",
        description="为镜头约束氛围描述与情绪标签。",
        fields=("atmosphere", "mood_tags"),
        guidance=(
            "用 atmosphere 描述画面氛围（如「雨夜霓虹的温柔孤寂」），用 mood_tags 给出 2-4 个情绪标签；"
            "氛围与情绪必须契合该镜头所处的剧情阶段，不要自由发散。"
        ),
    ),
    "action_beats": ShotSkill(
        key="action_beats",
        label="动作拍点",
        description="拆解镜头内的动作拍点序列。",
        fields=("action_beats",),
        guidance=(
            "用 action_beats 按时间顺序拆出镜头内的动作拍点，并与 action_timeline 的秒级拍点保持一致；"
            "image_prompt 描述的是「镜头开始时 / 最具代表性的瞬间」，连续动作链用「刚开始 / "
            "尚未完成 / 被打断」这类未完成态表达，不要直接写成完成态或情绪爆发结果。"
        ),
    ),
    "composition": ShotSkill(
        key="composition",
        label="构图与焦点",
        description="约束主体位置、环境重心与朝向关系。",
        fields=("composition",),
        guidance=(
            "用 composition 说明构图与空间锚点：主体在画面中的相对位置、朝向、环境重心，"
            "明确主体朝向以减少构图与轴线突变。"
        ),
    ),
    "continuity": ShotSkill(
        key="continuity",
        label="镜头连贯性",
        description="保证前后镜头的动作、空间、情绪承接。",
        fields=("script_excerpt",),
        guidance=(
            "每个镜头用 script_excerpt 摘录对应剧本原文；若存在上一镜头，当前镜头画面要承接其动作、"
            "空间方向、视线或情绪，不要像全新场景重新开局；若存在下一镜头，应为下一镜头留下自然的"
            "动作或情绪收束，避免硬切。"
        ),
    ),
    "keyframe_prompt": ShotSkill(
        key="keyframe_prompt",
        label="关键帧提示词",
        description="约束关键帧（文生图）提示词规范。",
        fields=("image_prompt",),
        guidance=(
            "image_prompt 一律用中文，描述镜头第一个动作拍点（镜头开始时）最具代表性的瞬间，"
            "必须写清 subjects 里每个人物的姓名、画面位置、动作、神态与手持道具，以及 props 的归属；"
            "再补景别 / 机位 / 运镜、场景环境、氛围情绪与风格收束，且继承项目统一画面风格。"
        ),
    ),
    "video_prompt": ShotSkill(
        key="video_prompt",
        label="视频提示词",
        description="约束图生视频提示词规范。",
        fields=("video_prompt",),
        guidance=(
            "video_prompt 一律用中文，依据 action_timeline 写成「0.0-1.5s：…；1.5-3.0s：…」"
            "的秒级动作过程，覆盖镜头完整时长；每个时段写清动作、神态、视线与运镜，"
            "与 image_prompt 对应同一镜头，保持主体、场景与风格一致。"
        ),
    ),
}


# 默认全部启用（前端不传 skills 时行为与改造前完全一致）
DEFAULT_SKILL_KEYS: tuple[str, ...] = tuple(SHOT_SKILLS.keys())


def resolve_skills(keys: Sequence[str] | None = None) -> list[ShotSkill]:
    """把 skill key 列表解析为 skill 对象列表。

    - ``keys`` 为空（None 或空列表）时返回全部默认 skill；
    - 忽略未知 key；
    - 返回顺序始终与 ``DEFAULT_SKILL_KEYS`` 一致。
    """
    if not keys:
        return [SHOT_SKILLS[key] for key in DEFAULT_SKILL_KEYS]
    selected = set(keys)
    return [SHOT_SKILLS[key] for key in DEFAULT_SKILL_KEYS if key in selected]


def skills_instructions(skills: Sequence[ShotSkill]) -> str:
    """把这些 skill 的 guidance 拼成一段中文规范文本（每条一行，带 ``- `` 前缀）。"""
    return "\n".join(f"- {skill.guidance}" for skill in skills)


def skills_fields(skills: Sequence[ShotSkill]) -> list[str]:
    """汇总所有启用 skill 的字段名（去重、保序）。"""
    fields: list[str] = []
    for skill in skills:
        for name in skill.fields:
            if name not in fields:
                fields.append(name)
    return fields
