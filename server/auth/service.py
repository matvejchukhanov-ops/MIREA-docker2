"""Регистрация и вход.

Сетевого слоя здесь нет: функции принимают логин и пароль и возвращают
пользователя или исключение. Поэтому правила входа проверяются тестами
без сокетов.
"""
from __future__ import annotations

from server.db import UserExistsError, UserRepository
from server.session import AuthenticatedUser

from .passwords import (
    DUMMY_HASH,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)


class LoginFailedError(Exception):
    """Неверный логин или пароль. Что именно неверно — не сообщается."""


class AccountLockedError(Exception):
    """Учётная запись временно заблокирована после серии неудачных попыток."""


class AuthService:
    def __init__(
        self, users: UserRepository, *, max_failures: int, lockout_seconds: int
    ) -> None:
        self._users = users
        self._max_failures = max_failures
        self._lockout_seconds = lockout_seconds

    def register(self, username: str, password: str) -> AuthenticatedUser:
        """Создать пользователя.

        Бросает UsernamePolicyError, PasswordPolicyError, UserExistsError.
        """
        validate_username(username)
        validate_password(password, username)
        user_id = self._users.create(username, hash_password(password))
        return AuthenticatedUser(user_id, username)

    def login(self, username: str, password: str) -> AuthenticatedUser:
        """Проверить логин и пароль.

        Бросает LoginFailedError или AccountLockedError.
        """
        record = self._users.find(username)
        if record is None:
            # Хешируем и для несуществующего логина: время ответа не должно
            # выдавать, есть такой пользователь или нет.
            verify_password(password, DUMMY_HASH)
            raise LoginFailedError()

        if record.locked:
            # Пока блокировка действует, пароль даже не проверяется: иначе
            # перебор продолжался бы, просто ответы стали бы другими.
            raise AccountLockedError()

        if not verify_password(password, record.password_hash):
            if self._users.register_failure(record.id, self._max_failures, self._lockout_seconds):
                raise AccountLockedError()
            raise LoginFailedError()

        self._users.register_success(record.id)
        # Логин берётся из базы, а не из запроса: «ADMIN» и «admin» — один
        # пользователь, и в журнале он должен выглядеть одинаково.
        return AuthenticatedUser(record.id, record.username)


__all__ = ["AccountLockedError", "AuthService", "LoginFailedError", "UserExistsError"]
