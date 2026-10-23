"""Разбор введённой строки в запрос протокола.

Пароль и многострочное содержимое запрашиваются через переданные функции,
а не через input() напрямую: так разбор проверяется тестами без консоли.
"""
from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from common import strict_json

# Строка из одной точки завершает ввод многострочного текста — как в SMTP.
END_OF_TEXT = "."

FORCE_FLAGS = {"--force", "-f"}


class UsageError(ValueError):
    """Команда введена неверно. Сообщение — подсказка пользователю."""


class RawRequest(dict):
    """Запрос, введённый сырым JSON: уходит на сервер введённым текстом."""

    def __init__(self, data: dict[str, Any], *, raw: str) -> None:
        super().__init__(data)
        self.raw = raw


@dataclass(frozen=True, slots=True)
class Input:
    """Откуда клиент берёт то, что нельзя передать в строке команды."""

    password: Callable[[str], str]
    text_block: Callable[[str], str]
    confirm: Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    usage: str
    summary: str


COMMANDS: dict[str, CommandSpec] = {
    "help": CommandSpec("help", "список команд"),
    "cls": CommandSpec("cls (clear)", "очистить экран"),
    "register": CommandSpec("register <логин>", "регистрация (пароль спрашивается отдельно)"),
    "login": CommandSpec("login <логин>", "вход"),
    "logout": CommandSpec("logout", "выход из учётной записи"),
    "whoami": CommandSpec("whoami", "кто вошёл"),
    "drives": CommandSpec("drives", "диски сервера: объём, свободное место, файловая система"),
    "ls": CommandSpec("ls [путь]", "содержимое каталога"),
    "info": CommandSpec("info <путь>", "свойства файла или каталога"),
    "cat": CommandSpec("cat <путь>", "прочитать текстовый файл"),
    "write": CommandSpec("write <путь> [--force]", "записать текстовый файл"),
    "rm": CommandSpec("rm <путь>", "удалить файл"),
    "mkdir": CommandSpec("mkdir <путь>", "создать каталог"),
    "rmdir": CommandSpec("rmdir <путь>", "удалить пустой каталог"),
    "json-read": CommandSpec("json-read <путь>", "прочитать JSON"),
    "json-write": CommandSpec("json-write <путь> [--force]", "записать JSON"),
    "xml-read": CommandSpec("xml-read <путь>", "прочитать XML"),
    "xml-write": CommandSpec("xml-write <путь> [--force]", "записать XML"),
    "zip": CommandSpec("zip <архив.zip> <путь> [путь ...] [--force]", "упаковать в ZIP"),
    "zip-list": CommandSpec("zip-list <архив.zip>", "содержимое архива"),
    "unzip": CommandSpec("unzip <архив.zip> [каталог]", "распаковать в новый каталог"),
    "log": CommandSpec("log [N]", "журнал ваших действий"),
    "ops": CommandSpec("ops [N] [путь]", "журнал операций с файлами"),
    "ping": CommandSpec("ping", "проверка связи"),
    "quit": CommandSpec("quit", "выход из программы"),
}

EXIT_WORDS = {"quit", "exit", "выход"}

# Команды, которые выполняет сам клиент, не обращаясь к серверу.
LOCAL_COMMANDS = {"help": "help", "cls": "cls", "clear": "cls"}


def local_action(line: str) -> str | None:
    """Имя локального действия ("help", "cls") или None, если команда для сервера."""
    return LOCAL_COMMANDS.get(line.strip().lower())


def help_text() -> str:
    width = max(len(spec.usage) for spec in COMMANDS.values())
    lines = [f"  {spec.usage.ljust(width)}  {spec.summary}" for spec in COMMANDS.values()]
    lines.append("")
    lines.append("Пути — от вашего каталога на сервере, разделитель «/».")
    lines.append(f"Многострочный ввод завершается строкой из одной точки «{END_OF_TEXT}».")
    lines.append('Строка, начинающаяся с «{», уходит на сервер как есть (отладка протокола).')
    return "\n".join(lines)


