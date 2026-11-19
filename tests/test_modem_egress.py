"""Тесты локального egress-прокси модема."""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from parser.modem.egress import EgressProxy, RouteBinder


class _EchoServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(5)
        self.port = self.socket.getsockname()[1]
        self._stop = threading.Event()

    def run(self):
        self.socket.settimeout(0.5)
        while not self._stop.is_set():
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    @staticmethod
    def _serve(client):
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    return
                client.sendall(data)
        except OSError:
            return
        finally:
            client.close()

    def stop(self):
        self._stop.set()
        self.socket.close()
        self.join(timeout=2)


class _HttpHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        body = b"plain-http-ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _read_until(sock: socket.socket, marker: bytes, timeout: float = 5.0) -> bytes:
    sock.settimeout(timeout)
    buffer = b""
    while marker not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer += chunk
    return buffer


def test_route_binder_connects_with_source_ip():
    echo = _EchoServer()
    echo.start()
    try:
        binder = RouteBinder(local_ip="127.0.0.1")
        connection = binder.connect("127.0.0.1", echo.port, timeout=3.0)
        connection.sendall(b"binder")
        assert connection.recv(100) == b"binder"
        connection.close()
    finally:
        echo.stop()


def test_connect_tunnel_relays_bytes():
    echo = _EchoServer()
    echo.start()
    proxy = EgressProxy(lambda host, port: socket.create_connection((host, port), timeout=3))
    proxy.start()
    try:
        client = socket.create_connection(("127.0.0.1", proxy.port), timeout=3)
        client.sendall(
            f"CONNECT 127.0.0.1:{echo.port} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
        )
        response = _read_until(client, b"\r\n\r\n")
        assert b"200" in response.split(b"\r\n", 1)[0]

        client.sendall(b"hello through modem")
        assert client.recv(100) == b"hello through modem"
        client.close()
    finally:
        proxy.stop()
        echo.stop()


def test_plain_http_forwarding():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HttpHandler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()

    proxy = EgressProxy(lambda host, port: socket.create_connection((host, port), timeout=3))
    proxy.start()
    try:
        client = socket.create_connection(("127.0.0.1", proxy.port), timeout=3)
        client.sendall(
            f"GET http://127.0.0.1:{server.server_address[1]}/test HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{server.server_address[1]}\r\n\r\n".encode()
        )
        client.settimeout(5)
        buffer = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            buffer += chunk
        assert b"plain-http-ok" in buffer
        client.close()
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_proxy_returns_502_when_target_is_unreachable():
    proxy = EgressProxy(
        lambda host, port: (_ for _ in ()).throw(OSError("unreachable"))
    )
    proxy.start()
    try:
        client = socket.create_connection(("127.0.0.1", proxy.port), timeout=3)
        client.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\nHost: x\r\n\r\n")
        response = _read_until(client, b"\r\n\r\n")
        assert b"502" in response.split(b"\r\n", 1)[0]
        client.close()
    finally:
        proxy.stop()


def test_proxy_stop_is_idempotent():
    proxy = EgressProxy(lambda host, port: socket.create_connection((host, port), timeout=3))
    proxy.start()
    proxy.stop()
    proxy.stop()
    assert proxy.port > 0
