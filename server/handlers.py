"""Обработчики команд.

Каждый принимает разобранный запрос и состояние соединения, возвращает то,
что попадёт в поле result ответа. О JSON, кадрах и сокетах обработчики
не знают ничего — этим занимаются слои выше.

Проверка входа и запись в журнал аудита сделаны в диспетчере, а не здесь:
обработчик файловой команды может рассчитывать, что session.user заполнен.

Об ошибке сообщается исключением ProtocolError с кодом. Ошибки файловой
части (FsError) переводятся в него декоратором translate_errors.
"""
from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from .app import Services
from .auth.passwords import PasswordPolicyError, UsernamePolicyError
from .auth.service import AccountLockedError, LoginFailedError, UserExistsError
from .db import FileChange
from .dispatcher import Dispatcher
from .fs import archive, drives, formats
from .fs.errors import FsError
from .fs.manager import UserFiles, WrittenFile
from .protocol import ErrorCode, ProtocolError, Request
from .session import AuthenticatedUser, Session

DEFAULT_JOURNAL_LIMIT = 20
MAX_JOURNAL_LIMIT = 500
MAX_ZIP_SOURCES = 100


def translate_errors(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Перевести исключения файловой части в ошибки протокола."""

    @functools.wraps(handler)
    def wrapper(*args: Any) -> Any:
        try:
            return handler(*args)
        except FsError as exc:
            raise ProtocolError(ErrorCode(exc.code), exc.message) from exc

    return wrapper


class Handlers:
    def __init__(self, services: Services) -> None:
        self._services = services
        self._config = services.config

    # --- Служебные ---

    def ping(self, request: Request, session: Session) -> str:
        """Проверка живости соединения."""
        return "pong"

    def quit(self, request: Request, session: Session) -> str:
        """Завершение сеанса по инициативе клиента."""
        # Соединение закрывается не здесь: сначала нужно отправить ответ.
        session.should_close = True
        return "bye"

    # --- Учётные записи ---

    def register(self, request: Request, session: Session) -> dict[str, Any]:
        if session.is_authenticated:
            raise ProtocolError(ErrorCode.ALREADY_LOGGED_IN, "Сначала выйдите (logout)")
        if not self._config.allow_registration:
            raise ProtocolError(ErrorCode.REGISTRATION_CLOSED, "Регистрация на сервере закрыта")

        username = request.require_str("username", max_length=64)
        password = request.require_str("password", max_length=256)
        try:
            user = self._services.auth.register(username, password)
        except UsernamePolicyError as exc:
            raise ProtocolError(ErrorCode.BAD_USERNAME, str(exc)) from exc
        except PasswordPolicyError as exc:
            raise ProtocolError(ErrorCode.WEAK_PASSWORD, str(exc)) from exc
        except UserExistsError as exc:
            raise ProtocolError(ErrorCode.USER_EXISTS, "Такой логин уже занят") from exc

        # Сразу после регистрации пользователь вошёл: вводить пароль второй
        # раз ради того же самого — лишний шаг без пользы для безопасности.
        session.user = user
        self._services.files.for_user(user.id)  # создать каталог пользователя
        return {"username": user.username}

    def login(self, request: Request, session: Session) -> dict[str, Any]:
        if session.is_authenticated:
            raise ProtocolError(ErrorCode.ALREADY_LOGGED_IN, "Сначала выйдите (logout)")

        username = request.require_str("username", max_length=64)
        password = request.require_str("password", max_length=256)
        try:
            user = self._services.auth.login(username, password)
        except AccountLockedError as exc:
            self._count_failure(session)
            raise ProtocolError(
                ErrorCode.ACCOUNT_LOCKED,
                "Учётная запись временно заблокирована после серии неудачных попыток",
            ) from exc
        except LoginFailedError as exc:
            self._count_failure(session)
            # Одно сообщение на «нет такого логина» и «неверный пароль»:
            # иначе ответ сервера сам подсказывает, какие логины существуют.
            raise ProtocolError(ErrorCode.AUTH_FAILED, "Неверный логин или пароль") from exc

        session.user = user
        session.failed_logins = 0
        return {"username": user.username}

    def logout(self, request: Request, session: Session) -> str:
        session.user = None
        return "ok"

    def whoami(self, request: Request, session: Session) -> dict[str, Any]:
        user = self._user(session)
        return {"username": user.username}

    def _count_failure(self, session: Session) -> None:
        session.failed_logins += 1
        # Блокировка учётной записи не мешает перебирать пароли к разным
        # логинам в одном соединении. После предела соединение закрывается.
        if session.failed_logins >= self._config.max_login_failures:
            session.should_close = True

    # --- Диски ---

    def drives(self, request: Request, session: Session) -> list[dict[str, Any]]:
        return drives.list_drives()

    # --- Файлы и каталоги ---

    @translate_errors
    def ls(self, request: Request, session: Session) -> dict[str, Any]:
        return self._files(session).list_dir(request.optional_str("path", ""))

    @translate_errors
    def info(self, request: Request, session: Session) -> dict[str, Any]:
        user = self._user(session)
        result = self._files(session).info(request.optional_str("path", ""))
        if result["type"] == "file":
            # Сведения из реестра: когда файл появился в системе и как менялся.
            result["record"] = self._services.registry.live_record(user.id, result["path"])
        return result

    @translate_errors
    def read(self, request: Request, session: Session) -> dict[str, Any]:
        path, text = self._files(session).read_text(request.require_str("path"))
        return {"path": path, "content": text}

    @translate_errors
    def write(self, request: Request, session: Session) -> dict[str, Any]:
        content = request.require_str(
            "content", max_length=self._config.max_file_size, allow_empty=True
        )
        written = self._files(session).write_text(
            request.require_str("path"), content, overwrite=request.optional_bool("overwrite", False)
        )
        return self._register(session, written)

    @translate_errors
    def delete(self, request: Request, session: Session) -> dict[str, Any]:
        location = self._files(session).delete_file(request.require_str("path"))
        self._services.registry.record_delete(self._user(session).id, location)
        return {"path": location}

    @translate_errors
    def mkdir(self, request: Request, session: Session) -> dict[str, Any]:
        return {"path": self._files(session).make_dir(request.require_str("path"))}

    @translate_errors
    def rmdir(self, request: Request, session: Session) -> dict[str, Any]:
        return {"path": self._files(session).remove_dir(request.require_str("path"))}

    # --- JSON и XML ---

    @translate_errors
    def json_read(self, request: Request, session: Session) -> dict[str, Any]:
        path, text = self._files(session).read_text(request.require_str("path"))
        return {"path": path, "data": formats.parse_json(text)}

    @translate_errors
    def json_write(self, request: Request, session: Session) -> dict[str, Any]:
        text = formats.dump_json(request.require_present("data"))
        written = self._files(session).write_text(
            request.require_str("path"), text, overwrite=request.optional_bool("overwrite", False)
        )
        return self._register(session, written)

    @translate_errors
    def xml_read(self, request: Request, session: Session) -> dict[str, Any]:
        path, text = self._files(session).read_text(request.require_str("path"))
        return {"path": path, "tree": formats.parse_xml(text)}

    @translate_errors
    def xml_write(self, request: Request, session: Session) -> dict[str, Any]:
        has_content = request.payload.get("content") is not None
        has_tree = request.payload.get("tree") is not None
        if has_content == has_tree:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, "Нужно ровно одно из полей: content (текст XML) или tree"
            )
        if has_content:
            text = request.require_str("content", max_length=self._config.max_file_size)
            # Текст записывается как прислан, но только после того, как он
            # прошёл тот же безопасный разбор, что и при чтении. Иначе на
            # диск попал бы документ с DTD, опасный для других программ.
            formats.parse_xml(text)
        else:
            text = formats.build_xml(request.payload["tree"])
        written = self._files(session).write_text(
            request.require_str("path"), text, overwrite=request.optional_bool("overwrite", False)
        )
        return self._register(session, written)

    # --- ZIP ---

    @translate_errors
    def zip(self, request: Request, session: Session) -> dict[str, Any]:
        written, count = archive.create_archive(
            self._files(session),
            request.require_str("archive"),
            request.require_str_list("sources", max_items=MAX_ZIP_SOURCES),
            self._services.zip_limits,
            overwrite=request.optional_bool("overwrite", False),
        )
        return {**self._register(session, written), "files": count}

    @translate_errors
    def zip_list(self, request: Request, session: Session) -> dict[str, Any]:
        entries = archive.list_archive(
            self._files(session), request.require_str("archive"), self._services.zip_limits
        )
        return {"archive": request.payload["archive"], "entries": entries}

    @translate_errors
    def unzip(self, request: Request, session: Session) -> dict[str, Any]:
        archive_raw = request.require_str("archive")
        destination = request.optional_str("destination", "")
        if not destination:
            # По умолчанию — каталог рядом с архивом, названный как архив.
            pure = PurePosixPath(archive_raw.replace("\\", "/"))
            destination = str(pure.with_suffix("")) if pure.suffix else f"{pure}_files"
        files = archive.extract_archive(
            self._files(session), archive_raw, destination, self._services.zip_limits
        )
        self._services.registry.record_changes(
            self._user(session).id, [_change(item) for item in files]
        )
        return {
            "destination": destination,
            "files": [{"path": item.location, "size": item.size} for item in files],
        }

    # --- Журналы ---

    def log(self, request: Request, session: Session) -> list[dict[str, Any]]:
        limit = request.optional_int(
            "limit", DEFAULT_JOURNAL_LIMIT, minimum=1, maximum=MAX_JOURNAL_LIMIT
        )
        return self._services.audit.recent(self._user(session).id, limit)

    @translate_errors
    def ops(self, request: Request, session: Session) -> list[dict[str, Any]]:
        limit = request.optional_int(
            "limit", DEFAULT_JOURNAL_LIMIT, minimum=1, maximum=MAX_JOURNAL_LIMIT
        )
        location = None
        if request.payload.get("path") is not None:
            # Путь приводится к тому виду, в каком он лежит в реестре:
            # «./docs//a.txt» и «docs\a.txt» — та же запись, что «docs/a.txt».
            sandbox = self._files(session).sandbox
            location = sandbox.relative(sandbox.resolve(request.require_str("path")))
        return self._services.registry.operations(self._user(session).id, limit, location)

    # --- Внутреннее ---

    @staticmethod
    def _user(session: Session) -> AuthenticatedUser:
        if session.user is None:
            # Диспетчер не пускает сюда без входа. Если всё же попали —
            # это ошибка в регистрации команды, и лучше отказать громко.
            raise ProtocolError(ErrorCode.AUTH_REQUIRED, "Требуется вход")
        return session.user

    def _files(self, session: Session) -> UserFiles:
        return self._services.files.for_user(self._user(session).id)

    def _register(self, session: Session, written: WrittenFile) -> dict[str, Any]:
        """Записать изменение файла в реестр базы и собрать ответ."""
        self._services.registry.record_change(self._user(session).id, _change(written))
        return {"path": written.location, "size": written.size, "created": written.created}


def _change(written: WrittenFile) -> FileChange:
    return FileChange(written.location, written.size, written.created)


def build_dispatcher(services: Services) -> Dispatcher:
    """Собрать реестр команд.

    Вызывается один раз при запуске сервера. Реестр после сборки только
    читается, поэтому его безопасно делить между потоками.
    """
    handlers = Handlers(services)
    dispatcher = Dispatcher(audit=services.audit)

    public: dict[str, Callable[..., Any]] = {
        "register": handlers.register,
        "login": handlers.login,
    }
    for name, handler in public.items():
        dispatcher.register(name, handler, public=True)

    # Служебные команды не пишутся в журнал: ping от клиента-монитора
    # раз в секунду похоронил бы под собой все осмысленные записи.
    dispatcher.register("ping", handlers.ping, public=True, audited=False)
    dispatcher.register("quit", handlers.quit, public=True, audited=False)
    dispatcher.register("whoami", handlers.whoami, audited=False)

    private: dict[str, Callable[..., Any]] = {
        "logout": handlers.logout,
        "drives": handlers.drives,
        "ls": handlers.ls,
        "info": handlers.info,
        "read": handlers.read,
        "write": handlers.write,
        "delete": handlers.delete,
        "mkdir": handlers.mkdir,
        "rmdir": handlers.rmdir,
        "json_read": handlers.json_read,
        "json_write": handlers.json_write,
        "xml_read": handlers.xml_read,
        "xml_write": handlers.xml_write,
        "zip": handlers.zip,
        "zip_list": handlers.zip_list,
        "unzip": handlers.unzip,
        "log": handlers.log,
        "ops": handlers.ops,
    }
    for name, handler in private.items():
        dispatcher.register(name, handler)

    return dispatcher
