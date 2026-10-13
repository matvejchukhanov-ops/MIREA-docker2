"""Блокировки по пути: защита от гонок между клиентами.

Каждый клиент обслуживается своим потоком, и два клиента одного
пользователя могут одновременно писать в один файл или удалять файл,
пока другой его архивирует. Блокировка на путь упорядочивает такие
операции, не мешая работе с разными файлами.

Замки создаются по требованию и удаляются, когда их больше никто не держит,
иначе словарь рос бы с каждым когда-либо тронутым путём.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class PathLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}

    @contextmanager
    def hold(self, *paths: Path) -> Iterator[None]:
        """Захватить замки на все пути сразу.

        Порядок захвата всегда один — по отсортированным ключам. Иначе поток,
        архивирующий a и b, и поток, архивирующий b и a, взаимно заблокируются.
        """
        keys = sorted({_key(path) for path in paths})
        locks = [self._acquire_entry(key) for key in keys]
        acquired: list[threading.Lock] = []
        try:
            for lock in locks:
                lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
            for key in keys:
                self._release_entry(key)

    def _acquire_entry(self, key: str) -> threading.Lock:
        with self._guard:
            lock, users = self._locks.get(key, (None, 0))
            if lock is None:
                lock = threading.Lock()
            self._locks[key] = (lock, users + 1)
            return lock

    def _release_entry(self, key: str) -> None:
        with self._guard:
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


def _key(path: Path) -> str:
    # normcase: на Windows «A.txt» и «a.txt» — один файл и один замок.
    return os.path.normcase(str(path))
