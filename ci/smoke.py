"""Проверка развёрнутого сервера: он отвечает, регистрирует, принимает файлы.

Скрипт самодостаточный, только стандартная библиотека. В конвейере он
выполняется внутри контейнера приложения:

    docker exec -i -e SMOKE_PASSWORD filemanager python - < ci/smoke.py

Пароль приходит из переменной окружения SMOKE_PASSWORD — в коде его нет.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

HOST = os.environ.get("SMOKE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SMOKE_PORT", "9000"))
USERNAME = os.environ.get("SMOKE_USER", "smoke_user")
STARTUP_WAIT = 20.0


class SmokeError(Exception):
    pass


def connect() -> socket.socket:
    """Подключиться, дав серверу время подняться после docker run."""
    deadline = time.monotonic() + STARTUP_WAIT
    while True:
        try:
            sock = socket.create_connection((HOST, PORT), timeout=5)
            sock.settimeout(10)
            return sock
        except OSError:
            if time.monotonic() > deadline:
                raise SmokeError(f"Сервер {HOST}:{PORT} не отвечает {STARTUP_WAIT:.0f} с")
            time.sleep(0.5)


class Client:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._stream = sock.makefile("rb")

    def call(self, request: dict) -> dict:
        frame = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
        self._sock.sendall(frame)
        line = self._stream.readline()
        if not line:
            raise SmokeError("Сервер закрыл соединение")
        return json.loads(line)

    def ok(self, request: dict) -> object:
        reply = self.call(request)
        if reply.get("status") != "ok":
            raise SmokeError(f"{request['cmd']}: {reply.get('code')} {reply.get('message')}")
        print(f"  ok  {request['cmd']}")
        return reply["result"]


def main() -> int:
    password = os.environ.get("SMOKE_PASSWORD")
    if not password:
        print("SMOKE_PASSWORD не задан: передайте его через секреты раннера", file=sys.stderr)
        return 2

    with connect() as sock:
        client = Client(sock)
        client.ok({"cmd": "ping"})

        reply = client.call({"cmd": "register", "username": USERNAME, "password": password})
        if reply.get("status") == "ok":
            print("  ok  register")
        elif reply.get("code") == "USER_EXISTS":
            client.ok({"cmd": "login", "username": USERNAME, "password": password})
        else:
            raise SmokeError(f"register: {reply.get('code')} {reply.get('message')}")

        content = f"smoke {time.strftime('%Y-%m-%d %H:%M:%S')}"
        client.ok({"cmd": "write", "path": "smoke.txt", "content": content, "overwrite": True})
        read_back = client.ok({"cmd": "read", "path": "smoke.txt"})
        if content not in json.dumps(read_back, ensure_ascii=False):
            raise SmokeError("read вернул не то, что было записано")
        client.ok({"cmd": "ls"})
        client.ok({"cmd": "quit"})

    print("Smoke-проверка пройдена")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeError as exc:
        print(f"Smoke-проверка не пройдена: {exc}", file=sys.stderr)
        sys.exit(1)
