"""Сквозные тесты: настоящие сокеты, настоящий сервер, настоящая база.

Юнит-тесты проверяют части по отдельности; здесь проверяется, что они
складываются в работающую систему. Сервер поднимается в отдельном потоке
на свободном порту с хранилищем и базой во временном каталоге, клиенты
подключаются через реальный TCP.

Запуск:
    python -m unittest discover
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
import unittest

from common.framing import FrameReader, encode_frame
from server.config import ServerConfig, frame_size_for
from server.tcp_server import TCPServer

from .support import PASSWORD, TempDirTestCase

CONNECT_TIMEOUT = 5.0
FILE_LIMIT = 16 * 1024
FRAME_LIMIT = frame_size_for(FILE_LIMIT)


def free_port() -> int:
    """Занять и сразу отпустить порт, чтобы узнать свободный номер."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Peer:
    """Минимальный клиент протокола для тестов."""

    def __init__(self, host: str, port: int) -> None:
        self._sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        self._sock.settimeout(CONNECT_TIMEOUT)
        self._reader = FrameReader(FRAME_LIMIT)
        self._ready: list[bytes] = []

    def send_raw(self, data: bytes) -> None:
        self._sock.sendall(data)

    def send(self, request: dict) -> None:
        self.send_raw(encode_frame(json.dumps(request, ensure_ascii=False).encode("utf-8")))

    def receive(self) -> dict:
        while not self._ready:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("сервер закрыл соединение")
            self._ready.extend(self._reader.feed(chunk))
        return json.loads(self._ready.pop(0).decode("utf-8"))

    def ask(self, request: dict) -> dict:
        self.send(request)
        return self.receive()

    def ok(self, request: dict) -> object:
        reply = self.ask(request)
        assert reply["status"] == "ok", reply
        return reply["result"]

    def server_closed(self) -> bool:
        try:
            return self._sock.recv(4096) == b""
        except OSError:
            return False

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "Peer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ServerTestCase(TempDirTestCase):
    """Поднимает сервер на время теста."""

    max_connections = 64

    def setUp(self) -> None:
        super().setUp()
        # Сервер намеренно пишет в лог предупреждения об отказах и нарушениях
        # протокола. В выводе тестов они выглядели бы как сбои, хотя это
        # ожидаемое поведение, которое тесты и проверяют.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

        self.port = free_port()
        self.config = ServerConfig(
            host="127.0.0.1",
            port=self.port,
            max_connections=self.max_connections,
            max_frame_size=FRAME_LIMIT,
            max_file_size=FILE_LIMIT,
            storage_root=self.tmp / "storage",
            db_path=self.tmp / "fm.db",
            log_level="ERROR",
        )
        self.server = TCPServer(self.config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_until_listening()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        super().tearDown()

    def connect(self) -> Peer:
        return Peer("127.0.0.1", self.port)

    def connect_as(self, username: str, *, register: bool = False) -> Peer:
        peer = self.connect()
        cmd = "register" if register else "login"
        peer.ok({"cmd": cmd, "username": username, "password": PASSWORD})
        return peer

    def _wait_until_listening(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.02)
        self.fail("Сервер не начал слушать порт")


class ProtocolOverSocketTests(ServerTestCase):
    def test_ping_без_входа(self) -> None:
        with self.connect() as peer:
            self.assertEqual(peer.ask({"cmd": "ping"}), {"status": "ok", "result": "pong"})

    def test_файлы_без_входа_закрыты(self) -> None:
        with self.connect() as peer:
            self.assertEqual(peer.ask({"cmd": "ls"})["code"], "AUTH_REQUIRED")

    def test_идентификатор_возвращается(self) -> None:
        with self.connect() as peer:
            self.assertEqual(peer.ask({"cmd": "ping", "id": "x"})["id"], "x")

    def test_ошибка_не_рвёт_соединение(self) -> None:
        with self.connect_as("alice", register=True) as peer:
            self.assertEqual(peer.ask({"cmd": "read", "path": "nope.txt"})["code"], "NOT_FOUND")
            # Соединение обязано пережить ошибку в данных клиента.
            self.assertEqual(peer.ask({"cmd": "ping"})["result"], "pong")

    def test_конвейер_из_двух_запросов(self) -> None:
        with self.connect() as peer:
            peer.send_raw(
                encode_frame(b'{"cmd":"ping","id":1}') + encode_frame(b'{"cmd":"ls","id":2}')
            )
            first, second = peer.receive(), peer.receive()
            self.assertEqual((first["id"], first["result"]), (1, "pong"))
            self.assertEqual((second["id"], second["code"]), (2, "AUTH_REQUIRED"))

    def test_quit_закрывает_соединение(self) -> None:
        with self.connect() as peer:
            self.assertEqual(peer.ask({"cmd": "quit"})["result"], "bye")
            self.assertTrue(peer.server_closed())

    def test_превышение_кадра(self) -> None:
        with self.connect() as peer:
            peer.send_raw(b"x" * (FRAME_LIMIT + 100))
            self.assertEqual(peer.receive()["code"], "FRAME_TOO_LONG")
            self.assertTrue(peer.server_closed())

    def test_дубликат_ключа_не_доходит_до_файла(self) -> None:
        # Клиента можно обойти и писать в сокет напрямую, поэтому проверка
        # обязана быть на сервере, а не только в клиенте.
        with self.connect_as("alice", register=True) as peer:
            peer.send_raw(
                encode_frame(
                    b'{"cmd":"json_write","path":"role.json","data":{"role":"user","role":"admin"}}'
                )
            )
            self.assertEqual(peer.receive()["code"], "BAD_JSON")
            self.assertEqual(peer.ask({"cmd": "info", "path": "role.json"})["code"], "NOT_FOUND")

    def test_сервер_жив_после_нарушений(self) -> None:
        with self.connect() as peer:
            peer.send_raw(b"x" * (FRAME_LIMIT + 100))
            peer.receive()
        with self.connect() as peer:
            self.assertEqual(peer.ask({"cmd": "ping"})["result"], "pong")


class FileManagerOverSocketTests(ServerTestCase):
    def test_сценарий_пользователя(self) -> None:
        with self.connect_as("alice", register=True) as peer:
            peer.ok({"cmd": "mkdir", "path": "docs"})
            peer.ok({"cmd": "write", "path": "docs/заметка.txt", "content": "строка 1\nстрока 2\n"})
            listing = peer.ok({"cmd": "ls", "path": "docs"})
            self.assertEqual([e["name"] for e in listing["entries"]], ["заметка.txt"])
            self.assertEqual(
                peer.ok({"cmd": "read", "path": "docs/заметка.txt"})["content"], "строка 1\nстрока 2\n"
            )
            peer.ok({"cmd": "json_write", "path": "data.json", "data": {"k": [1, 2]}})
            peer.ok({"cmd": "zip", "archive": "all.zip", "sources": ["docs", "data.json"]})
            unpacked = peer.ok({"cmd": "unzip", "archive": "all.zip", "destination": "copy"})
            self.assertEqual(len(unpacked["files"]), 2)
            self.assertEqual(peer.ok({"cmd": "json_read", "path": "copy/data.json"})["data"], {"k": [1, 2]})
            self.assertTrue(peer.ok({"cmd": "drives"}))

    def test_файл_на_пределе_размера(self) -> None:
        # Управляющие символы раздувают JSON в шесть раз — кадр это выдерживает.
        content = "\x01" * FILE_LIMIT
        with self.connect_as("alice", register=True) as peer:
            reply = peer.ask({"cmd": "write", "path": "big.txt", "content": content})
            self.assertEqual(reply["status"], "ok", reply)
            self.assertEqual(peer.ok({"cmd": "read", "path": "big.txt"})["content"], content)
            too_big = peer.ask({"cmd": "write", "path": "huge.txt", "content": "x" * (FILE_LIMIT + 1)})
            self.assertEqual(too_big["code"], "BAD_REQUEST")

    def test_сессия_живёт_в_соединении(self) -> None:
        with self.connect_as("alice", register=True):
            pass
        with self.connect() as peer:
            self.assertEqual(peer.ask({"cmd": "ls"})["code"], "AUTH_REQUIRED")

    def test_перебор_паролей_обрывает_соединение(self) -> None:
        with self.connect_as("alice", register=True):
            pass
        with self.connect() as peer:
            for _ in range(self.config.max_login_failures):
                reply = peer.ask({"cmd": "login", "username": "alice", "password": "wrong-pass-1"})
                self.assertIn(reply["code"], {"AUTH_FAILED", "ACCOUNT_LOCKED"})
            self.assertTrue(peer.server_closed())


class ConcurrencyTests(ServerTestCase):
    """Проверка того, ради чего появились потоки и блокировки."""

    def test_несколько_клиентов_одновременно(self) -> None:
        count = 5
        peers = [self.connect() for _ in range(count)]
        try:
            # Все подключены и никто ещё не отключился: одному потоку такое
            # не под силу — он завис бы на первом клиенте.
            deadline = time.monotonic() + 5
            while self.server.active_connections < count and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.server.active_connections, count)
            for peer in peers:
                self.assertEqual(peer.ask({"cmd": "ping"})["result"], "pong")
        finally:
            for peer in peers:
                peer.close()

    def test_два_соединения_создают_один_файл(self) -> None:
        # Один пользователь, два окна клиента, одновременное создание файла:
        # ровно одно должно преуспеть, второе — получить ALREADY_EXISTS.
        with self.connect_as("alice", register=True):
            pass
        codes: list[str] = []
        barrier = threading.Barrier(4)
        guard = threading.Lock()

        def worker(index: int) -> None:
            with self.connect_as("alice") as peer:
                barrier.wait(timeout=5)
                reply = peer.ask({"cmd": "write", "path": "shared.txt", "content": f"окно {index}"})
                with guard:
                    codes.append(reply.get("code", "OK"))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(sorted(codes), ["ALREADY_EXISTS"] * 3 + ["OK"])

        with self.connect_as("alice") as peer:
            ops = peer.ok({"cmd": "ops"})
            self.assertEqual([op["operation_type"] for op in ops], ["create"])


class ConnectionLimitTests(ServerTestCase):
    max_connections = 2

    def test_лишний_клиент_получает_внятный_отказ(self) -> None:
        # Молчаливый разрыв выглядел бы для клиента как поломка сети.
        held = [self.connect() for _ in range(2)]
        try:
            deadline = time.monotonic() + 5
            while self.server.active_connections < 2 and time.monotonic() < deadline:
                time.sleep(0.02)

            with self.connect() as extra:
                reply = extra.receive()
                self.assertEqual(reply["status"], "error")
                self.assertEqual(reply["code"], "SERVER_BUSY")

            # Освободилось место — новый клиент обслуживается.
            held.pop().close()
            deadline = time.monotonic() + 5
            while self.server.active_connections >= 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            with self.connect() as peer:
                self.assertEqual(peer.ask({"cmd": "ping"})["result"], "pong")
        finally:
            for peer in held:
                peer.close()


if __name__ == "__main__":
    unittest.main()
