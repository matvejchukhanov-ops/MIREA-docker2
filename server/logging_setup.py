"""Настройка логирования.

Логи здесь не украшение: у сетевой ошибки нет наглядного проявления, кроме
того, что «ничего не работает». Без записи о том, кто подключился и что
прислал, отладка превращается в гадание.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Имя потока в каждой строке: с появлением потока на клиента без него
# невозможно понять, к какому соединению относится запись.
LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(threadName)-22s  %(message)s"
DATE_FORMAT = "%H:%M:%S"


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Настроить корневой логгер: вывод в консоль и, по желанию, в файл."""
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Неизвестный уровень логирования: {level!r}")

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    handlers: list[logging.Handler] = []

    _force_utf8_stdout()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    handlers.append(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    # Перенастройка при повторном вызове не должна плодить дубликаты строк.
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    for handler in handlers:
        root.addHandler(handler)


def _force_utf8_stdout() -> None:
    """Заставить стандартный вывод работать в UTF-8.

    На Windows при перенаправлении вывода в файл или канал Python берёт
    кодировку системы (обычно cp1251), и русский текст в логах превращается
    в мусор или роняет чтение на стороне получателя. В консоли проблема та же
    из-за кодовой страницы cp866.

    errors="replace" гарантирует, что логирование никогда не станет причиной
    падения: непредставимый символ будет заменён, а не выбросит исключение.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        # Поток заменён на что-то, что не поддерживает перенастройку, —
        # не повод прекращать запуск сервера.
        pass
