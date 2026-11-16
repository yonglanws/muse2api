"""Driver interface: the only layer that knows how to talk to muse.ai.

A driver turns normalised requests into upstream actions for one account at a
time. Scheduling, retries, persistence and the OpenAI wire format all live
above this layer, so a new transport (browser automation, direct HTTP/WebSocket
protocol, ...) only needs to implement this class.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..accounts.model import Account
from ..errors import FeatureNotImplemented

ProgressCallback = Callable[[int], None]
"""Receives a 0-100 progress estimate."""


@dataclass
class InputImage:
    data: bytes
    mime: str = "image/png"


@dataclass
class GeneratedMedia:
    """A file the agent produced inside a chat turn (image, video or document).

    muse.ai shows these inline in its web UI (``delta.presentation``); the
    gateway re-hosts the bytes so OpenAI-shaped clients can render them too.
    """

    data: bytes
    mime: str
    kind: str = "image"
    label: str = ""


@dataclass
class Citation:
    """A web source the agent actually read during the turn."""

    url: str
    title: str = ""


@dataclass
class ChatRequest:
    prompt: str
    model: str
    images: list[InputImage] = field(default_factory=list)
    timeout: float = 300.0
    first_token_timeout: float = 75.0
    conversation_hint: str | None = None
    turns: list[tuple[str, str]] = field(default_factory=list)
    """User/assistant turns of the request, used to continue a hot page instead of
    resending history. System prompts and tool plumbing are excluded."""
    followup_prompt: str | None = None
    """Text to send when the page already holds this conversation. None means
    send the full ``prompt`` instead."""
    tool_continuation: bool = False
    """The newest message is a tool result, not a new user turn."""
    images_current_only: bool = False
    """``images`` contains only the newest user turn, so a follow-up must attach
    all of them instead of slicing off images already on the page."""
    page_echo: str | None = None
    """Short text from the newest user turn as it appears on the muse page; used to
    recognise a live continuation when the thread URL has not updated yet."""
    force_new_thread: bool = False
    """Retry path: never continue the page even when the turns match, so a refused
    reply cannot poison the retry."""
    system_fingerprint: str | None = None
    """Changes when the system prompt changes, so a new instruction set never
    continues a thread created under different instructions."""
    tools: list[dict[str, Any]] | None = None
    """Client tool schemas; the gateway passes them through for prompt building."""
    system_prompt: str | None = None
    """System/developer text already folded into the prompt (for logging/tools)."""
    cancel: asyncio.Event | None = None
    media: list[GeneratedMedia] = field(default_factory=list)
    """Inline artifacts produced by the turn (populated by the driver)."""
    citations: list[Citation] = field(default_factory=list)
    """Web sources read during the turn (populated by the driver)."""


@dataclass
class ImageRequest:
    prompt: str
    model: str
    size: str | None = None
    """Aspect ratio such as ``1:1`` / ``16:9`` or an OpenAI size like ``1024x1024``."""
    n: int = 1
    reference_images: list[InputImage] = field(default_factory=list)
    timeout: float = 240.0
    on_progress: ProgressCallback | None = None
    cancel: asyncio.Event | None = None


@dataclass
class VideoRequest:
    prompt: str
    model: str
    size: str | None = None
    duration: int | None = None
    first_frame: InputImage | None = None
    timeout: float = 600.0
    on_progress: ProgressCallback | None = None
    cancel: asyncio.Event | None = None


@dataclass
class MediaResult:
    data: bytes
    mime: str
    kind: Literal["image", "video"]
    width: int | None = None
    height: int | None = None
    revised_prompt: str | None = None


@dataclass
class SessionInfo:
    ok: bool
    cookies: dict[str, str] = field(default_factory=dict)
    cookie_expires: dict[str, int] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DriverCapabilities:
    chat: bool = False
    chat_images: bool = False
    image: bool = False
    image_edit: bool = False
    video: bool = False
    renew_session: bool = False
    quota: bool = False


class MuseDriver(ABC):
    name: str = "base"
    requires_account: bool = True
    capabilities: DriverCapabilities = DriverCapabilities()

    async def startup(self) -> None:  # noqa: B027
        """Acquire long-lived resources (browser process, HTTP client, ...)."""

    async def shutdown(self) -> None:  # noqa: B027
        """Release resources acquired in ``startup``."""

    def prewarm(self, account: Account) -> None:  # noqa: B027
        """Start getting ``account`` ready for its first request in the background."""

    async def health(self) -> dict[str, Any]:
        return {"driver": self.name, "ok": True}

    @abstractmethod
    def chat_stream(self, account: Account, req: ChatRequest) -> AsyncIterator[str]:
        """Yield assistant text deltas. Implementations are async generators."""

    async def generate_image(self, account: Account, req: ImageRequest) -> list[MediaResult]:
        raise FeatureNotImplemented(f"driver '{self.name}' does not support image generation")

    async def generate_video(self, account: Account, req: VideoRequest) -> MediaResult:
        raise FeatureNotImplemented(f"driver '{self.name}' does not support video generation")

    async def renew_session(self, account: Account) -> SessionInfo:
        raise FeatureNotImplemented(f"driver '{self.name}' does not support session renewal")

    async def quota(self, account: Account) -> dict[str, Any]:
        raise FeatureNotImplemented(f"driver '{self.name}' does not support quota lookup")
