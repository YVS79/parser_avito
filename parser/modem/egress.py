"""Локальный HTTP/HTTPS-прокси, жёстко привязанный к интерфейсу 4G-модема.

Зачем это нужно
---------------
Если в компьютере одновременно есть Wi-Fi/Ethernet и USB-модем, одного
``bind()`` к IP модема недостаточно: Windows выбирает исходящий интерфейс по
таблице маршрутов. Поэтому для каждого исходящего сокета выставляется
``IP_UNICAST_IF`` (Windows) / ``SO_BINDTODEVICE`` (Linux), который принудительно
направляет трафик в интерфейс модема. Парсер ходит через локальный прокси
``127.0.0.1:<port>``, поэтому остальной трафик компьютера не затрагивается.
"""
from __future__ import annotations

import select
import socket
import socketserver
import struct
import sys
import threading
import urllib.parse
from typing import Callable, Optional

from loguru import logger

# Windows: IPPROTO_IP, IP_UNICAST_IF. Индекс интерфейса передаётся
# в сетевом порядке байт (так же делает wireguard-windows).
_IP_UNICAST_IF = 31
# macOS: IPPROTO_IP, IP_BOUND_IF (индекс в host byte order).
_IP_BOUND_IF = 25
# Linux: SOL_SOCKET, SO_BINDTODEVICE.
_SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)

_BUFFER_SIZE = 65536
_MAX_HEADER = 65536
_TUNNEL_IDLE_TIMEOUT = 120.0


class RouteBinder:
    """Создаёт TCP-соединения через заданный сетевой интерфейс."""

    def __init__(
        self,
        ifindex: Optional[int] = None,
        local_ip: Optional[str] = None,
        interface_name: Optional[str] = None,
    ):
        self.ifindex = ifindex
        self.local_ip = local_ip
        self.interface_name = interface_name

    def __str__(self) -> str:
        parts = []
        if self.interface_name:
            parts.append(self.interface_name)
        if self.local_ip:
            parts.append(self.local_ip)
        if self.ifindex is not None:
            parts.append(f"ifindex={self.ifindex}")
        return ", ".join(parts) or "без привязки"

    def _apply_interface(self, sock: socket.socket) -> None:
        if self.ifindex is None:
            return
        try:
            if sys.platform == "win32":
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    _IP_UNICAST_IF,
                    struct.pack("!I", int(self.ifindex)),
                )
            elif sys.platform == "darwin":
                sock.setsockopt(
                    socket.IPPROTO_IP, _IP_BOUND_IF, int(self.ifindex)
                )
            elif self.interface_name:
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    _SO_BINDTODEVICE,
                    self.interface_name.encode("utf-8"),
                )
        except OSError as error:
            message = f"Не удалось привязать сокет к интерфейсу {self}: {error}"
            if sys.platform == "win32":
                logger.warning(message)
            else:
                logger.debug(message)

    def connect(
        self, host: str, port: int, timeout: float = 15.0
    ) -> socket.socket:
        """Подключиться к host:port, выбрав интерфейс модема."""
        try:
            addresses = socket.getaddrinfo(
                host, port, socket.AF_INET, socket.SOCK_STREAM
            )
        except socket.gaierror as error:
            raise OSError(f"Не удалось разрешить {host}: {error}") from error

        last_error: Optional[OSError] = None
        for family, socktype, proto, _canonname, sockaddr in addresses:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(timeout)
            try:
                self._apply_interface(sock)
                if self.local_ip:
                    try:
                        sock.bind((self.local_ip, 0))
                    except OSError as bind_error:
                        # При заданном IP_UNICAST_IF bind не обязателен;
                        # например, если DHCP выдал модему новый адрес.
                        if self.ifindex is None:
                            raise
                        logger.debug(
                            f"bind к {self.local_ip} не удался, "
                            f"продолжаю через интерфейс: {bind_error}"
                        )
                sock.connect(sockaddr)
                sock.settimeout(None)
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                return sock
            except OSError as error:
                last_error = error
                try:
                    sock.close()
                except OSError:
                    pass
        raise last_error or OSError(f"Не удалось подключиться к {host}:{port}")


