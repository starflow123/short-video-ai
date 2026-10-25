"""提示词模板：分类树 / 列表 / 新建 / 编辑 / 删除 / 渲染。

内置模板（``is_system=True``）对所有用户可见但不可改不可删；用户可复制另存为自己的模板。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.prompt_template import PromptTemplate
from app.models.user import User
from app.schemas.prompt_template import (
    TemplateCategoryRead,
    TemplateCreate,
    TemplateRead,
    TemplateRenderRead,
    TemplateRenderRequest,
    TemplateUpdate,
)
from app.services.prompt_templates import (
    CATEGORY_LABELS,
    PROMPT_CATEGORY_GROUPS,
    extract_variables,
    render_template,
)

router = APIRouter(prefix="/prompt-templates", tags=["提示词模板"])


def _serialize(items: list[PromptTemplate]) -> list[TemplateRead]:
    result: list[TemplateRead] = []
    for item in items:
        data = TemplateRead.model_validate(item)
        data.category_label = CATEGORY_LABELS.get(item.category, item.category)
        if not data.variables:
            data.variables = extract_variables(item.content)
        result.append(data)
    return result


def _visible_to(user_id: int):
    """可见范围：本人模板 + 全站内置模板。"""
    return or_(PromptTemplate.user_id == user_id, PromptTemplate.is_system.is_(True))


async def _get_owned_template(db: AsyncSession, user_id: int, template_id: int) -> PromptTemplate:
    template = await db.get(PromptTemplate, template_id)
    if not template or not (template.is_system or template.user_id == user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模板不存在")
    return template


@router.get("/categories", response_model=list[TemplateCategoryRead])
async def list_categories(user: User = Depends(get_current_user)) -> list[TemplateCategoryRead]:
    """两级分类树：一级分组 → 二级分类（前端左侧导航）。"""
    return [
        TemplateCategoryRead(
            key=group["key"], label=group["label"], categories=group["categories"]
        )
        for group in PROMPT_CATEGORY_GROUPS
    ]


@router.get("", response_model=list[TemplateRead])
async def list_templates(
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TemplateRead]:
    """列出可见模板：可按分类过滤，也可按名称 / 正文关键词搜索。"""
    stmt = select(PromptTemplate).where(_visible_to(user.id))
    if category:
        stmt = stmt.where(PromptTemplate.category == category)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(PromptTemplate.name.ilike(like), PromptTemplate.content.ilike(like))
        )
    # 内置在前、用户自定义在后，同组内按 sort_index 排
    stmt = stmt.order_by(
        PromptTemplate.is_system.desc(),
        PromptTemplate.category,
        PromptTemplate.sort_index,
        PromptTemplate.id,
    )
    rows = (await db.execute(stmt)).scalars().all()
    return _serialize(list(rows))


@router.post("", response_model=TemplateRead, status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: TemplateCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateRead:
    template = PromptTemplate(
        user_id=user.id,
        category=payload.category,
        name=payload.name.strip(),
        preview=payload.preview,
        content=payload.content,
        variables=extract_variables(payload.content),
        is_system=False,
        is_default=payload.is_default,
        sort_index=0,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return _serialize([template])[0]


@router.patch("/{template_id}", response_model=TemplateRead)
async def update_template(
    template_id: int,
    payload: TemplateUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateRead:
    template = await _get_owned_template(db, user.id, template_id)
    if template.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="内置模板不可修改，可「另存为我的模板」后再调整",
        )
    values = payload.model_dump(exclude_unset=True)
    if "name" in values and isinstance(values["name"], str):
        values["name"] = values["name"].strip()
    for field, value in values.items():
        setattr(template, field, value)
    if "content" in values:
        template.variables = extract_variables(template.content)
    await db.commit()
    await db.refresh(template)
    return _serialize([template])[0]


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    template = await _get_owned_template(db, user.id, template_id)
    if template.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="内置模板不可删除"
        )
    await db.delete(template)
    await db.commit()


@router.post("/{template_id}/render", response_model=TemplateRenderRead)
async def render(
    template_id: int,
    payload: TemplateRenderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateRenderRead:
    """按给定变量渲染模板，返回渲染结果（用于「预览」与「插入到字段」）。"""
    template = await _get_owned_template(db, user.id, template_id)
    return TemplateRenderRead(
        content=render_template(template.content, payload.variables),
        variables=extract_variables(template.content),
    )
