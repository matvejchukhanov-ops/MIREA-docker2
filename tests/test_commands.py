"""Тесты команд сервера целиком: обработчики, база, хранилище — без сокетов."""
from __future__ import annotations

from server.session import Session

from .support import PASSWORD, ServicesTestCase


class AccountCommandTests(ServicesTestCase):
    def test_регистрация_сразу_входит(self) -> None:
        session = self.logged_in("alice")
        self.assertEqual(self.call(session, "whoami"), {"username": "alice"})

    def test_файловые_команды_закрыты_без_входа(self) -> None:
        session = self.new_session()
        for cmd, payload in [("ls", {}), ("read", {"path": "a"}), ("drives", {}),
                             ("write", {"path": "a", "content": ""}), ("log", {})]:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.call_error(session, cmd, **payload), "AUTH_REQUIRED")

    def test_вход_и_выход(self) -> None:
        self.logged_in("alice")
        session = self.new_session()
        self.assertEqual(self.call(session, "login", username="alice", password=PASSWORD),
                         {"username": "alice"})
        self.call(session, "logout")
        self.assertEqual(self.call_error(session, "ls"), "AUTH_REQUIRED")

    def test_повторный_вход_в_том_же_соединении(self) -> None:
        session = self.logged_in("alice")
        self.assertEqual(
            self.call_error(session, "login", username="alice", password=PASSWORD),
            "ALREADY_LOGGED_IN",
        )

    def test_ошибки_регистрации(self) -> None:
        session = self.new_session()
        self.assertEqual(self.call_error(session, "register", username="a", password=PASSWORD),
                         "BAD_USERNAME")
        self.assertEqual(self.call_error(session, "register", username="alice", password="123"),
                         "WEAK_PASSWORD")
        self.logged_in("alice")
        self.assertEqual(self.call_error(session, "register", username="ALICE", password=PASSWORD),
                         "USER_EXISTS")

    def test_перебор_паролей_закрывает_соединение(self) -> None:
        self.logged_in("alice")
        session = self.new_session()
        for _ in range(self.config.max_login_failures):
            self.assertIn(
                self.call_error(session, "login", username="alice", password="wrong-pass-1"),
                {"AUTH_FAILED", "ACCOUNT_LOCKED"},
            )
        self.assertTrue(session.should_close)

    def test_блокировка_учётной_записи(self) -> None:
        self.logged_in("alice")
        for _ in range(self.config.max_login_failures):
            self.call_error(self.new_session(), "login", username="alice", password="wrong-pass-1")
        # С нового соединения и с верным паролем — всё равно отказ.
        self.assertEqual(
            self.call_error(self.new_session(), "login", username="alice", password=PASSWORD),
            "ACCOUNT_LOCKED",
        )


class RegistrationClosedTests(ServicesTestCase):
    config_overrides = {"allow_registration": False}

    def test_регистрация_закрыта(self) -> None:
        self.assertEqual(
            self.call_error(self.new_session(), "register", username="alice", password=PASSWORD),
            "REGISTRATION_CLOSED",
        )


