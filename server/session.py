"""Состояние одного соединения.

Живёт в потоке, который обслуживает клиента, и умирает вместе с ним. Общего
с другими соединениями здесь ничего нет: всё, что должно пережить
соединение, — пользователи, файлы, журнал — хранится в базе данных.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Кто вошёл в этом соединении."""

    id: int
    username: str


@dataclass(slots=True)
class Session:
    """Что сервер помнит о клиенте, пока тот подключён."""

    peer: str

    # Пока здесь None, доступны только команды входа и регистрации.
    user: AuthenticatedUser | None = None

    # Неудачные попытки входа в пределах соединения. Блокировка учётной
    # записи не мешает перебирать разные логины — это мешает.
    failed_logins: int = 0

    # Обработчик команды не закрывает соединение сам: ответ нужно сначала
    # отправить. Он лишь поднимает флаг, а цикл обмена завершается после
    # того, как ответ ушёл.
    should_close: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.user is not None