def build_request(line: str, source: Input) -> dict[str, Any] | None:
    """Строка пользователя -> запрос. None — команда локальная (help, cls)."""
    text = line.strip()

    # Сырой JSON пропускаем как есть: так можно вручную проверить, как сервер
    # реагирует на нарушения протокола, не переписывая клиент. Отправляется
    # именно введённый текст: после json.loads и json.dumps дубликат ключа
    # исчез бы, и до сервера нарушение не дошло бы.
    if text.startswith("{"):
        try:
            return RawRequest(json.loads(text), raw=text)
        except json.JSONDecodeError as exc:
            raise UsageError(f"Некорректный JSON: {exc.msg}") from exc

    try:
        # posix=False оставил бы кавычки в аргументах, posix=True съел бы
        # обратные слеши. Пути на сервере пишутся через «/», так что
        # обратный слеш в аргументе — редкость, а кавычки нужны для пробелов.
        words = shlex.split(text, posix=True)
    except ValueError as exc:
        raise UsageError(f"Не закрыта кавычка: {exc}") from exc
    if not words:
        return None

    name, args = words[0].lower(), words[1:]
    force = any(arg in FORCE_FLAGS for arg in args)
    args = [arg for arg in args if arg not in FORCE_FLAGS]

    if name in EXIT_WORDS:
        return {"cmd": "quit"}
    if name in LOCAL_COMMANDS:
        _expect_local(name, args)
        return None
    if name in {"ping", "logout", "whoami", "drives"}:
        _expect(name, args, 0, 0)
        return {"cmd": name}

    if name in {"register", "login"}:
        _expect(name, args, 1, 1)
        password = source.password("Пароль: ")
        if name == "register" and source.password("Повторите пароль: ") != password:
            raise UsageError("Пароли не совпадают")
        return {"cmd": name, "username": args[0], "password": password}

    if name == "ls":
        _expect(name, args, 0, 1)
        return {"cmd": "ls", "path": args[0] if args else ""}

    simple = {
        "info": "info",
        "cat": "read",
        "rm": "delete",
        "mkdir": "mkdir",
        "rmdir": "rmdir",
        "json-read": "json_read",
        "xml-read": "xml_read",
    }
    if name in simple:
        _expect(name, args, 1, 1)
        return {"cmd": simple[name], "path": args[0]}

    if name == "write":
        _expect(name, args, 1, 1)
        content = source.text_block("Введите текст файла")
        return {"cmd": "write", "path": args[0], "content": content, "overwrite": force}

    if name == "json-write":
        _expect(name, args, 1, 1)
        raw = source.text_block("Введите JSON")
        # Обычный json.loads молча оставил бы последнее значение
        # повторяющегося ключа и отправил серверу уже «чистый» объект.
        try:
            data = strict_json.loads(raw)
        except strict_json.JSONRuleError as exc:
            raise UsageError(f"Некорректный JSON: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise UsageError(f"Некорректный JSON: {exc.msg} (строка {exc.lineno})") from exc
        except RecursionError as exc:
            raise UsageError("Некорректный JSON: слишком глубокая вложенность") from exc
        return {"cmd": "json_write", "path": args[0], "data": data, "overwrite": force}

    if name == "xml-write":
        _expect(name, args, 1, 1)
        content = source.text_block("Введите XML")
        return {"cmd": "xml_write", "path": args[0], "content": content, "overwrite": force}

    if name == "zip":
        if len(args) < 2:
            raise UsageError(f"Использование: {COMMANDS['zip'].usage}")
        return {"cmd": "zip", "archive": args[0], "sources": args[1:], "overwrite": force}

    if name == "zip-list":
        _expect(name, args, 1, 1)
        return {"cmd": "zip_list", "archive": args[0]}

    if name == "unzip":
        _expect(name, args, 1, 2)
        request: dict[str, Any] = {"cmd": "unzip", "archive": args[0]}
        if len(args) == 2:
            request["destination"] = args[1]
        return request

    if name == "log":
        _expect(name, args, 0, 1)
        request = {"cmd": "log"}
        if args:
            request["limit"] = _number(args[0])
        return request

    if name == "ops":
        _expect(name, args, 0, 2)
        request = {"cmd": "ops"}
        for arg in args:
            if arg.isdigit():
                request["limit"] = _number(arg)
            else:
                request["path"] = arg
        return request

    raise UsageError(f"Неизвестная команда {name!r}. Список команд — help")


def _expect(name: str, args: list[str], minimum: int, maximum: int) -> None:
    if not minimum <= len(args) <= maximum:
        raise UsageError(f"Использование: {COMMANDS[name].usage}")


def _expect_local(name: str, args: list[str]) -> None:
    if args:
        usage = COMMANDS[LOCAL_COMMANDS[name]].usage
        raise UsageError(f"Использование: {usage}")


def _number(raw: str) -> int:
    if not raw.isdigit():
        raise UsageError(f"Ожидалось число, получено {raw!r}")
    return int(raw)
