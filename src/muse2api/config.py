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

    # --- storage ---
    data_dir: Path = Path("data")
    request_log_retention_days: float = Field(
        default=14, description="Days of /v1/* request history kept in data/requests.db."
    )
    media_retention_days: float = Field(
        default=14, description="Days generated media files are kept in data/media. "
        "0 disables automatic cleanup."
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

    # --- AstrBot / agent-client compatibility ---
    astrbot_compat: Literal["auto", "on", "off"] = Field(
        default="auto",
        description="'auto' applies the AstrBot prompt/sanitizer layer when the "
        "x-astrbot-conversation-id header or AstrBot's scene XML is present; "
        "'on' always applies it; 'off' disables it.",
    )
    astrbot_sanitize: bool = Field(
        default=True,
        description="Strip muse.ai's out-of-character disclaimers and replace the "
        "account owner's nickname in replies to AstrBot.",
    )
    astrbot_buffer_stream: bool = Field(
        default=True,
        description="Buffer an AstrBot streaming reply so disclaimers can be removed "
        "before the first chunk reaches the client.",
    )
    astrbot_aliases: str = Field(
        default="小红,Xiaohong,小紅",
        description="Comma-separated nicknames from the account owner's memory that "
        "must be replaced with the current AstrBot user's nickname.",
    )
    astrbot_max_prompt_chars: int = Field(
        default=18000,
        description="Upper bound on a transformed AstrBot prompt; older history is "
        "trimmed first.",
    )
    astrbot_persona_sticky: bool = Field(
        default=False,
        description="Keep one thread when the persona changes. Off by default: a "
        "different persona opens a fresh thread so the old identity cannot leak.",
    )
    emulate_tools: bool = Field(
        default=True,
        description="Emulate OpenAI tool calling by asking the model for a JSON call "
        "intent and parsing it. muse.ai itself never executes client tools.",
    )
    astrbot_tool_retries: int = Field(
        default=2,
        description="Retries when muse.ai refuses an emulated tool call instead of "
        "returning JSON. Each retry uses a fresh thread and a strict exam prompt.",
    )
    astrbot_persona_retries: int = Field(
        default=1,
        description="Retries when muse.ai refuses the persona (e.g. calls a fictional "
        "minor unsafe); the retry uses a style-only prompt.",
    )

    # --- account pool ---
    pool_strategy: PoolStrategyName = "affinity"
    pool_acquire_timeout: float = 60.0
    account_max_concurrency: int = 4
    account_cooldown: float = 120.0
    max_failover: int = 2

    # --- background removal (images with background="transparent") ---
    matting_model: str = Field(
        default="birefnet-general",
        description="rembg model used to cut out the subject, e.g. birefnet-general "
        "(best) or birefnet-general-lite (faster).",
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
        for d in (self.data_dir, self.media_dir):
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
