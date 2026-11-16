import time

from fastapi import APIRouter, Depends

from ...core.models import MODELS
from ..deps import require_api_key

router = APIRouter(tags=["models"], dependencies=[Depends(require_api_key)])

_CREATED = int(time.time())


@router.get("/v1/models")
async def list_models() -> dict:
    """The models this gateway really runs."""
    data = [
        {"id": m.id, "object": "model", "created": _CREATED, "owned_by": m.owned_by, "kind": m.kind}
        for m in MODELS.values()
    ]
    return {"object": "list", "data": data}
