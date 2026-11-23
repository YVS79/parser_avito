"""Клиент CGI-API модемов ZTE (MF823D, MF79U, MF920 и совместимые).

Модем отдаёт локальный веб-интерфейс по адресу 192.168.0.1 (зависит от
прошивки). Управление выполняется GET/POST-запросами к
``/goform/goform_set_cmd_process`` и ``/goform/goform_get_cmd_process``.

Ключевые команды:
* ``DISCONNECT_NETWORK`` / ``CONNECT_NETWORK`` — переподключение PDN
  (самый быстрый способ получить новый IP, обычно 2–5 секунд);
* ``SET_BEARER_PREFERENCE`` — смена стандарта сети (Only_LTE, Only_WCDMA,
  NETWORK_auto и т.д.), заставляет модем перерегистрироваться у оператора;
* ``REBOOT_DEVICE`` — перезагрузка модема (крайняя мера).
"""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Optional

from loguru import logger

from .models import (
    ModemAuthError,
    ModemCommandError,
    ModemError,
    ModemStatus,
    ModemTransportError,
)

# Допустимые значения BearerPreference (чувствительны к регистру).
BEARER_MODES = (
    "NETWORK_auto",
    "WCDMA_preferred",
    "GSM_preferred",
    "Only_GSM",
    "Only_WCDMA",
    "Only_LTE",
    "WCDMA_AND_GSM",
    "WCDMA_AND_LTE",
    "GSM_AND_LTE",
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def normalize_gateway(value) -> str:
    """Привести адрес модема к виду host[:port] (без схемы и слэшей)."""
    text = str(value or "").strip()
    for prefix in ("http://", "https://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    text = text.split("/", 1)[0].strip()
    return text.rstrip("/")


def normalize_ip(value) -> Optional[str]:
    """Привести значение CGI к корректному IPv4 или None."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip().strip('"').strip()
    text = text.split(",", 1)[0].strip()
    if text.lower() in {"", "0", "0.0.0.0", "null", "none", "unknown"}:
        return None
    if not _IP_RE.match(text):
        return None
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return None
    return text


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    text = str(value).strip()
    return "" if text.lower() in {"", "null", "none"} else text


def _parse_json(body: str) -> Optional[dict]:
    body = (body or "").strip()
    if not body:
        return None
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", body, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _is_success(body: str) -> bool:
    body = (body or "").strip()
    if not body:
        # Некоторые прошивки отвечают пустым телом на успешную команду.
        return True
    data = _parse_json(body)
    if data is not None:
        result = _clean(data.get("result")).lower()
        if result in {"success", "0", "ok"}:
            return True
        if result:
            return False
    lowered = body.lower()
    return "success" in lowered and "failure" not in lowered


class ZTEClient:
    """Тонкий клиент CGI-API ZTE с авторизацией и быстрым опросом IP."""

    STATUS_FIELDS = (
        "network_provider",
        "network_type",
        "wan_ipaddr",
        "wan_csq",
        "signal_icon",
        "sim_status",
        "modem_main_state",
        "wan_rrc_state",
        "lte_rsrp",
        "lte_snr",
        "imei",
        "module_name",
        "soft_version",
        "hardware_version",
        "web_version",
    )

    def __init__(
        self,
        gateway: str,
        password: str = "",
        timeout: float = 2.5,
        poll_timeout: float = 1.2,
    ):
        self.gateway = normalize_gateway(gateway)
        self.password = password or ""
        self.timeout = timeout
        self.poll_timeout = poll_timeout

        self._lock = threading.RLock()
        self._cookie_jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
        )
        self._stok: Optional[str] = None
        self._auth_attempted = False

    # ------------------------------------------------------------------ HTTP
    def _base_url(self, path: str) -> str:
        return f"http://{self.gateway}/{path.lstrip('/')}"

    def _headers(self) -> dict:
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"http://{self.gateway}/index.html",
            "Origin": f"http://{self.gateway}",
        }
        if self._stok:
            headers["Cookie"] = f"stok={self._stok}"
        return headers

    def _remember_stok(self) -> None:
        for cookie in self._cookie_jar:
            if cookie.name == "stok" and cookie.value:
                self._stok = cookie.value
                return

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> tuple[int, str]:
        url = self._base_url(path)
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            headers=self._headers(),
            method=method,
        )
        effective_timeout = timeout or self.timeout
        with self._lock:
            try:
                with self._opener.open(request, timeout=effective_timeout) as response:
                    text = response.read(131072).decode("utf-8", errors="replace")
                    self._remember_stok()
                    return int(response.status), text
            except urllib.error.HTTPError as error:
                text = error.read(131072).decode("utf-8", errors="replace")
                self._remember_stok()
                return int(error.code), text
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise ModemTransportError(
                    f"Нет связи с веб-интерфейсом модема {self.gateway}: {error}"
                ) from error

    def login(self) -> None:
        """Авторизоваться в веб-интерфейсе (если он защищён паролем)."""
        if not self.password:
            raise ModemAuthError(
                "Веб-интерфейс модема требует пароль. Укажите его в "
                "настройке «Пароль модема» (modem_password)."
            )
        encoded = base64.b64encode(self.password.encode("utf-8")).decode("ascii")
        form = {
            "isTest": "false",
            "goformId": "LOGIN",
            "username": "admin",
            "password": encoded,
        }
        code, body = self._request(
            "POST", "goform/goform_set_cmd_process", data=form
        )
        data = _parse_json(body) or {}
        result = _clean(data.get("result")).lower()
        if code >= 400 or result not in {"0", "success", "ok"}:
            raise ModemAuthError(
                "Не удалось войти в веб-интерфейс модема. Проверьте пароль "
                f"(modem_password). Ответ модема: {body[:200]!r}"
            )
        self._remember_stok()
        logger.info("Авторизация в веб-интерфейсе модема выполнена")

    def _auth_retry(self, action):
        """Выполнить действие; при ошибке авторизации один раз войти и повторить."""
        try:
            return action()
        except ModemAuthError as error:
            if self._auth_attempted or not self.password:
                if not self.password:
                    raise ModemAuthError(
                        f"{error}. Укажите пароль веб-интерфейса модема "
                        "в настройке modem_password."
                    ) from error
                raise
            self._auth_attempted = True
            self.login()
            return action()

    # --------------------------------------------------------------- команды
    def get_cmd(self, *commands: str, timeout: Optional[float] = None) -> dict:
        """Прочитать значения CGI-параметров (cmd=...&multi_data=1)."""
        params = {
            "isTest": "false",
            "cmd": ",".join(commands),
            "multi_data": "1",
            "_": str(int(time.time() * 1000)),
        }

        def action() -> dict:
            code, body = self._request(
                "GET",
                "goform/goform_get_cmd_process",
                params=params,
                timeout=timeout or self.poll_timeout,
            )
            if code in (401, 403):
                raise ModemAuthError("Веб-интерфейс модема требует авторизацию")
            data = _parse_json(body)
            if data is None:
                if "<html" in body.lower() or "login" in body.lower():
                    raise ModemAuthError(
                        "Веб-интерфейс модема требует авторизацию"
                    )
                raise ModemCommandError(
                    f"Модем вернул неожиданный ответ: {body[:200]!r}"
                )
            return data

        return self._auth_retry(action)

    def _set_cmd(self, goform_id: str, **extra) -> None:
        """Выполнить управляющую команду (GET, при неудаче POST)."""
        params = {"isTest": "false", "goformId": goform_id, **extra}

        def action() -> None:
            code, body = self._request(
                "GET", "goform/goform_set_cmd_process", params=params
            )
            if code in (401, 403) or (
                _parse_json(body) is None
                and ("login" in body.lower() or "<html" in body.lower())
            ):
                raise ModemAuthError("Веб-интерфейс модема требует авторизацию")
            if code < 400 and _is_success(body):
                return

            # Часть прошивок принимает команды только через POST.
            code, body = self._request(
                "POST", "goform/goform_set_cmd_process", data=params
            )
            if code < 400 and _is_success(body):
                return
            raise ModemCommandError(
                f"Модем отклонил команду {goform_id}: HTTP {code}, {body[:200]!r}"
            )

        self._auth_retry(action)

    def wan_ip(self, timeout: Optional[float] = None) -> Optional[str]:
        data = self.get_cmd("wan_ipaddr", timeout=timeout or self.poll_timeout)
        return normalize_ip(data.get("wan_ipaddr"))

    def status(self, timeout: Optional[float] = None) -> ModemStatus:
        data = self.get_cmd(*self.STATUS_FIELDS, timeout=timeout)
        if "wan_ipaddr" not in data:
            # Некоторые прошивки игнорируют часть полей в multi_data-запросе.
            try:
                data["wan_ipaddr"] = self.wan_ip(timeout=timeout)
            except ModemError:
                data["wan_ipaddr"] = None

        signal_parts: list[str] = []
        icon = _clean(data.get("signal_icon"))
        if icon:
            signal_parts.append(f"{icon}/5")
        csq = _clean(data.get("wan_csq"))
        if csq:
            signal_parts.append(f"CSQ {csq}")
        rsrp = _clean(data.get("lte_rsrp"))
        if rsrp:
            signal_parts.append(f"RSRP {rsrp} dBm")

        wan_ip = normalize_ip(data.get("wan_ipaddr"))
        return ModemStatus(
            gateway=self.gateway,
            wan_ip=wan_ip,
            connected=wan_ip is not None,
            network_type=_clean(data.get("network_type")),
            operator=_clean(data.get("network_provider")),
            signal=", ".join(signal_parts),
            imei=_clean(data.get("imei")),
            model=_clean(data.get("module_name")) or "ZTE",
            firmware=_clean(data.get("soft_version"))
            or _clean(data.get("hardware_version")),
            raw=data,
        )

    def connect(self) -> None:
        self._set_cmd("CONNECT_NETWORK", notCallback="true")

    def disconnect(self) -> None:
        self._set_cmd("DISCONNECT_NETWORK", notCallback="true")

    def set_bearer(self, mode: str) -> None:
        if mode not in BEARER_MODES:
            raise ValueError(
                f"Неизвестный режим сети {mode!r}. Допустимые: {', '.join(BEARER_MODES)}"
            )
        self._set_cmd("SET_BEARER_PREFERENCE", BearerPreference=mode)

    def reboot(self) -> bool:
        """Отправить REBOOT_DEVICE.

        При перезагрузке модем часто обрывает HTTP-соединение, не успевая
        ответить. Такой обрыв считается нормальным: команда уже доставлена,
        поэтому транспортная ошибка не пробрасывается. Если же модем явно
        отклонил команду (result=failure), выбрасывается ModemCommandError.
        """
        try:
            self._set_cmd("REBOOT_DEVICE")
            logger.info("REBOOT_DEVICE: команда принята модемом")
            return True
        except ModemTransportError as error:
            logger.info(
                "REBOOT_DEVICE: связь с модемом оборвалась "
                f"({error}) — это ожидаемо при перезагрузке"
            )
            return True

    def cgi_available(self, timeout: float = 2.0) -> bool:
        """Проверить, отвечает ли веб-интерфейс (CGI) модема.

        Любой HTTP-ответ (включая 401/403) означает, что веб-сервер модема
        уже поднялся. Транспортная ошибка означает, что шлюз ещё недоступен.
        """
        params = {
            "isTest": "false",
            "cmd": "wan_ipaddr",
            "multi_data": "1",
            "_": str(int(time.time() * 1000)),
        }
        try:
            self._request(
                "GET",
                "goform/goform_get_cmd_process",
                params=params,
                timeout=max(0.3, timeout),
            )
            return True
        except ModemError:
            return False

    def wait_for_cgi(
        self,
        timeout: float = 120.0,
        interval: float = 1.0,
        should_stop=None,
    ) -> bool:
        """Ждать восстановления CGI-шлюза после перезагрузки."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if should_stop is not None and should_stop():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.cgi_available(timeout=min(2.0, max(0.3, remaining))):
                return True
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    def is_alive(self) -> bool:
        try:
            self.wan_ip(timeout=self.poll_timeout)
            return True
        except ModemError:
            return False

    # ------------------------------------------------------- ожидание событий
    def wait_for_ip(
        self,
        previous: Optional[str] = None,
        timeout: float = 8.0,
        require_change: bool = True,
        interval: float = 0.2,
        should_stop=None,
    ) -> Optional[str]:
        """Ждать подключения и (опционально) смены WAN IP.

        Возвращает новый IP или None по таймауту. Опрос идёт каждые 0.2 с,
        поэтому типичное переподключение занимает 2–5 секунд.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        last_error: Optional[Exception] = None
        while True:
            if should_stop is not None and should_stop():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                current = self.wan_ip(timeout=min(self.poll_timeout, remaining))
            except ModemError as error:
                last_error = error
                current = None
            if current and (
                not require_change or previous is None or current != previous
            ):
                return current
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

        if last_error is not None:
            logger.debug(f"Ожидание IP модема завершилось ошибкой: {last_error}")
        return None

    def wait_for_disconnect(
        self,
        timeout: float = 1.5,
        interval: float = 0.1,
        should_stop=None,
    ) -> bool:
        """Ждать, пока модем отпустит WAN-адрес (для быстрого реконнекта)."""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                return False
            try:
                if self.wan_ip(timeout=min(self.poll_timeout, 0.8)) is None:
                    return True
            except ModemError:
                return True
            time.sleep(interval)
        return False
