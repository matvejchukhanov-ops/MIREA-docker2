"""Обслуживание одного клиентского соединения.

Цепочка на один кадр:
    байты -> кадр -> Request -> обработчик -> результат -> кадр ответа

Модуль вызывается из рабочего потока: на каждого клиента свой. Ничего общего
с другими соединениями здесь нет — ни буфера, ни истории, — поэтому и
синхронизировать нечего.
"""
from __future__ import annotations

import logging
import re
import socket

from common.framing import FrameReader, FrameTooLongError

from .config import ServerConfig
from .dispatcher import Dispatcher
from .protocol import ErrorCode, ProtocolError, error_frame, parse_request, success_frame
from .session import Session

logger = logging.getLogger("server.connection")

Address = tuple[str, int]

# Пределы «вежливого» закрытия после ошибки протокола.
DRAIN_TIMEOUT = 1.0
DRAIN_LIMIT = 64 * 1024

# Сколько байт кадра показывать в отладочном логе. Кадр с файлом весит
# мегабайты, и целиком в логе он только мешает.
DEBUG_PREVIEW = 300

# Значение поля password в сыром кадре. Отладочный лог пишет кадры как есть,
# и без маскировки пароль каждого входа оседал бы в файле лога открытым
# текстом — ровно то, от чего защищает хеширование в базе.
_PASSWORD_FIELD = re.compile(rb'("password"\s*:\s*)"(?:[^"\\]|\\.)*"')


def handle_connection(
    sock: socket.socket,
    addr: Address,
    config: ServerConfig,
    dispatcher: Dispatcher,
) -> None:
    """Обслужить одного клиента до отключения.

    Вызывающая сторона отвечает за закрытие сокета — здесь только обмен.
    """
    peer = f"{addr[0]}:{addr[1]}"
    logger.info("Клиент подключился: %s", peer)

    # Молчащий клиент не должен занимать соединение бесконечно.
    sock.settimeout(config.idle_timeout)

    session = Session(peer=peer)
    reader = FrameReader(config.max_frame_size)
    frames_total = 0

    try:
        while not session.should_close:
            try:
                chunk = sock.recv(config.recv_size)
            except TimeoutError:
                logger.warning(
                    "Клиент %s молчал %.0f с, закрываю соединение", peer, config.idle_timeout
                )
                break

            # Ноль байт — единственный корректный признак того, что
            # противоположная сторона закрыла соединение. Не исключение,
            # не таймаут, а именно пустой результат чтения.
            if not chunk:
                logger.info("Клиент %s закрыл соединение", peer)
                break

            try:
                frames = reader.feed(chunk)
            except FrameTooLongError as exc:
                # Продолжать нельзя: позиция в потоке потеряна, неизвестно,
                # где заканчивается текущее сообщение и начинается следующее.
                logger.warning(
                    "Клиент %s превысил предельный размер кадра (%d байт), закрываю соединение",
                    peer,
                    exc.limit,
                )
                _try_send(
                    sock,
                    error_frame(ErrorCode.FRAME_TOO_LONG, f"Кадр длиннее {exc.limit} байт"),
                    peer,
                )
                _close_gracefully(sock, peer)
                break

            # Одно чтение из сокета не равно одному сообщению ни в какую
            # сторону: кадров может прийти несколько, а может не прийти ни одного.
            for frame in frames:
                frames_total += 1
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Запрос от %s: %r", peer, _preview(_redact(frame)))
                response = _process_frame(frame, session, dispatcher)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Ответ для %s: %r", peer, _preview(response))
                # sendall, а не send: send отправляет столько, сколько получилось,
                # и остаток пришлось бы досылать вручную.
                sock.sendall(response)
                if session.should_close:
                    logger.info("Клиент %s завершил сеанс командой quit", peer)
                    break

    except ConnectionError as exc:
        # Клиент закрыл окно или оборвал связь на полуслове — штатная ситуация,
        # а не сбой сервера. Ловим родителя всего семейства: разные системы
        # сообщают об обрыве по-разному (ConnectionResetError на Linux,
        # ConnectionAbortedError на Windows), и перечислять их поимённо —
        # верный способ пропустить один и залить лог ложными ошибками.
        logger.info("Соединение с %s разорвано клиентом (%s)", peer, type(exc).__name__)
    except OSError as exc:
        logger.error("Ошибка сокета при работе с %s: %s", peer, exc)
    finally:
        logger.info("Клиент отключился: %s (обработано запросов: %d)", peer, frames_total)


