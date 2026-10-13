"""Тесты диспетчера: поиск команды, контроль доступа, журнал аудита.

Запуск:
    python -m unittest discover
"""
from __future__ import annotations

import unittest
from typing import Any

from server.dispatcher import Dispatcher
from server.protocol import ErrorCode, ProtocolError, Request
from server.session import AuthenticatedUser, Session


def make_request(cmd: str, **payload: object) -> Request:
    return Request(cmd=cmd, id=None, payload={"cmd": cmd, **payload})


class MemoryAudit:
    """Журнал в памяти — чтобы видеть, что диспетчер в него пишет."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **fields: Any) -> None:
        self.records.append(fields)


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Session(peer="127.0.0.1:1234")

    def test_вызывает_зарегистрированный_обработчик(self) -> None:
        dispatcher = Dispatcher()
        dispatcher.register("double", lambda request, session: 42, public=True)
        self.assertEqual(dispatcher.dispatch(make_request("double"), self.session), 42)

    def test_обработчик_получает_запрос_и_сессию(self) -> None:
        seen: list[object] = []
        dispatcher = Dispatcher()
        dispatcher.register(
            "spy", lambda request, session: seen.append((request, session)), public=True
        )
        dispatcher.dispatch(make_request("spy"), self.session)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][1], self.session)

    def test_неизвестная_команда(self) -> None:
        dispatcher = Dispatcher()
        dispatcher.register("ping", lambda request, session: "pong", public=True)
        with self.assertRaises(ProtocolError) as ctx:
            dispatcher.dispatch(make_request("нет такой"), self.session)
        self.assertEqual(ctx.exception.code, ErrorCode.UNKNOWN_COMMAND)
        # Иначе клиенту негде узнать, что вообще поддерживается.
        self.assertIn("ping", ctx.exception.message)

    def test_повторная_регистрация_запрещена(self) -> None:
        # Тихая перезапись означала бы, что часть команд молча перестала
        # работать. Лучше упасть при запуске сервера.
        dispatcher = Dispatcher()
        dispatcher.register("ping", lambda request, session: "pong")
        with self.assertRaises(ValueError):
            dispatcher.register("ping", lambda request, session: "другое")


class AccessControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.dispatcher = Dispatcher()
        self.dispatcher.register("secret", lambda request, session: self.calls.append("secret"))
        self.dispatcher.register(
            "open", lambda request, session: self.calls.append("open"), public=True
        )

    def test_по_умолчанию_нужен_вход(self) -> None:
        # Команда, при регистрации которой забыли про доступ, закрыта, а не открыта.
        with self.assertRaises(ProtocolError) as ctx:
            self.dispatcher.dispatch(make_request("secret"), Session(peer="p"))
        self.assertEqual(ctx.exception.code, ErrorCode.AUTH_REQUIRED)
        self.assertEqual(self.calls, [])

    def test_после_входа_доступно(self) -> None:
        session = Session(peer="p", user=AuthenticatedUser(1, "ivan"))
        self.dispatcher.dispatch(make_request("secret"), session)
        self.assertEqual(self.calls, ["secret"])

    def test_открытая_команда_доступна_без_входа(self) -> None:
        self.dispatcher.dispatch(make_request("open"), Session(peer="p"))
        self.assertEqual(self.calls, ["open"])


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit = MemoryAudit()
        self.dispatcher = Dispatcher(audit=self.audit)
        self.session = Session(peer="10.0.0.1:5000", user=AuthenticatedUser(7, "ivan"))

    def test_успешная_команда_записывается(self) -> None:
        self.dispatcher.register("read", lambda request, session: "ok")
        self.dispatcher.dispatch(make_request("read", path="docs/a.txt"), self.session)
        self.assertEqual(
            self.audit.records,
            [{
                "user_id": 7, "username": "ivan", "peer": "10.0.0.1:5000", "action": "read",
                "target": "docs/a.txt", "success": True, "details": None,
            }],
        )

    def test_отказ_записывается_с_кодом(self) -> None:
        def fail(request: Request, session: Session) -> None:
            raise ProtocolError(ErrorCode.NOT_FOUND, "нет")

        self.dispatcher.register("read", fail)
        with self.assertRaises(ProtocolError):
            self.dispatcher.dispatch(make_request("read", path="x"), self.session)
        self.assertEqual(self.audit.records[0]["success"], False)
        self.assertEqual(self.audit.records[0]["details"], "NOT_FOUND")

    def test_попытка_без_входа_записывается(self) -> None:
        self.dispatcher.register("read", lambda request, session: "ok")
        with self.assertRaises(ProtocolError):
            self.dispatcher.dispatch(make_request("read", path="x"), Session(peer="p"))
        self.assertEqual(self.audit.records[0]["details"], "AUTH_REQUIRED")
        self.assertIsNone(self.audit.records[0]["user_id"])

    def test_пароль_в_журнал_не_попадает(self) -> None:
        self.dispatcher.register("login", lambda request, session: "ok", public=True)
        self.dispatcher.dispatch(
            make_request("login", username="ivan", password="tops3cret!"), Session(peer="p")
        )
        self.assertNotIn("tops3cret!", repr(self.audit.records))
        self.assertEqual(self.audit.records[0]["username"], "ivan")

    def test_служебная_команда_не_записывается(self) -> None:
        self.dispatcher.register("ping", lambda request, session: "pong", public=True, audited=False)
        self.dispatcher.dispatch(make_request("ping"), self.session)
        self.assertEqual(self.audit.records, [])

    def test_выход_записывается_от_имени_вышедшего(self) -> None:
        def logout(request: Request, session: Session) -> str:
            session.user = None
            return "ok"

        self.dispatcher.register("logout", logout)
        self.dispatcher.dispatch(make_request("logout"), self.session)
        self.assertEqual(self.audit.records[0]["user_id"], 7)

    def test_сбой_журнала_не_ломает_команду(self) -> None:
        class BrokenAudit:
            def record(self, **fields: Any) -> None:
                raise RuntimeError("диск заполнен")

        dispatcher = Dispatcher(audit=BrokenAudit())
        dispatcher.register("read", lambda request, session: "данные")
        with self.assertLogs("server.dispatcher", level="ERROR"):
            result = dispatcher.dispatch(make_request("read"), self.session)
        self.assertEqual(result, "данные")


if __name__ == "__main__":
    unittest.main()
