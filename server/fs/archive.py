"""Создание и распаковка ZIP-архивов.

Распаковка архива от пользователя — одна из самых опасных операций
файлового менеджера. Угрозы и защита:

* Zip Slip — имя записи  ../../server/config.py  или  C:/Windows/x.dll.
  Наивное  dest / info.filename  пишет файл куда угодно. Каждое имя
  проходит те же правила, что и путь от клиента (sandbox.split_user_path),
  а итоговый путь дополнительно сверяется с каталогом распаковки.

* Zip-бомба — архив в 42 КБ, разворачивающийся в 4,5 ПБ. Проверяются:
  число записей, суммарный объявленный размер, степень сжатия каждой
  записи. Но заголовкам верить нельзя — размер в них пишет создатель
  архива. Поэтому байты считаются ещё и при самой распаковке, и она
  обрывается, как только записано больше объявленного или больше предела.

* Символические ссылки в архиве — запись-ссылка на /etc, а следом файл
  «внутрь» этой ссылки. Ссылки не распаковываются вовсе.

* Зашифрованные записи — содержимое нельзя проверить, не зная пароля.

Распаковка идёт во временный каталог рядом с целевым, и только после
успеха он переименовывается в целевой. Неудача на середине не оставляет
на диске половину архива.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    AlreadyExistsError,
    BadArchiveError,
    BadPathError,
    LimitExceededError,
    NotAFileError,
    NotFoundError,
    UnsafeArchiveError,
)
from .manager import UserFiles, WrittenFile, atomic_writer, exists_as_file
from .sandbox import TEMP_PREFIX, split_user_path

CHUNK_SIZE = 64 * 1024

# Признак шифрования в поле флагов записи ZIP.
_ENCRYPTED_FLAG = 0x1
# Значение create_system для архивов, созданных в Unix: только у них
# старшие биты external_attr — это режим файла, по которому видна ссылка.
_UNIX = 3


@dataclass(frozen=True, slots=True)
class ZipLimits:
    max_entries: int
    max_total_size: int
    max_ratio: int


@dataclass(frozen=True, slots=True)
class _PlannedEntry:
    info: zipfile.ZipInfo
    parts: list[str]
    is_dir: bool


# --- Создание ---


def create_archive(
    files: UserFiles,
    archive_raw: str,
    sources_raw: list[str],
    limits: ZipLimits,
    *,
    overwrite: bool,
) -> tuple[WrittenFile, int]:
    """Упаковать файлы и каталоги. Возвращает (архив, число файлов в нём)."""
    archive = files.file_target(archive_raw)
    if archive.suffix.lower() != ".zip":
        raise BadPathError("Имя архива должно оканчиваться на .zip")

    sources = [files.sandbox.resolve(raw) for raw in sources_raw]
    items = _collect(files, sources, archive, limits)
    if not items:
        raise NotFoundError("Нечего упаковывать: в указанных путях нет файлов")

    with files.locks.hold(archive, *sources):
        existed = exists_as_file(archive, archive_raw)
        if existed and not overwrite:
            raise AlreadyExistsError(f"Архив {archive_raw!r} уже существует")
        with atomic_writer(archive) as handle:
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for path, name in items:
                    bundle.write(path, name)
        size = archive.stat().st_size

    written = WrittenFile(files.sandbox.relative(archive), size, created=not existed)
    return written, len(items)


def _collect(
    files: UserFiles, sources: list[Path], archive: Path, limits: ZipLimits
) -> list[tuple[Path, str]]:
    """Список (файл на диске, имя в архиве) с проверкой пределов.

    Пределы те же, что при распаковке: архив, который сервер сам создал,
    он же должен суметь распаковать.
    """
    items: dict[str, Path] = {}
    total = 0

    def add(path: Path) -> None:
        nonlocal total
        if path == archive:
            return
        name = files.sandbox.relative(path)
        if name in items:
            return
        size = path.stat().st_size
        total += size
        if len(items) + 1 > limits.max_entries:
            raise LimitExceededError(f"В архив попадает больше {limits.max_entries} файлов")
        if total > limits.max_total_size:
            raise LimitExceededError(f"Суммарный размер больше {limits.max_total_size} байт")
        items[name] = path

    for source in sources:
        try:
            st = os.lstat(source)
        except FileNotFoundError as exc:
            raise NotFoundError(f"{files.sandbox.relative(source) or '.'!r} не найден") from exc
        if stat.S_ISREG(st.st_mode):
            add(source)
        elif stat.S_ISDIR(st.st_mode):
            # followlinks=False: ссылка внутри каталога могла бы затянуть
            # в архив файлы из-за пределов песочницы.
            for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
                dirnames.sort()
                for filename in sorted(filenames):
                    path = Path(dirpath) / filename
                    if filename.startswith(TEMP_PREFIX) or path.is_symlink():
                        continue
                    add(path)
        else:
            raise NotAFileError("Упаковываются только обычные файлы и каталоги")

    return [(path, name) for name, path in sorted(items.items())]


# --- Просмотр ---


def list_archive(files: UserFiles, archive_raw: str, limits: ZipLimits) -> list[dict[str, Any]]:
    archive = files.sandbox.resolve(archive_raw)
    with _open_zip(archive, archive_raw) as bundle:
        infos = bundle.infolist()
        if len(infos) > limits.max_entries:
            raise UnsafeArchiveError(f"В архиве больше {limits.max_entries} записей")
        return [
            {
                "name": info.filename,
                "size": info.file_size,
                "compressed": info.compress_size,
                "is_dir": info.is_dir(),
            }
            for info in infos
        ]


# --- Распаковка ---


def extract_archive(
    files: UserFiles, archive_raw: str, destination_raw: str, limits: ZipLimits
) -> list[WrittenFile]:
    """Распаковать архив в новый каталог. Возвращает распакованные файлы."""
    archive = files.sandbox.resolve(archive_raw)
    destination = files.sandbox.resolve(destination_raw)
    if destination == files.sandbox.root:
        raise BadPathError("Распаковка возможна только в новый каталог, а не в корень")
    if not destination.parent.is_dir():
        raise NotFoundError(f"Каталог для {destination_raw!r} не существует")

    with files.locks.hold(archive, destination):
        if destination.exists():
            # Только в новый каталог: распаковка поверх существующих файлов
            # молча заменила бы их, и запрос «распакуй» стал бы способом
            # перезаписать что угодно без подтверждения.
            raise AlreadyExistsError(f"{destination_raw!r} уже существует — укажите новый каталог")

        with _open_zip(archive, archive_raw) as bundle:
            plan = _plan(bundle, limits)
            staging = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=destination.parent))
            try:
                written = _extract(bundle, plan, staging, limits)
                os.rename(staging, destination)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise

    return [
        WrittenFile(files.sandbox.relative(destination.joinpath(*parts)), size, created=True)
        for parts, size in written
    ]


def _plan(bundle: zipfile.ZipFile, limits: ZipLimits) -> list[_PlannedEntry]:
    """Проверить все записи до того, как будет записан первый байт."""
    infos = bundle.infolist()
    if len(infos) > limits.max_entries:
        raise UnsafeArchiveError(f"В архиве больше {limits.max_entries} записей")

    plan: list[_PlannedEntry] = []
    seen: set[str] = set()
    declared_total = 0

    for info in infos:
        name = info.filename
        if info.flag_bits & _ENCRYPTED_FLAG:
            raise UnsafeArchiveError(f"Запись {name!r} зашифрована — проверить её нельзя")
        if info.create_system == _UNIX and stat.S_ISLNK(info.external_attr >> 16):
            raise UnsafeArchiveError(f"Запись {name!r} — символическая ссылка")

        try:
            parts = split_user_path(name)
        except BadPathError as exc:
            # Zip Slip: имя пытается выйти за каталог распаковки.
            raise UnsafeArchiveError(f"Недопустимое имя записи {name!r}: {exc.message}") from exc
        if not parts:
            continue

        # Без учёта регистра: на Windows «A.txt» и «a.txt» — один файл, и
        # вторая запись молча заменила бы первую.
        key = "/".join(parts).casefold()
        if key in seen:
            raise UnsafeArchiveError(f"Запись {name!r} повторяется")
        seen.add(key)

        if not info.is_dir():
            declared_total += info.file_size
            if declared_total > limits.max_total_size:
                raise UnsafeArchiveError(
                    f"Распакованный размер больше предела {limits.max_total_size} байт"
                )
            if info.file_size > 0:
                if info.compress_size == 0:
                    raise UnsafeArchiveError(f"Запись {name!r}: нулевой сжатый размер")
                if info.file_size / info.compress_size > limits.max_ratio:
                    raise UnsafeArchiveError(
                        f"Запись {name!r} сжата в {info.file_size // info.compress_size} раз"
                        f" — больше предела {limits.max_ratio}. Похоже на zip-бомбу"
                    )

        plan.append(_PlannedEntry(info, parts, info.is_dir()))

    return plan


def _extract(
    bundle: zipfile.ZipFile, plan: list[_PlannedEntry], staging: Path, limits: ZipLimits
) -> list[tuple[list[str], int]]:
    written: list[tuple[list[str], int]] = []
    total = 0
    staging_root = staging.resolve()

    for entry in plan:
        target = staging.joinpath(*entry.parts)
        # Вторая линия обороны от Zip Slip: имя уже проверено, но сверка
        # с реальным путём не зависит от того, всё ли учли правила имён.
        if not target.resolve().is_relative_to(staging_root):
            raise UnsafeArchiveError(f"Запись {entry.info.filename!r} выходит за каталог")

        try:
            if entry.is_dir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)

            size = 0
            with bundle.open(entry.info) as source, open(target, "xb") as sink:
                while chunk := source.read(CHUNK_SIZE):
                    size += len(chunk)
                    total += len(chunk)
                    # Заголовок мог соврать о размере. Считаем сами.
                    if size > entry.info.file_size or total > limits.max_total_size:
                        raise UnsafeArchiveError(
                            f"Запись {entry.info.filename!r} распаковывается больше"
                            " объявленного размера"
                        )
                    sink.write(chunk)
        except (zipfile.BadZipFile, EOFError) as exc:
            raise BadArchiveError(f"Архив повреждён: {exc}") from exc
        except (FileExistsError, NotADirectoryError, IsADirectoryError) as exc:
            raise UnsafeArchiveError(
                f"Запись {entry.info.filename!r} конфликтует с другой записью архива"
            ) from exc
        written.append((entry.parts, size))

    return written


def _open_zip(archive: Path, shown: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(archive)
    except FileNotFoundError as exc:
        raise NotFoundError(f"Архив {shown!r} не найден") from exc
    except (IsADirectoryError, PermissionError) as exc:
        if archive.is_dir():
            raise NotAFileError(f"{shown!r} — каталог, а не архив") from exc
        raise
    except zipfile.BadZipFile as exc:
        raise BadArchiveError(f"{shown!r} — не ZIP-архив или архив повреждён") from exc
