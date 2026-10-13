"""Сопоставление имени команды с обработчиком.

Словарь вместо цепочки if-elif: добавление команды не требует правок в
обработке запросов, а список поддерживаемых команд доступен для самопроверки.

Здесь же — две проверки, которые нельзя доверять отдельным обработчикам:

* **контроль доступа.** Команда по умолчанию требует входа; открытой её
  делает только явный флаг при регистрации. Забытая проверка в одном из
  двадцати обработчиков иначе означала бы дыру — а так забыть её нельзя;
* **журнал аудита.** Запись делается для каждой команды, успешной или нет,
  в одном месте, поэтому новая команда попадает в журнал автоматически.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .protocol import ErrorCode, ProtocolError, Request
from .session import AuthenticatedUser, Session

logger = logging.getLogger("server.dispatcher")

Handler = Callable[[Request, Session], Any]

# Поля запроса, которые попадают в журнал как «объект действия». Пароли и
# содержимое файлов сюда не входят намеренно: журнал читают люди, и
# секреты в нём — утечка, а содержимое — раздувание базы.
TARGET_FIELDS = ("path", "archive", "username")
MAX_TARGET_LENGTH = 512


class AuditSink(Protocol):
    """Куда диспетчер пишет журнал. Реализация — в server/db/repository.py."""

    def record(
        self,
        *,
        user_id: int | None,
        username: str | None,
        peer: str,
        action: str,
        target: str | None,
        success: bool,
        details: str | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Command:
    handler: Handler
    public: bool
    audited: bool


class Dispatcher:
    """Реестр команд."""

    __slots__ = ("_commands", "_audit")

    def __init__(self, audit: AuditSink | None = None) -> None:
        self._commands: dict[str, Command] = {}
        self._audit = audit

    def register(
        self, name: str, handler: Handler, *, public: bool = False, audited: bool = True
    ) -> None:
        """Добавить команду.

        public — команда доступна без входа. По умолчанию False: доступ
        запрещён, пока не разрешён явно.
        audited — записывать ли вызовы в журнал. Выключается только для
        служебных команд вроде ping, которые засорили бы журнал.
        """
        if name in self._commands:
            # Тихая перезапись привела бы к тому, что часть команд молча
            # перестаёт работать. Лучше упасть при запуске.
            raise ValueError(f"Команда {name!r} уже зарегистрирована")
        self._commands[name] = Command(handler, public, audited)

    @property
    def commands(self) -> list[str]:
        """Имена всех зарегистрированных команд."""
        return sorted(self._commands)

    def dispatch(self, request: Request, session: Session) -> Any:
        """Выполнить команду и вернуть её результат."""
        command = self._commands.get(request.cmd)
        if command is None:
            raise ProtocolError(
                ErrorCode.UNKNOWN_COMMAND,
                f"Неизвестная команда {request.cmd!r}. Доступны: {', '.join(self.commands)}",
            )

        if not command.public and not session.is_authenticated:
            self._record(request, session, None, success=False, details=ErrorCode.AUTH_REQUIRED)
            raise ProtocolError(
                ErrorCode.AUTH_REQUIRED, "Команда доступна только после входа (login)"
            )

        # Пользователь до вызова: после logout его уже не будет в сессии,
        # а записать в журнал нужно, кто именно вышел.
        user_before = session.user
        try:
            result = command.handler(request, session)
        except ProtocolError as exc:
            if command.audited:
                self._record(request, session, user_before, success=False, details=exc.code)
            raise
        except Exception:
            if command.audited:
                self._record(
                    request, session, user_before, success=False, details=ErrorCode.INTERNAL_ERROR
                )
            raise

        if command.audited:
            self._record(request, session, user_before, success=True, details=None)
        return result

    def _record(
        self,
        request: Request,
        session: Session,
        user_before: AuthenticatedUser | None,
        *,
        success: bool,
        details: str | None,
    ) -> None:
        if self._audit is None:
            return
        # После входа пользователь появляется в сессии, после выхода исчезает;
        # в журнал идёт тот, кто есть хоть в одном из моментов.
        actor = session.user or user_before
        try:
            self._audit.record(
                user_id=actor.id if actor else None,
                username=actor.username if actor else _claimed_username(request),
                peer=session.peer,
                action=request.cmd,
                target=_extract_target(request),
                success=success,
                details=str(details) if details is not None else None,
            )
        except Exception:
            # Сбой журнала не должен ронять уже выполненную команду: ответ
            # клиенту важнее. Но и молча терять запись нельзя — в лог сервера.
            logger.exception("Не удалось записать действие %r в журнал аудита", request.cmd)


def _extract_target(request: Request) -> str | None:
    """Объект действия для журнала: путь, архив или логин."""
    parts = []
    for name in TARGET_FIELDS:
        value = request.payload.get(name)
        if isinstance(value, str) and value:
            parts.append(value)
    sources = request.payload.get("sources")
    if isinstance(sources, list):
        parts.extend(item for item in sources if isinstance(item, str))
    destination = request.payload.get("destination")
    if isinstance(destination, str) and destination:
        parts.append("-> " + destination)
    if not parts:
        return None
    text = " ".join(parts)
    return text if len(text) <= MAX_TARGET_LENGTH else text[: MAX_TARGET_LENGTH - 1] + "…"


def _claimed_username(request: Request) -> str | None:
    """Логин из запроса входа: кто пытался войти, даже если не смог."""
    value = request.payload.get("username")
    if isinstance(value, str):
        return value[:64]
    return None
