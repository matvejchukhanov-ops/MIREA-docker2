"""Тесты песочницы: обход каталогов (path traversal) и опасные имена.

Каждый случай — реальный приём, которым пытаются выйти за пределы
разрешённого каталога или обратиться к файлу под чужим именем.
"""
from __future__ import annotations

import unittest

from server.fs.errors import BadPathError
from server.fs.sandbox import Sandbox, split_user_path

from .support import TempDirTestCase


class SplitUserPathTests(unittest.TestCase):
    def test_обычный_путь(self) -> None:
        self.assertEqual(split_user_path("docs/report.txt"), ["docs", "report.txt"])

    def test_корень(self) -> None:
        for raw in ("", ".", "./", "/" * 0):
            with self.subTest(raw=raw):
                self.assertEqual(split_user_path(raw), [])

    def test_обратный_слеш_тоже_разделитель(self) -> None:
        # Клиент на Windows пришлёт путь со своими разделителями.
        self.assertEqual(split_user_path("docs\\report.txt"), ["docs", "report.txt"])

    def test_лишние_разделители_и_точки(self) -> None:
        self.assertEqual(split_user_path("a//./b/"), ["a", "b"])

    def test_отвергается(self) -> None:
        attacks = {
            "родитель": "../secret.txt",
            "родитель в середине": "docs/../../secret.txt",
            "родитель через обратный слеш": "..\\..\\windows\\win.ini",
            "абсолютный POSIX": "/etc/passwd",
            "абсолютный Windows": "C:\\Windows\\win.ini",
            "буква диска без слеша": "C:secret.txt",
            "UNC": "\\\\server\\share\\file",
            "нулевой байт": "report.txt\x00.jpg",
            "альтернативный поток NTFS": "report.txt:hidden",
            "устройство CON": "CON",
            "устройство с расширением": "nul.txt",
            "устройство COM1 в каталоге": "docs/com1.log",
            "завершающая точка": "report.txt.",
            "завершающий пробел": "report.txt ",
            "управляющий символ": "rep\nort.txt",
            "звёздочка": "*.txt",
            "служебный временный файл": ".fm-tmp-123",
        }
        for name, raw in attacks.items():
            with self.subTest(name), self.assertRaises(BadPathError):
                split_user_path(raw)

    def test_многоточие_в_имени_допустимо(self) -> None:
        # «..» запрещено как компонент, но не как часть имени.
        self.assertEqual(split_user_path("a..b/...c"), ["a..b", "...c"])

    def test_предел_длины(self) -> None:
        with self.assertRaises(BadPathError):
            split_user_path("a/" * 600)
        with self.assertRaises(BadPathError):
            split_user_path("x" * 256)
        with self.assertRaises(BadPathError):
            split_user_path("/".join(["d"] * 40))


class SandboxTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sandbox = Sandbox(self.tmp / "user-1")
        self.outside = self.tmp / "outside.txt"
        self.outside.write_text("секрет", encoding="utf-8")

    def test_путь_внутри(self) -> None:
        path = self.sandbox.resolve("docs/a.txt")
        self.assertTrue(path.is_relative_to(self.sandbox.root))
        self.assertEqual(self.sandbox.relative(path), "docs/a.txt")

    def test_корень(self) -> None:
        self.assertEqual(self.sandbox.resolve(""), self.sandbox.root)
        self.assertEqual(self.sandbox.relative(self.sandbox.root), "")

    def test_соседний_каталог_с_тем_же_префиксом(self) -> None:
        # Классическая ошибка — проверять startswith(str(root)): каталог
        # «user-10» начинается с «user-1». Сравнение по компонентам не обманешь.
        neighbour = self.tmp / "user-10"
        neighbour.mkdir()
        self.assertFalse(self.sandbox.contains(neighbour.resolve()))

    def test_ссылка_наружу_не_пропускается(self) -> None:
        link = self.sandbox.root / "escape"
        try:
            link.symlink_to(self.tmp, target_is_directory=True)
        except OSError:
            self.skipTest("создание ссылок требует прав (Windows без режима разработчика)")
        # Синтаксически путь безупречен; ловит его только проверка после resolve().
        with self.assertRaises(BadPathError):
            self.sandbox.resolve("escape/outside.txt")


if __name__ == "__main__":
    unittest.main()