class EgressProxy:
    """Минимальный HTTP CONNECT-прокси для исходящих соединений парсера."""

    def __init__(
        self,
        connect_fn: Callable[[str, int], socket.socket],
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self.connect_fn = connect_fn
        self.host = host
        self.port = port
        self._server: Optional[_ProxyServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        if self.port == 0:
            raise RuntimeError("Прокси ещё не запущен")
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        if self._server is not None:
            return self.url
        self._server = _ProxyServer((self.host, self.port), _ProxyHandler, self.connect_fn)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="modem-egress-proxy",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Локальный прокси модема запущен: {self.url}")
        return self.url

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


class _ProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, connect_fn):
        self.connect_fn = connect_fn
        super().__init__(address, handler)


class _ProxyHandler(socketserver.BaseRequestHandler):
    server: _ProxyServer

    def setup(self) -> None:
        self.request.settimeout(30.0)

    def handle(self) -> None:
        try:
            header, rest = self._read_header()
            if header is None:
                return
            lines = header.split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) < 3:
                self._send_error(400, "Bad Request")
                return
            method, target, version = parts[0], parts[1], parts[2]
            headers = self._parse_headers(lines[1:])

            if method.upper() == "CONNECT":
                self._handle_connect(target)
            else:
                self._handle_http(method, target, version, headers, rest)
        except (socket.timeout, ConnectionError):
            return
        except Exception as error:  # noqa: BLE001 - прокси не должен падать
            logger.debug(f"Ошибка локального прокси модема: {error}")
            try:
                self._send_error(502, "Bad Gateway")
            except OSError:
                pass

    # ------------------------------------------------------------- разбор
    def _read_header(self) -> tuple[Optional[str], bytes]:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.request.recv(_BUFFER_SIZE)
            if not chunk:
                return None, b""
            buffer += chunk
            if len(buffer) > _MAX_HEADER:
                self._send_error(431, "Request Header Fields Too Large")
                return None, b""
        head, _, rest = buffer.partition(b"\r\n\r\n")
        return head.decode("latin-1", errors="replace"), rest

    @staticmethod
    def _parse_headers(lines: list[str]) -> dict[str, str]:
        headers: dict[str, str] = {}
        for line in lines:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        return headers

    def _send_error(self, code: int, reason: str) -> None:
        body = f"{code} {reason}".encode("ascii")
        self.request.sendall(
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n".encode("ascii")
            + body
        )

    # ------------------------------------------------------------- CONNECT
    def _handle_connect(self, target: str) -> None:
        host, port = self._split_host_port(target, default_port=443)
        upstream = self._connect(host, port)
        try:
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self._relay(upstream)
        finally:
            _close(upstream)

    def _split_host_port(self, target: str, default_port: int) -> tuple[str, int]:
        if target.startswith("["):
            host, _, port_text = target[1:].partition("]:")
            return host, int(port_text or default_port)
        if ":" in target:
            host, port_text = target.rsplit(":", 1)
            return host, int(port_text or default_port)
        return target, default_port

    def _connect(self, host: str, port: int) -> socket.socket:
        upstream = self.server.connect_fn(host, port)
        upstream.settimeout(None)
        try:
            upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return upstream

    def _relay(self, upstream: socket.socket) -> None:
        client = self.request
        peers = (client, upstream)
        idle_deadline = _deadline(_TUNNEL_IDLE_TIMEOUT)
        while True:
            remaining = idle_deadline - _now()
            if remaining <= 0:
                return
            readable, _, _ = select.select(peers, [], [], min(remaining, 1.0))
            if not readable:
                continue
            for source in readable:
                try:
                    data = source.recv(_BUFFER_SIZE)
                except OSError:
                    return
                if not data:
                    return
                destination = upstream if source is client else client
                destination.sendall(data)
                idle_deadline = _deadline(_TUNNEL_IDLE_TIMEOUT)

    # ---------------------------------------------------------- обычный HTTP
    def _handle_http(
        self,
        method: str,
        target: str,
        version: str,
        headers: dict[str, str],
        rest: bytes,
    ) -> None:
        if target.startswith("https://"):
            self._send_error(400, "Use CONNECT for HTTPS")
            return

        if target.startswith("http://"):
            parsed = urllib.parse.urlsplit(target)
            host = parsed.hostname or ""
            port = parsed.port or 80
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        else:
            host = headers.get("host", "")
            if ":" in host:
                host, port_text = host.rsplit(":", 1)
                port = int(port_text or 80)
            else:
                port = 80
            path = target

        if not host:
            self._send_error(400, "Bad Request")
            return

        upstream = self._connect(host, port)
        try:
            skip = {
                "proxy-connection",
                "proxy-authorization",
                "connection",
                "keep-alive",
                "upgrade",
            }
            request_lines = [f"{method} {path} {version}"]
            for name, value in headers.items():
                if name in skip:
                    continue
                request_lines.append(f"{name}: {value}")
            request_lines.append("Connection: close")
            request = "\r\n".join(request_lines) + "\r\n\r\n"
            upstream.sendall(request.encode("latin-1", errors="replace"))

            if rest:
                upstream.sendall(rest)
            content_length = int(headers.get("content-length", "0") or 0)
            already_sent = len(rest)
            while already_sent < content_length:
                chunk = self.request.recv(
                    min(_BUFFER_SIZE, content_length - already_sent)
                )
                if not chunk:
                    break
                upstream.sendall(chunk)
                already_sent += len(chunk)

            while True:
                data = upstream.recv(_BUFFER_SIZE)
                if not data:
                    break
                self.request.sendall(data)
        finally:
            _close(upstream)


def _now() -> float:
    import time

    return time.monotonic()


def _deadline(seconds: float) -> float:
    return _now() + seconds


def _close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass
