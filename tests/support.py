"""Общее для тестов: временное хранилище, база и вызов команд без сети."""
from __future__ import annotations

import logging
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from server.app import Services
from server.config import ServerConfig
from server.dispatcher import Dispatcher
from server.handlers import build_dispatcher
from server.protocol import ProtocolError, Request
from server.session import Session

PASSWORD = "correct-horse-1"


class TempDirTestCase(unittest.TestCase):
    """Отдельный временный каталог на каждый тест."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class ServicesTestCase(TempDirTestCase):
    """Настоящие база и хранилище во временном каталоге, но без сокетов."""

    config_overrides: dict[str, Any] = {}

    def setUp(self) -> None:
        super().setUp()
        # Ожидаемые отказы пишутся в лог предупреждениями; в выводе тестов
        # они выглядели бы как сбои, хотя тесты их и проверяют.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

        self.config = replace(
            ServerConfig(storage_root=self.tmp / "storage", db_path=self.tmp / "fm.db"),
            **self.config_overrides,
        )
        self.services = Services.build(self.config)
        self.dispatcher: Dispatcher = build_dispatcher(self.services)

    def new_session(self, peer: str = "127.0.0.1:5000") -> Session:
        return Session(peer=peer)

    def call(self, session: Session, cmd: str, **payload: Any) -> Any:
        request = Request(cmd=cmd, id=None, payload={"cmd": cmd, **payload})
        return self.dispatcher.dispatch(request, session)

    def call_error(self, session: Session, cmd: str, **payload: Any) -> str:
        """Вызвать команду, ожидая отказа. Возвращает код ошибки."""
        with self.assertRaises(ProtocolError) as ctx:
            self.call(session, cmd, **payload)
        return str(ctx.exception.code)

    def logged_in(self, username: str = "alice") -> Session:
        session = self.new_session()
        self.call(session, "register", username=username, password=PASSWORD)
        return session
