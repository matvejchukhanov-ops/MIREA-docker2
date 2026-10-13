"""Сборка зависимостей сервера.

Всё, что живёт дольше одного соединения, создаётся здесь один раз при
запуске и передаётся обработчикам команд. Объекты либо неизменяемы после
создания, либо потокобезопасны сами (база упорядочивает запись, у файлов
свои блокировки), поэтому их можно делить между потоками клиентов.
"""
from __future__ import annotations

from dataclasses import dataclass

from .auth.service import AuthService
from .config import ServerConfig
from .db import AuditLog, Database, FileRegistry, UserRepository
from .fs.archive import ZipLimits
from .fs.manager import FileManager


@dataclass(frozen=True, slots=True)
class Services:
    config: ServerConfig
    db: Database
    auth: AuthService
    files: FileManager
    registry: FileRegistry
    audit: AuditLog
    zip_limits: ZipLimits

    @classmethod
    def build(cls, config: ServerConfig) -> Services:
        db = Database(config.db_path)
        db.initialize()
        return cls(
            config=config,
            db=db,
            auth=AuthService(
                UserRepository(db),
                max_failures=config.max_login_failures,
                lockout_seconds=config.lockout_seconds,
            ),
            files=FileManager(config.storage_root, max_file_size=config.max_file_size),
            registry=FileRegistry(db),
            audit=AuditLog(db),
            zip_limits=ZipLimits(
                max_entries=config.zip_max_entries,
                max_total_size=config.zip_max_total_size,
                max_ratio=config.zip_max_ratio,
            ),
        )
