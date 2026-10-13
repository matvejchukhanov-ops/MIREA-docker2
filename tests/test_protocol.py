"""Тесты протокола: разбор запросов и сборка ответов.

Данные приходят из сети, поэтому проверяется не только удачный случай,
но и каждый способ прислать мусор.

Запуск:
    python -m unittest discover
"""
from __future__ import annotations

import json
import unittest

from server.protocol import (
    MAX_ARGUMENT_LENGTH,
    ErrorCode,
    ProtocolError,
    error_frame,
    parse_request,
    success_frame,
)


def decode(frame: bytes) -> dict:
    """Разобрать кадр ответа обратно в словарь."""
    assert frame.endswith(b"\n"), "Ответ обязан быть завершённым кадром"
    return json.loads(frame[:-1].decode("utf-8"))


class ParseRequestTests(unittest.TestCase):
    def test_минимальный_запрос(self) -> None:
        request = parse_request(b'{"cmd":"ping"}')
        self.assertEqual(request.cmd, "ping")
        self.assertIsNone(request.id)

    def test_идентификатор_сохраняется(self) -> None:
        self.assertEqual(parse_request(b'{"cmd":"ping","id":42}').id, 42)
        self.assertEqual(parse_request(b'{"cmd":"ping","id":"a-1"}').id, "a-1")

    def test_аргументы_доступны(self) -> None:
        request = parse_request(b'{"cmd":"read","path":"docs/a.txt"}')
        self.assertEqual(request.require_str("path"), "docs/a.txt")

    def test_кириллица_в_аргументах(self) -> None:
        request = parse_request('{"cmd":"read","path":"отчёт.txt"}'.encode("utf-8"))
        self.assertEqual(request.require_str("path"), "отчёт.txt")

    def test_не_json(self) -> None:
        with self.assertRaises(ProtocolError) as ctx:
            parse_request("это не json".encode("utf-8"))
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_JSON)

    def test_json_но_не_объект(self) -> None:
        # Массив разбирается как JSON, но протокол требует именно объект.
        with self.assertRaises(ProtocolError) as ctx:
            parse_request(b'["ping"]')
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_JSON)

    def test_битая_кодировка(self) -> None:
        # Одинокий байт, невозможный в UTF-8.
        with self.assertRaises(ProtocolError) as ctx:
            parse_request(b'{"cmd":"\xff"}')
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_JSON)

    def test_глубокая_вложенность_не_роняет_разбор(self) -> None:
        # Сто тысяч скобок исчерпывают стек разборщика. Без перехвата
        # RecursionError соединение оборвалось бы вместо ответа BAD_JSON.
        frame = b'{"cmd":"ping","x":' + b"[" * 100_000 + b"]" * 100_000 + b"}"
        with self.assertRaises(ProtocolError) as ctx:
            parse_request(frame)
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_JSON)

    def test_нет_поля_cmd(self) -> None:
        with self.assertRaises(ProtocolError) as ctx:
            parse_request(b'{"path":"a.txt"}')
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_REQUEST)

    def test_cmd_не_строка(self) -> None:
        with self.assertRaises(ProtocolError) as ctx:
            parse_request(b'{"cmd":123}')
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_REQUEST)

    def test_cmd_пустая_строка(self) -> None:
        with self.assertRaises(ProtocolError) as ctx:
            parse_request(b'{"cmd":""}')
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_REQUEST)

    def test_id_логического_типа_отвергается(self) -> None:
        # В Python bool — подкласс int, и без явной проверки true прошёл бы
        # как единица.
        with self.assertRaises(ProtocolError) as ctx:
            parse_request(b'{"cmd":"ping","id":true}')
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_REQUEST)


