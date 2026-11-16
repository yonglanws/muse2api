"""OpenAI-compatible request schemas. Unknown fields are accepted and ignored so that
clients sending newer parameters keep working."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChatMessage(_Lenient):
    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None


class ChatCompletionRequest(_Lenient):
    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    user: str | None = None
    # Accepted for compatibility; muse.ai does not expose sampling or tool controls.
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    # OpenAI's current spelling; takes precedence over the deprecated max_tokens.
    max_completion_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
    # Legacy function-calling surface, still used by older SDKs/clients.
    functions: list[dict[str, Any]] | None = None
    function_call: Any = None
    # Built-in web search (Chat Completions spelling) → muse's native browser.
    web_search_options: dict[str, Any] | None = None
    service_tier: str | None = None
    metadata: dict[str, Any] | None = None
    # Stored Chat Completions (OpenAI's post-2025 surface). Implemented by
    # ChatCompletionStore; store=false keeps the old stateless behavior.
    store: bool | None = None
    # Audio output is not available upstream; explicit audio requests are
    # rejected instead of silently producing text.
    modalities: list[str] | None = None
    audio: dict[str, Any] | None = None
    # Executed parameters.
    stop: str | list[str] | None = None
    n: int | None = Field(default=None, ge=1, le=8)
    response_format: dict[str, Any] | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None


class ChatCompletionUpdateRequest(_Lenient):
    """POST /v1/chat/completions/{id}: re-label a stored completion."""
    metadata: dict[str, Any] | None = None


class ImageGenerationRequest(_Lenient):
    prompt: str = Field(min_length=1)
    model: str | None = None
    n: int = Field(default=1, ge=1, le=10)
    size: str | None = None
    response_format: Literal["url", "b64_json"] = "url"
    # OpenAI's image background values. "transparent" cuts the subject out locally
    # and returns an RGBA PNG; "auto"/"opaque" leave the image as generated.
    background: Literal["auto", "opaque", "transparent"] | None = None
    quality: Literal["low", "medium", "high", "xhigh", "max"] | str | None = None
    output_format: Literal["png", "jpeg", "webp"] | None = None
    output_compression: int | None = Field(default=None, ge=0, le=100)
    stream: bool = False
    safety_identifier: str | None = None
    image: str | list[str] | None = Field(
        default=None, description="Reference image(s) as data URL, http(s) URL or base64 (max 4)")
    # A sync call can outlive a proxy's timeout (Cloudflare cuts at ~100 s with 524), so
    # "async": true returns a task to poll at GET /v1/images/generations/{task_id}.
    async_: bool = Field(default=False, alias="async")
    user: str | None = None


class VideoCreateRequest(_Lenient):
    prompt: str = Field(min_length=1)
    model: str | None = None
    size: str | None = None
    duration: int | None = Field(default=None, ge=1, le=60)
    seconds: int | str | None = None
    image: str | dict[str, Any] | None = Field(
        default=None, description="First frame as data URL/http(s)/base64 or a file reference")
    input_reference: str | dict[str, Any] | None = None


class VideoRemixRequest(BaseModel):
    prompt: str = Field(min_length=1)


class AccountCreate(BaseModel):
    label: str = ""
    # Three accepted formats (TODO D1):
    #   1. dict {"name": "value"}
    #   2. raw "Cookie" request header string "a=1; b=2"
    #   3. list of cookie-export entries (e.g. EditThisCookie):
    #      [{"name": ..., "value": ..., "expirationDate": ...}, ...]
    cookies: dict[str, str] | str | list[dict[str, Any]]
    cookie_expires: dict[str, int] = Field(default_factory=dict)
    enabled: bool = True


class AccountsImportRequest(BaseModel):
    """Bulk cookie import: every entry is validated like AccountCreate; entries that
    fail are reported individually without aborting the batch."""
    accounts: list[AccountCreate] = Field(min_length=1)


class AccountUpdate(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    cookies: dict[str, str] | None = None
    cookie_expires: dict[str, int] | None = None


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


class KeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)
    revoked: bool | None = None
