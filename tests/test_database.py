"""Тесты базы данных: схема, триггеры журнала, неизменяемость истории, SQL-инъекции."""
from __future__ import annotations

import ast
import sqlite3
import threading
import unittest
from pathlib import Path

from server.db import AuditLog, Database, FileChange, FileRegistry, UserRepository
from server.db import repository

from .support import TempDirTestCase


class DatabaseTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.tmp / "fm.db")
        self.db.initialize()
        self.users = UserRepository(self.db)
        self.registry = FileRegistry(self.db)
        self.audit = AuditLog(self.db)
        self.user_id = self.users.create("ivan", "scrypt$fake")

    def operations(self) -> list[str]:
        return [row["operation_type"] for row in reversed(self.registry.operations(self.user_id, 100))]


class SchemaTests(DatabaseTestCase):
    def test_повторная_инициализация_безопасна(self) -> None:
        self.db.initialize()
        self.assertIsNotNone(self.users.find("ivan"))

    def test_таблицы_из_задания(self) -> None:
        with self.db.connect() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"users", "files", "operations", "audit_log"}, tables)

    def test_пароль_без_хеша_не_вставить(self) -> None:
        # Ограничение CHECK — последняя линия: даже ошибка в коде не запишет
        # в базу пароль открытым текстом.
        with self.assertRaises(sqlite3.IntegrityError):
            self.users.create("petr", "plain-password")

    def test_тип_операции_ограничен(self) -> None:
        self.registry.record_change(self.user_id, FileChange("a.txt", 1, created=True))
        with self.db.connect() as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operations (operation_type, file_id, user_id) VALUES ('hack', 1, ?)",
                (self.user_id,),
            )

    def test_внешние_ключи_включены(self) -> None:
        with self.db.connect() as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO files (filename, size, location, owner_id) VALUES ('a', 0, 'a', 999)"
            )


class OperationTriggerTests(DatabaseTestCase):
    def test_создание_изменение_удаление(self) -> None:
        self.registry.record_change(self.user_id, FileChange("docs/a.txt", 5, created=True))
        self.registry.record_change(self.user_id, FileChange("docs/a.txt", 9, created=False))
        self.registry.record_delete(self.user_id, "docs/a.txt")
        self.assertEqual(self.operations(), ["create", "modify", "delete"])

    def test_запись_файла_без_журнала_невозможна(self) -> None:
        # Даже «ручной» UPDATE в обход приложения попадает в журнал: его ведёт
        # не код сервера, а триггер в самой базе.
        self.registry.record_change(self.user_id, FileChange("a.txt", 1, created=True))
        with self.db.transaction() as conn:
            conn.execute("UPDATE files SET size = 100 WHERE location = 'a.txt'")
        self.assertEqual(self.operations(), ["create", "modify"])

    def test_удалённый_и_созданный_заново_файл(self) -> None:
        self.registry.record_change(self.user_id, FileChange("a.txt", 1, created=True))
        self.registry.record_delete(self.user_id, "a.txt")
        self.registry.record_change(self.user_id, FileChange("a.txt", 2, created=True))
        ops = self.registry.operations(self.user_id, 10)
        self.assertEqual([op["operation_type"] for op in reversed(ops)], ["create", "delete", "create"])
        # Новый файл — новая запись: история старого не смешивается с новым.
        self.assertNotEqual(ops[0]["file_id"], ops[-1]["file_id"])

    def test_удаление_незарегистрированного_файла_оставляет_след(self) -> None:
        # Файл, положенный на диск в обход сервера, при удалении сначала
        # регистрируется, а затем помечается удалённым.
        self.registry.record_delete(self.user_id, "manual.txt")
        self.assertEqual(self.operations(), ["create", "delete"])

    def test_пакетная_запись_атомарна(self) -> None:
        changes = [FileChange("a.txt", 1, True), FileChange("b.txt", -1, True)]
        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.record_changes(self.user_id, changes)
        # Первая запись пакета откатилась вместе со второй.
        self.assertEqual(self.operations(), [])


