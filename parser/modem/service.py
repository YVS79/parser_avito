"""Сервис модема: поиск, проверка, запуск прокси и смена IP.

Стратегии смены IP:
* ``fast`` — только переподключение PDN (DISCONNECT_NETWORK/CONNECT_NETWORK);
  самый быстрый вариант, обычно 2–5 секунд;
* ``mode`` — смена стандарта сети (например, LTE → 3G → LTE). Модем
  перерегистрируется у оператора, IP меняется гарантированнее, но дольше
  (10–40 секунд);
* ``fast_then_mode`` (по умолчанию) — сначала быстрый реконнект, и только
  если IP не изменился, смена стандарта сети.
"""
from __future__ import annotations

import errno
import threading
import time
from typing import Optional

from loguru import logger

from .discovery import clear_cache, find_modem_endpoint
from .egress import EgressProxy, RouteBinder
from .models import (
    ModemAuthError,
    ModemCommandError,
    ModemEndpoint,
    ModemError,
    ModemStatus,
    RotationResult,
)
from .zte import BEARER_MODES, ZTEClient

ROTATION_STRATEGIES = ("fast_then_mode", "fast", "mode")

_WIN_ADDR_ERRORS = {10049, 10051, 10065}


def _as_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да"}


def _as_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, result))


def alternate_mode(preferred: str) -> str:
    """Подобрать стандарт сети, отличный от предпочтительного."""
    if preferred in {
        "Only_LTE",
        "WCDMA_AND_LTE",
        "GSM_AND_LTE",
        "NETWORK_auto",
    }:
        return "Only_WCDMA"
    return "Only_LTE"


