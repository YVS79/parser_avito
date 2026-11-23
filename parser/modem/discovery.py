"""Автопоиск USB-модема ZTE и сетевого адаптера, через который он раздаёт интернет.

Модем ZTE MF823D (и родственные MF79U/MF920 и т.п.) по умолчанию работает
в режиме «сетевой карты» (CDC Ethernet/RNDIS): компьютер получает адрес
192.168.0.x, а веб-интерфейс модема доступен по адресу 192.168.0.1.
Через этот же веб-интерфейс доступно CGI-API управления (goform).
"""
from __future__ import annotations

import json
import locale
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from loguru import logger

from .models import ModemAuthError, ModemEndpoint, ModemNotFoundError, NetAdapter
from .zte import normalize_gateway

# Шлюзы, которые ZTE использует по умолчанию в разных прошивках/операторах.
ZTE_DEFAULT_GATEWAYS = (
    "192.168.0.1",
    "192.168.4.1",
    "192.168.32.1",
    "192.168.254.1",
)

_MODEM_NAME_KEYWORDS = (
    "zte",
    "mf823",
    "mf79",
    "mf920",
    "mobile",
    "4g",
    "lte",
    "wwan",
    "rndis",
    "remote ndis",
    "usb",
    "cellular",
    "modem",
)

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_PROBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "http://192.168.0.1/index.html",
}

_cache_lock = threading.Lock()
_cache: dict = {"key": None, "endpoint": None, "ts": 0.0}
_CACHE_TTL = 10.0


def clear_cache() -> None:
    """Сбросить кэш найденного модема (например, после переподключения USB)."""
    with _cache_lock:
        _cache.update({"key": None, "endpoint": None, "ts": 0.0})


