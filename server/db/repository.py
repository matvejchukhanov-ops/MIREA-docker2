"""Запросы к базе данных.

Все запросы — подготовленные выражения: значения передаются отдельно от
текста SQL через «?», и драйвер никогда не склеивает их со строкой запроса.
Поэтому логин вида  admin' OR '1'='1  остаётся просто строкой, которую
ищут в таблице, и не становится частью SQL.

В текст запроса не попадает ни одно значение извне. Единственное, что
к нему приклеивается, — константа NOW из этого же модуля. Тест
test_database разбирает исходник и проверяет, что первым аргументом
execute() везде стоят только строковые литералы и эта константа.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .database import Database

NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"


class UserExistsError(Exception):
    """Пользователь с таким логином уже есть."""


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: int
    username: str
    password_hash: str
    locked: bool


class UserRepository:
    """Таблица users."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, username: str, password_hash: str) -> int:
        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, password_hash),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            # Проверять «есть ли такой» перед вставкой бессмысленно: между
            # проверкой и вставкой успеет зарегистрироваться другой клиент.
            # Единственный надёжный арбитр — ограничение UNIQUE в самой базе.
            if "UNIQUE" in str(exc):
                raise UserExistsError(username) from exc
            raise

    def find(self, username: str) -> UserRecord | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash,"
                " (locked_until IS NOT NULL AND locked_until > " + NOW + ") AS locked"
                " FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return UserRecord(row["id"], row["username"], row["password_hash"], bool(row["locked"]))

    def register_failure(self, user_id: int, max_failures: int, lockout_seconds: int) -> bool:
        """Учесть неудачный вход. Возвращает True, если учётная запись заблокирована.

        Счётчик увеличивается в самой базе (failed_logins = failed_logins + 1),
        а не «прочитать, прибавить, записать» в Python: иначе параллельные
        попытки входа затирали бы друг друга и перебор шёл бы быстрее предела.
        """
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET failed_logins = failed_logins + 1 WHERE id = ?", (user_id,)
            )
            cursor = conn.execute(
                "UPDATE users SET locked_until = strftime('%Y-%m-%d %H:%M:%f', 'now', ?),"
                " failed_logins = 0"
                " WHERE id = ? AND failed_logins >= ?",
                (f"+{int(lockout_seconds)} seconds", user_id, max_failures),
            )
            return cursor.rowcount > 0

    def register_success(self, user_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?",
                (user_id,),
            )


@dataclass(frozen=True, slots=True)
class FileChange:
    """Изменение файла для записи в реестр."""

    location: str
    size: int
    created: bool


class FileRegistry:
    """Таблица files. Журнал operations заполняют триггеры схемы."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record_changes(self, owner_id: int, changes: list[FileChange]) -> None:
        """Записать созданные или изменённые файлы одной транзакцией.

        Распаковка архива порождает сотни файлов; запись их по одному
        оставила бы реестр наполовину заполненным при сбое посередине.
        """
        with self._db.transaction() as conn:
            for change in changes:
                self._record_change(conn, owner_id, change)

    def record_change(self, owner_id: int, change: FileChange) -> None:
        self.record_changes(owner_id, [change])

    def record_delete(self, owner_id: int, location: str) -> None:
        with self._db.transaction() as conn:
            row = self._live(conn, owner_id, location)
            if row is None:
                # Файл появился в обход сервера (например, положен на диск
                # вручную). Удаление всё равно должно оставить след.
                file_id = self._insert(conn, owner_id, location, 0)
            else:
                file_id = row["id"]
            conn.execute(
                "UPDATE files SET deleted_at = " + NOW + " WHERE id = ?", (file_id,)
            )

    def live_record(self, owner_id: int, location: str) -> dict[str, Any] | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT id, filename, location, size, created_at, modified_at"
                " FROM files WHERE owner_id = ? AND location = ? AND deleted_at IS NULL",
                (owner_id, location),
            ).fetchone()
        return dict(row) if row else None

    def operations(
        self, owner_id: int, limit: int, location: str | None = None
    ) -> list[dict[str, Any]]:
        """Последние операции пользователя, новые сверху."""
        with self._db.connect() as conn:
            if location is None:
                rows = conn.execute(
                    "SELECT id, timestamp, operation_type, file_id, filename, location"
                    " FROM operations_view WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, timestamp, operation_type, file_id, filename, location"
                    " FROM operations_view WHERE user_id = ? AND location = ?"
                    " ORDER BY id DESC LIMIT ?",
                    (owner_id, location, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    # --- Внутреннее ---

    def _record_change(self, conn: sqlite3.Connection, owner_id: int, change: FileChange) -> None:
        row = self._live(conn, owner_id, change.location)
        if row is not None and not change.created:
            conn.execute(
                "UPDATE files SET size = ?, modified_at = " + NOW + " WHERE id = ?",
                (change.size, row["id"]),
            )
            return
        if row is not None:
            # Файл создан заново на месте того, что был удалён в обход
            # сервера. Старую запись закрываем, чтобы история не смешалась.
            conn.execute("UPDATE files SET deleted_at = " + NOW + " WHERE id = ?", (row["id"],))
        self._insert(conn, owner_id, change.location, change.size)

    @staticmethod
    def _live(conn: sqlite3.Connection, owner_id: int, location: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT id FROM files WHERE owner_id = ? AND location = ? AND deleted_at IS NULL",
            (owner_id, location),
        ).fetchone()

    @staticmethod
    def _insert(conn: sqlite3.Connection, owner_id: int, location: str, size: int) -> int:
        filename = location.rsplit("/", 1)[-1]
        cursor = conn.execute(
            "INSERT INTO files (filename, size, location, owner_id) VALUES (?, ?, ?, ?)",
            (filename, size, location, owner_id),
        )
        return int(cursor.lastrowid)


class AuditLog:
    """Таблица audit_log: журнал активности пользователей."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        user_id: int | None,
        username: str | None,
        peer: str,
        action: str,
        target: str | None,
        success: bool,
        details: str | None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log (user_id, username, peer, action, target, success, details)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, peer, action, target, int(success), details),
            )

    def recent(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """Последние действия пользователя, новые сверху."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT id, timestamp, peer, action, target, success, details"
                " FROM audit_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [{**dict(row), "success": bool(row["success"])} for row in rows]
