"""Точка входа веб-интерфейса.

Запуск рядом с сервером:
    python -m server.main
    python -m web.main
и открыть http://127.0.0.1:8080 в браузере.

Параметры — аргументы командной строки или переменные окружения
FM_WEB_HOST, FM_WEB_PORT, FM_SERVER_HOST, FM_SERVER_PORT, FM_WEB_ALLOWED_HOSTS.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .gateway import GatewayServer, WebConfig

logger = logging.getLogger("web.main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="web", description="Веб-интерфейс файлового менеджера")
    parser.add_argument("--host", default=os.environ.get("FM_WEB_HOST", "127.0.0.1"),
                        help="Адрес шлюза (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FM_WEB_PORT", "8080")),
                        help="Порт шлюза")
    parser.add_argument("--server-host", default=os.environ.get("FM_SERVER_HOST", "127.0.0.1"),
                        help="Адрес TCP-сервера файлового менеджера")
    parser.add_argument("--server-port", type=int,
                        default=int(os.environ.get("FM_SERVER_PORT", "9000")),
                        help="Порт TCP-сервера")
    parser.add_argument("--allowed-host", action="append",
                        default=[h for h in os.environ.get("FM_WEB_ALLOWED_HOSTS", "").split(",") if h],
                        help="Дополнительное разрешённое значение Host, например myhost:8080")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    config = WebConfig(
        host=args.host,
        port=args.port,
        server_host=args.server_host,
        server_port=args.server_port,
        extra_hosts=frozenset(args.allowed_host),
    )
    try:
        server = GatewayServer(config)
    except OSError as exc:
        logger.error("Не удалось занять %s:%d: %s", config.host, config.port, exc)
        return 1

    logger.info("Веб-интерфейс: http://127.0.0.1:%d (слушает %s)", config.port, config.host)
    logger.info("TCP-сервер: %s:%d", config.server_host, config.server_port)
    logger.info("Разрешённые Host: %s", ", ".join(sorted(config.allowed_hosts)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Получен сигнал прерывания")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
