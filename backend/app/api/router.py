from app.api.routes import (
    assets,
    auth,
    credentials,
    edits,
    episodes,
    media,
    pipeline,
    projects,
    prompt_templates,
    providers,
    shots,
    skills,
    tasks,
)
from fastapi import APIRouter

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(providers.router)
api_router.include_router(skills.router)
api_router.include_router(credentials.router)
api_router.include_router(projects.router)
api_router.include_router(episodes.router)
api_router.include_router(shots.router)
api_router.include_router(tasks.router)
api_router.include_router(media.router)
api_router.include_router(assets.router)
api_router.include_router(prompt_templates.router)
api_router.include_router(pipeline.router)
api_router.include_router(edits.router)
