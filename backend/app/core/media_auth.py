"""媒体访问授权：登录 Cookie / Bearer Token / 带签名的临时地址。

浏览器直接用 <img>/<video> 拉 /media，带不了 Authorization 头，
所以登录时额外下发一个 HttpOnly Cookie；厂商拉参考图则使用限时签名地址。
"""
from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request

from app.core.config import settings
from app.core.security import decode_access_token

MEDIA_COOKIE = "aivs_session"


def _signature(path: str, exp: int) -> str:
    payload = f"{path}:{exp}".encode("utf-8")
    return hmac.new(settings.secret_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def signed_media_path(path: str, ttl_seconds: int | None = None) -> str:
    exp = int(time.time()) + (ttl_seconds or settings.media_signature_ttl_seconds)
    return f"{path}?exp={exp}&sig={_signature(path, exp)}"


def verify_media_signature(path: str, exp: str | None, sig: str | None) -> bool:
    if not exp or not sig or not exp.isdigit():
        return False
    if int(exp) < int(time.time()):
        return False
    return hmac.compare_digest(_signature(path, int(exp)), sig)


def is_media_authorized(request: Request) -> bool:
    token = request.cookies.get(MEDIA_COOKIE)
    if not token:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if token:
        try:
            decode_access_token(token)
            return True
        except Exception:  # noqa: BLE001 - 过期/伪造的 token 继续走签名校验
            pass
    return verify_media_signature(
        request.url.path,
        request.query_params.get("exp"),
        request.query_params.get("sig"),
    )
