"""Model registry: the only model ids this gateway exposes and accepts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..errors import InvalidRequest, NotFound

ModelKind = Literal["chat", "image", "video"]


@dataclass(frozen=True)
class ModelSpec:
    id: str
    kind: ModelKind
    description: str = ""
    owned_by: str = "muse"


MODELS: dict[str, ModelSpec] = {
    m.id: m
    for m in (
        ModelSpec("muse-chat", "chat", "Default muse.ai assistant"),
        ModelSpec("muse-image", "image", "Text-to-image / image editing"),
        ModelSpec("muse-video", "video", "Text-to-video / first-frame image-to-video"),
    )
}

DEFAULT_BY_KIND: dict[ModelKind, str] = {
    "chat": "muse-chat",
    "image": "muse-image",
    "video": "muse-video",
}


def resolve_model(name: str | None, kind: ModelKind) -> ModelSpec:
    """Resolve a client-supplied model name to a ``ModelSpec`` of the expected kind.

    Only the gateway's own muse models are accepted. Any other vendor or alias name
    raises ``model_not_found``; a known model of the wrong kind is rejected so that
    e.g. ``muse-video`` is never silently used for chat.
    """
    if not name:
        return MODELS[DEFAULT_BY_KIND[kind]]
    spec = MODELS.get(name)
    if spec is None:
        raise NotFound(
            f"The model '{name}' does not exist or you do not have access to it.",
            param="model", code="model_not_found",
        )
    if spec.kind != kind:
        raise InvalidRequest(f"model '{name}' is a {spec.kind} model, expected {kind}")
    return spec
