"""OpenAI Uploads API (``/v1/uploads``).

The SDK uses this surface for large files: create an upload, POST <=64 MiB
parts, then complete it into a regular Files object. Parts are stored under
``data/uploads`` and assembled into :class:`FileStore` on completion, so the
resulting ``file_id`` works in every chat/Responses attachment path.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, File, Request, UploadFile

from ...errors import InvalidRequest, NotFound
from ...services.container import Services
from ...services.file_store import FILE_PURPOSES
from ..deps import get_services, require_api_key
from ..schemas import UploadCompleteRequest, UploadCreateRequest

router = APIRouter(tags=["uploads"], dependencies=[Depends(require_api_key)])


def _owner(request: Request) -> str | None:
    return getattr(request.state, "key_id", None)


def _missing(upload_id: str) -> NotFound:
    return NotFound(f"No upload found with id '{upload_id}'.",
                    param="upload_id", code="upload_not_found")


def _file_expiration(value: dict | None) -> float | None:
    """Validate the optional file expiration policy from the create body."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidRequest("expires_after must be an object", param="expires_after")
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


@router.post("/v1/uploads")
async def create_upload(body: UploadCreateRequest, request: Request,
                        svc: Services = Depends(get_services)) -> dict:
    if body.purpose not in FILE_PURPOSES:
        raise InvalidRequest(
            f"invalid purpose '{body.purpose}'; expected one of "
            f"{', '.join(sorted(FILE_PURPOSES))}", param="purpose")
    return svc.uploads.create(
        filename=body.filename, purpose=body.purpose, mime_type=body.mime_type,
        total_bytes=body.bytes, owner=_owner(request),
        file_expires_at=_file_expiration(body.expires_after))


@router.post("/v1/uploads/{upload_id}/parts")
async def create_upload_part(upload_id: str, request: Request,
                             data: UploadFile = File(...),
                             svc: Services = Depends(get_services)) -> dict:
    part = svc.uploads.add_part(upload_id, await data.read(), _owner(request))
    if part is None:
        raise _missing(upload_id)
    return part


@router.post("/v1/uploads/{upload_id}/complete")
async def complete_upload(upload_id: str, body: UploadCompleteRequest,
                          request: Request,
                          svc: Services = Depends(get_services)) -> dict:
    result = svc.uploads.complete(upload_id, body.part_ids, md5=body.md5,
                                  owner=_owner(request))
    if result is None:
        raise _missing(upload_id)
    return result


@router.post("/v1/uploads/{upload_id}/cancel")
async def cancel_upload(upload_id: str, request: Request,
                        svc: Services = Depends(get_services)) -> dict:
    result = svc.uploads.cancel(upload_id, _owner(request))
    if result is None:
        raise _missing(upload_id)
    return result
