"""Конфигурация сервера.

Приоритет источников: аргументы командной строки, затем переменные окружения,
затем умолчания из этого модуля. Порядок именно такой — явное указание при
запуске перекрывает окружение, окружение перекрывает умолчания.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from common.framing import MAX_FRAME_SIZE as DEFAULT_MAX_FRAME_SIZE

# --- Сеть ---

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000

# Размер очереди подключений: сколько клиентов ОС подержит в ожидании, пока
# сервер занят предыдущим вызовом accept().
DEFAULT_BACKLOG = 16

# Сколько байт запрашиваем у сокета за одно чтение. Это НЕ размер сообщения:
# recv() вправе вернуть меньше запрошенного и почти всегда так и делает.
DEFAULT_RECV_SIZE = 64 * 1024

# Сколько секунд ждать данных от молчащего клиента, прежде чем закрыть
# соединение. Без таймаута забытое соединение занимает ресурсы бесконечно.
DEFAULT_IDLE_TIMEOUT = 300.0

# Сколько клиентов обслуживать одновременно. Каждый занимает поток, а поток
# стоит памяти под стек: без предела клиент, открывающий соединения пачками,
# исчерпает ресурсы машины.
DEFAULT_MAX_CONNECTIONS = 64

DEFAULT_LOG_LEVEL = "INFO"

# --- Хранилище ---

# Каталог, внутри которого живут каталоги пользователей. За его пределы
# сервер не выходит ни при каких путях от клиента.
DEFAULT_STORAGE_ROOT = Path("storage")
DEFAULT_DB_PATH = Path("data") / "filemanager.db"

# Предел размера файла, который читается и пишется через протокол целиком.
DEFAULT_MAX_FILE_SIZE = 256 * 1024

# --- ZIP ---
# Пределы распаковки — защита от zip-бомб: архив в килобайты, разворачивающийся
# в гигабайты, заполняет диск сервера за секунды.
DEFAULT_ZIP_MAX_ENTRIES = 1000
DEFAULT_ZIP_MAX_TOTAL_SIZE = 100 * 1024 * 1024
DEFAULT_ZIP_MAX_RATIO = 100

# --- Вход ---
# После стольких неудачных попыток подряд учётная запись блокируется.
DEFAULT_MAX_LOGIN_FAILURES = 5
DEFAULT_LOCKOUT_SECONDS = 300

_MIN_PORT = 1
_MAX_PORT = 65535
_PRIVILEGED_PORT_MAX = 1023


class ConfigError(ValueError):
    """Некорректное значение конфигурации."""


# Запас кадра на всё, кроме содержимого файла: путь, имена полей, кавычки.
FRAME_OVERHEAD = 16 * 1024


def frame_size_for(max_file_size: int) -> int:
    """Наименьший кадр, в который гарантированно помещается файл.

    В худшем случае каждый байт файла превращается в JSON в шесть: управляющий
    символ \\x01 записывается как \\u0001. Если кадр меньше, файл, который
    сервер сам разрешил записать, нельзя будет прочитать обратно.
    """
    return max_file_size * 6 + FRAME_OVERHEAD


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Параметры запуска сервера."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    backlog: int = DEFAULT_BACKLOG
    recv_size: int = DEFAULT_RECV_SIZE
    max_frame_size: int = DEFAULT_MAX_FRAME_SIZE
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    log_level: str = DEFAULT_LOG_LEVEL

    storage_root: Path = DEFAULT_STORAGE_ROOT
    db_path: Path = DEFAULT_DB_PATH
    max_file_size: int = DEFAULT_MAX_FILE_SIZE
    allow_registration: bool = True

    zip_max_entries: int = DEFAULT_ZIP_MAX_ENTRIES
    zip_max_total_size: int = DEFAULT_ZIP_MAX_TOTAL_SIZE
    zip_max_ratio: int = DEFAULT_ZIP_MAX_RATIO

    max_login_failures: int = DEFAULT_MAX_LOGIN_FAILURES
    lockout_seconds: int = DEFAULT_LOCKOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.host:
            raise ConfigError("Адрес не может быть пустым")
        if not _MIN_PORT <= self.port <= _MAX_PORT:
            raise ConfigError(
                f"Порт должен быть в диапазоне {_MIN_PORT}-{_MAX_PORT}, получено {self.port}"
            )
        if self.backlog < 1:
            raise ConfigError("Размер очереди подключений должен быть не меньше 1")
        if self.recv_size < 1:
            raise ConfigError("Размер буфера чтения должен быть не меньше 1")
        if self.max_frame_size < 2:
            raise ConfigError("Предельный размер кадра должен вмещать байт и разделитель")
        if self.idle_timeout <= 0:
            raise ConfigError("Таймаут бездействия должен быть положительным")
        if self.max_connections < 1:
            raise ConfigError("Число одновременных подключений должно быть не меньше 1")
        if self.max_file_size < 1:
            raise ConfigError("Предельный размер файла должен быть положительным")
        if self.max_frame_size < frame_size_for(self.max_file_size):
            raise ConfigError(
                f"Кадр ({self.max_frame_size} байт) не вмещает файл предельного размера"
                f" ({self.max_file_size} байт): нужно не меньше"
                f" {frame_size_for(self.max_file_size)}"
            )
        if self.zip_max_entries < 1 or self.zip_max_total_size < 1 or self.zip_max_ratio < 1:
            raise ConfigError("Пределы распаковки ZIP должны быть положительными")
        if self.max_login_failures < 1 or self.lockout_seconds < 1:
            raise ConfigError("Пределы входа должны быть положительными")

    @property
    def address(self) -> tuple[str, int]:
        """Адрес в виде, который принимают методы сокета."""
        return self.host, self.port

    @property
    def needs_privileges(self) -> bool:
        """Порты до 1024 требуют прав администратора."""
        return self.port <= _PRIVILEGED_PORT_MAX

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Собрать конфигурацию из переменных окружения."""
        return cls(
            host=os.environ.get("FM_HOST", DEFAULT_HOST),
            port=_env_int("FM_PORT", DEFAULT_PORT),
            backlog=_env_int("FM_BACKLOG", DEFAULT_BACKLOG),
            recv_size=_env_int("FM_RECV_SIZE", DEFAULT_RECV_SIZE),
            max_frame_size=_env_int("FM_MAX_FRAME_SIZE", DEFAULT_MAX_FRAME_SIZE),
            idle_timeout=_env_float("FM_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT),
            max_connections=_env_int("FM_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS),
            log_level=os.environ.get("FM_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
            storage_root=_env_path("FM_STORAGE", DEFAULT_STORAGE_ROOT),
            db_path=_env_path("FM_DB", DEFAULT_DB_PATH),
            max_file_size=_env_int("FM_MAX_FILE_SIZE", DEFAULT_MAX_FILE_SIZE),
            allow_registration=_env_bool("FM_ALLOW_REGISTRATION", True),
            zip_max_entries=_env_int("FM_ZIP_MAX_ENTRIES", DEFAULT_ZIP_MAX_ENTRIES),
            zip_max_total_size=_env_int("FM_ZIP_MAX_TOTAL_SIZE", DEFAULT_ZIP_MAX_TOTAL_SIZE),
            zip_max_ratio=_env_int("FM_ZIP_MAX_RATIO", DEFAULT_ZIP_MAX_RATIO),
            max_login_failures=_env_int("FM_MAX_LOGIN_FAILURES", DEFAULT_MAX_LOGIN_FAILURES),
            lockout_seconds=_env_int("FM_LOCKOUT_SECONDS", DEFAULT_LOCKOUT_SECONDS),
        )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Переменная {name} должна быть целым числом, получено {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Переменная {name} должна быть числом, получено {raw!r}") from exc


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Переменная {name} должна быть 1/0 или true/false, получено {raw!r}")
