"""Тесты клиента: разбор команд и вывод ответов. Без сети и без консоли."""
from __future__ import annotations

import unittest

from client.commands import COMMANDS, Input, UsageError, build_request, help_text, local_action
from client.render import human_size, render


def scripted(password: str = "pass-1234", block: str = "", confirm: bool = False) -> Input:
    return Input(
        password=lambda prompt: password,
        text_block=lambda prompt: block,
        confirm=lambda prompt: confirm,
    )


class BuildRequestTests(unittest.TestCase):
    def test_простые_команды(self) -> None:
        cases = {
            "ping": {"cmd": "ping"},
            "ls": {"cmd": "ls", "path": ""},
            "ls docs": {"cmd": "ls", "path": "docs"},
            "cat a.txt": {"cmd": "read", "path": "a.txt"},
            "rm a.txt": {"cmd": "delete", "path": "a.txt"},
            "json-read a.json": {"cmd": "json_read", "path": "a.json"},
            "zip-list a.zip": {"cmd": "zip_list", "archive": "a.zip"},
            "unzip a.zip out": {"cmd": "unzip", "archive": "a.zip", "destination": "out"},
            "log 5": {"cmd": "log", "limit": 5},
            "ops 3 a.txt": {"cmd": "ops", "limit": 3, "path": "a.txt"},
            "quit": {"cmd": "quit"},
            "выход": {"cmd": "quit"},
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(build_request(line, scripted()), expected)

    def test_пробелы_в_пути_в_кавычках(self) -> None:
        self.assertEqual(
            build_request('cat "my docs/a b.txt"', scripted()), {"cmd": "read", "path": "my docs/a b.txt"}
        )

    def test_пароль_не_из_командной_строки(self) -> None:
        # Пароль спрашивается отдельно и не оседает в истории команд.
        request = build_request("login ivan", scripted(password="s3cret-x"))
        self.assertEqual(request, {"cmd": "login", "username": "ivan", "password": "s3cret-x"})

    def test_регистрация_сверяет_пароли(self) -> None:
        answers = iter(["first-pass1", "other-pass1"])
        source = Input(password=lambda prompt: next(answers), text_block=lambda p: "",
                       confirm=lambda p: False)
        with self.assertRaises(UsageError):
            build_request("register ivan", source)

    def test_многострочная_запись(self) -> None:
        request = build_request("write a.txt --force", scripted(block="a\nb\n"))
        self.assertEqual(request, {"cmd": "write", "path": "a.txt", "content": "a\nb\n", "overwrite": True})

    def test_json_проверяется_на_клиенте(self) -> None:
        self.assertEqual(
            build_request("json-write a.json", scripted(block='{"a": 1}'))["data"], {"a": 1}
        )
        with self.assertRaises(UsageError):
            build_request("json-write a.json", scripted(block="{oops"))

    def test_zip(self) -> None:
        self.assertEqual(
            build_request("zip a.zip docs b.txt", scripted()),
            {"cmd": "zip", "archive": "a.zip", "sources": ["docs", "b.txt"], "overwrite": False},
        )

    def test_ошибки_ввода(self) -> None:
        for line in ("cat", "cat a b", "zip a.zip", "log x", "нечто", 'cat "незакрыто'):
            with self.subTest(line=line), self.assertRaises(UsageError):
                build_request(line, scripted())

    def test_help_локально(self) -> None:
        self.assertIsNone(build_request("help", scripted()))
        for spec in COMMANDS.values():
            self.assertIn(spec.usage, help_text())

    def test_очистка_экрана_локально(self) -> None:
        # cls не уходит на сервер: это действие самого клиента.
        for line in ("cls", "CLEAR", "  clear  "):
            with self.subTest(line=line):
                self.assertEqual(local_action(line), "cls")
                self.assertIsNone(build_request(line, scripted()))
        self.assertEqual(local_action("help"), "help")
        self.assertIsNone(local_action("ls"))
        with self.assertRaises(UsageError):
            build_request("cls лишнее", scripted())

    def test_сырой_json(self) -> None:
        self.assertEqual(build_request('{"cmd":"ping","id":1}', scripted()), {"cmd": "ping", "id": 1})


class RenderTests(unittest.TestCase):
    def test_ошибка(self) -> None:
        text = render({"cmd": "read"}, {"status": "error", "code": "NOT_FOUND", "message": "нет"})
        self.assertEqual(text, "Ошибка [NOT_FOUND]: нет")

    def test_листинг(self) -> None:
        text = render(
            {"cmd": "ls"},
            {"status": "ok", "result": {"path": "docs", "truncated": False, "entries": [
                {"name": "sub", "type": "dir", "size": None, "modified": "2026-01-01 00:00:00"},
                {"name": "a.txt", "type": "file", "size": 2048, "modified": "2026-01-01 00:00:00"},
            ]}},
        )
        self.assertIn("/docs", text)
        self.assertIn("[кат]", text)
        self.assertIn("2.0 КБ", text)

    def test_незнакомый_ответ_не_роняет_клиента(self) -> None:
        text = render({"cmd": "ls"}, {"status": "ok", "result": {"неожиданно": True}})
        self.assertIn("неожиданно", text)
        self.assertIn("status", render({"cmd": "ls"}, {"result": 1}))

    def test_размеры(self) -> None:
        self.assertEqual(human_size(512), "512 Б")
        self.assertEqual(human_size(1536), "1.5 КБ")
        self.assertEqual(human_size(None), "—")


if __name__ == "__main__":
    unittest.main()
