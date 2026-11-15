from __future__ import annotations

import hmac

from fastapi import Request

from ..errors import Unauthorized
from ..services.container import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _check(provided: str, expected: str) -> None:
    if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise Unauthorized("invalid or missing API key")


def _matches(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(provided.encode(), expected.encode())


def _identify(request: Request, key_id: str, key_name: str) -> None:
    # Read by the request-log middleware.
    request.state.key_id = key_id
    request.state.key_name = key_name


def require_api_key(request: Request) -> None:
    """Accept the admin key, the legacy single ``api_key``, or any active stored key."""
    svc = get_services(request)
    s = svc.settings
    provided = _bearer(request)
    if not provided:
        raise Unauthorized("invalid or missing API key")
    # The admin key is a superset of the API key.
    if _matches(provided, s.admin_key):
        _identify(request, "admin", "admin")
        return
    if _matches(provided, s.api_key):
        _identify(request, "legacy", "legacy")
        return
    key = svc.keys.verify(provided)
    if key is None:
        raise Unauthorized("invalid or missing API key")
    svc.keys.touch(key)
    _identify(request, key.id, key.name)


def astrbot_conversation_id(request: Request) -> str | None:
    """AstrBot's stable per-chat id, sent as ``x-astrbot-conversation-id``."""
    value = request.headers.get("x-astrbot-conversation-id")
    return value.strip() if value and value.strip() else None


def conversation_key(request: Request, user: str | None) -> str | None:
    """The browser driver continues a muse.ai thread only for a non-empty key. Scoping
    the key by the API key keeps two keys that send the same value apart.

    AstrBot's conversation header wins over ``user``: it is one chat session, while
    ``user`` may be shared by every member of a group.
    """
    raw = astrbot_conversation_id(request) or user
    if not raw:
        return None
    return f"{getattr(request.state, 'key_id', '')}:{raw}"


def require_admin_key(request: Request) -> None:
    _check(_bearer(request), get_services(request).settings.effective_admin_key)


def public_base(request: Request) -> str:
    base = get_services(request).settings.public_base
    return base.rstrip("/") if base else str(request.base_url).rstrip("/")