class ArgumentValidationTests(unittest.TestCase):
    def test_обязательное_поле_отсутствует(self) -> None:
        request = parse_request(b'{"cmd":"read"}')
        with self.assertRaises(ProtocolError) as ctx:
            request.require_str("path")
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_REQUEST)

    def test_обязательное_поле_не_строка(self) -> None:
        request = parse_request(b'{"cmd":"read","path":42}')
        with self.assertRaises(ProtocolError):
            request.require_str("path")

    def test_пустая_строка_по_умолчанию_запрещена(self) -> None:
        request = parse_request(b'{"cmd":"read","path":""}')
        with self.assertRaises(ProtocolError):
            request.require_str("path")
        self.assertEqual(request.require_str("path", allow_empty=True), "")

    def test_слишком_длинный_аргумент(self) -> None:
        # Логин в мегабайт — не ошибка пользователя, а попытка нагрузить сервер.
        request = parse_request(
            json.dumps({"cmd": "login", "username": "a" * (MAX_ARGUMENT_LENGTH + 1)}).encode()
        )
        with self.assertRaises(ProtocolError):
            request.require_str("username")

    def test_необязательное_число_по_умолчанию(self) -> None:
        request = parse_request(b'{"cmd":"log"}')
        self.assertEqual(request.optional_int("limit", 10, minimum=1, maximum=50), 10)

    def test_необязательное_число_задано(self) -> None:
        request = parse_request(b'{"cmd":"log","limit":5}')
        self.assertEqual(request.optional_int("limit", 10, minimum=1, maximum=50), 5)

    def test_число_вне_диапазона(self) -> None:
        request = parse_request(b'{"cmd":"log","limit":999}')
        with self.assertRaises(ProtocolError) as ctx:
            request.optional_int("limit", 10, minimum=1, maximum=50)
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_REQUEST)

    def test_логическое_значение_вместо_числа(self) -> None:
        request = parse_request(b'{"cmd":"log","limit":true}')
        with self.assertRaises(ProtocolError):
            request.optional_int("limit", 10, minimum=1, maximum=50)

    def test_логический_флаг(self) -> None:
        self.assertTrue(parse_request(b'{"cmd":"w","overwrite":true}').optional_bool("overwrite", False))
        self.assertFalse(parse_request(b'{"cmd":"w"}').optional_bool("overwrite", False))

    def test_строка_вместо_логического_флага(self) -> None:
        # "false" — непустая строка и в Python истинна. Принять её значило бы
        # перезаписать файл, когда клиент просил обратного.
        request = parse_request(b'{"cmd":"w","overwrite":"false"}')
        with self.assertRaises(ProtocolError):
            request.optional_bool("overwrite", False)

    def test_список_строк(self) -> None:
        request = parse_request(b'{"cmd":"zip","sources":["a","b"]}')
        self.assertEqual(request.require_str_list("sources", max_items=10), ["a", "b"])

    def test_список_строк_с_мусором(self) -> None:
        for raw in (b'{"cmd":"zip","sources":[]}', b'{"cmd":"zip","sources":["a",1]}',
                    b'{"cmd":"zip","sources":"a"}', b'{"cmd":"zip","sources":["a",""]}'):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                parse_request(raw).require_str_list("sources", max_items=10)

    def test_список_длиннее_предела(self) -> None:
        request = parse_request(json.dumps({"cmd": "zip", "sources": ["a"] * 11}).encode())
        with self.assertRaises(ProtocolError):
            request.require_str_list("sources", max_items=10)


class ResponseTests(unittest.TestCase):
    def test_успешный_ответ(self) -> None:
        self.assertEqual(decode(success_frame("pong")), {"status": "ok", "result": "pong"})

    def test_идентификатор_возвращается(self) -> None:
        self.assertEqual(decode(success_frame("pong", request_id=7))["id"], 7)

    def test_ответ_с_ошибкой(self) -> None:
        data = decode(error_frame(ErrorCode.NOT_FOUND, "Файл не найден"))
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["code"], "NOT_FOUND")
        self.assertEqual(data["message"], "Файл не найден")
        self.assertNotIn("position", data)

    def test_позиция_ошибки(self) -> None:
        data = decode(error_frame(ErrorCode.BAD_JSON, "Ожидалась запятая", position=4))
        self.assertEqual(data["position"], 4)

    def test_ответ_всегда_одна_строка(self) -> None:
        # Иначе кадр развалился бы на два: перевод строки — это разделитель.
        frame = success_frame({"content": "строка\nс переводом"})
        self.assertEqual(frame.count(b"\n"), 1)
        self.assertEqual(decode(frame)["result"]["content"], "строка\nс переводом")

    def test_кириллица_не_экранируется(self) -> None:
        # ensure_ascii=False: сообщения остаются читаемыми в логах и telnet.
        frame = error_frame(ErrorCode.NOT_FOUND, "Файл не найден")
        self.assertIn("Файл".encode("utf-8"), frame)

    def test_поле_status_есть_всегда(self) -> None:
        self.assertIn("status", decode(success_frame(None)))
        self.assertIn("status", decode(error_frame(ErrorCode.INTERNAL_ERROR, "сбой")))


if __name__ == "__main__":
    unittest.main()
