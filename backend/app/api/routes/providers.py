"""厂商元信息：供前端在添加 Key 时展示可选厂商与模型。"""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas.provider import ProviderInfo
from app.services.providers import list_specs

router = APIRouter(prefix="/providers", tags=["厂商"])


@router.get("", response_model=list[ProviderInfo])
async def list_providers() -> list[ProviderInfo]:
    return [
        ProviderInfo(
            key=spec.key,
            label=spec.label,
            capabilities=list(spec.capabilities),
            default_base_url=spec.default_base_url,
            models=spec.models,
            docs_url=spec.docs_url,
            description=spec.description,
        )
        for spec in list_specs()
    ]
