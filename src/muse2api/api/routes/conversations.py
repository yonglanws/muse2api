"""OpenAI Conversations API (``/v1/conversations``).

Conversations are local lists of Responses input items. They let clients use
``conversation=...`` instead of ``previous_response_id``; the Responses route
flattens the items into muse.ai chat messages and appends each new turn.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ...core.params import validate_metadata
from ...errors import InvalidRequest, NotFound
from ...services.container import Services
from ..deps import get_services, require_api_key
from ..schemas import (
    ConversationCreateRequest,
    ConversationItemsRequest,
    ConversationUpdateRequest,
)

router = APIRouter(tags=["conversations"], dependencies=[Depends(require_api_key)])


def _owner(request: Request) -> str | None:
    return getattr(request.state, "key_id", None)


def _missing(conversation_id: str) -> NotFound:
    return NotFound(f"No conversation found with id '{conversation_id}'.",
                    param="conversation_id", code="conversation_not_found")


def _missing_item(item_id: str) -> NotFound:
    return NotFound(f"No item found with id '{item_id}'.",
                    param="item_id", code="item_not_found")


@router.post("/v1/conversations")
async def create_conversation(body: ConversationCreateRequest, request: Request,
                              svc: Services = Depends(get_services)) -> dict:
    return svc.conversations.create(
        metadata=validate_metadata(body.metadata) or {},
        items=body.items, owner=_owner(request))


@router.get("/v1/conversations/{conversation_id}")
async def retrieve_conversation(conversation_id: str, request: Request,
                                svc: Services = Depends(get_services)) -> dict:
    conversation = svc.conversations.get(conversation_id, _owner(request))
    if conversation is None:
        raise _missing(conversation_id)
    return conversation


@router.post("/v1/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, body: ConversationUpdateRequest,
                              request: Request,
                              svc: Services = Depends(get_services)) -> dict:
    conversation = svc.conversations.update_metadata(
        conversation_id, validate_metadata(body.metadata) or {},
        _owner(request))
    if conversation is None:
        raise _missing(conversation_id)
    return conversation


@router.delete("/v1/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request,
                              svc: Services = Depends(get_services)) -> dict:
    if not svc.conversations.delete(conversation_id, _owner(request)):
        raise _missing(conversation_id)
    return {"id": conversation_id, "object": "conversation.deleted",
            "deleted": True}


@router.get("/v1/conversations/{conversation_id}/items")
async def list_conversation_items(
    conversation_id: str,
    request: Request,
    after: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    svc: Services = Depends(get_services),
) -> dict:
    try:
        page = svc.conversations.list_items(
            conversation_id, after=after, limit=limit, order=order,
            owner=_owner(request))
    except ValueError as exc:
        raise InvalidRequest(str(exc), param="after") from exc
    if page is None:
        raise _missing(conversation_id)
    return page


@router.post("/v1/conversations/{conversation_id}/items")
async def create_conversation_items(conversation_id: str,
                                    body: ConversationItemsRequest, request: Request,
                                    svc: Services = Depends(get_services)) -> dict:
    items = svc.conversations.add_items(
        conversation_id, body.items, _owner(request))
    if items is None:
        raise _missing(conversation_id)
    return {"object": "list", "data": items,
            "first_id": items[0]["id"] if items else None,
            "last_id": items[-1]["id"] if items else None,
            "has_more": False}


@router.get("/v1/conversations/{conversation_id}/items/{item_id}")
async def retrieve_conversation_item(conversation_id: str, item_id: str,
                                     request: Request,
                                     svc: Services = Depends(get_services)) -> dict:
    item = svc.conversations.get_item(
        conversation_id, item_id, _owner(request))
    if item is None:
        if svc.conversations.get(conversation_id, _owner(request)) is None:
            raise _missing(conversation_id)
        raise _missing_item(item_id)
    return item


@router.delete("/v1/conversations/{conversation_id}/items/{item_id}")
async def delete_conversation_item(conversation_id: str, item_id: str,
                                   request: Request,
                                   svc: Services = Depends(get_services)) -> dict:
    conversation = svc.conversations.delete_item(
        conversation_id, item_id, _owner(request))
    if conversation is None:
        if svc.conversations.get(conversation_id, _owner(request)) is None:
            raise _missing(conversation_id)
        raise _missing_item(item_id)
    return conversation
