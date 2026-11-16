"""Service container wired once at startup and stored on ``app.state.services``."""

from __future__ import annotations

from dataclasses import dataclass

from ..accounts.pool import AccountPool
from ..accounts.store import JsonAccountStore
from ..auth.keys import KeyStore
from ..config import Settings
from ..core.matting import Matting
from ..core.media import MediaStore
from ..drivers.base import MuseDriver
from ..drivers.registry import create_driver
from .chat_store import ChatCompletionStore
from .file_store import FileStore
from .gateway import Gateway
from .request_log import RequestLog, current_record
from .response_store import ResponseStore
from .tasks import Task, TaskManager
from .upload_store import UploadStore


@dataclass
class Services:
    settings: Settings
    driver: MuseDriver
    pool: AccountPool
    gateway: Gateway
    tasks: TaskManager
    responses: ResponseStore
    completions: ChatCompletionStore
    media: MediaStore
    files: FileStore
    uploads: UploadStore
    matting: Matting
    keys: KeyStore
    requests: RequestLog

    @classmethod
    def build(cls, settings: Settings, driver: MuseDriver | None = None) -> Services:
        settings.ensure_dirs()
        driver = driver or create_driver(settings)
        pool = AccountPool(
            JsonAccountStore(settings.accounts_file),
            strategy=settings.pool_strategy,
            max_concurrency=settings.account_max_concurrency,
            cooldown=settings.account_cooldown,
            acquire_timeout=settings.pool_acquire_timeout,
        )
        requests = RequestLog(settings.requests_db)

        async def record_outcome(task: Task) -> None:
            record = current_record.get()
            await requests.finish_task(task.id, task.status.value, task.updated_at,
                                       (task.error or {}).get("message"),
                                       record.get("account_id") if record else None)

        files = FileStore(settings.files_dir, settings.files_file,
                          max_bytes=settings.file_max_bytes,
                          ttl_seconds=settings.files_retention_days * 86400)
        return cls(
            settings=settings,
            driver=driver,
            pool=pool,
            gateway=Gateway(pool, driver, max_failover=settings.max_failover),
            tasks=TaskManager(settings.tasks_file, on_finish=record_outcome),
            responses=ResponseStore(settings.responses_file, ttl_seconds=settings.response_ttl),
            completions=ChatCompletionStore(settings.chat_completions_file,
                                            ttl_seconds=settings.chat_store_ttl),
            media=MediaStore(settings.media_dir),
            files=files,
            uploads=UploadStore(settings.uploads_dir, settings.uploads_file, files),
            matting=Matting(settings.matting_model),
            keys=KeyStore(settings.keys_file),
            requests=requests,
        )