def refuse_connection(sock: socket.socket, peer: str, limit: int) -> None:
    """Отказать клиенту, когда предел одновременных подключений исчерпан.

    Молчаливый разрыв выглядел бы для клиента как поломка сети. Внятный код
    ошибки позволяет ему понять, что сервер жив, просто занят, и повторить
    попытку позже.
    """
    with sock:
        _try_send(
            sock,
            error_frame(
                ErrorCode.SERVER_BUSY,
                f"Сервер обслуживает предельное число клиентов ({limit}), попробуйте позже",
            ),
            peer,
        )
        _close_gracefully(sock, peer)


def _process_frame(frame: bytes, session: Session, dispatcher: Dispatcher) -> bytes:
    """Превратить кадр запроса в кадр ответа.

    Функция никогда не выбрасывает исключений: любая беда должна стать
    ответом с кодом ошибки, иначе одна кривая строка от клиента обрывает
    соединение целиком.
    """
    try:
        request = parse_request(frame)
    except ProtocolError as exc:
        # Разобрать не удалось, поэтому id запроса неизвестен.
        return error_frame(exc.code, exc.message, position=exc.position)

    try:
        result = dispatcher.dispatch(request, session)
    except ProtocolError as exc:
        return error_frame(exc.code, exc.message, request_id=request.id, position=exc.position)
    except Exception:
        # Ошибка в обработчике — вина сервера, а не клиента. Клиенту уходит
        # нейтральное сообщение, подробности с трассировкой остаются в логе:
        # внутреннее устройство сервера наружу отдавать не следует.
        logger.exception("Непредвиденная ошибка при обработке команды %r", request.cmd)
        return error_frame(
            ErrorCode.INTERNAL_ERROR, "Внутренняя ошибка сервера", request_id=request.id
        )

    return success_frame(result, request_id=request.id)


def _redact(frame: bytes) -> bytes:
    """Заменить значение пароля звёздочками."""
    return _PASSWORD_FIELD.sub(rb'\1"***"', frame)


def _preview(data: bytes) -> bytes:
    if len(data) <= DEBUG_PREVIEW:
        return data
    return data[:DEBUG_PREVIEW] + b"... (%d bytes)" % len(data)


def _try_send(sock: socket.socket, data: bytes, peer: str) -> None:
    """Отправить, не поднимая шума, если клиент уже ушёл.

    Сообщение об ошибке — вежливость, а не обязанность. Если отправить его
    не удалось, настоящая причина закрытия соединения не должна потеряться
    за исключением из этой попытки.
    """
    try:
        sock.sendall(data)
    except OSError as exc:
        logger.debug("Не удалось отправить уведомление клиенту %s: %s", peer, exc)


def _close_gracefully(sock: socket.socket, peer: str) -> None:
    """Закрыть соединение так, чтобы клиент успел прочитать уведомление.

    Если просто закрыть сокет, когда во входящем буфере остались непрочитанные
    данные, система отправляет RST — аварийный сброс. При нём уже отправленное
    уведомление может быть отброшено, и клиент вместо внятного «кадр слишком
    длинный» увидит «соединение разорвано». Проверено: именно так и вело себя
    закрытие после превышения лимита.

    Правильная последовательность: сообщить, что мы больше ничего не пришлём,
    затем вычитать и выбросить остаток, пока клиент не закроет свою сторону.

    Пределы по объёму и времени обязательны: без них клиент, продолжающий
    слать данные, удержит поток навсегда — то есть защита от переполнения
    буфера обернулась бы новым способом занять сервер.
    """
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        # Клиент уже ушёл — дочитывать нечего и незачем.
        return

    sock.settimeout(DRAIN_TIMEOUT)
    discarded = 0
    try:
        while discarded < DRAIN_LIMIT:
            chunk = sock.recv(4096)
            if not chunk:
                break
            discarded += len(chunk)
    except OSError:
        pass

    if discarded:
        logger.debug("От %s отброшено %d байт после ошибки протокола", peer, discarded)
