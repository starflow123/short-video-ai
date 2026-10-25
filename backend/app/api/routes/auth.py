"""注册 / 登录 / 当前用户。"""
from __future__ import annotations

import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.media_auth import MEDIA_COOKIE
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, Token, UserCreate, UserRead

router = APIRouter(prefix="/auth", tags=["认证"])

# 进程内限流：单实例部署足够挡住暴力破解，多实例需换成 Redis 计数
_LOGIN_LIMIT = 10
_LOGIN_WINDOW = 300
_REGISTER_LIMIT = 10
_REGISTER_WINDOW = 3600
_ATTEMPTS: dict[str, deque[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_many(key: str, limit: int, window: int) -> bool:
    bucket = _ATTEMPTS.get(key)
    if not bucket:
        return False
    now = time.monotonic()
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    return len(bucket) >= limit


def _record_attempt(key: str) -> None:
    if len(_ATTEMPTS) > 5000:
        _ATTEMPTS.clear()
    _ATTEMPTS.setdefault(key, deque()).append(time.monotonic())


def _set_media_cookie(response: Response, token: str) -> None:
    # 浏览器加载 <img>/<video> 时带不上 Authorization 头，用 HttpOnly Cookie 授权 /media
    response.set_cookie(
        MEDIA_COOKIE,
        token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )


async def _issue_token(user: User, response: Response) -> Token:
    token = Token(access_token=create_access_token(user.id), user=UserRead.model_validate(user))
    _set_media_cookie(response, token.access_token)
    return token


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserCreate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Token:
    rate_key = f"register:{_client_ip(request)}"
    if _too_many(rate_key, _REGISTER_LIMIT, _REGISTER_WINDOW):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="注册过于频繁，请稍后再试"
        )
    _record_attempt(rate_key)

    invite = settings.registration_invite_code.strip()
    if invite and (payload.invite_code or "").strip() != invite:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="邀请码不正确")

    existing = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已被占用")

    user = User(
        username=payload.username,
        email=payload.email,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return await _issue_token(user, response)


@router.post("/login", response_model=Token)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Token:
    rate_key = f"login:{_client_ip(request)}:{payload.username.lower()}"
    if _too_many(rate_key, _LOGIN_LIMIT, _LOGIN_WINDOW):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录失败次数过多，请 5 分钟后再试",
        )

    user = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.hashed_password):
        _record_attempt(rate_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    _ATTEMPTS.pop(rate_key, None)
    return await _issue_token(user, response)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(response: Response):
    response.delete_cookie(MEDIA_COOKIE, path="/")


@router.get("/me", response_model=UserRead)
async def me(
    response: Response,
    user: User = Depends(get_current_user),
) -> UserRead:
    # 已有登录态（localStorage token）的会话在这里补发媒体 Cookie
    _set_media_cookie(response, create_access_token(user.id))
    return UserRead.model_validate(user)
