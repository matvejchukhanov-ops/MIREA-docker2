"""Тесты хеширования паролей, регистрации и входа."""
from __future__ import annotations

import time
import unittest

from server.auth.passwords import (
    PasswordPolicyError,
    UsernamePolicyError,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)
from server.auth.service import AccountLockedError, AuthService, LoginFailedError
from server.db import Database, UserExistsError, UserRepository

from .support import PASSWORD, TempDirTestCase


class PasswordHashTests(unittest.TestCase):
    def test_верный_пароль_подходит(self) -> None:
        self.assertTrue(verify_password("s3cret-pass", hash_password("s3cret-pass")))

    def test_неверный_пароль_не_подходит(self) -> None:
        self.assertFalse(verify_password("s3cret-pasS", hash_password("s3cret-pass")))

    def test_пароль_не_хранится_открыто(self) -> None:
        stored = hash_password("s3cret-pass")
        self.assertNotIn("s3cret-pass", stored)
        self.assertTrue(stored.startswith("scrypt$"))

    def test_соль_делает_хеши_разными(self) -> None:
        # Без соли одинаковые пароли выдали бы себя одинаковыми хешами.
        self.assertNotEqual(hash_password("same-pass-1"), hash_password("same-pass-1"))

    def test_испорченный_хеш_не_исключение(self) -> None:
        for stored in ("", "plain", "md5$abc", "scrypt$1$8$1$AAAA$AAAA",
                       "scrypt$16384$8$1$!!!$AAAA", "scrypt$1099511627776$8$1$AAAA$AAAA"):
            with self.subTest(stored=stored):
                self.assertFalse(verify_password("x", stored))


class PolicyTests(unittest.TestCase):
    def test_допустимые_логины(self) -> None:
        for name in ("ivan", "Ivan_Petrov", "user.name-1"):
            validate_username(name)

    def test_недопустимые_логины(self) -> None:
        for name in ("ab", "x" * 33, "иван", "a b", "admin'--", "admin\n", "../root"):
            with self.subTest(name=name), self.assertRaises(UsernamePolicyError):
                validate_username(name)

    def test_слабые_пароли(self) -> None:
        for password in ("short1", "12345678", "password", "Ivan"):
            with self.subTest(password=password), self.assertRaises(PasswordPolicyError):
                validate_password(password, "ivan")

    def test_пароль_равный_логину(self) -> None:
        with self.assertRaises(PasswordPolicyError):
            validate_password("ivan2026", "IVAN2026")

    def test_слишком_длинный_пароль(self) -> None:
        with self.assertRaises(PasswordPolicyError):
            validate_password("a1" * 100, "ivan")


class AuthServiceTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        db = Database(self.tmp / "fm.db")
        db.initialize()
        self.users = UserRepository(db)
        self.auth = AuthService(self.users, max_failures=3, lockout_seconds=60)
        self.auth.register("ivan", PASSWORD)

    def test_вход(self) -> None:
        self.assertEqual(self.auth.login("ivan", PASSWORD).username, "ivan")

    def test_логин_без_учёта_регистра(self) -> None:
        # Имя в ответе — из базы: «IVAN» и «ivan» — один пользователь.
        self.assertEqual(self.auth.login("IVAN", PASSWORD).username, "ivan")

    def test_двойник_с_другим_регистром_не_регистрируется(self) -> None:
        with self.assertRaises(UserExistsError):
            self.auth.register("Ivan", PASSWORD)

    def test_неверный_пароль_и_неизвестный_логин_неразличимы(self) -> None:
        with self.assertRaises(LoginFailedError):
            self.auth.login("ivan", "wrong-pass-1")
        with self.assertRaises(LoginFailedError):
            self.auth.login("nobody", "wrong-pass-1")

    def test_неизвестный_логин_проверяется_так_же_долго(self) -> None:
        # Если бы для неизвестного логина хеш не считался, ответ приходил бы
        # в разы быстрее, и перебором логинов можно было бы узнать, какие есть.
        auth = AuthService(self.users, max_failures=100, lockout_seconds=60)

        def measure(username: str) -> float:
            started = time.perf_counter()
            with self.assertRaises(LoginFailedError):
                auth.login(username, "wrong-pass-1")
            return time.perf_counter() - started

        measure("ivan")  # прогрев
        known = min(measure("ivan") for _ in range(3))
        unknown = min(measure("nobody") for _ in range(3))
        self.assertGreater(unknown, known * 0.3)

    def test_блокировка_после_серии_неудач(self) -> None:
        for _ in range(2):
            with self.assertRaises(LoginFailedError):
                self.auth.login("ivan", "wrong-pass-1")
        with self.assertRaises(AccountLockedError):
            self.auth.login("ivan", "wrong-pass-1")
        # Даже верный пароль не пускает, пока блокировка действует.
        with self.assertRaises(AccountLockedError):
            self.auth.login("ivan", PASSWORD)

    def test_успешный_вход_сбрасывает_счётчик(self) -> None:
        for _ in range(2):
            with self.assertRaises(LoginFailedError):
                self.auth.login("ivan", "wrong-pass-1")
        self.auth.login("ivan", PASSWORD)
        for _ in range(2):
            with self.assertRaises(LoginFailedError):
                self.auth.login("ivan", "wrong-pass-1")


if __name__ == "__main__":
    unittest.main()
