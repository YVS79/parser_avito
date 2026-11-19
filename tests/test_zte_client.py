"""Тесты клиента CGI-API ZTE (без реального модема)."""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from parser.modem.zte import ZTEClient, normalize_ip


class _ZTEState:
    def __init__(self):
        self.connected = True
        self.ip = "100.64.0.10"
        self.bearer = "NETWORK_auto"
        self.require_auth = False
        self.logged_in = False
        self.commands: list[str] = []
        self.login_attempts = 0
        self.ip_counter = 10

    def next_ip(self) -> str:
        self.ip_counter += 1
        return f"100.64.0.{self.ip_counter}"


def _make_handler(state: _ZTEState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: D401 - отключаем шум в тестах
            return

        def _send(self, payload, code=200, headers=None):
            body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not state.require_auth:
                return True
            return state.logged_in or "stok=test" in (self.headers.get("Cookie") or "")

        def _handle_get_cmd(self, query):
            if not self._authorized():
                self._send({"result": "failure"}, code=403)
                return
            data = {
                "wan_ipaddr": state.ip if state.connected else "",
                "network_provider": "TestOperator",
                "network_type": "LTE",
                "wan_csq": "20",
                "signal_icon": "4",
                "lte_rsrp": "-95",
                "imei": "123456789012345",
                "module_name": "ZTE MF823D",
                "soft_version": "MF823D_V1.0",
            }
            self._send({key: data.get(key, "") for key in query.get("cmd", [""])[0].split(",")})

        def _handle_set_cmd(self, params):
            command = (params.get("goformId") or [""])[0]
            state.commands.append(command)

            if command == "LOGIN":
                state.login_attempts += 1
                expected = base64.b64encode(b"admin").decode()
                if (params.get("password") or [""])[0] == expected:
                    state.logged_in = True
                    self._send({"result": "0"}, headers={"Set-Cookie": "stok=test; Path=/"})
                else:
                    self._send({"result": "1"})
                return

            if self.headers.get("Referer") is None:
                self._send({"result": "failure"})
                return

            if not self._authorized():
                self._send({"result": "failure"}, code=403)
                return

            if command == "DISCONNECT_NETWORK":
                state.connected = False
                self._send({"result": "success"})
            elif command == "CONNECT_NETWORK":
                state.connected = True
                state.ip = state.next_ip()
                self._send({"result": "success"})
            elif command == "SET_BEARER_PREFERENCE":
                state.bearer = (params.get("BearerPreference") or [""])[0]
                self._send({"result": "success"})
            else:
                self._send({"result": "failure"})

        def do_GET(self):
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if parts.path.endswith("goform_get_cmd_process"):
                self._handle_get_cmd(query)
            elif parts.path.endswith("goform_set_cmd_process"):
                self._handle_set_cmd(query)
            else:
                self._send({"result": "failure"}, code=404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            params = parse_qs(raw)
            parts = urlsplit(self.path)
            if parts.path.endswith("goform_set_cmd_process"):
                self._handle_set_cmd(params)
            else:
                self._send({"result": "failure"}, code=404)

    return Handler


@pytest.fixture()
def zte_server():
    state = _ZTEState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield state, f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_status_parses_zte_fields(zte_server):
    state, gateway = zte_server
    client = ZTEClient(gateway, timeout=1.0, poll_timeout=0.5)

    status = client.status()

    assert status.wan_ip == state.ip
    assert status.operator == "TestOperator"
    assert status.network_type == "LTE"
    assert status.model == "ZTE MF823D"
    assert status.imei == "123456789012345"
    assert status.connected is True


def test_set_bearer_and_reconnect_are_sent(zte_server):
    state, gateway = zte_server
    client = ZTEClient(gateway, timeout=1.0, poll_timeout=0.5)

    client.set_bearer("Only_WCDMA")
    assert state.bearer == "Only_WCDMA"

    old_ip = state.ip
    client.disconnect()
    assert state.connected is False
    assert client.wan_ip() is None

    client.connect()
    assert state.connected is True
    assert state.ip != old_ip
    assert client.wan_ip() == state.ip
    assert "SET_BEARER_PREFERENCE" in state.commands
    assert "DISCONNECT_NETWORK" in state.commands
    assert "CONNECT_NETWORK" in state.commands


def test_login_retry_when_auth_required(zte_server):
    state, gateway = zte_server
    state.require_auth = True
    client = ZTEClient(gateway, password="admin", timeout=1.0, poll_timeout=0.5)

    client.connect()

    assert state.login_attempts == 1
    assert state.logged_in is True
    assert "CONNECT_NETWORK" in state.commands


def test_auth_error_without_password(zte_server):
    state, gateway = zte_server
    state.require_auth = True
    client = ZTEClient(gateway, password="", timeout=1.0, poll_timeout=0.5)

    with pytest.raises(Exception) as error:
        client.connect()

    assert "пароль" in str(error.value).lower()


def test_wait_for_ip_returns_new_address(zte_server):
    state, gateway = zte_server
    client = ZTEClient(gateway, timeout=1.0, poll_timeout=0.5)
    old_ip = client.wan_ip()

    client.disconnect()
    client.connect()

    new_ip = client.wait_for_ip(previous=old_ip, timeout=3.0, interval=0.05)
    assert new_ip == state.ip
    assert new_ip != old_ip


def test_normalize_ip():
    assert normalize_ip("100.64.1.2") == "100.64.1.2"
    assert normalize_ip("0.0.0.0") is None
    assert normalize_ip("") is None
    assert normalize_ip("not-an-ip") is None
