"""FastAPI 应用装配。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import settings
from app.core.media_auth import is_media_authorized
from app.services.storage import media_root

app = FastAPI(title=settings.app_name, version="0.1.0", debug=settings.debug)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")


@app.middleware("http")
async def protect_media(request: Request, call_next):
    prefix = settings.media_url_prefix.rstrip("/") + "/"
    if request.url.path.startswith(prefix) and not is_media_authorized(request):
        return JSONResponse(status_code=401, content={"detail": "未登录或媒体链接已失效"})
    return await call_next(request)


# 本地媒体静态服务（生成的图片 / 视频 / 成片），访问需登录 Cookie 或签名地址
app.mount(settings.media_url_prefix, StaticFiles(directory=str(media_root())), name="media")


@app.get("/health", tags=["系统"])
async def health() -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name}
