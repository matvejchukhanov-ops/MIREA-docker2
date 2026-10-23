"""Тесты веб-шлюза: настоящий TCP-сервер, настоящий HTTP-шлюз, запросы через http.client.

Проверяется и работа (вход, файлы, JSON), и то, что шлюз не добавляет новых
дыр: чужой Host, запросы с чужого сайта, лишние команды, дубликаты ключей.
"""
from __future__ import annotations

import http.client
import json
import threading
from typing import Any

from web.gateway import MAX_BODY, SESSION_COOKIE, GatewayServer, WebConfig

from .support import PASSWORD
from .test_integration import ServerTestCase, free_port


class Browser:
    """Минимальный браузер: помнит cookie сессии и шлёт заголовки страницы."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.host = f"127.0.0.1:{port}"
        self.cookie: str | None = None

    def request(
        self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        all_headers = {"Host": self.host}
        if self.cookie:
            all_headers["Cookie"] = f"{SESSION_COOKIE}={self.cookie}"
        all_headers.update(headers or {})
        try:
            connection.request(method, path, body=body, headers=all_headers)
            response = connection.getresponse()
            data = response.read()
            response_headers = {name.lower(): value for name, value in response.getheaders()}
        finally:
            connection.close()
        set_cookie = response_headers.get("set-cookie")
        if set_cookie and set_cookie.startswith(f"{SESSION_COOKIE}="):
            self.cookie = set_cookie.split(";", 1)[0].split("=", 1)[1]
        return response.status, response_headers, data

    def call_raw(self, body: bytes, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        base = {"Content-Type": "application/json", "X-FM-Request": "1", "Origin": f"http://{self.host}"}
        status, _, data = self.request("POST", "/api/call", body, {**base, **(headers or {})})
        return status, json.loads(data)

    def call(self, cmd: str, **args: Any) -> dict[str, Any]:
        _, reply = self.call_raw(json.dumps({"cmd": cmd, **args}).encode("utf-8"))
        return reply

    def ok(self, cmd: str, **args: Any) -> Any:
        reply = self.call(cmd, **args)
        assert reply.get("status") == "ok", reply
        return reply["result"]


class GatewayTestCase(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.web_port = free_port()
        self.gateway = GatewayServer(
            WebConfig(host="127.0.0.1", port=self.web_port, server_host="127.0.0.1", server_port=self.port)
        )
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()
        self.addCleanup(self._stop_gateway)

    def _stop_gateway(self) -> None:
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_thread.join(timeout=5)

    def browser(self) -> Browser:
        return Browser(self.web_port)


class PageTests(GatewayTestCase):
    def test_страница_с_csp_и_cookie_сессии(self) -> None:
        browser = self.browser()
        status, headers, body = browser.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/static/app.js", body)
        self.assertIn("script-src 'self'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")
        cookie = headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_статика_и_неизвестный_адрес(self) -> None:
        browser = self.browser()
        self.assertEqual(browser.request("GET", "/static/app.js")[0], 200)
        self.assertEqual(browser.request("GET", "/static/../gateway.py")[0], 404)
        self.assertEqual(browser.request("GET", "/etc/passwd")[0], 404)

    def test_healthz_проверяет_сервер(self) -> None:
        status, _, body = self.browser().request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["server"])


class ProtectionTests(GatewayTestCase):
    def test_чужой_host_отвергается(self) -> None:
        # DNS rebinding: домен атакующего указывает на 127.0.0.1, но Host — его.
        browser = self.browser()
        status, _, _ = browser.request("GET", "/", headers={"Host": "evil.example:8080"})
        self.assertEqual(status, 421)

    def test_без_заголовка_страницы_отказ(self) -> None:
        # Обычная форма с чужого сайта не может добавить свой заголовок.
        status, reply = self.browser().call_raw(b'{"cmd":"ping"}', {"X-FM-Request": ""})
        self.assertEqual((status, reply["code"]), (403, "FORBIDDEN"))

    def test_чужой_origin_отказ(self) -> None:
        status, reply = self.browser().call_raw(b'{"cmd":"ping"}', {"Origin": "http://evil.example"})
        self.assertEqual((status, reply["code"]), (403, "FORBIDDEN"))

    def test_не_json_тип_отказ(self) -> None:
        # text/plain форма отправляет без предварительного CORS-запроса.
        status, reply = self.browser().call_raw(b'{"cmd":"ping"}', {"Content-Type": "text/plain"})
        self.assertEqual((status, reply["code"]), (415, "BAD_REQUEST"))

    def test_quit_и_неизвестные_команды_недоступны(self) -> None:
        browser = self.browser()
        for cmd in ("quit", "shutdown", "drop_table"):
            with self.subTest(cmd=cmd):
                status, reply = browser.call_raw(json.dumps({"cmd": cmd}).encode())
                self.assertEqual((status, reply["code"]), (400, "UNKNOWN_COMMAND"))

    def test_дубликат_ключа_в_теле(self) -> None:
        status, reply = self.browser().call_raw(b'{"cmd":"ping","cmd":"quit"}')
        self.assertEqual((status, reply["code"]), (400, "BAD_JSON"))

    def test_дубликат_ключа_в_тексте_json_write(self) -> None:
        # Браузерный JSON.parse схлопнул бы ключи, поэтому текст разбирает шлюз.
        browser = self.browser()
        browser.ok("register", username="alice", password=PASSWORD)
        status, reply = browser.call_raw(json.dumps({
            "cmd": "json_write", "path": "role.json",
            "data_text": '{"role": "user", "role": "admin"}',
        }).encode())
        self.assertEqual((status, reply["code"]), (400, "BAD_FORMAT"))
        self.assertEqual(browser.call("info", path="role.json")["code"], "NOT_FOUND")

    def test_слишком_большое_тело(self) -> None:
        status, reply = self.browser().call_raw(b" " * (MAX_BODY + 1))
        self.assertEqual((status, reply["code"]), (413, "FRAME_TOO_LONG"))

    def test_сессии_не_смешиваются(self) -> None:
        alice = self.browser()
        alice.ok("register", username="alice", password=PASSWORD)
        stranger = self.browser()
        self.assertEqual(stranger.call("whoami")["code"], "AUTH_REQUIRED")
        self.assertEqual(stranger.call("ls")["code"], "AUTH_REQUIRED")

    def test_токен_меняется_после_входа(self) -> None:
        # Токен, выданный до входа, мог быть подсмотрен или навязан.
        browser = self.browser()
        browser.request("GET", "/")
        before = browser.cookie
        browser.ok("register", username="alice", password=PASSWORD)
        self.assertNotEqual(browser.cookie, before)
        stale = self.browser()
        stale.cookie = before
        self.assertEqual(stale.call("whoami")["code"], "AUTH_REQUIRED")


class ScenarioTests(GatewayTestCase):
    def test_работа_с_файлами_через_шлюз(self) -> None:
        browser = self.browser()
        browser.request("GET", "/")
        self.assertEqual(browser.ok("register", username="alice", password=PASSWORD)["username"], "alice")
        self.assertEqual(browser.ok("whoami")["username"], "alice")
        browser.ok("mkdir", path="docs")
        browser.ok("write", path="docs/заметка.txt", content="привет\n")
        self.assertEqual(browser.ok("read", path="docs/заметка.txt")["content"], "привет\n")
        browser.ok("json_write", path="data.json", data_text='{"k": [1, 2]}')
        self.assertEqual(browser.ok("json_read", path="data.json")["data"], {"k": [1, 2]})
        names = [entry["name"] for entry in browser.ok("ls")["entries"]]
        self.assertEqual(sorted(names), ["data.json", "docs"])
        # Песочница сервера работает и через шлюз.
        self.assertEqual(browser.call("read", path="../../etc/passwd")["code"], "BAD_PATH")
        browser.ok("logout")
        self.assertEqual(browser.call("ls")["code"], "AUTH_REQUIRED")
