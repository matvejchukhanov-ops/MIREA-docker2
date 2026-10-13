"""Тесты файловых операций: чтение, запись, удаление, каталоги, гонки."""
from __future__ import annotations

import threading
import unittest

from server.fs import errors
from server.fs.manager import FileManager
from server.fs.sandbox import TEMP_PREFIX

from .support import TempDirTestCase

LIMIT = 1024


class FilesTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.manager = FileManager(self.tmp / "storage", max_file_size=LIMIT)
        self.files = self.manager.for_user(1)
        self.root = self.files.sandbox.root


class ReadWriteTests(FilesTestCase):
    def test_создание_и_чтение(self) -> None:
        written = self.files.write_text("a.txt", "Привет\n", overwrite=False)
        self.assertTrue(written.created)
        self.assertEqual(written.size, len("Привет\n".encode("utf-8")))
        self.assertEqual(self.files.read_text("a.txt"), ("a.txt", "Привет\n"))

    def test_существующий_файл_без_overwrite(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        with self.assertRaises(errors.AlreadyExistsError):
            self.files.write_text("a.txt", "2", overwrite=False)
        self.assertEqual(self.files.read_text("a.txt")[1], "1")

    def test_перезапись(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        written = self.files.write_text("a.txt", "2", overwrite=True)
        self.assertFalse(written.created)
        self.assertEqual(self.files.read_text("a.txt")[1], "2")

    def test_предел_размера_при_записи(self) -> None:
        with self.assertRaises(errors.FileTooLargeError):
            self.files.write_text("big.txt", "x" * (LIMIT + 1), overwrite=False)
        self.assertFalse((self.root / "big.txt").exists())

    def test_предел_размера_при_чтении(self) -> None:
        (self.root / "big.txt").write_bytes(b"x" * (LIMIT + 1))
        with self.assertRaises(errors.FileTooLargeError):
            self.files.read_text("big.txt")

    def test_двоичный_файл_не_читается_как_текст(self) -> None:
        (self.root / "image.bin").write_bytes(b"\x89PNG\xff\xfe\x00")
        with self.assertRaises(errors.NotTextError):
            self.files.read_text("image.bin")

    def test_метка_bom_снимается(self) -> None:
        (self.root / "bom.txt").write_bytes("﻿текст".encode("utf-8"))
        self.assertEqual(self.files.read_text("bom.txt")[1], "текст")

    def test_нет_файла(self) -> None:
        with self.assertRaises(errors.NotFoundError):
            self.files.read_text("nope.txt")

    def test_каталог_вместо_файла(self) -> None:
        self.files.make_dir("docs")
        with self.assertRaises(errors.NotAFileError):
            self.files.read_text("docs")
        with self.assertRaises(errors.NotAFileError):
            self.files.write_text("docs", "x", overwrite=True)

    def test_нет_родительского_каталога(self) -> None:
        # Каталоги не создаются молча: опечатка в пути — ошибка, а не дерево.
        with self.assertRaises(errors.NotFoundError):
            self.files.write_text("no/such/dir/a.txt", "x", overwrite=False)

    def test_запись_в_корень(self) -> None:
        with self.assertRaises(errors.BadPathError):
            self.files.write_text("", "x", overwrite=True)

    def test_временные_файлы_не_остаются(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        self.files.write_text("a.txt", "2", overwrite=True)
        with self.assertRaises(errors.AlreadyExistsError):
            self.files.write_text("a.txt", "3", overwrite=False)
        leftovers = [p.name for p in self.root.iterdir() if p.name.startswith(TEMP_PREFIX)]
        self.assertEqual(leftovers, [])

    def test_путь_за_пределы_отвергается(self) -> None:
        with self.assertRaises(errors.BadPathError):
            self.files.read_text("../user-2/secret.txt")


class DeleteAndDirectoryTests(FilesTestCase):
    def test_удаление_файла(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        self.assertEqual(self.files.delete_file("a.txt"), "a.txt")
        self.assertFalse((self.root / "a.txt").exists())

    def test_удаление_несуществующего(self) -> None:
        with self.assertRaises(errors.NotFoundError):
            self.files.delete_file("a.txt")

    def test_удаление_каталога_командой_для_файла(self) -> None:
        self.files.make_dir("docs")
        with self.assertRaises(errors.NotAFileError):
            self.files.delete_file("docs")

    def test_корень_не_удаляется(self) -> None:
        with self.assertRaises(errors.BadPathError):
            self.files.delete_file("")
        with self.assertRaises(errors.BadPathError):
            self.files.remove_dir(".")

    def test_каталоги(self) -> None:
        self.files.make_dir("docs")
        with self.assertRaises(errors.AlreadyExistsError):
            self.files.make_dir("docs")
        with self.assertRaises(errors.NotFoundError):
            self.files.make_dir("a/b")
        self.files.write_text("docs/a.txt", "1", overwrite=False)
        with self.assertRaises(errors.DirectoryNotEmptyError):
            self.files.remove_dir("docs")
        self.files.delete_file("docs/a.txt")
        self.files.remove_dir("docs")
        self.assertFalse((self.root / "docs").exists())

    def test_rmdir_для_файла(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        with self.assertRaises(errors.NotADirectoryError_):
            self.files.remove_dir("a.txt")


class ListingTests(FilesTestCase):
    def test_листинг(self) -> None:
        self.files.make_dir("b-dir")
        self.files.write_text("a.txt", "12345", overwrite=False)
        listing = self.files.list_dir("")
        self.assertEqual(listing["path"], "")
        self.assertEqual([(e["name"], e["type"]) for e in listing["entries"]],
                         [("b-dir", "dir"), ("a.txt", "file")])
        self.assertEqual(listing["entries"][1]["size"], 5)

    def test_листинг_файла(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        with self.assertRaises(errors.NotADirectoryError_):
            self.files.list_dir("a.txt")

    def test_свойства(self) -> None:
        self.files.write_text("a.txt", "123", overwrite=False)
        info = self.files.info("a.txt")
        self.assertEqual((info["name"], info["type"], info["size"]), ("a.txt", "file", 3))
        self.assertRegex(info["created"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_ответы_не_раскрывают_путь_сервера(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        text = repr([self.files.list_dir(""), self.files.info("a.txt")])
        self.assertNotIn(str(self.tmp), text)


class IsolationTests(FilesTestCase):
    def test_у_пользователей_разные_каталоги(self) -> None:
        other = self.manager.for_user(2)
        self.files.write_text("secret.txt", "alice", overwrite=False)
        with self.assertRaises(errors.NotFoundError):
            other.read_text("secret.txt")


class RaceTests(FilesTestCase):
    def test_одновременное_создание_одного_файла(self) -> None:
        # Без блокировки оба потока увидели бы «файла нет» и оба записали бы,
        # второй молча затёр бы работу первого. С блокировкой — ровно один успех.
        outcomes: list[str] = []
        barrier = threading.Barrier(8)
        guard = threading.Lock()

        def worker(index: int) -> None:
            barrier.wait(timeout=5)
            try:
                self.files.write_text("race.txt", f"writer {index}", overwrite=False)
                result = "created"
            except errors.AlreadyExistsError:
                result = "exists"
            with guard:
                outcomes.append(result)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["created"] + ["exists"] * 7)

    def test_читатель_не_видит_файл_наполовину(self) -> None:
        # Атомарная замена: читатель получает либо старое содержимое целиком,
        # либо новое целиком — но не смесь и не пустой файл.
        old, new = "A" * LIMIT, "B" * LIMIT
        self.files.write_text("f.txt", old, overwrite=False)
        seen: set[str] = set()
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                try:
                    seen.add(self.files.read_text("f.txt")[1])
                except (errors.NotFoundError, errors.AccessDeniedError):
                    # Windows на мгновение запрещает открыть файл, который
                    # прямо сейчас подменяется. Это не частичное чтение.
                    continue

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for index in range(50):
                self.files.write_text("f.txt", new if index % 2 == 0 else old, overwrite=True)
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertLessEqual(seen, {old, new})


if __name__ == "__main__":
    unittest.main()