class ModemService:
    """Управление USB-модемом ZTE и трафиком парсера через него."""

    def __init__(
        self,
        config,
        endpoint: Optional[ModemEndpoint] = None,
        client: Optional[ZTEClient] = None,
    ):
        self.config = config
        self._endpoint = endpoint
        self._client = client
        self._binder: Optional[RouteBinder] = None
        self._proxy: Optional[EgressProxy] = None
        self._status: Optional[ModemStatus] = None
        self._lock = threading.RLock()
        self._closed = False

    # -------------------------------------------------------------- настройки
    @property
    def password(self) -> str:
        return str(getattr(self.config, "modem_password", "") or "")

    @property
    def preferred_mode(self) -> str:
        mode = str(
            getattr(self.config, "modem_preferred_mode", "NETWORK_auto")
            or "NETWORK_auto"
        )
        return mode if mode in BEARER_MODES else "NETWORK_auto"

    @property
    def strategy(self) -> str:
        strategy = str(
            getattr(self.config, "modem_rotation_strategy", "fast_then_mode")
            or "fast_then_mode"
        )
        return strategy if strategy in ROTATION_STRATEGIES else "fast_then_mode"

    @property
    def fast_timeout(self) -> int:
        return _as_int(
            getattr(self.config, "modem_fast_timeout", 8), 8, 2, 60
        )

    @property
    def mode_timeout(self) -> int:
        return _as_int(
            getattr(self.config, "modem_mode_timeout", 45), 45, 10, 180
        )

    @property
    def fast_attempts(self) -> int:
        return _as_int(
            getattr(self.config, "modem_fast_attempts", 2), 2, 1, 5
        )

    @property
    def bind_interface(self) -> bool:
        return _as_bool(getattr(self.config, "modem_bind_interface", True), True)

    @property
    def reboot_on_fail(self) -> bool:
        return _as_bool(getattr(self.config, "modem_reboot_on_fail", False), False)

    # --------------------------------------------------------------- клиент
    def _create_binder(self, endpoint: ModemEndpoint) -> RouteBinder:
        if not self.bind_interface:
            return RouteBinder()
        adapter = endpoint.adapter
        ifindex = endpoint.ifindex
        local_ip = endpoint.local_ip
        interface_name = adapter.name if adapter else None
        if ifindex is None and not local_ip:
            logger.warning(
                "Не удалось определить сетевой адаптер модема. Трафик пойдёт по "
                "маршруту по умолчанию. Укажите modem_local_ip или modem_interface "
                "в config.toml, если парсер ходит не через модем."
            )
        return RouteBinder(
            ifindex=ifindex,
            local_ip=local_ip,
            interface_name=interface_name,
        )

    def _get_client(self) -> ZTEClient:
        with self._lock:
            if self._client is not None:
                return self._client

            endpoint = self._endpoint or find_modem_endpoint(self.config)
            self._endpoint = endpoint
            self._client = ZTEClient(
                endpoint.gateway,
                password=self.password,
                timeout=2.5,
                poll_timeout=1.2,
            )
            self._binder = self._create_binder(endpoint)
            logger.info(
                f"Модем: шлюз {endpoint.gateway}, привязка трафика: {self._binder}"
            )
            return self._client

    def _refresh_route(self) -> None:
        """Перечитать сетевой адаптер модема после смены IP/переподключения."""
        with self._lock:
            if self._endpoint is None:
                return
            clear_cache()
            try:
                endpoint = find_modem_endpoint(self.config, force_refresh=True)
            except ModemError as error:
                logger.warning(f"Не удалось обновить маршрут до модема: {error}")
                return
            self._endpoint = endpoint
            self._binder = self._create_binder(endpoint)

    def _connect_target(self, host: str, port: int):
        """Подключение исходящего сокета через интерфейс модема."""
        binder = self._binder or RouteBinder()
        try:
            return binder.connect(host, port)
        except OSError as error:
            winerror = getattr(error, "winerror", None)
            if error.errno in {errno.EADDRNOTAVAIL, errno.ENETUNREACH} or (
                winerror in _WIN_ADDR_ERRORS
            ):
                logger.warning(
                    f"Маршрут через модем устарел ({error}), обновляю настройки сети"
                )
                self._refresh_route()
                return (self._binder or binder).connect(host, port)
            raise

    # --------------------------------------------------------------- запуск
    def _read_status(self, client: ZTEClient) -> ModemStatus:
        """Прочитать статус; если прошивка не отдаёт часть полей — не падать."""
        try:
            return client.status()
        except ModemCommandError as error:
            logger.warning(f"Полный статус модема недоступен: {error}")
            try:
                wan_ip = client.wan_ip()
            except ModemError:
                wan_ip = None
            return ModemStatus(
                gateway=self._endpoint.gateway if self._endpoint else "",
                wan_ip=wan_ip,
                connected=wan_ip is not None,
            )

    def probe(self) -> ModemStatus:
        """Проверить модем и при необходимости поднять соединение."""
        with self._lock:
            client = self._get_client()
            status = self._read_status(client)
            self._apply_endpoint(status)

            if not status.wan_ip:
                # Статус мог не содержать wan_ipaddr — проверяем отдельно.
                try:
                    direct_ip = client.wan_ip()
                except ModemError:
                    direct_ip = None
                if direct_ip:
                    status.wan_ip = direct_ip
                    status.connected = True

            if not status.wan_ip:
                logger.info("Модем не подключён, отправляю CONNECT_NETWORK")
                try:
                    client.connect()
                except ModemAuthError:
                    raise
                except ModemError as error:
                    logger.warning(f"CONNECT_NETWORK: {error}")
                new_ip = client.wait_for_ip(timeout=20.0)
                if new_ip:
                    status = self._read_status(client)
                    self._apply_endpoint(status)
                    if not status.wan_ip:
                        status.wan_ip = new_ip
                        status.connected = True

            self._status = status
            logger.info(
                "Состояние модема: "
                f"{status.operator or 'оператор н/д'}, "
                f"{status.network_type or 'сеть н/д'}, "
                f"IP {status.wan_ip or 'нет'}"
            )
            return status

    def _apply_endpoint(self, status: ModemStatus) -> None:
        if self._endpoint is None:
            return
        status.local_ip = self._endpoint.local_ip
        status.ifindex = self._endpoint.ifindex

    def start(self) -> str:
        """Подготовить модем и запустить локальный прокси. Вернуть URL прокси."""
        with self._lock:
            if self._proxy is not None:
                return self._proxy.url

            status = self.probe()
            if not status.wan_ip:
                raise ModemError(
                    "Модем найден, но нет подключения к интернету. Проверьте SIM, "
                    "баланс и настройки APN; при необходимости отправьте "
                    "CONNECT_NETWORK через веб-интерфейс модема."
                )

            proxy = EgressProxy(self._connect_target)
            self._proxy = proxy
            url = proxy.start()
            logger.info(
                f"Парсер будет работать через USB-модем {self._endpoint.gateway if self._endpoint else ''}"
            )
            return url

    @property
    def proxy_url(self) -> Optional[str]:
        return self._proxy.url if self._proxy else None

    # ------------------------------------------------------------- ротация
    def rotate(self, reason: str = "block") -> RotationResult:
        """Сменить IP модема. Не бросает исключение — возвращает результат."""
        started = time.monotonic()
        with self._lock:
            client = self._get_client()
            try:
                old_ip = client.wan_ip()
            except ModemError as error:
                logger.warning(f"Не удалось прочитать текущий IP модема: {error}")
                old_ip = None

            strategies = (
                ["fast", "mode"] if self.strategy == "fast_then_mode" else [self.strategy]
            )
            logger.info(
                f"Смена IP модема (причина: {reason}), текущий IP {old_ip or 'нет'}, "
                f"стратегия {self.strategy}"
            )

            last_error = ""
            for strategy_name in strategies:
                try:
                    if strategy_name == "fast":
                        new_ip = self._rotate_fast(client, old_ip)
                    else:
                        new_ip = self._rotate_mode(client, old_ip)
                except ModemError as error:
                    last_error = str(error)
                    logger.warning(f"Смена IP ({strategy_name}) не удалась: {error}")
                    new_ip = None

                if new_ip and new_ip != old_ip:
                    elapsed = int((time.monotonic() - started) * 1000)
                    logger.success(
                        f"IP модема изменён за {elapsed / 1000:.1f} с: "
                        f"{old_ip or 'нет'} -> {new_ip} ({strategy_name})"
                    )
                    self._status = None
                    return RotationResult(
                        ok=True,
                        old_ip=old_ip,
                        new_ip=new_ip,
                        strategy=strategy_name,
                        elapsed_ms=elapsed,
                    )
                if new_ip == old_ip:
                    logger.warning(
                        f"После переподключения ({strategy_name}) оператор выдал "
                        f"тот же IP {new_ip}; пробую следующий способ"
                    )

            if self.reboot_on_fail:
                logger.warning("Пробую перезагрузить модем для смены IP")
                new_ip = self._rotate_reboot(client, old_ip)
                if new_ip and new_ip != old_ip:
                    elapsed = int((time.monotonic() - started) * 1000)
                    logger.success(
                        f"IP модема изменён после перезагрузки за "
                        f"{elapsed / 1000:.1f} с: {old_ip} -> {new_ip}"
                    )
                    self._status = None
                    return RotationResult(
                        ok=True,
                        old_ip=old_ip,
                        new_ip=new_ip,
                        strategy="reboot",
                        elapsed_ms=elapsed,
                    )

            elapsed = int((time.monotonic() - started) * 1000)
            error = last_error or (
                "оператор выдаёт тот же IP. Попробуйте стратегию mode, "
                "другой APN или перезагрузку модема"
            )
            logger.error(f"Не удалось сменить IP модема: {error}")
            self._status = None
            return RotationResult(
                ok=False,
                old_ip=old_ip,
                new_ip=None,
                strategy=",".join(strategies),
                elapsed_ms=elapsed,
                error=error,
            )

    def _rotate_fast(self, client: ZTEClient, before: Optional[str]) -> Optional[str]:
        for attempt in range(1, self.fast_attempts + 1):
            logger.debug(
                f"Быстрое переподключение PDN, попытка {attempt}/{self.fast_attempts}"
            )
            try:
                client.disconnect()
            except ModemError as error:
                logger.warning(f"DISCONNECT_NETWORK: {error}")
            client.wait_for_disconnect(timeout=1.5)

            try:
                client.connect()
            except ModemError as error:
                logger.warning(f"CONNECT_NETWORK: {error}")
                time.sleep(0.3)
                continue

            new_ip = client.wait_for_ip(
                previous=before,
                timeout=self.fast_timeout,
            )
            if new_ip:
                return new_ip
            logger.info("Быстрое переподключение не изменило IP")
            time.sleep(0.3)
        return None

    def _rotate_mode(self, client: ZTEClient, before: Optional[str]) -> Optional[str]:
        preferred = self.preferred_mode
        alternate = alternate_mode(preferred)
        logger.info(f"Переключение стандарта сети: {alternate} -> {preferred}")

        # MF823D позволяет менять стандарт сети только в отключённом состоянии.
        self._quietly(client.disconnect, "DISCONNECT_NETWORK")
        client.wait_for_disconnect(timeout=1.2)
        client.set_bearer(alternate)
        self._quietly(client.connect, "CONNECT_NETWORK")

        first_ip = client.wait_for_ip(previous=before, timeout=self.mode_timeout)
        if not first_ip:
            raise ModemCommandError(
                f"После переключения на {alternate} модем не подключился за "
                f"{self.mode_timeout} с"
            )

        # Возвращаем предпочтительный режим. Это ещё одна перерегистрация,
        # поэтому IP может измениться повторно — это нормально.
        restored_ip = None
        try:
            self._quietly(client.disconnect, "DISCONNECT_NETWORK")
            client.wait_for_disconnect(timeout=1.2)
            client.set_bearer(preferred)
            self._quietly(client.connect, "CONNECT_NETWORK")
            restored_ip = client.wait_for_ip(
                previous=None, timeout=self.mode_timeout, require_change=False
            )
        except ModemError as error:
            logger.warning(
                f"Не удалось вернуть режим {preferred}: {error}. "
                f"Остаюсь на {alternate} до следующей смены IP."
            )

        if restored_ip and restored_ip != before:
            return restored_ip
        if first_ip != before:
            return first_ip
        return None

    @staticmethod
    def _quietly(action, name: str) -> None:
        """Выполнить необязательную команду модема, не прерывая ротацию."""
        try:
            action()
        except ModemError as error:
            logger.debug(f"{name}: {error} (продолжаю)")

    def _rotate_reboot(self, client: ZTEClient, before: Optional[str]) -> Optional[str]:
        try:
            client.reboot()
        except ModemError as error:
            logger.warning(f"REBOOT_DEVICE: {error}")
            return None
        time.sleep(5.0)
        return client.wait_for_ip(previous=before, timeout=90.0)

    # -------------------------------------------------------------- статус
    def status(self) -> ModemStatus:
        with self._lock:
            client = self._get_client()
            status = self._read_status(client)
            self._apply_endpoint(status)
            self._status = status
            return status

    def describe(self) -> str:
        status = self.status()
        strategy = self.strategy
        preferred = self.preferred_mode

        if strategy == "fast":
            strategy_text = "быстрое переподключение (2-5 с)"
        elif strategy == "mode":
            strategy_text = (
                f"смена стандарта сети ({alternate_mode(preferred)} -> {preferred})"
            )
        else:
            strategy_text = (
                "сначала быстрое переподключение, при том же IP — смена стандарта "
                f"сети ({alternate_mode(preferred)} -> {preferred})"
            )

        return (
            status.describe()
            + f"\nСтратегия смены IP: {strategy_text}"
            + f"\nПредпочтительный режим: {preferred}"
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._proxy is not None:
                self._proxy.stop()
                self._proxy = None


def preflight_modem(config) -> str:
    """Проверить модем без запуска парсера (для GUI/CLI). Вернуть описание."""
    service = ModemService(config)
    try:
        status = service.probe()
        return status.describe()
    finally:
        service.close()
