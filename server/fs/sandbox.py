"""Песочница: превращение пути от клиента в путь на диске сервера.

Главная угроза — обход каталогов (path traversal). Клиент присылает строку
вида  ../../etc/passwd  или  C:\\Windows\\win.ini, и наивное
root / user_path  отдаёт ему любой файл сервера. Защита в два слоя:

1. Синтаксический. Путь разбирается на компоненты, и отвергается всё, что
   может увести наружу или значит на Windows не то, чем кажется:
   абсолютные пути, буквы дисков, «..», двоеточия (альтернативные потоки
   NTFS: «file.txt:hidden»), зарезервированные имена устройств (CON, NUL),
   завершающие точки и пробелы (Windows их отбрасывает, и «a.txt.»
   оказывается тем же файлом, что «a.txt»).

2. Семантический. Готовый путь разрешается через resolve() — со всеми
   символическими ссылками — и проверяется, что результат лежит внутри
   корня. Это ловит то, что синтаксис не видит: ссылку внутри песочницы,
   указывающую наружу.

Проверка именно «после resolve», а не сравнение строк до него: строковое
сравнение обманывается ссылками, регистром и короткими именами 8.3.
"""
from __future__ import annotations

import re
from pathlib import Path

from .errors import BadPathError

MAX_PATH_LENGTH = 1024
MAX_COMPONENT_LENGTH = 255
MAX_DEPTH = 32

# Префикс служебных временных файлов сервера. Клиенту такие имена
# недоступны, чтобы он не мог вмешаться в атомарную запись.
TEMP_PREFIX = ".fm-tmp-"

_FORBIDDEN_CHARS = frozenset('<>:"|?*') | frozenset(chr(code) for code in range(32)) | {"\x7f"}

_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)

_DRIVE = re.compile(r"^[A-Za-z]:")


def split_user_path(raw: str) -> list[str]:
    """Разобрать путь клиента на компоненты или отвергнуть его.

    Разделителем считаются и «/», и «\\», независимо от ОС сервера: клиент
    может работать на другой системе. Пустая строка и «.» — корень.
    """
    if not isinstance(raw, str):
        raise BadPathError("Путь должен быть строкой")
    if len(raw) > MAX_PATH_LENGTH:
        raise BadPathError(f"Путь длиннее {MAX_PATH_LENGTH} символов")
    if "\x00" in raw:
        # Нулевой байт обрезает строку на уровне системных вызовов:
        # «a.txt\x00.jpg» проходит проверку расширения и открывает a.txt.
        raise BadPathError("Путь содержит нулевой байт")

    text = raw.replace("\\", "/")
    if text.startswith("/"):
        raise BadPathError("Путь должен быть относительным — от вашего каталога")
    if _DRIVE.match(text):
        raise BadPathError("Буквы дисков в пути недопустимы")

    parts = [part for part in text.split("/") if part not in ("", ".")]
    if len(parts) > MAX_DEPTH:
        raise BadPathError(f"Вложенность больше {MAX_DEPTH} уровней")

    for part in parts:
        _check_component(part)
    return parts


def _check_component(part: str) -> None:
    if part == "..":
        raise BadPathError("Переход в родительский каталог («..») недопустим")
    if len(part) > MAX_COMPONENT_LENGTH:
        raise BadPathError(f"Имя длиннее {MAX_COMPONENT_LENGTH} символов")
    bad = sorted({char for char in part if char in _FORBIDDEN_CHARS})
    if bad:
        shown = ", ".join(repr(char) for char in bad)
        raise BadPathError(f"Недопустимые символы в имени {part!r}: {shown}")
    if part.endswith((".", " ")):
        raise BadPathError(f"Имя {part!r} не может оканчиваться точкой или пробелом")
    stem = part.split(".", 1)[0].upper()
    if stem in _RESERVED_NAMES:
        raise BadPathError(f"Имя {part!r} зарезервировано системой")
    if part.startswith(TEMP_PREFIX):
        raise BadPathError(f"Имена, начинающиеся с {TEMP_PREFIX!r}, зарезервированы сервером")


class Sandbox:
    """Каталог одного пользователя и всё, что разрешено внутри него."""

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        # Корень разрешается один раз: дальше все пути сравниваются с ним.
        self._root = root.resolve(strict=True)

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, raw: str) -> Path:
        """Путь клиента -> абсолютный путь внутри песочницы."""
        parts = split_user_path(raw)
        candidate = self._root.joinpath(*parts)
        # strict=False: путь может ещё не существовать (создание файла).
        # Существующие части при этом всё равно проходят через ссылки.
        resolved = candidate.resolve(strict=False)
        if not self.contains(resolved):
            raise BadPathError("Путь выходит за пределы вашего каталога")
        return resolved

    def contains(self, path: Path) -> bool:
        return path == self._root or path.is_relative_to(self._root)

    def relative(self, path: Path) -> str:
        """Абсолютный путь -> вид, который показывается клиенту."""
        relative = path.relative_to(self._root).as_posix()
        return "" if relative == "." else relative
