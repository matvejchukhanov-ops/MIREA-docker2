"""Исключения файловой части.

Каждое несёт код, совпадающий с кодом ошибки протокола. Слой команд
превращает их в ответ клиенту одной строкой и не разбирает по типам.

Сообщения пишутся для пользователя и никогда не содержат абсолютных путей
сервера: клиенту незачем знать, где на диске лежит его каталог.
"""
from __future__ import annotations


class FsError(Exception):
    code = "INTERNAL_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BadPathError(FsError):
    code = "BAD_PATH"


class NotFoundError(FsError):
    code = "NOT_FOUND"


class AlreadyExistsError(FsError):
    code = "ALREADY_EXISTS"


class NotAFileError(FsError):
    code = "NOT_A_FILE"


class NotADirectoryError_(FsError):
    # Подчёркивание — чтобы не затенять встроенный NotADirectoryError.
    code = "NOT_A_DIRECTORY"


class DirectoryNotEmptyError(FsError):
    code = "DIR_NOT_EMPTY"


class FileTooLargeError(FsError):
    code = "FILE_TOO_LARGE"


class NotTextError(FsError):
    code = "NOT_TEXT"


class AccessDeniedError(FsError):
    code = "ACCESS_DENIED"


class BadFormatError(FsError):
    code = "BAD_FORMAT"


class BadArchiveError(FsError):
    code = "BAD_ARCHIVE"


class UnsafeArchiveError(FsError):
    code = "UNSAFE_ARCHIVE"


class LimitExceededError(FsError):
    code = "LIMIT_EXCEEDED"
