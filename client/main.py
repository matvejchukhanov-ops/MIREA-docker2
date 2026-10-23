"""Точка входа клиента файлового менеджера.

Запуск:
    python -m client.main
    python -m client.main --host 127.0.0.1 --port 9000

Список команд — help внутри клиента.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from typing import Any

from common.framing import FrameTooLongError

from .commands import (
    END_OF_TEXT,
    Input,
    RawRequest,
    UsageError,
    build_request,
    help_text,
    local_action,
)
from .connection import ServerClosedConnection, ServerConnection
from .render import render

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000

# Команды записи, для которых клиент предлагает перезапись, если файл уже есть.
OVERWRITABLE = {"write", "json_write", "xml_write", "zip"}


class ExchangeFailed(Exception):
    """Обмен с сервером прерван: продолжать сеанс нельзя."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="client", description="Клиент файлового менеджера")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Адрес сервера")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Порт сервера")
    return parser


def console_input() -> Input:
    return Input(password=_read_password, text_block=_read_block, confirm=_confirm)


def _read_password(prompt: str) -> str:
    # getpass не показывает вводимые символы. Пароль на экране — пароль
    # в истории терминала и на записи демонстрации экрана.
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    # Ввод перенаправлен из файла (сценарий проверки). getpass на Windows
    # читает прямо с консоли в обход stdin и ждал бы нажатий вечно.
    return input(prompt)


def _read_block(prompt: str) -> str:
    print(f"{prompt}. Конец ввода — строка «{END_OF_TEXT}»:")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == END_OF_TEXT:
            break
        lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes", "д", "да"}
    except EOFError:
        return False


def exchange(connection: ServerConnection, request: dict[str, Any]) -> Any:
    """Отправить запрос и дождаться разобранного ответа."""
    if isinstance(request, RawRequest):
        payload = request.raw
    else:
        payload = json.dumps(request, ensure_ascii=False)
    try:
        connection.send(payload.encode("utf-8"))
        reply = connection.receive()
    except FrameTooLongError as exc:
        raise ExchangeFailed(f"Ответ сервера не укладывается в допустимый размер: {exc}") from exc
    except TimeoutError as exc:
        raise ExchangeFailed("Сервер не ответил за отведённое время") from exc
    except ServerClosedConnection as exc:
        raise ExchangeFailed("Сервер закрыл соединение") from exc
    except OSError as exc:
        raise ExchangeFailed(f"Ошибка соединения: {exc}") from exc

    try:
        return json.loads(reply.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "error", "code": "BAD_REPLY", "message": f"Ответ не JSON: {reply!r}"}


def run_session(connection: ServerConnection, source: Input) -> None:
    """Цикл «ввод — запрос — ответ»."""
    print("Подключено. help — список команд, quit — выход.\n")
    username: str | None = None

    while True:
        try:
            line = input(f"{username or ''}> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not line.strip():
            continue

        action = local_action(line)
        if action == "cls":
            clear_screen()
            continue
        if action == "help":
            print(help_text())
            continue

        try:
            request = build_request(line, source)
        except UsageError as exc:
            print(exc)
            continue
        except (EOFError, KeyboardInterrupt):
            print("\nОтменено")
            continue

        if request is None:
            print(help_text())
            continue

        try:
            data = exchange(connection, request)
            if _offers_overwrite(request, data) and source.confirm("Файл уже существует. Перезаписать?"):
                data = exchange(connection, {**request, "overwrite": True})
        except ExchangeFailed as exc:
            print(exc)
            return

        print(render(request, data))

        if isinstance(data, dict) and data.get("status") == "ok":
            if request.get("cmd") in {"login", "register"}:
                username = data["result"]["username"]
            elif request.get("cmd") == "logout":
                username = None

        if request.get("cmd") == "quit":
            return


def clear_screen() -> None:
    """Очистить окно терминала."""
    if not sys.stdout.isatty():
        # Вывод перенаправлен в файл — там очищать нечего.
        return
    if os.name == "nt":
        # Консоль Windows не всегда понимает управляющие последовательности
        # ANSI, а встроенная команда cls работает везде. Список аргументов,
        # а не строка для оболочки: os.system("...") — классическое место
        # для внедрения команд, даже если сегодня строка постоянная.
        subprocess.run(["cmd", "/c", "cls"], check=False)
    else:
        # Перевести курсор в начало и стереть экран.
        sys.stdout.write("\033[H\033[2J")
        sys.stdout.flush()


def _offers_overwrite(request: dict[str, Any], data: Any) -> bool:
    return (
        request.get("cmd") in OVERWRITABLE
        and not request.get("overwrite")
        and isinstance(data, dict)
        and data.get("code") == "ALREADY_EXISTS"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _force_utf8_output()

    try:
        with ServerConnection.connect(args.host, args.port) as connection:
            run_session(connection, console_input())
    except (ConnectionRefusedError, TimeoutError):
        print(f"Сервер {args.host}:{args.port} недоступен. Запущен ли он?", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Ошибка соединения: {exc}", file=sys.stderr)
        return 1

    print("Отключено.")
    return 0


def _force_utf8_output() -> None:
    # Та же причина, что у сервера: при выводе в файл или канал Windows
    # берёт кодировку системы, и кириллица превращается в мусор.
    streams = [sys.stdout]
    # Ввод из консоли Python читает в Юникоде сам. Из файла или канала —
    # в кодировке системы (cp1251), и UTF-8 сценария искажался бы.
    if not sys.stdin.isatty():
        streams.append(sys.stdin)
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
