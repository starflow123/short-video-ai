"""智能剪辑：上传素材 + 创建剪辑任务。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.media import MediaAsset
from app.models.project import Project
from app.models.user import User
from app.schemas.edit import EditCreateIn
from app.schemas.media import MediaRead
from app.schemas.task import TaskRead
from app.services import storage
from app.services.dispatch import create_task
from app.services.editing import probe_video

router = APIRouter(prefix="/edits", tags=["智能剪辑"])

_MAX_UPLOAD_BYTES = 300 * 1024 * 1024
_ALLOWED_RATIOS = {"9:16", "16:9", "1:1", "4:3", "3:4"}
_ALLOWED_PACE = {"fast", "medium", "slow"}


@router.post("/upload", response_model=MediaRead, status_code=status.HTTP_201_CREATED)
async def upload_clip(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MediaRead:
    """上传一段待剪辑的视频素材，落库到素材库。"""
    content_type = (file.content_type or "").lower()
    suffix = Path(file.filename or "").suffix.lower()
    if not content_type.startswith("video/") and suffix not in {
        ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi",
    }:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="只支持上传视频文件")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"单个素材不能超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
        )

    asset = await storage.persist_media(
        db,
        user_id=user.id,
        kind="video",
        data=data,
        mime_type=content_type or None,
        source="upload",
        note=file.filename,
    )
    # 探测时长与分辨率：前端要显示时长，剪辑任务也要据此判断上限
    duration, width, height, _audio = await probe_video(Path(asset.path))
    asset.duration_seconds = duration
    asset.width = width
    asset.height = height
    await db.commit()
    await db.refresh(asset)
    return MediaRead.model_validate(asset)


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_edit(
    payload: EditCreateIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    """创建智能剪辑任务：AI 选段 + ffmpeg 合成。"""
    if payload.aspect_ratio not in _ALLOWED_RATIOS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不支持的画幅")
    if payload.pace not in _ALLOWED_PACE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不支持的节奏")

    clips: list[dict] = []
    for item in payload.clips:
        media = await db.get(MediaAsset, item.media_id)
        if not media or media.user_id != user.id or media.kind != "video":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="素材无效或不是视频")
        if not Path(media.path).exists():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="素材文件缺失")
        clips.append({"media_id": media.id, "note": (item.note or "").strip() or None})

    if payload.project_id is not None:
        project = await db.get(Project, payload.project_id)
        if not project or project.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="项目不存在")

    task = await create_task(
        db,
        user_id=user.id,
        kind="edit",
        project_id=payload.project_id,
        params={
            "clips": clips,
            "target_duration": payload.target_duration,
            "aspect_ratio": payload.aspect_ratio,
            "pace": payload.pace,
            "instruction": (payload.instruction or "").strip() or None,
            "flow": "smart_edit",
        },
    )
    return TaskRead.model_validate(task)