def _run(command: list[str], timeout: float = 10.0) -> str:
    """Выполнить системную команду и вернуть stdout (пустая строка при ошибке)."""
    kwargs = {
        "capture_output": True,
        "timeout": timeout,
        "check": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        process = subprocess.run(command, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return ""

    raw = process.stdout or b""
    encodings = ["utf-8-sig", locale.getpreferredencoding(False), "cp866", "cp1251"]
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _normalize(value) -> Optional[str]:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _windows_adapters() -> list[NetAdapter]:
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-NetIPConfiguration | "
        "Where-Object { $_.IPv4Address -and $_.IPv4DefaultGateway } | "
        "ForEach-Object { [pscustomobject]@{ "
        "ifindex=[int]$_.InterfaceIndex; "
        "name=[string]$_.InterfaceAlias; "
        "description=[string]$_.InterfaceDescription; "
        "ipv4=[string](@($_.IPv4Address)[0].IPAddress); "
        "gateway=[string](@($_.IPv4DefaultGateway)[0].NextHop) } } | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    output = _run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        timeout=15.0,
    ).strip()
    if not output:
        return []

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logger.debug("Не удалось разобрать вывод PowerShell Get-NetIPConfiguration")
        return []

    if isinstance(data, dict):
        data = [data]

    adapters: list[NetAdapter] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        adapter = NetAdapter(
            name=str(item.get("name") or ""),
            description=str(item.get("description") or ""),
            ipv4=_normalize(item.get("ipv4")),
            gateway=_normalize(item.get("gateway")),
            ifindex=int(item["ifindex"]) if str(item.get("ifindex", "")).isdigit() else None,
        )
        if adapter.ipv4 or adapter.gateway:
            adapters.append(adapter)
    return adapters


def _windows_adapters_from_ipconfig() -> list[NetAdapter]:
    """Резервный вариант для старых Windows без Get-NetIPConfiguration."""
    output = _run(["ipconfig"], timeout=10.0)
    if not output:
        return []

    adapters: list[NetAdapter] = []
    current: Optional[dict] = None
    ipv4_re = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")

    def flush() -> None:
        nonlocal current
        if current and (current.get("ipv4") or current.get("gateway")):
            adapters.append(
                NetAdapter(
                    name=current.get("name", ""),
                    description=current.get("description", ""),
                    ipv4=current.get("ipv4"),
                    gateway=current.get("gateway"),
                )
            )
        current = None

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue

        if not line[:1].isspace() and line.endswith(":"):
            flush()
            current = {"name": line[:-1].strip(), "description": ""}
            continue

        if current is None:
            continue

        lowered = line.lower()
        match = ipv4_re.search(line)
        if not match:
            continue

        if ("ipv4" in lowered or "ip-адрес" in lowered) and not current.get("ipv4"):
            current["ipv4"] = match.group(1)
        elif ("gateway" in lowered or "шлюз" in lowered) and not current.get("gateway"):
            current["gateway"] = match.group(1)

    flush()
    return adapters


def _linux_adapters() -> list[NetAdapter]:
    adapters: list[NetAdapter] = []
    try:
        routes = json.loads(_run(["ip", "-j", "route", "show", "default"], 5.0) or "[]")
        addresses = json.loads(_run(["ip", "-j", "addr", "show"], 5.0) or "[]")
    except json.JSONDecodeError:
        return _linux_adapters_plain()

    if not isinstance(routes, list) or not isinstance(addresses, list):
        return _linux_adapters_plain()

    address_by_ifindex: dict[int, tuple[str, str]] = {}
    address_by_name: dict[str, tuple[str, int]] = {}
    for item in addresses:
        if not isinstance(item, dict):
            continue
        ifindex = item.get("ifindex")
        name = item.get("ifname") or ""
        ipv4 = None
        for addr in item.get("addr_info") or []:
            if isinstance(addr, dict) and addr.get("family") == "inet":
                ipv4 = addr.get("local")
                break
        if isinstance(ifindex, int) and ipv4:
            address_by_ifindex[ifindex] = (ipv4, name)
            if name:
                address_by_name[name] = (ipv4, ifindex)

    for route in routes:
        if not isinstance(route, dict):
            continue
        gateway = route.get("gateway")
        name = route.get("dev") or ""
        ifindex = route.get("ifindex")
        ipv4 = None
        if isinstance(ifindex, int):
            ipv4, known_name = address_by_ifindex.get(ifindex, (None, ""))
            name = name or known_name
        if ipv4 is None and name:
            ipv4, known_index = address_by_name.get(name, (None, None))
            ifindex = ifindex if isinstance(ifindex, int) else known_index
        if gateway:
            adapters.append(
                NetAdapter(
                    name=name,
                    ipv4=ipv4,
                    gateway=gateway,
                    ifindex=ifindex if isinstance(ifindex, int) else None,
                )
            )
    return adapters


def _linux_adapters_plain() -> list[NetAdapter]:
    route_output = _run(["ip", "route", "show", "default"], 5.0)
    addr_output = _run(["ip", "-o", "-4", "addr", "show"], 5.0)

    ip_by_name: dict[str, tuple[str, int]] = {}
    for line in addr_output.splitlines():
        match = re.match(r"\s*(\d+):\s+(\S+)\s+inet\s+(\d{1,3}(?:\.\d{1,3}){3})", line)
        if match:
            ip_by_name[match.group(2)] = (match.group(3), int(match.group(1)))

    adapters: list[NetAdapter] = []
    for line in route_output.splitlines():
        gw_match = re.search(r"via\s+(\d{1,3}(?:\.\d{1,3}){3})", line)
        dev_match = re.search(r"dev\s+(\S+)", line)
        if not gw_match or not dev_match:
            continue
        name = dev_match.group(1)
        ipv4, ifindex = ip_by_name.get(name, (None, None))
        adapters.append(
            NetAdapter(name=name, ipv4=ipv4, gateway=gw_match.group(1), ifindex=ifindex)
        )
    return adapters


def _macos_adapters() -> list[NetAdapter]:
    route_output = _run(["route", "-n", "get", "default"], 5.0)
    gateway_match = re.search(r"gateway:\s*(\d{1,3}(?:\.\d{1,3}){3})", route_output)
    interface_match = re.search(r"interface:\s*(\S+)", route_output)
    if not gateway_match or not interface_match:
        return []

    name = interface_match.group(1)
    ifconfig = _run(["ifconfig", name], 5.0)
    ipv4_match = re.search(r"inet\s+(\d{1,3}(?:\.\d{1,3}){3})", ifconfig)
    try:
        ifindex = socket.if_nametoindex(name)
    except OSError:
        ifindex = None
    return [
        NetAdapter(
            name=name,
            ipv4=ipv4_match.group(1) if ipv4_match else None,
            gateway=gateway_match.group(1),
            ifindex=ifindex,
        )
    ]


def list_adapters() -> list[NetAdapter]:
    """Вернуть сетевые адаптеры с IPv4-шлюзом (кандидаты на роль модема)."""
    if sys.platform == "win32":
        adapters = _windows_adapters()
        if not adapters:
            adapters = _windows_adapters_from_ipconfig()
        return adapters
    if sys.platform == "darwin":
        return _macos_adapters()
    return _linux_adapters()


def _adapter_score(adapter: NetAdapter) -> int:
    score = 0
    if adapter.gateway in ZTE_DEFAULT_GATEWAYS:
        score += 100
    text = f"{adapter.name} {adapter.description}".lower()
    for keyword in _MODEM_NAME_KEYWORDS:
        if keyword in text:
            score += 10
    if adapter.ipv4:
        score += 1
    if adapter.ifindex is not None:
        score += 1
    return score


def _gateway_url(gateway: str, path: str) -> str:
    return f"http://{gateway}/{path.lstrip('/')}"


def _probe_zte(gateway: str, timeout: float = 1.5) -> tuple[bool, bool]:
    """Проверить, отвечает ли по адресу ZTE CGI.

    Возвращает (найден_zte, требует_авторизации).
    """
    url = _gateway_url(
        gateway,
        "goform/goform_get_cmd_process?isTest=false&cmd=wan_ipaddr&multi_data=1&_="
        + str(int(time.time() * 1000)),
    )
    headers = dict(_PROBE_HEADERS)
    headers["Referer"] = f"http://{gateway}/index.html"
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(8192).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return False, True
        return False, False
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, False

    lowered = body.lower()
    if "wan_ipaddr" in lowered or "goform" in lowered or "zte" in lowered:
        return True, False
    return False, False


def _match_adapter(
    adapters: list[NetAdapter],
    gateway: Optional[str] = None,
    local_ip: Optional[str] = None,
    interface: Optional[str] = None,
) -> Optional[NetAdapter]:
    interface = (interface or "").strip()
    for adapter in adapters:
        if interface:
            if interface.isdigit() and adapter.ifindex == int(interface):
                return adapter
            if interface.lower() in f"{adapter.name} {adapter.description}".lower():
                return adapter
        if local_ip and adapter.ipv4 == local_ip:
            return adapter
        if gateway and adapter.gateway == gateway:
            return adapter
    return None


def _manual_adapter(config) -> NetAdapter:
    local_ip = (getattr(config, "modem_local_ip", "") or "").strip() or None
    interface = (getattr(config, "modem_interface", "") or "").strip()
    ifindex = int(interface) if interface.isdigit() else None
    return NetAdapter(
        name=interface or "задан вручную",
        ipv4=local_ip,
        gateway=(getattr(config, "modem_gateway", "") or "").strip() or None,
        ifindex=ifindex,
    )


def find_modem_endpoint(config, force_refresh: bool = False) -> ModemEndpoint:
    """Найти ZTE-модем: CGI-шлюз и сетевой адаптер для привязки трафика.

    Порядок поиска:
    1. адрес из ``modem_gateway`` (если задан);
    2. шлюзы сетевых адаптеров, похожих на USB-модем;
    3. типовые адреса ZTE (192.168.0.1 и т.п.).
    """
    manual_gateway = normalize_gateway(getattr(config, "modem_gateway", ""))
    manual_local_ip = (getattr(config, "modem_local_ip", "") or "").strip()
    manual_interface = (getattr(config, "modem_interface", "") or "").strip()
    cache_key = (manual_gateway, manual_local_ip, manual_interface, sys.platform)

    with _cache_lock:
        if (
            not force_refresh
            and _cache["endpoint"] is not None
            and _cache["key"] == cache_key
            and time.monotonic() - _cache["ts"] < _CACHE_TTL
        ):
            return _cache["endpoint"]

    adapters = list_adapters()

    candidates: list[tuple[str, Optional[NetAdapter]]] = []
    seen: set[str] = set()

    def add(gateway: Optional[str], adapter: Optional[NetAdapter]) -> None:
        if not gateway or gateway in seen:
            return
        seen.add(gateway)
        candidates.append((gateway, adapter))

    if manual_gateway:
        add(
            manual_gateway,
            _match_adapter(
                adapters,
                gateway=manual_gateway,
                local_ip=manual_local_ip or None,
                interface=manual_interface,
            ),
        )
    else:
        for adapter in sorted(adapters, key=_adapter_score, reverse=True):
            if adapter.gateway:
                add(adapter.gateway, adapter)
        for gateway in ZTE_DEFAULT_GATEWAYS:
            add(
                gateway,
                _match_adapter(
                    adapters,
                    gateway=gateway,
                    local_ip=manual_local_ip or None,
                    interface=manual_interface,
                ),
            )

    auth_required = False
    auth_gateway = None
    for gateway, adapter in candidates:
        found, needs_auth = _probe_zte(gateway)
        if needs_auth and (
            manual_gateway == gateway or gateway in ZTE_DEFAULT_GATEWAYS
        ):
            auth_required = True
            auth_gateway = gateway
        if not found:
            if needs_auth and auth_gateway == gateway:
                logger.warning(
                    f"Модем по адресу {gateway} требует авторизацию в веб-интерфейсе"
                )
            continue

        if adapter is None:
            adapter = _match_adapter(
                adapters,
                gateway=gateway,
                local_ip=manual_local_ip or None,
                interface=manual_interface,
            )
        if adapter is None and manual_local_ip:
            candidate = _manual_adapter(config)
            adapter = NetAdapter(
                name=candidate.name,
                ipv4=manual_local_ip,
                gateway=gateway,
                ifindex=candidate.ifindex,
            )

        endpoint = ModemEndpoint(
            gateway=gateway,
            adapter=adapter,
            auth_required=False,
        )
        with _cache_lock:
            _cache.update(
                {"key": cache_key, "endpoint": endpoint, "ts": time.monotonic()}
            )
        logger.info(
            f"USB-модем найден: {gateway}"
            + (f" через {adapter}" if adapter else " (адаптер не определён)")
        )
        return endpoint

    if auth_required and auth_gateway:
        if not (getattr(config, "modem_password", "") or "").strip():
            raise ModemAuthError(
                f"Модем {auth_gateway} требует авторизацию в веб-интерфейсе. "
                "Укажите пароль в настройке modem_password."
            )
        # Пароль задан — пусть ZTEClient выполнит LOGIN и продолжит работу.
        adapter = _match_adapter(
            adapters,
            gateway=auth_gateway,
            local_ip=manual_local_ip or None,
            interface=manual_interface,
        )
        endpoint = ModemEndpoint(
            gateway=auth_gateway,
            adapter=adapter,
            auth_required=True,
        )
        with _cache_lock:
            _cache.update(
                {"key": cache_key, "endpoint": endpoint, "ts": time.monotonic()}
            )
        logger.info(f"USB-модем найден (требуется вход): {auth_gateway}")
        return endpoint

    adapters_text = (
        "\n".join(f"  • {adapter}" for adapter in adapters) or "  • адаптеры не найдены"
    )
    hint = ""
    if manual_gateway:
        hint = (
            f"\nПроверьте адрес modem_gateway={manual_gateway!r}: откройте "
            f"http://{manual_gateway}/ в браузере."
        )
    raise ModemNotFoundError(
        "USB 4G модем ZTE не найден. Убедитесь, что модем подключён, работает "
        "в режиме сетевой карты и его веб-интерфейс открывается.\n"
        f"Проверенные адаптеры:\n{adapters_text}{hint}"
    )
