"""技能（skills）：镜头指令规范的可选集合，供前端展示与选择。"""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas.skill import SkillInfo
from app.services.skills import resolve_skills

router = APIRouter(prefix="/skills", tags=["技能"])


@router.get("", response_model=list[SkillInfo])
async def list_skills() -> list[SkillInfo]:
    """返回全部默认 skill（按 DEFAULT_SKILL_KEYS 顺序）。"""
    return [
        SkillInfo(
            key=skill.key,
            label=skill.label,
            description=skill.description,
            fields=list(skill.fields),
        )
        for skill in resolve_skills()
    ]
