"""Операции с файлами и каталогами в песочнице пользователя.

Чего здесь сознательно нет: проверок «существует ли файл» перед действием
в духе  if path.exists(): path.unlink(). Между проверкой и действием
состояние диска может измениться (гонка TOCTOU — time of check to time of
use). Поэтому:

* действие выполняется сразу, а ошибка ОС переводится в понятный код;
* где проверка всё же нужна (создать, только если файла нет), она и
  действие выполняются под одной блокировкой пути;
* запись идёт во временный файл, который затем атомарно подменяет целевой
  (os.replace). Читатель видит либо старое содержимое, либо новое целиком,
  но никогда — наполовину записанный файл, даже если сервер упал посреди
  записи.
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .errors import (
    AccessDeniedError,
    AlreadyExistsError,
    BadPathError,
    DirectoryNotEmptyError,
    FileTooLargeError,
    NotADirectoryError_,
    NotAFileError,
    NotFoundError,
    NotTextError,
)
from .locks import PathLocks
from .sandbox import TEMP_PREFIX, Sandbox

# Сколько записей каталога отдавать за один раз. Каталог на сто тысяч
# файлов иначе превратился бы в ответ на десятки мегабайт.
MAX_LISTING = 1000


@dataclass(frozen=True, slots=True)
class WrittenFile:
    """Что получилось после записи — для реестра в базе."""

    location: str
    size: int
    created: bool


class FileManager:
    """Общая часть для всех пользователей: корень хранилища и блокировки."""

    def __init__(self, storage_root: Path, *, max_file_size: int) -> None:
        storage_root.mkdir(parents=True, exist_ok=True)
        self._storage_root = storage_root.resolve(strict=True)
        self._max_file_size = max_file_size
        self.locks = PathLocks()

    @property
    def root(self) -> Path:
        return self._storage_root

    @property
    def max_file_size(self) -> int:
        return self._max_file_size

    def for_user(self, user_id: int) -> UserFiles:
        # Каталог именуется по числовому id, а не по логину: логин — данные
        # от пользователя, а id выдаёт база. Так имя каталога не зависит
        # от того, что прислал клиент.
        sandbox = Sandbox(self._storage_root / f"user-{int(user_id)}")
        return UserFiles(sandbox, self.locks, self._max_file_size)


class UserFiles:
    """Файлы одного пользователя."""

    def __init__(self, sandbox: Sandbox, locks: PathLocks, max_file_size: int) -> None:
        self.sandbox = sandbox
        self.locks = locks
        self.max_file_size = max_file_size

    # --- Чтение ---

    def list_dir(self, raw: str) -> dict[str, Any]:
        path = self.sandbox.resolve(raw)
        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            with os.scandir(path) as iterator:
                for entry in iterator:
                    if entry.name.startswith(TEMP_PREFIX):
                        continue
                    if len(entries) >= MAX_LISTING:
                        truncated = True
                        break
                    entries.append(_describe_entry(entry))
        except FileNotFoundError as exc:
            raise NotFoundError(f"Каталог {raw!r} не найден") from exc
        except NotADirectoryError as exc:
            raise NotADirectoryError_(f"{raw!r} — не каталог") from exc
        except PermissionError as exc:
            raise AccessDeniedError(f"Нет доступа к {raw!r}") from exc

        # Каталоги первыми, внутри групп — по имени без учёта регистра.
        entries.sort(key=lambda item: (item["type"] != "dir", item["name"].casefold()))
        return {
            "path": self.sandbox.relative(path),
            "entries": entries,
            "truncated": truncated,
        }

    def info(self, raw: str) -> dict[str, Any]:
        path = self.sandbox.resolve(raw)
        try:
            st = path.stat()
        except FileNotFoundError as exc:
            raise NotFoundError(f"{raw!r} не найден") from exc
        except PermissionError as exc:
            raise AccessDeniedError(f"Нет доступа к {raw!r}") from exc
        return {
            "path": self.sandbox.relative(path),
            "name": path.name if path != self.sandbox.root else "",
            "type": _kind(st.st_mode),
            "size": st.st_size if stat.S_ISREG(st.st_mode) else None,
            "created": _iso(_birth_time(st)),
            "modified": _iso(st.st_mtime),
        }

    def read_text(self, raw: str) -> tuple[str, str]:
        """Прочитать текстовый файл целиком. Возвращает (путь, текст)."""
        path = self.sandbox.resolve(raw)
        data = self.read_bytes(path, raw)
        try:
            # utf-8-sig: Блокнот Windows пишет в начало файла метку BOM,
            # и без её снятия первый символ текста оказался бы мусорным.
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise NotTextError(f"{raw!r} — не текстовый файл в UTF-8") from exc
        return self.sandbox.relative(path), text

    def read_bytes(self, path: Path, shown: str) -> bytes:
        """Прочитать файл с проверкой размера по уже открытому дескриптору."""
        try:
            handle = open(path, "rb")
        except FileNotFoundError as exc:
            raise NotFoundError(f"Файл {shown!r} не найден") from exc
        except (IsADirectoryError, PermissionError) as exc:
            if path.is_dir():
                raise NotAFileError(f"{shown!r} — каталог, а не файл") from exc
            raise AccessDeniedError(f"Нет доступа к {shown!r}") from exc

        with handle:
            # Размер проверяется у открытого файла (fstat), а не по пути до
            # открытия: файл могли подменить между проверкой и открытием.
            st = os.fstat(handle.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise NotAFileError(f"{shown!r} — не обычный файл")
            if st.st_size > self.max_file_size:
                raise FileTooLargeError(
                    f"{shown!r} больше предела {self.max_file_size} байт ({st.st_size})"
                )
            # Читаем на байт больше предела: если файл дописывают прямо
            # сейчас, fstat мог показать старый размер.
            data = handle.read(self.max_file_size + 1)
        if len(data) > self.max_file_size:
            raise FileTooLargeError(f"{shown!r} больше предела {self.max_file_size} байт")
        return data

    # --- Изменение ---

    def write_text(self, raw: str, content: str, *, overwrite: bool) -> WrittenFile:
        data = content.encode("utf-8")
        if len(data) > self.max_file_size:
            raise FileTooLargeError(
                f"Содержимое больше предела {self.max_file_size} байт ({len(data)})"
            )
        path = self.file_target(raw)

        with self.locks.hold(path):
            # Проверка и запись — под одной блокировкой: второй клиент,
            # создающий тот же файл, дождётся первого и получит ALREADY_EXISTS,
            # а не молча перезапишет его работу.
            existed = exists_as_file(path, raw)
            if existed and not overwrite:
                raise AlreadyExistsError(
                    f"Файл {raw!r} уже существует. Для замены нужен overwrite"
                )
            with atomic_writer(path) as handle:
                handle.write(data)

        return WrittenFile(self.sandbox.relative(path), len(data), created=not existed)

    def delete_file(self, raw: str) -> str:
        path = self.sandbox.resolve(raw)
        if path == self.sandbox.root:
            raise BadPathError("Корневой каталог удалить нельзя")
        with self.locks.hold(path):
            if path.is_dir():
                raise NotAFileError(f"{raw!r} — каталог. Для каталогов команда rmdir")
            try:
                path.unlink()
            except FileNotFoundError as exc:
                raise NotFoundError(f"Файл {raw!r} не найден") from exc
            except PermissionError as exc:
                raise AccessDeniedError(f"Нет доступа к {raw!r}") from exc
        return self.sandbox.relative(path)

    def make_dir(self, raw: str) -> str:
        path = self.sandbox.resolve(raw)
        try:
            # parents=False: опечатка в середине пути не должна молча
            # создавать цепочку каталогов.
            path.mkdir(parents=False)
        except FileExistsError as exc:
            raise AlreadyExistsError(f"{raw!r} уже существует") from exc
        except FileNotFoundError as exc:
            raise NotFoundError(f"Родительский каталог для {raw!r} не существует") from exc
        except PermissionError as exc:
            raise AccessDeniedError(f"Нет доступа к {raw!r}") from exc
        return self.sandbox.relative(path)

    def remove_dir(self, raw: str) -> str:
        path = self.sandbox.resolve(raw)
        if path == self.sandbox.root:
            raise BadPathError("Корневой каталог удалить нельзя")
        with self.locks.hold(path):
            try:
                # Только пустой каталог. Рекурсивное удаление одной командой —
                # слишком дорогая цена за опечатку, и файлы ушли бы мимо журнала.
                path.rmdir()
            except FileNotFoundError as exc:
                raise NotFoundError(f"Каталог {raw!r} не найден") from exc
            except NotADirectoryError as exc:
                raise NotADirectoryError_(f"{raw!r} — не каталог") from exc
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST) or _has_children(path):
                    raise DirectoryNotEmptyError(f"Каталог {raw!r} не пуст") from exc
                if not path.is_dir():
                    raise NotADirectoryError_(f"{raw!r} — не каталог") from exc
                raise AccessDeniedError(f"Не удалось удалить {raw!r}") from exc
        return self.sandbox.relative(path)

    def file_target(self, raw: str) -> Path:
        """Путь, по которому будет создан или перезаписан файл."""
        path = self.sandbox.resolve(raw)
        if path == self.sandbox.root:
            raise BadPathError("Нужен путь к файлу, а не к корню")
        if not path.parent.is_dir():
            raise NotFoundError(f"Каталог для {raw!r} не существует — создайте его командой mkdir")
        return path


@contextmanager
def atomic_writer(path: Path) -> Iterator[BinaryIO]:
    """Записать файл целиком или не изменить его вовсе.

    Временный файл создаётся в том же каталоге: os.replace атомарен только
    в пределах одной файловой системы. mkstemp создаёт файл с уникальным
    именем эксклюзивно (O_EXCL) и правами только для владельца.
    """
    fd, temp_name = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            yield handle
            handle.flush()
            # Без fsync данные могут остаться в кеше ОС, и после сбоя
            # питания на месте файла окажется пустышка.
            os.fsync(handle.fileno())
        _replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


# Сколько раз повторить подмену файла, занятого читателем, и пауза между попытками.
REPLACE_ATTEMPTS = 20
REPLACE_DELAY = 0.025


def _replace(source: Path, target: Path) -> None:
    """os.replace с повтором на Windows.

    Windows не даёт заменить файл, пока его держит открытым другой поток
    (например, клиент читает этот файл прямо сейчас), и отвечает отказом в
    доступе. Чтение длится миллисекунды, поэтому короткое ожидание надёжнее,
    чем вернуть пользователю ошибку на ровном месте.
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise AccessDeniedError(
                    "Файл занят другой операцией, повторите попытку"
                ) from None
            time.sleep(REPLACE_DELAY)


def exists_as_file(path: Path, shown: str) -> bool:
    """Есть ли по пути файл. Каталог на этом месте — ошибка, а не «нет»."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(st.st_mode):
        raise NotAFileError(f"{shown!r} — каталог, а не файл")
    return True


def _has_children(path: Path) -> bool:
    try:
        return any(path.iterdir())
    except OSError:
        return False


def _describe_entry(entry: os.DirEntry[str]) -> dict[str, Any]:
    # follow_symlinks=False: ссылка показывается ссылкой, а не тем, на что
    # она указывает, — иначе листинг раскрывал бы сведения о файлах снаружи.
    st = entry.stat(follow_symlinks=False)
    kind = _kind(st.st_mode)
    return {
        "name": entry.name,
        "type": kind,
        "size": st.st_size if kind == "file" else None,
        "modified": _iso(st.st_mtime),
    }


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "link"
    return "other"


def _birth_time(st: os.stat_result) -> float:
    # Время создания есть не везде: на Windows и BSD — st_birthtime,
    # на Linux его нет, и ближайшее — время изменения метаданных.
    return getattr(st, "st_birthtime", None) or st.st_ctime


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S")
