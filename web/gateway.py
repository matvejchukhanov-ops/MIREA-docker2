"""HTTP-шлюз между браузером и TCP-сервером файлового менеджера.

Браузер не умеет открыть TCP-соединение и говорить по нашему протоколу,
поэтому между ними стоит шлюз:

    браузер --HTTP--> шлюз --TCP, протокол из docs--> сервер

Каждой вкладке браузера соответствует своя сессия шлюза со своим
TCP-соединением. Вход, права и все проверки остаются на сервере: шлюз ничего
не решает за него, он только переводит HTTP в кадры протокола и обратно.

Шлюз — новая точка входа, и у веба свои атаки. Что закрыто здесь:

* DNS rebinding — чужой сайт подменяет адрес своего домена на 127.0.0.1 и
  читает ответы шлюза. Защита: заголовок Host должен быть из списка
  разрешённых, иначе 421.
* CSRF — чужая страница отправляет запрос от имени вошедшего пользователя.
  Защита: cookie SameSite=Strict, обязательный заголовок X-FM-Request и тип
  application/json (с чужого сайта такой запрос требует предварительного
  CORS-запроса, на который шлюз не отвечает), проверка Origin.
* XSS — данные пользователя исполняются как код. Защита: страница выводит всё
  через textContent, а CSP запрещает встроенные скрипты и чужие источники.
* Кража сессии — cookie HttpOnly (недоступна скриптам), случайный токен на
  256 бит, новый токен после входа (защита от фиксации сессии), сессия
  забывается после 15 минут бездействия.
* Лишние команды — пересылаются только команды из белого списка; quit
  недоступна, соединением управляет шлюз.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from client.connection import ServerClosedConnection, ServerConnection
from common import strict_json
from common.framing import FramingError

logger = logging.getLogger("web.gateway")

STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
}

# Файл до 256 КБ в JSON может вырасти в шесть раз (\u0001 вместо байта):
# предел тела запроса с запасом на остальные поля.
MAX_BODY = 2 * 1024 * 1024

SESSION_COOKIE = "fm_session"
REQUEST_HEADER = "X-FM-Request"
DEFAULT_IDLE_TIMEOUT = 15 * 60

ALLOWED_COMMANDS = frozenset(
    {
        "ping", "register", "login", "logout", "whoami", "drives",
        "ls", "info", "read", "write", "delete", "mkdir", "rmdir",
        "json_read", "json_write", "xml_read", "xml_write",
        "zip", "zip_list", "unzip", "log", "ops",
    }
)

# Встроенные скрипты и стили запрещены, загружать можно только своё.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


@dataclass(frozen=True, slots=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    server_host: str = "127.0.0.1"
    server_port: int = 9000
    extra_hosts: frozenset[str] = frozenset()
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT

    @property
    def allowed_hosts(self) -> frozenset[str]:
        own = {f"localhost:{self.port}", f"127.0.0.1:{self.port}"}
        return frozenset(own | {host.lower() for host in self.extra_hosts})


class GatewayError(Exception):
    """Отказ шлюза: запрос не дошёл до сервера."""

    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class BrowserSession:
    """Сессия одной вкладки браузера и её TCP-соединение с сервером."""

    token: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    connection: ServerConnection | None = None
    last_used: float = field(default_factory=time.monotonic)

    def call(self, request: dict[str, Any], config: WebConfig) -> dict[str, Any]:
        """Переслать запрос серверу и вернуть разобранный ответ.

        Запросы одной сессии идут строго по очереди: протокол отвечает в
        порядке запросов, и два потока на одном сокете перепутали бы ответы.
        """
        with self.lock:
            self.last_used = time.monotonic()
            try:
                return self._exchange(request, config)
            except ServerClosedConnection:
                # Сервер закрывает соединение только после ответа на прошлый
                # запрос (предел неудачных входов, простой) — значит, этот
                # запрос он не обработал, и повторить его безопасно.
                self.close()
                try:
                    return self._exchange(request, config)
                except ServerClosedConnection as exc:
                    self.close()
                    raise _unavailable() from exc

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _exchange(self, request: dict[str, Any], config: WebConfig) -> dict[str, Any]:
        if self.connection is None:
            try:
                self.connection = ServerConnection.connect(config.server_host, config.server_port)
            except OSError as exc:
                raise _unavailable() from exc
        try:
            self.connection.send(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            reply = self.connection.receive()
        except ServerClosedConnection:
            raise
        except (OSError, FramingError) as exc:
            self.close()
            raise _unavailable() from exc
        try:
            data = json.loads(reply.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.close()
            raise GatewayError(HTTPStatus.BAD_GATEWAY, "BAD_REPLY", "Сервер прислал не JSON") from exc
        if not isinstance(data, dict):
            raise GatewayError(HTTPStatus.BAD_GATEWAY, "BAD_REPLY", "Сервер прислал не объект")
        return data


def _unavailable() -> GatewayError:
    return GatewayError(
        HTTPStatus.BAD_GATEWAY, "SERVER_UNAVAILABLE", "Сервер файлового менеджера недоступен"
    )


class SessionStore:
    """Сессии браузеров по токену из cookie."""

    def __init__(self, idle_timeout: float) -> None:
        self._idle_timeout = idle_timeout
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = threading.Lock()

    def get(self, token: str | None) -> BrowserSession | None:
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is not None and self._expired(session):
                del self._sessions[token]
                session.close()
                return None
            return session

    def create(self) -> BrowserSession:
        session = BrowserSession(token=secrets.token_urlsafe(32))
        with self._lock:
            self._sessions[session.token] = session
        return session

    def rotate(self, session: BrowserSession) -> None:
        """Выдать сессии новый токен: старый мог быть известен до входа."""
        with self._lock:
            self._sessions.pop(session.token, None)
            session.token = secrets.token_urlsafe(32)
            self._sessions[session.token] = session

    def sweep(self) -> None:
        with self._lock:
            expired = [token for token, s in self._sessions.items() if self._expired(s)]
            for token in expired:
                self._sessions.pop(token).close()

    def close_all(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                session.close()
            self._sessions.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _expired(self, session: BrowserSession) -> bool:
        # Сессию, занятую запросом, не трогаем: её время обновится по его окончании.
        if session.lock.locked():
            return False
        return time.monotonic() - session.last_used > self._idle_timeout


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: WebConfig) -> None:
        self.config = config
        self.sessions = SessionStore(config.idle_timeout)
        super().__init__((config.host, config.port), GatewayHandler)

    def server_close(self) -> None:
        super().server_close()
        self.sessions.close_all()


class GatewayHandler(BaseHTTPRequestHandler):
    server: GatewayServer
    server_version = "filemanager-web"
    sys_version = ""

    # --- Методы HTTP ---

    def do_GET(self) -> None:  # noqa: N802 — имя задано http.server
        if not self._host_allowed():
            return
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._healthz()
            return
        static = STATIC_FILES.get(path)
        if static is None:
            self._send_json(HTTPStatus.NOT_FOUND, _error("NOT_FOUND", "Нет такой страницы"))
            return
        name, content_type = static
        body = (STATIC_DIR / name).read_bytes()
        headers = {"Cache-Control": "no-cache"}
        if path == "/":
            session = self.server.sessions.get(self._cookie_token())
            if session is None:
                session = self.server.sessions.create()
                headers["Set-Cookie"] = _cookie(session.token)
        self._send(HTTPStatus.OK, body, content_type, headers)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_allowed():
            return
        if self.path != "/api/call":
            self._send_json(HTTPStatus.NOT_FOUND, _error("NOT_FOUND", "Нет такого адреса"))
            return
        self.server.sessions.sweep()
        try:
            request = self._read_api_request()
            self._api_call(request)
        except GatewayError as exc:
            self._send_json(exc.status, _error(exc.code, exc.message))

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    do_DELETE = do_PATCH = do_PUT  # noqa: N815

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.info("%s %s", self.address_string(), format % args)

    # --- API ---

    def _read_api_request(self) -> dict[str, Any]:
        # Тело читается до проверок: ответ отказом при недочитанном теле
        # закрывает сокет с RST, и клиент вместо 403 видит обрыв связи.
        body = self._read_body()
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {
            f"http://{host}" for host in self.server.config.allowed_hosts
        }:
            raise GatewayError(HTTPStatus.FORBIDDEN, "FORBIDDEN", "Запрос с чужого сайта")
        if self.headers.get(REQUEST_HEADER) != "1":
            raise GatewayError(
                HTTPStatus.FORBIDDEN, "FORBIDDEN", f"Нет заголовка {REQUEST_HEADER}"
            )
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise GatewayError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "BAD_REQUEST", "Ожидался application/json"
            )

        # Тот же строгий разбор, что и на сервере: дубликат ключа в теле
        # отвергается здесь, а не теряется при пересборке запроса.
        try:
            request = strict_json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise GatewayError(HTTPStatus.BAD_REQUEST, "BAD_JSON", _json_problem(exc)) from exc
        if not isinstance(request, dict) or not isinstance(request.get("cmd"), str):
            raise GatewayError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Нужен объект с полем cmd")
        if request["cmd"] not in ALLOWED_COMMANDS:
            raise GatewayError(
                HTTPStatus.BAD_REQUEST, "UNKNOWN_COMMAND", f"Команда {request['cmd']!r} недоступна"
            )
        return _expand_json_text(request)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            self.close_connection = True
            raise GatewayError(HTTPStatus.LENGTH_REQUIRED, "BAD_REQUEST", "Нет Content-Length") from exc
        if length < 0 or length > MAX_BODY:
            # Небольшой перебор дочитываем, чтобы клиент получил ответ, а не
            # обрыв. Огромное тело не читаем вовсе: это и есть защита.
            if 0 < length <= 2 * MAX_BODY:
                self.rfile.read(length)
            self.close_connection = True
            raise GatewayError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "FRAME_TOO_LONG", "Запрос слишком большой"
            )
        return self.rfile.read(length)

    def _api_call(self, request: dict[str, Any]) -> None:
        sessions = self.server.sessions
        headers: dict[str, str] = {}
        session = sessions.get(self._cookie_token())
        if session is None:
            session = sessions.create()
            headers["Set-Cookie"] = _cookie(session.token)

        reply = session.call(request, self.server.config)

        if request["cmd"] in {"login", "register"} and reply.get("status") == "ok":
            sessions.rotate(session)
            headers["Set-Cookie"] = _cookie(session.token)
        self._send_json(HTTPStatus.OK, reply, headers)

    def _healthz(self) -> None:
        """Шлюз жив и достаёт до сервера: короткое соединение с ping."""
        config = self.server.config
        try:
            with ServerConnection.connect(config.server_host, config.server_port) as connection:
                connection.send(b'{"cmd":"ping"}')
                ok = json.loads(connection.receive()).get("result") == "pong"
        except (OSError, ServerClosedConnection, FramingError, ValueError):
            ok = False
        status = HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE
        self._send_json(status, {"status": "ok" if ok else "error", "server": ok})

    # --- Проверки и ответы ---

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if host in self.server.config.allowed_hosts:
            return True
        self._send_json(HTTPStatus.MISDIRECTED_REQUEST, _error("FORBIDDEN", "Неизвестный Host"))
        return False

    def _cookie_token(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return None

    def _method_not_allowed(self) -> None:
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED, _error("BAD_REQUEST", "Метод не поддерживается"),
            {"Allow": "GET, POST"},
        )

    def _send_json(
        self, status: HTTPStatus, payload: Any, headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(
            status, body, "application/json; charset=utf-8",
            {"Cache-Control": "no-store", **(headers or {})},
        )

    def _send(
        self, status: HTTPStatus, body: bytes, content_type: str, headers: dict[str, str]
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in {**SECURITY_HEADERS, **headers}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _expand_json_text(request: dict[str, Any]) -> dict[str, Any]:
    """json_write с полем data_text: текст разбирается строго здесь.

    Браузерный JSON.parse молча схлопывает повторяющиеся ключи — ровно та
    ошибка, что была в консольном клиенте. Поэтому страница отправляет
    введённый текст как есть, а разбирает его шлюз строгим разборщиком.
    """
    if request["cmd"] != "json_write" or "data_text" not in request:
        return request
    text = request.pop("data_text")
    if not isinstance(text, str):
        raise GatewayError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "data_text должен быть строкой")
    try:
        request["data"] = strict_json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise GatewayError(HTTPStatus.BAD_REQUEST, "BAD_FORMAT", _json_problem(exc)) from exc
    return request


def _json_problem(exc: Exception) -> str:
    if isinstance(exc, strict_json.JSONRuleError):
        return str(exc)
    if isinstance(exc, json.JSONDecodeError):
        return f"Некорректный JSON: {exc.msg} (строка {exc.lineno}, столбец {exc.colno})"
    if isinstance(exc, RecursionError):
        return "Слишком глубокая вложенность JSON"
    return "Тело запроса не в UTF-8"


def _error(code: str, message: str) -> dict[str, str]:
    return {"status": "error", "code": code, "message": message}


def _cookie(token: str) -> str:
    # Secure не ставится: шлюз работает по HTTP на 127.0.0.1, и браузер с
    # флагом Secure не вернул бы cookie. Наружу шлюз не публикуется.
    return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"
