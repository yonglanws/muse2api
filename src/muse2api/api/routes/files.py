"""OpenAI Files API (``/v1/files``).

Uploaded bytes stay in the gateway's ``data/files`` directory and can be
referenced by ``file_id`` in chat / Responses inputs. This is what makes the
standard OpenAI attachment workflow (upload once, reference many times) behave
like the real API; muse.ai itself only accepts inline file_ref items.
"""

from __future__ import annotations

import json
import mimetypes
import re
import time

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response

from ...errors import InvalidRequest, NotFound
from ...services.container import Services
from ..deps import get_services, require_api_key

router = APIRouter(tags=["files"], dependencies=[Depends(require_api_key)])

_PURPOSES = {"assistants", "batch", "fine-tune", "vision", "user_data", "evals"}


def _owner(request: Request) -> str | None:
    return getattr(request.state, "key_id", None)


def _expiration(raw: str | None) -> float | None:
    """Parse ``expires_after`` (a JSON object in multipart form data)."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        raise InvalidRequest("expires_after must be a JSON object",
                             param="expires_after") from None
    if not isinstance(value, dict):
        raise InvalidRequest("expires_after must be a JSON object",
                             param="expires_after")
    if value.get("anchor", "created_at") != "created_at":
        raise InvalidRequest("expires_after.anchor must be 'created_at'",
                             param="expires_after.anchor")
    try:
        seconds = int(value.get("seconds"))
    except (TypeError, ValueError):
        raise InvalidRequest("expires_after.seconds must be an integer",
                             param="expires_after.seconds") from None
    if not 3600 <= seconds <= 2592000:
        raise InvalidRequest(
            "expires_after.seconds must be between 3600 and 2592000",
            param="expires_after.seconds")
    return time.time() + seconds


@router.post("/v1/files")
async def create_file(
    request: Request,
    file: UploadFile = File(...),
    purpose: str = Form(default="assistants"),
    expires_after: str | None = Form(default=None),
    svc: Services = Depends(get_services),
) -> dict:
    if purpose not in _PURPOSES:
        raise InvalidRequest(
            f"invalid purpose '{purpose}'; expected one of "
            f"{', '.join(sorted(_PURPOSES))}", param="purpose")
    limit = svc.settings.file_max_bytes
    if limit and file.size and file.size > limit:
        raise InvalidRequest(
            f"file exceeds the {limit} byte upload limit", param="file")
    data = await file.read()
    if limit and len(data) > limit:
        raise InvalidRequest(
            f"file exceeds the {limit} byte upload limit", param="file")
    filename = file.filename or "upload.bin"
    mime = file.content_type or mimetypes.guess_type(filename)[0] or ""
    return svc.files.create(filename=filename, purpose=purpose,
                            data=data, mime=mime,
                            expires_at=_expiration(expires_after),
                            owner=_owner(request))


@router.get("/v1/files")
async def list_files(
    request: Request,
    purpose: str | None = Query(default=None),
    limit: int = Query(default=10000, ge=1, le=10000),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    after: str | None = Query(default=None),
    svc: Services = Depends(get_services),
) -> dict:
    owner = _owner(request)
    if after and svc.files.get(after, owner) is None:
        raise NotFound(f"No file found with id '{after}'.", param="after")
    # One extra row tells us whether another page exists.
    data = svc.files.list(purpose=purpose, limit=limit + 1,
                          order=order, after=after, owner=owner)
    has_more = len(data) > limit
    page = data[:limit]
    return {"object": "list", "data": page,
            "first_id": page[0]["id"] if page else None,
            "last_id": page[-1]["id"] if page else None,
            "has_more": has_more}


@router.get("/v1/files/{file_id}/content")
async def get_file_content(file_id: str, request: Request,
                           svc: Services = Depends(get_services)):
    data, entry = svc.files.read(file_id, _owner(request))
    # Client-supplied filenames must not be able to inject header syntax.
    filename = re.sub(r'[^A-Za-z0-9._-]+', "_",
                      str(entry.get("filename") or file_id))[:120] or file_id
    # Serve from memory so a deleted/replaced backing file can never leak, and
    # so the response has a precise length.
    return Response(
        content=data,
        media_type=str(entry.get("mime") or "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/v1/files/{file_id}")
async def retrieve_file(file_id: str, request: Request,
                        svc: Services = Depends(get_services)) -> dict:
    return svc.files.retrieve(file_id, _owner(request))


@router.delete("/v1/files/{file_id}")
async def delete_file(file_id: str, request: Request,
                      svc: Services = Depends(get_services)) -> dict:
    return svc.files.delete(file_id, _owner(request))
