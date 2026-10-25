"""用户自带的厂商 API Key 管理。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.net import UnsafeUrlError, validate_outbound_url
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.security import decrypt_secret, encrypt_secret, mask_secret
from app.db.session import get_db
from app.models.credential import ProviderCredential
from app.models.user import User
from app.schemas.credential import (
    ConnectionTestResult,
    CredentialCreate,
    CredentialRead,
    CredentialUpdate,
)
from app.services.providers import build_provider, get_spec

router = APIRouter(prefix="/credentials", tags=["密钥管理"])


def _to_read(credential: ProviderCredential) -> CredentialRead:
    try:
        masked = mask_secret(decrypt_secret(credential.api_key_encrypted))
    except Exception:  # noqa: BLE001 - 解密失败时降级展示
        masked = "******"
    item = CredentialRead.model_validate(credential)
    item.masked_key = masked
    return item


async def _get_owned(db: AsyncSession, user_id: int, credential_id: int) -> ProviderCredential:
    credential = await db.get(ProviderCredential, credential_id)
    if not credential or credential.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="密钥不存在")
    return credential


async def _validate_base_url(base_url: str | None) -> None:
    if not base_url:
        return
    try:
        await asyncio.to_thread(validate_outbound_url, base_url)
    except UnsafeUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"厂商地址不合法：{exc}"
        ) from exc


def _normalize_capabilities(provider: str, capabilities: list[str] | None) -> list[str]:
    spec = get_spec(provider)
    if not spec:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"不支持的厂商：{provider}")
    valid = [cap for cap in (capabilities or []) if cap in spec.capabilities]
    return valid or list(spec.capabilities)


async def _clear_default(db: AsyncSession, user_id: int, provider: str, keep_id: int | None) -> None:
    stmt = update(ProviderCredential).where(
        ProviderCredential.user_id == user_id, ProviderCredential.provider == provider
    )
    if keep_id is not None:
        stmt = stmt.where(ProviderCredential.id != keep_id)
    await db.execute(stmt.values(is_default=False))


@router.get("", response_model=list[CredentialRead])
async def list_credentials(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[CredentialRead]:
    rows = (
        await db.execute(
            select(ProviderCredential)
            .where(ProviderCredential.user_id == user.id)
            .order_by(ProviderCredential.id)
        )
    ).scalars().all()
    return [_to_read(row) for row in rows]


@router.post("", response_model=CredentialRead, status_code=status.HTTP_201_CREATED)
async def create_credential(
    payload: CredentialCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CredentialRead:
    spec = get_spec(payload.provider)
    if not spec:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"不支持的厂商：{payload.provider}")

    capabilities = _normalize_capabilities(payload.provider, payload.capabilities)
    await _validate_base_url(payload.base_url or spec.default_base_url)
    if payload.is_default:
        await _clear_default(db, user.id, payload.provider, None)

    credential = ProviderCredential(
        user_id=user.id,
        provider=payload.provider,
        name=payload.name,
        api_key_encrypted=encrypt_secret(payload.api_key),
        base_url=payload.base_url or spec.default_base_url,
        capabilities=",".join(capabilities),
        is_default=payload.is_default,
    )
    db.add(credential)
    await db.commit()
    await db.refresh(credential)
    return _to_read(credential)


@router.patch("/{credential_id}", response_model=CredentialRead)
async def update_credential(
    credential_id: int,
    payload: CredentialUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CredentialRead:
    credential = await _get_owned(db, user.id, credential_id)

    if payload.name is not None:
        credential.name = payload.name
    if payload.base_url is not None:
        await _validate_base_url(payload.base_url)
        credential.base_url = payload.base_url
    if payload.api_key:
        credential.api_key_encrypted = encrypt_secret(payload.api_key)
    if payload.capabilities is not None:
        credential.capabilities = ",".join(
            _normalize_capabilities(credential.provider, payload.capabilities)
        )
    if payload.is_default is not None:
        if payload.is_default:
            await _clear_default(db, user.id, credential.provider, credential.id)
        credential.is_default = payload.is_default

    await db.commit()
    await db.refresh(credential)
    return _to_read(credential)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    credential_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    credential = await _get_owned(db, user.id, credential_id)
    await db.delete(credential)
    await db.commit()


@router.post("/{credential_id}/test", response_model=ConnectionTestResult)
async def test_credential(
    credential_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionTestResult:
    credential = await _get_owned(db, user.id, credential_id)
    try:
        provider = build_provider(
            credential.provider,
            decrypt_secret(credential.api_key_encrypted),
            credential.base_url,
        )
        ok, message = await provider.test_connection()
    except Exception as exc:  # noqa: BLE001 - 测试失败需要落库并回显
        ok, message = False, str(exc)

    credential.last_checked_at = datetime.now(timezone.utc)
    credential.last_check_ok = ok
    credential.last_check_message = message[:500]
    await db.commit()
    return ConnectionTestResult(ok=ok, message=message)
