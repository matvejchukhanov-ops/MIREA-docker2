"""Проверка развёрнутого веб-интерфейса: страница, связь с сервером, защита.

Выполняется внутри контейнера шлюза:

    docker exec -i filemanager-web python - < ci/web_smoke.py

Только стандартная библиотека.
"""
from __future__ import annotations

import http.client
import json
import sys
import time

HOST, PORT = "127.0.0.1", 8080
STARTUP_WAIT = 20.0


class SmokeError(Exception):
    pass


def request(method: str, path: str, headers: dict[str, str] | None = None, body: bytes | None = None):
    connection = http.client.HTTPConnection(HOST, PORT, timeout=10)
    try:
        connection.request(method, path, body=body, headers={"Host": f"{HOST}:{PORT}", **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)
    print(f"  ok  {message}")


def wait_until_healthy() -> None:
    deadline = time.monotonic() + STARTUP_WAIT
    while True:
        try:
            status, _, body = request("GET", "/healthz")
            if status == 200 and json.loads(body).get("server"):
                print("  ok  шлюз отвечает и достаёт до сервера")
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise SmokeError(f"шлюз не стал здоровым за {STARTUP_WAIT:.0f} с")
        time.sleep(0.5)


def main() -> None:
    wait_until_healthy()

    status, headers, body = request("GET", "/")
    check(status == 200 and b"/static/app.js" in body, "страница открывается")
    check("script-src 'self'" in headers.get("Content-Security-Policy", ""), "страница отдаётся с CSP")
    check("HttpOnly" in headers.get("Set-Cookie", ""), "cookie сессии HttpOnly")

    status, _, _ = request("GET", "/", {"Host": "evil.example"})
    check(status == 421, "чужой Host отвергается")

    api_headers = {"Content-Type": "application/json"}
    status, _, _ = request("POST", "/api/call", api_headers, b'{"cmd":"ping"}')
    check(status == 403, "запрос без заголовка страницы отвергается (CSRF)")

    status, _, body = request("POST", "/api/call", {**api_headers, "X-FM-Request": "1"}, b'{"cmd":"ping"}')
    check(status == 200 and json.loads(body).get("result") == "pong", "ping проходит до сервера через шлюз")

    print("Smoke-проверка веб-интерфейса пройдена")


if __name__ == "__main__":
    try:
        main()
    except SmokeError as exc:
        print(f"Smoke-проверка веб-интерфейса не пройдена: {exc}", file=sys.stderr)
        sys.exit(1)
