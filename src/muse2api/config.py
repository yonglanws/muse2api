"""Runtime configuration, loaded from environment variables (prefix ``MUSE2API_``) and ``.env``."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DriverName = Literal["mock", "browser", "http", "protocol"]
PoolStrategyName = Literal["round_robin", "lru", "affinity"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUSE2API_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 18610
    log_level: str = "INFO"
    public_base: str = Field(
        default="",
        description="Externally reachable base URL used when building media links. "
        "Empty means derive from the incoming request.",
    )

    # --- auth ---
    api_key: str = Field(default="", description="Bearer key for /v1/*. Auto-generated if empty.")
    admin_key: str = Field(default="", description="Bearer key for /admin/*. Falls back to api_key.")
    cors_origins: str = Field(
        default="*",
        description="Comma-separated origins allowed to call /v1/* from a browser. "
        "'*' mirrors api.openai.com's Access-Control-Allow-Origin: *; admin routes "
        "stay restricted to the cookie-import extension.",
    )

    # --- storage ---
    data_dir: Path = Path("data")
    request_log_retention_days: float = Field(
        default=14, description="Days of /v1/* request history kept in data/requests.db."
    )
    media_retention_days: float = Field(
        default=14, description="Days generated media files are kept in data/media. "
        "0 disables automatic cleanup."
    )
    response_ttl: int = Field(
        default=7 * 86400, description="Seconds a stored Responses API conversation "
        "stays continuable via previous_response_id (data/responses.json)."
    )
    chat_store_ttl: int = Field(
        default=30 * 86400, description="Seconds a Chat Completions response created "
        "with store=true stays retrievable (data/chat_completions.json). OpenAI keeps "
        "stored completions for at least 30 days."
    )
    file_max_bytes: int = Field(
        default=100 * 1024 * 1024,
        description="Largest file accepted by POST /v1/files (OpenAI's own limit "
        "is 512 MB; the gateway keeps uploads in data/files/).",
    )
    files_retention_days: float = Field(
        default=30, description="Days uploaded files are kept in data/files. "
        "0 disables automatic cleanup.",
    )

    # --- upstream driver ---
    driver: DriverName = "mock"
    site_url: str = "https://muse.ai"

    # browser driver
    chromium_path: str = ""
    cdp_port: int = 19210
    headless: bool = True
    browser_proxy: str = Field(
        default="",
        description="Proxy for the browser only, e.g. socks5://127.0.0.1:10808 or http://10.144.1.10:8080. "
        "Python's own calls (session renewal) follow http_proxy/https_proxy unless "
        "MUSE2API_UPSTREAM_PROXY is set.",
    )
    upstream_proxy: str = Field(
        default="",
        description="Proxy for Python's upstream HTTP calls (session renewal / VM wake). "
        "Empty falls back to MUSE2API_BROWSER_PROXY, then to the standard "
        "http_proxy/https_proxy environment variables.",
    )
    browser_profile_dir: Path | None = None
    page_ready_timeout: float = 45.0
    warm_threads: int = Field(
        default=2,
        description="Pre-started muse.ai threads kept per account. A new thread takes muse.ai "
        "20-40s to answer its first message, a started one about 5s, so a chat that opens a new "
        "thread goes into a warm one instead. Each costs one extra short message of quota. "
        "Needs MUSE2API_ACCOUNT_MAX_CONCURRENCY of at least 2; 0 disables.",
    )
    warm_prompt: str = Field(default="Hi", description="Message that starts a warm thread.")

    # protocol driver
    protocol_ping_interval: float = Field(
        default=30.0,
        description="Seconds between /api/ping frames on a protocol connection. 0 disables.",
    )
    protocol_wake_confirm: float = Field(
        default=20.0,
        description="How long to poll /api/session for RUNNING after waking the VM.",
    )
    protocol_thread_ttl: float = Field(
        default=21600.0,
        description="Seconds an idle protocol thread stays subscribable. 0 keeps threads forever.",
    )
    protocol_max_threads: int = Field(
        default=64,
        description="Threads kept per account; the oldest idle thread is closed beyond this.",
    )
    protocol_connections: int = Field(
        default=2,
        description="Noise WebSocket connections kept per account for parallel turns. "
        "muse.ai accepts several simultaneous connections (probed 0.8.0: a second "
        "connection never evicts the first) and runs agent turns in parallel, so a "
        "small pool spreads concurrent streams without one WebSocket becoming the "
        "bottleneck. Threads stick to the connection that created them.",
    )

    # --- timeouts (seconds) ---
    chat_timeout: float = 300.0
    first_token_timeout: float = 75.0
    stream_heartbeat_interval: float = Field(
        default=0.0,
        description="SSE ': keepalive' comment interval while waiting for the first "
        "stream token; 0 disables. Keeps idle clients/proxies from timing out.",
    )
    image_timeout: float = 240.0
    video_timeout: float = 600.0

    # --- system prompt bridge and OpenAI tools ---
    max_prompt_chars: int = Field(
        default=18000,
        description="Upper bound on the prompt built from system messages and history; "
        "older history is trimmed first.",
    )
    emulate_tools: bool = Field(
        default=True,
        description="Emulate OpenAI tool calling with a flat JSON planner when the "
        "native client-command channel is unavailable.",
    )
    tool_channel: Literal["auto", "native", "planner"] = Field(
        default="auto",
        description="'native' (and 'auto') registers OpenAI tools as native client "
        "commands (client.register_capabilities) so muse.ai calls them through its "
        "own client.invoke loop; 'planner' uses only the flat JSON planner prompt.",
    )
    tool_retries: int = Field(
        default=2,
        description="Retries when muse.ai refuses an emulated tool call instead of "
        "returning planner JSON. Each retry uses a fresh thread.",
    )
    native_harness: bool = Field(
        default=True,
        description="Protocol driver: keep a fenced muse2api-managed section in the "
        "account's VM harness files (IDENTITY.md / SOUL.md / AGENTS.md) so API "
        "sessions get system-instruction adherence, memory isolation and reliable "
        "client-command use. Originals are backed up under data/harness_backup/; "
        "/admin/harness can restore them.",
    )
    chat_media: bool = Field(
        default=True,
        description="Re-host artifacts the agent generates inside a chat turn "
        "(images/videos/documents from delta.presentation) and append them to the "
        "reply, matching muse.ai's web chat. The web UI renders these inline; an "
        "API-only reply would silently lose them.",
    )
    chat_media_max_bytes: int = Field(
        default=32 * 1024 * 1024,
        description="Skip inline chat artifacts larger than this when downloading "
        "them from the account VM (0 means no cap).",
    )

    # --- web-chat wire parity (probe 18, 2026-11-17) ---
    chat_wire_parity: bool = Field(
        default=True,
        description="Send the same chat.stream envelope the muse.ai web client "
        "sends: the account's selected model (model.get), the client timezone "
        "(drives the server-side time-tag developer message; the value is sticky "
        "upstream) and the web client's capabilities list.",
    )
    timezone: str = Field(
        default="",
        description="IANA timezone sent with every chat.stream turn when "
        "chat_wire_parity is on (for example Asia/Shanghai). Upstream prepends a "
        "[Day YYYY-MM-DD HH:MM:SS TZ] [client_timezone=...] developer message from "
        "it and keeps the last valid value per account; empty omits the field so "
        "the account's own sticky value applies.",
    )
    citation_markers: Literal["native", "footnote", "strip"] = Field(
        default="native",
        description="What happens to Muse's inline citation markers in replies "
        "(【url_id†L9】/【post-x】: the web UI renders them as source chips and the "
        "URLs already travel as annotations). native keeps them, footnote rewrites "
        "each distinct marker as [1]/[2]/…, strip removes them.",
    )

    # --- account pool ---
    pool_strategy: PoolStrategyName = "affinity"
    pool_acquire_timeout: float = 60.0
    account_max_concurrency: int = Field(
        default=12,
        description="Parallel requests allowed per account. muse.ai runs agent turns "
        "in parallel on the account VM (12 concurrent streams probed clean on a "
        "single connection), so this caps the gateway, not the upstream.",
    )
    account_cooldown: float = 120.0
    max_failover: int = 2

    # --- background removal (images with background="transparent") ---
    matting_model: str = Field(
        default="birefnet-general-lite",
        description="rembg model used to cut out the subject: birefnet-general-lite "
        "(fast default) or birefnet-general (best, ~1 GB model).",
    )

    # --- keepalive (session renewal) ---
    keepalive_enabled: bool = True
    keepalive_interval: float = 600

    @field_validator("browser_profile_dir", mode="before")
    @classmethod
    def _blank_profile_dir_is_unset(cls, value: object) -> object:
        """``.env.example`` ships ``MUSE2API_BROWSER_PROFILE_DIR=``; ``Path("")`` would be the
        working directory, so Chrome would scatter its profile across the repo root."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def accounts_file(self) -> Path:
        return self.data_dir / "accounts.json"

    @property
    def tasks_file(self) -> Path:
        return self.data_dir / "tasks.json"

    @property
    def responses_file(self) -> Path:
        return self.data_dir / "responses.json"

    @property
    def conversations_file(self) -> Path:
        return self.data_dir / "conversations.json"

    @property
    def chat_completions_file(self) -> Path:
        return self.data_dir / "chat_completions.json"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def files_file(self) -> Path:
        return self.data_dir / "files.json"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def uploads_file(self) -> Path:
        return self.data_dir / "uploads.json"

    @property
    def key_file(self) -> Path:
        return self.data_dir / "api_key"

    @property
    def keys_file(self) -> Path:
        return self.data_dir / "keys.json"

    @property
    def requests_db(self) -> Path:
        return self.data_dir / "requests.db"

    @property
    def profile_dir(self) -> Path:
        return self.browser_profile_dir or (self.data_dir / "browser-profile")

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.files_dir, self.uploads_dir):
            d.mkdir(parents=True, exist_ok=True)

    def resolve_api_key(self) -> str:
        """Return the configured key, or load/generate a persistent one under ``data_dir``."""
        if self.api_key:
            return self.api_key
        self.ensure_dirs()
        if self.key_file.is_file():
            key = self.key_file.read_text(encoding="utf-8").strip()
            if key:
                self.api_key = key
                return key
        key = "m2a-" + secrets.token_urlsafe(24)
        self.key_file.write_text(key + "\n", encoding="utf-8")
        self.key_file.chmod(0o600)
        self.api_key = key
        return key

    @property
    def effective_admin_key(self) -> str:
        return self.admin_key or self.api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
