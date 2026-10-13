"""Подключение к SQLite и создание схемы.

Соединение открывается на каждую единицу работы и сразу закрывается.
Объект соединения sqlite3 нельзя делить между потоками, а сервер
обслуживает каждого клиента в своём потоке. Открытие файла базы стоит
десятки микросекунд — дешевле, чем следить за соединениями потоков.

Одновременную запись из разных потоков упорядочивает сама SQLite:
транзакция начинается с BEGIN IMMEDIATE, то есть сразу захватывает право
записи, и вторая такая транзакция ждёт первую до busy_timeout. Без
IMMEDIATE две транзакции могли бы прочитать одно и то же, а потом
одна из них упала бы на попытке записи с «database is locked».
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Сколько секунд ждать, пока другая транзакция освободит базу.
BUSY_TIMEOUT = 10.0


class Database:
    """Файл базы данных и способ с ним работать."""

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        """Создать файл базы и схему, если их ещё нет. Повторный вызов безопасен."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connect() as conn:
            # WAL: читатели не блокируют писателя и наоборот. Журнал аудита
            # пишется на каждую команду, и без этого чтение истории одним
            # клиентом тормозило бы работу всех остальных.
            conn.execute("PRAGMA journal_mode = WAL")
            # executescript — единственное место, где SQL выполняется без
            # параметров. Текст берётся из файла в репозитории, данных
            # клиента в нём нет.
            conn.executescript(schema)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Соединение для чтения или одиночного запроса."""
        conn = sqlite3.connect(self._path, timeout=BUSY_TIMEOUT, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            # Внешние ключи в SQLite выключены по умолчанию и включаются
            # для каждого соединения отдельно.
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Транзакция записи: всё или ничего."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