class ImmutabilityTests(DatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry.record_change(self.user_id, FileChange("a.txt", 1, created=True))
        self.audit.record(
            user_id=self.user_id, username="ivan", peer="p", action="read",
            target="a.txt", success=True, details=None,
        )

    def assertForbidden(self, sql: str) -> None:
        with self.db.connect() as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute(sql)

    def test_журнал_аудита_не_стирается(self) -> None:
        self.assertForbidden("DELETE FROM audit_log")

    def test_журнал_аудита_не_правится(self) -> None:
        self.assertForbidden("UPDATE audit_log SET success = 0")

    def test_журнал_операций_не_стирается(self) -> None:
        self.assertForbidden("DELETE FROM operations")

    def test_журнал_операций_не_правится(self) -> None:
        self.assertForbidden("UPDATE operations SET operation_type = 'create'")

    def test_запись_о_файле_не_удаляется(self) -> None:
        self.assertForbidden("DELETE FROM files")

    def test_владельца_файла_не_подменить(self) -> None:
        self.assertForbidden("UPDATE files SET owner_id = 1")


class InjectionTests(DatabaseTestCase):
    def test_логин_с_кавычками_остаётся_строкой(self) -> None:
        # Если бы запрос склеивался из строк, это условие стало бы истинным
        # для любой строки таблицы и вернуло бы первого пользователя.
        self.assertIsNone(self.users.find("x' OR '1'='1"))

    def test_инъекция_в_пути_файла(self) -> None:
        location = "a'); DROP TABLE users; --"
        self.registry.record_change(self.user_id, FileChange(location, 1, created=True))
        self.assertIsNotNone(self.users.find("ivan"))
        self.assertEqual(self.registry.live_record(self.user_id, location)["location"], location)

    def test_пользователь_с_опасным_логином_хранится_как_есть(self) -> None:
        name = "a';--"
        user_id = self.users.create(name, "scrypt$x")
        self.assertEqual(self.users.find(name).id, user_id)

    def test_в_тексте_запросов_нет_данных(self) -> None:
        # Статическая проверка исходника: первым аргументом каждого execute()
        # может быть только литерал или константа NOW. f-строка, .format() или
        # склейка с переменной — повод для провала теста.
        source = Path(repository.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        allowed_names = {"NOW"}

        def is_safe(node: ast.AST) -> bool:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return True
            if isinstance(node, ast.Name) and node.id in allowed_names:
                return True
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                return is_safe(node.left) and is_safe(node.right)
            return False

        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany", "executescript"}
        ]
        self.assertGreater(len(calls), 10)
        for call in calls:
            with self.subTest(line=call.lineno):
                self.assertTrue(is_safe(call.args[0]), f"строка {call.lineno}: текст SQL не литерал")


class ConcurrencyTests(DatabaseTestCase):
    def test_параллельная_запись_не_теряет_операций(self) -> None:
        # BEGIN IMMEDIATE упорядочивает писателей; без него часть потоков
        # получила бы «database is locked».
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                for step in range(10):
                    self.registry.record_change(
                        self.user_id, FileChange(f"f{index}.txt", step, created=step == 0)
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.registry.operations(self.user_id, 500)), 80)

    def test_параллельные_неудачные_входы_считаются_все(self) -> None:
        # Счётчик растёт в самой базе (failed_logins + 1); «прочитать, прибавить,
        # записать» в Python потеряло бы часть попыток.
        locked: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            result = self.users.register_failure(self.user_id, 1000, 60)
            with lock:
                locked.append(result)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        with self.db.connect() as conn:
            count = conn.execute(
                "SELECT failed_logins FROM users WHERE id = ?", (self.user_id,)
            ).fetchone()[0]
        self.assertEqual(count, 20)


if __name__ == "__main__":
    unittest.main()