class FileCommandTests(ServicesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.session = self.logged_in("alice")

    def test_полный_цикл_с_журналом_операций(self) -> None:
        self.call(self.session, "mkdir", path="docs")
        self.assertTrue(self.call(self.session, "write", path="docs/a.txt", content="1")["created"])
        self.assertFalse(
            self.call(self.session, "write", path="docs/a.txt", content="22", overwrite=True)["created"]
        )
        self.assertEqual(self.call(self.session, "read", path="docs/a.txt")["content"], "22")
        self.call(self.session, "delete", path="docs/a.txt")
        ops = self.call(self.session, "ops")
        self.assertEqual([op["operation_type"] for op in reversed(ops)], ["create", "modify", "delete"])
        self.assertEqual({op["location"] for op in ops}, {"docs/a.txt"})

    def test_журнал_операций_по_пути(self) -> None:
        self.call(self.session, "mkdir", path="docs")
        self.call(self.session, "write", path="docs/a.txt", content="1")
        self.call(self.session, "write", path="b.txt", content="1")
        for raw in ("docs/a.txt", "./docs//a.txt", "docs\\a.txt"):
            with self.subTest(raw=raw):
                ops = self.call(self.session, "ops", path=raw)
                self.assertEqual([op["location"] for op in ops], ["docs/a.txt"])
        self.assertEqual(self.call_error(self.session, "ops", path="../x"), "BAD_PATH")

    def test_ошибки_файлов_приходят_кодами(self) -> None:
        self.assertEqual(self.call_error(self.session, "read", path="nope.txt"), "NOT_FOUND")
        self.assertEqual(self.call_error(self.session, "read", path="../x"), "BAD_PATH")
        self.call(self.session, "write", path="a.txt", content="1")
        self.assertEqual(self.call_error(self.session, "write", path="a.txt", content="2"),
                         "ALREADY_EXISTS")
        self.assertEqual(self.call_error(self.session, "ls", path="a.txt"), "NOT_A_DIRECTORY")

    def test_сведения_о_файле_из_базы(self) -> None:
        self.call(self.session, "write", path="a.txt", content="123")
        info = self.call(self.session, "info", path="a.txt")
        self.assertEqual(info["size"], 3)
        self.assertEqual(info["record"]["location"], "a.txt")

    def test_json(self) -> None:
        data = {"name": "Иван", "tags": ["a", "b"], "age": 20}
        self.call(self.session, "json_write", path="p.json", data=data)
        self.assertEqual(self.call(self.session, "json_read", path="p.json")["data"], data)
        self.call(self.session, "write", path="bad.json", content='{"a": 1, "a": 2}')
        self.assertEqual(self.call_error(self.session, "json_read", path="bad.json"), "BAD_FORMAT")

    def test_xml_текстом_и_деревом(self) -> None:
        self.call(self.session, "xml_write", path="a.xml", content="<r><i>1</i></r>")
        self.assertEqual(self.call(self.session, "xml_read", path="a.xml")["tree"]["tag"], "r")
        tree = {"tag": "r", "attrs": {"v": "1"}, "text": "", "children": []}
        self.call(self.session, "xml_write", path="b.xml", tree=tree)
        self.assertEqual(self.call(self.session, "xml_read", path="b.xml")["tree"], tree)

    def test_xml_с_dtd_не_записывается(self) -> None:
        xxe = '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
        self.assertEqual(self.call_error(self.session, "xml_write", path="x.xml", content=xxe),
                         "BAD_FORMAT")
        self.assertEqual(self.call_error(self.session, "read", path="x.xml"), "NOT_FOUND")

    def test_xml_ровно_одно_из_полей(self) -> None:
        self.assertEqual(self.call_error(self.session, "xml_write", path="x.xml"), "BAD_REQUEST")

    def test_zip_и_реестр(self) -> None:
        self.call(self.session, "write", path="a.txt", content="1")
        self.call(self.session, "zip", archive="a.zip", sources=["a.txt"])
        result = self.call(self.session, "unzip", archive="a.zip")
        self.assertEqual(result["destination"], "a")
        self.assertEqual(result["files"], [{"path": "a/a.txt", "size": 1}])
        locations = [op["location"] for op in self.call(self.session, "ops")]
        self.assertEqual(locations, ["a/a.txt", "a.zip", "a.txt"])

    def test_журнал_аудита(self) -> None:
        self.call(self.session, "write", path="a.txt", content="1")
        self.call_error(self.session, "read", path="../secret")
        log = self.call(self.session, "log", limit=3)
        self.assertEqual(
            [(entry["action"], entry["success"], entry["details"]) for entry in log],
            [("read", False, "BAD_PATH"), ("write", True, None), ("register", True, None)],
        )


class IsolationTests(ServicesTestCase):
    def test_пользователи_не_видят_файлы_друг_друга(self) -> None:
        alice, bob = self.logged_in("alice"), self.logged_in("bobby")
        self.call(alice, "write", path="secret.txt", content="alice only")
        self.assertEqual(self.call(bob, "ls")["entries"], [])
        self.assertEqual(self.call_error(bob, "read", path="secret.txt"), "NOT_FOUND")
        # Каталоги пользователей — соседи, но путь к соседу не пропустит песочница.
        self.assertEqual(self.call_error(bob, "read", path="../user-1/secret.txt"), "BAD_PATH")

    def test_журналы_у_каждого_свои(self) -> None:
        alice, bob = self.logged_in("alice"), self.logged_in("bobby")
        self.call(alice, "write", path="a.txt", content="1")
        self.assertEqual(self.call(bob, "ops"), [])
        # Действия alice в журнал bobby не попадают — только его собственные.
        self.assertEqual({entry["action"] for entry in self.call(bob, "log")}, {"register", "ops"})


class StatelessSessionTests(ServicesTestCase):
    def test_новое_соединение_начинает_без_входа(self) -> None:
        self.logged_in("alice")
        self.assertEqual(self.call_error(Session(peer="p"), "ls"), "AUTH_REQUIRED")
