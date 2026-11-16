from fastapi import APIRouter

from . import (
    admin,
    chat,
    dashboard,
    files,
    health,
    images,
    media,
    models,
    responses,
    uploads,
    videos,
)


def build_router() -> APIRouter:
    router = APIRouter()
    for module in (health, models, files, uploads, chat, responses, images, videos, media,
                   admin, dashboard):
        router.include_router(module.router)
    return router
