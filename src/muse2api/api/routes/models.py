import time

from fastapi import APIRouter, Depends

from ...core.models import MODELS
from ...errors import NotFound
from ..deps import require_api_key

router = APIRouter(tags=["models"], dependencies=[Depends(require_api_key)])

_CREATED = int(time.time())


def _model_view(spec) -> dict:
    return {"id": spec.id, "object": "model", "created": _CREATED,
            "owned_by": spec.owned_by, "shutdown_date": None,
            "kind": spec.kind}


@router.get("/v1/models")
async def list_models() -> dict:
    """The models this gateway really runs."""
    return {"object": "list", "data": [_model_view(m) for m in MODELS.values()]}


@router.get("/v1/models/{model}")
async def retrieve_model(model: str) -> dict:
    """``client.models.retrieve`` works like the real API."""
    spec = MODELS.get(model)
    if spec is None:
        raise NotFound(
            f"The model '{model}' does not exist or you do not have access to it.",
            param="model", code="model_not_found")
    return _model_view(spec)


@router.delete("/v1/models/{model}")
async def delete_model(model: str) -> dict:
    """Only fine-tuned models can be deleted; this gateway serves none."""
    raise NotFound(
        f"The model '{model}' does not exist or you do not have access to it.",
        param="model", code="model_not_found")
