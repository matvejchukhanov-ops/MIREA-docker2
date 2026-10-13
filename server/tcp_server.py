"""Слушающий сокет, цикл приёма подключений и запуск потоков.

Каждому клиенту выделяется отдельный поток.

Зачем это нужно, видно из устройства сокетов: и accept(), и recv() —
блокирующие вызовы. Пока сервер в одном потоке разговаривает с клиентом,
он не возвращается к accept(), и следующий клиент ждёт в очереди
неопределённо долго.

Главный поток теперь занимается только приёмом подключений, а обмен идёт
в отдельных потоках.
"""
from __future__ import annotations

import logging
import socket
import threading

from .app import Services
from .config import ServerConfig
from .connection import handle_connection, refuse_connection
from .handlers import build_dispatcher

logger = logging.getLogger("server.tcp")

# Как часто accept() «просыпается», чтобы Python успел обработать сигналы.
# Без этого блокирующий accept() на Windows не прерывается по Ctrl+C, пока
# не подключится очередной клиент.
ACCEPT_POLL_INTERVAL = 0.5

# Сколько ждать завершения рабочих потоков при остановке.
SHUTDOWN_JOIN_TIMEOUT = 2.0


class TCPServer:
    """TCP-сервер: принимает подключения и обслуживает их в потоках."""

    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._socket: socket.socket | None = None
        self._running = False
        # База, хранилище и реестр команд создаются один раз при запуске и
        # делятся между потоками: см. server/app.py о том, почему это безопасно.
        self._services = Services.build(config)
        self._dispatcher = build_dispatcher(self._services)

        # Единственное, что потоки разделяют: счётчик активных соединений.
        # Операция «прочитать, прибавить, записать» не атомарна, поэтому
        # без блокировки два потока могут затереть изменения друг друга.
        self._lock = threading.Lock()
        self._active = 0
        self._threads: set[threading.Thread] = set()

    @property
    def active_connections(self) -> int:
        with self._lock:
            return self._active

    def serve_forever(self) -> None:
        """Принимать подключения, пока не остановят."""
        self._socket = self._create_listening_socket()
        self._running = True

        host, port = self._config.address
        logger.info("Сервер слушает %s:%d", host, port)
        logger.info("Хранилище: %s", self._services.files.root)
        logger.info("База данных: %s", self._services.db.path.resolve())
        logger.info("Доступные команды: %s", ", ".join(self._dispatcher.commands))
        logger.info("Предел одновременных клиентов: %d", self._config.max_connections)
        logger.info("Остановка — Ctrl+C")

        try:
            while self._running:
                try:
                    client_socket, addr = self._socket.accept()
                except TimeoutError:
                    # Ожидаемое срабатывание таймаута опроса, не ошибка.
                    self._forget_finished_threads()
                    continue
                except OSError:
                    if not self._running:
                        break
                    raise

                self._start_worker(client_socket, addr)

        except KeyboardInterrupt:
            logger.info("Получен сигнал прерывания")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Прекратить приём подключений и освободить порт."""
        self._running = False
        if self._socket is not None:
            self._socket.close()
            self._socket = None

        # Потоки фоновые и умрут вместе с процессом, но дать им немного
        # времени на корректное завершение дешевле, чем обрывать обмен
        # посреди отправки ответа.
        for thread in list(self._threads):
            thread.join(timeout=SHUTDOWN_JOIN_TIMEOUT)
        self._threads.clear()

        logger.info("Сервер остановлен")

    # --- Внутреннее ---

    def _start_worker(self, client_socket: socket.socket, addr: tuple[str, int]) -> None:
        """Выделить клиенту поток или вежливо отказать."""
        peer = f"{addr[0]}:{addr[1]}"

        with self._lock:
            if self._active >= self._config.max_connections:
                # Отказ обрабатывается прямо здесь, в главном потоке: он
                # укладывается в одну отправку и не стоит нового потока.
                logger.warning(
                    "Достигнут предел подключений (%d), отказываю %s",
                    self._config.max_connections,
                    peer,
                )
                refuse_connection(client_socket, peer, self._config.max_connections)
                return
            self._active += 1

        thread = threading.Thread(
            target=self._serve_client,
            args=(client_socket, addr, peer),
            name=f"client-{peer}",
            # Фоновый: аварийная остановка сервера не должна подвисать
            # в ожидании клиентов, которые могут молчать сколько угодно.
            daemon=True,
        )
        self._threads.add(thread)
        thread.start()

    def _serve_client(
        self, client_socket: socket.socket, addr: tuple[str, int], peer: str
    ) -> None:
        """Тело рабочего потока."""
        try:
            # Контекстный менеджер закрывает сокет в любом случае,
            # включая исключение внутри обработчика.
            with client_socket:
                handle_connection(client_socket, addr, self._config, self._dispatcher)
        except Exception:
            # Сбой в одном потоке завершает одно соединение. Сервер и
            # остальные клиенты продолжают работать.
            logger.exception("Поток обслуживания %s завершился с ошибкой", peer)
        finally:
            with self._lock:
                self._active -= 1

    def _forget_finished_threads(self) -> None:
        """Убрать из набора уже завершившиеся потоки."""
        self._threads = {thread for thread in self._threads if thread.is_alive()}

    def _create_listening_socket(self) -> socket.socket:
        """Создать, настроить и перевести в режим прослушивания."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _configure_address_reuse(sock)
            sock.settimeout(ACCEPT_POLL_INTERVAL)
            sock.bind(self._config.address)
            sock.listen(self._config.backlog)
        except OSError:
            sock.close()
            raise
        return sock


def _configure_address_reuse(sock: socket.socket) -> None:
    """Разрешить повторную привязку к порту после перезапуска.

    После остановки сервера соединения остаются в состоянии TIME_WAIT, и
    повторный запуск может упасть с «адрес уже используется». На отладке
    перезапуск происходит десятки раз в час, поэтому вопрос не теоретический.

    Правильный флаг зависит от системы:

    * Windows — SO_EXCLUSIVEADDRUSE. Здесь SO_REUSEADDR означает совсем другое:
      он позволяет чужому процессу привязаться к уже занятому порту и
      перехватывать подключения. Использовать его на Windows опасно.
    * POSIX — SO_REUSEADDR. Разрешает занять порт, висящий в TIME_WAIT, но
      не отнимает порт у работающего процесса.
    """
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
