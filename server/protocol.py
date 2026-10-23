"""Разбор запросов и сборка ответов.

Слой между кадрами и командами: сюда приходят байты кадра, отсюда уходят
готовые к отправке кадры ответа. Обработчики команд про JSON ничего не знают —
они принимают Request и возвращают результат.

Формат зафиксирован в docs/protocol.md.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from common import strict_json
from common.framing import encode_frame


class ErrorCode(StrEnum):
    """Машиночитаемые коды ошибок.

    Клиент принимает решения по коду, человек читает message. Разбирать
    текст сообщения в клиенте нельзя: формулировки меняются, коды — нет.
    """

    # Протокол и транспорт
    BAD_JSON = "BAD_JSON"
    BAD_REQUEST = "BAD_REQUEST"
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    FRAME_TOO_LONG = "FRAME_TOO_LONG"
    SERVER_BUSY = "SERVER_BUSY"

    # Аутентификация
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_FAILED = "AUTH_FAILED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    ALREADY_LOGGED_IN = "ALREADY_LOGGED_IN"
    REGISTRATION_CLOSED = "REGISTRATION_CLOSED"
    BAD_USERNAME = "BAD_USERNAME"
    WEAK_PASSWORD = "WEAK_PASSWORD"
    USER_EXISTS = "USER_EXISTS"

    # Файлы
    BAD_PATH = "BAD_PATH"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    NOT_A_FILE = "NOT_A_FILE"
    NOT_A_DIRECTORY = "NOT_A_DIRECTORY"
    DIR_NOT_EMPTY = "DIR_NOT_EMPTY"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    NOT_TEXT = "NOT_TEXT"
    ACCESS_DENIED = "ACCESS_DENIED"
    BAD_FORMAT = "BAD_FORMAT"
    BAD_ARCHIVE = "BAD_ARCHIVE"
    UNSAFE_ARCHIVE = "UNSAFE_ARCHIVE"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"


class ProtocolError(Exception):
    """Ошибка, о которой нужно сообщить клиенту кодом из ErrorCode."""

    def __init__(self, code: ErrorCode, message: str, *, position: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.position = position


# Предел длины строковых аргументов, кроме содержимого файла. Имя пользователя,
# путь или пароль длиной в мегабайт — не ошибка пользователя, а попытка
# заставить сервер тратить время на хеширование и разбор.
MAX_ARGUMENT_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class Request:
    """Разобранный запрос клиента."""

    cmd: str
    id: str | int | None
    payload: dict[str, Any]

    def require_str(
        self, name: str, *, max_length: int = MAX_ARGUMENT_LENGTH, allow_empty: bool = False
    ) -> str:
        """Достать обязательный строковый аргумент."""
        value = self.payload.get(name)
        if value is None:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"Команда {self.cmd!r} требует поля {name!r}"
            )
        if not isinstance(value, str):
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"Поле {name!r} должно быть строкой")
        if not value and not allow_empty:
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"Поле {name!r} не может быть пустым")
        if len(value) > max_length:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"Поле {name!r} длиннее {max_length} символов"
            )
        return value

    def optional_str(self, name: str, default: str) -> str:
        """Необязательный строковый аргумент."""
        if self.payload.get(name) is None:
            return default
        return self.require_str(name, allow_empty=True)

    def optional_int(self, name: str, default: int, *, minimum: int, maximum: int) -> int:
        """Достать необязательный целочисленный аргумент с проверкой границ."""
        value = self.payload.get(name)
        if value is None:
            return default
        # bool в Python — подкласс int, и True прошёл бы проверку как единица.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"Поле {name!r} должно быть целым числом")
        if not minimum <= value <= maximum:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST,
                f"Поле {name!r} должно быть в диапазоне {minimum}-{maximum}, получено {value}",
            )
        return value

    def optional_bool(self, name: str, default: bool) -> bool:
        """Необязательный логический аргумент. Строка "true" не считается."""
        value = self.payload.get(name)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"Поле {name!r} должно быть true или false")
        return value

    def require_str_list(self, name: str, *, max_items: int) -> list[str]:
        """Обязательный непустой список строк."""
        value = self.payload.get(name)
        if not isinstance(value, list) or not value:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"Поле {name!r} должно быть непустым списком строк"
            )
        if len(value) > max_items:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"В поле {name!r} больше {max_items} элементов"
            )
        if not all(isinstance(item, str) and item for item in value):
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"Элементы поля {name!r} должны быть непустыми строками"
            )
        if any(len(item) > MAX_ARGUMENT_LENGTH for item in value):
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"Слишком длинный элемент в поле {name!r}")
        return value

    def require_present(self, name: str) -> Any:
        """Обязательное поле произвольного JSON-типа."""
        if name not in self.payload:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"Команда {self.cmd!r} требует поля {name!r}"
            )
        return self.payload[name]


def parse_request(frame: bytes) -> Request:
    """Разобрать кадр в запрос.

    Данные пришли из сети, поэтому проверяется всё: кодировка, разбираемость
    JSON, тип верхнего уровня, наличие и тип обязательных полей.
    """
    try:
        text = frame.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(ErrorCode.BAD_JSON, "Сообщение не в кодировке UTF-8") from exc

    # Строгий разбор: дубликат ключа или NaN в запросе — такое же нарушение,
    # как и в файле. Иначе {"role": "user", "role": "admin"} в поле data
    # пройдёт мимо проверки и запишется в файл уже без дубликата.
    try:
        data = strict_json.loads(text)
    except strict_json.JSONRuleError as exc:
        raise ProtocolError(ErrorCode.BAD_JSON, str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise ProtocolError(
            ErrorCode.BAD_JSON, f"Не удалось разобрать JSON: {exc.msg}", position=exc.pos
        ) from exc
    except RecursionError as exc:
        # Строка из тысяч открывающих скобок исчерпывает стек разборщика.
        # Без перехвата исключение вылетело бы мимо обработки ошибок и
        # оборвало соединение вместо внятного ответа.
        raise ProtocolError(ErrorCode.BAD_JSON, "Слишком глубокая вложенность JSON") from exc

    if not isinstance(data, dict):
        raise ProtocolError(ErrorCode.BAD_JSON, "Ожидался объект JSON")

    cmd = data.get("cmd")
    if cmd is None:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "Поле 'cmd' обязательно")
    if not isinstance(cmd, str) or not cmd:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "Поле 'cmd' должно быть непустой строкой")
    if len(cmd) > 64:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "Поле 'cmd' слишком длинное")

    request_id = data.get("id")
    if request_id is not None and not isinstance(request_id, (str, int)):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "Поле 'id' должно быть строкой или числом")
    if isinstance(request_id, bool):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "Поле 'id' должно быть строкой или числом")
    if isinstance(request_id, str) and len(request_id) > 128:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "Поле 'id' слишком длинное")

    return Request(cmd=cmd, id=request_id, payload=data)


def success_frame(result: Any, request_id: str | int | None = None) -> bytes:
    """Собрать кадр успешного ответа."""
    payload: dict[str, Any] = {"status": "ok", "result": result}
    if request_id is not None:
        payload["id"] = request_id
    return _serialize(payload)


def error_frame(
    code: ErrorCode,
    message: str,
    request_id: str | int | None = None,
    position: int | None = None,
) -> bytes:
    """Собрать кадр ответа с ошибкой."""
    payload: dict[str, Any] = {"status": "error", "code": str(code), "message": message}
    if request_id is not None:
        payload["id"] = request_id
    if position is not None:
        payload["position"] = position
    return _serialize(payload)


def _serialize(payload: dict[str, Any]) -> bytes:
    # ensure_ascii=False оставляет кириллицу читаемой в логах и в telnet.
    # Переводы строк внутри значений json экранирует сам, поэтому кадр
    # гарантированно останется однострочным.
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return encode_frame(text.encode("utf-8"))
