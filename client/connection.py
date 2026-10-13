"""Соединение клиента с сервером: подключение, отправка и приём кадров.

Проблема границ сообщений симметрична: ответ сервера точно так же может прийти
по частям или слипнуться со следующим. Поэтому клиент использует тот же
накопитель кадров, что и сервер, — из общего модуля.

Прочитать «один раз и разобрать» нельзя, даже когда ответ короткий и на
локальной машине почти всегда приходит целиком.
"""
from __future__ import annotations

import socket
from collections import deque
from types import TracebackType

from common.framing import MAX_FRAME_SIZE, FrameReader, encode_frame

DEFAULT_RECV_SIZE = 64 * 1024
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_REPLY_TIMEOUT = 10.0


class ServerClosedConnection(Exception):
    """Сервер закрыл соединение до того, как пришёл ответ."""


class ServerConnection:
    """Обёртка над сокетом, работающая сообщениями, а не байтами."""

    def __init__(
        self,
        sock: socket.socket,
        *,
        recv_size: int = DEFAULT_RECV_SIZE,
        max_frame_size: int = MAX_FRAME_SIZE,
    ) -> None:
        self._sock = sock
        self._recv_size = recv_size
        self._reader = FrameReader(max_frame_size)
        # Одно чтение может принести несколько кадров. Лишние нужно сохранить,
        # а не выбросить, иначе ответы начнут теряться при нагрузке.
        self._ready: deque[bytes] = deque()

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        reply_timeout: float = DEFAULT_REPLY_TIMEOUT,
    ) -> ServerConnection:
        """Установить соединение с сервером."""
        sock = socket.create_connection((host, port), timeout=connect_timeout)
        sock.settimeout(reply_timeout)
        return cls(sock)

    def send(self, payload: bytes) -> None:
        """Отправить одно сообщение."""
        self._sock.sendall(encode_frame(payload))

    def receive(self) -> bytes:
        """Дождаться одного сообщения целиком.

        Бросает ServerClosedConnection, если сервер закрыл соединение,
        и TimeoutError, если ответа не было слишком долго.
        """
        while not self._ready:
            chunk = self._sock.recv(self._recv_size)
            if not chunk:
                raise ServerClosedConnection("Сервер закрыл соединение")
            self._ready.extend(self._reader.feed(chunk))
        return self._ready.popleft()

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> ServerConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
