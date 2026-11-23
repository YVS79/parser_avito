"""Сервис модема: поиск, проверка, запуск прокси и смена WAN IP.

Стратегии смены IP:
* ``fast`` — только переподключение PDN (DISCONNECT_NETWORK/CONNECT_NETWORK);
  самый быстрый вариант, обычно 2–5 секунд;
* ``reboot`` — сразу перезагрузка модема через REBOOT_DEVICE;
* ``fast_then_reboot`` (по умолчанию) — сначала быстрое переподключение,
  и только если WAN IP не изменился, перезагрузка модема.

Переход на 3G/Only_WCDMA не используется: модем остаётся в LTE.
"""
from __future__ import annotations

import errno
import threading
import time
from typing import Optional

from loguru import logger

from .discovery import clear_cache, find_modem_endpoint
from .egress import EGRESS_MODES, EgressProxy, RouteBinder
from .models import (
    ModemAuthError,
    ModemCommandError,
    ModemEndpoint,
    ModemError,
    ModemStatus,
    RotationResult,
)
from .zte import ZTEClient

ROTATION_STRATEGIES = ("fast_then_reboot", "fast", "reboot")
_LEGACY_STRATEGIES = {
    "fast_then_mode": "fast_then_reboot",
    "mode": "fast_then_reboot",
}

_WIN_ADDR_ERRORS = {10049, 10051, 10065}
_FAST_DISCONNECT_TIMEOUT = 3.0
_FAST_SETTLE_SECONDS = 2.0

# Внешние адреса для проверки, что трафик реально уходит через модем.
_EGRESS_CHECK_TARGETS = (
    ("www.avito.ru", 443),
    ("1.1.1.1", 443),
)


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


class ModemService:
    """Управление USB-модемом ZTE и трафиком парсера через него."""

    def __init__(
        self,
        config,
        endpoint: Optional[ModemEndpoint] = None,
        client: Optional[ZTEClient] = None,
        stop_event=None,
    ):
        self.config = config
        self._endpoint = endpoint
        self._client = client
        self._binder: Optional[RouteBinder] = None
        self._proxy: Optional[EgressProxy] = None
        self._status: Optional[ModemStatus] = None
        self._lock = threading.RLock()
        self._rotation_lock = threading.Lock()
        self._stop_event = stop_event
        self._route_mode: Optional[str] = None
        self._closed = False
        self._legacy_strategy_warned = False

    # -------------------------------------------------------------- настройки
    @property
    def password(self) -> str:
        return str(getattr(self.config, "modem_password", "") or "")

    @property
    def strategy(self) -> str:
        raw = str(
            getattr(self.config, "modem_rotation_strategy", "fast_then_reboot")
            or "fast_then_reboot"
        ).strip()
        if raw in _LEGACY_STRATEGIES:
            if not self._legacy_strategy_warned:
                logger.warning(
                    f"Стратегия {raw!r} устарела: переход на 3G отключён. "
                    "Использую 'fast_then_reboot' (LTE сохраняется)."
                )
                self._legacy_strategy_warned = True
            return _LEGACY_STRATEGIES[raw]
        if raw not in ROTATION_STRATEGIES:
            logger.warning(
                f"Неизвестная стратегия смены IP {raw!r}; "
                "использую 'fast_then_reboot'"
            )
            return "fast_then_reboot"
        return raw

    @property
    def fast_timeout(self) -> int:
        return _as_int(
            getattr(self.config, "modem_fast_timeout", 8), 8, 2, 60
        )

    @property
    def reboot_timeout(self) -> int:
        return _as_int(
            getattr(self.config, "modem_reboot_timeout", 120), 120, 1, 600
        )

    @property
    def fast_attempts(self) -> int:
        return _as_int(
            getattr(self.config, "modem_fast_attempts", 2), 2, 1, 5
        )

    @property
    def max_reboots(self) -> int:
        return _as_int(
            getattr(self.config, "modem_max_reboots", 1), 1, 0, 5
        )

    @property
    def bind_interface(self) -> bool:
        return _as_bool(getattr(self.config, "modem_bind_interface", True), True)

    @property
    def route_mode(self) -> Optional[str]:
        return self._route_mode

    @property
    def reboot_on_fail(self) -> bool:
        return _as_bool(getattr(self.config, "modem_reboot_on_fail", True), True)

    def _stop_requested(self) -> bool:
        return bool(self._stop_event is not None and self._stop_event.is_set())

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
            mode=self._route_mode or "iface_then_bind",
        )

    def _select_egress_mode(self, endpoint: ModemEndpoint) -> None:
        """Найти режим привязки, при котором трафик реально идёт через модем.

        На разных сборках Windows рабочим может быть разный порядок
        IP_UNICAST_IF/bind. Проверяем соединением на внешний адрес и
        сравниваем локальный IP сокета с IP модема. Если ни один режим не
        дал результата — парсер не запускается, чтобы не уйти через Ethernet.
        """
        if not self.bind_interface:
            logger.warning(
                "Привязка к модему отключена (modem_bind_interface=false): "
                "гарантировать выход через модем нельзя."
            )
            return

        if endpoint.adapter is None and endpoint.ifindex is None and not endpoint.local_ip:
            raise ModemError(
                "Не удалось определить сетевой интерфейс модема. "
                "Укажите modem_local_ip или modem_interface в config.toml."
            )

        errors: list[str] = []
        for mode in EGRESS_MODES:
            binder = RouteBinder(
                ifindex=endpoint.ifindex,
                local_ip=endpoint.local_ip,
                interface_name=endpoint.adapter.name if endpoint.adapter else None,
                mode=mode,
            )
            last_error: Optional[OSError] = None
            for host, port in _EGRESS_CHECK_TARGETS:
                try:
                    sock = binder.connect(host, port, timeout=2.5)
                    try:
                        source_ip = sock.getsockname()[0]
                    finally:
                        sock.close()
                except OSError as error:
                    last_error = error
                    continue

                self._route_mode = mode
                self._binder = binder
                logger.success(
                    "Выход через USB-модем подтверждён: "
                    f"режим {mode}, источник {source_ip}"
                )
                return

            message = f"{mode}: {last_error or 'нет соединения'}"
            errors.append(message)
            logger.warning(f"Режим привязки не сработал — {message}")

        raise ModemError(
            "Не удалось направить трафик через USB-модем: ни один режим "
            "привязки не дал внешнего соединения. Парсер не запущен, чтобы "
            "не работать через Ethernet/Wi-Fi. Проверьте маршрут до "
            "192.168.4.1, отключите Ethernet/Wi-Fi или задайте "
            "modem_local_ip/modem_interface. Если включён VPN, настройте "
            "split tunneling (исключите 192.168.4.0/24 и python.exe) "
            "или отключите VPN: VPN может перехватывать трафик. Детали: "
            + "; ".join(errors)
        )

    def check_egress(self) -> Optional[str]:
        """Проверить, что трафик действительно уходит через модем.

        Возвращает выбранный режим привязки. Если модем/адаптер не найден
        или ни один режим не даёт внешнего соединения — бросает ModemError.
        """
        with self._lock:
            self._get_client()
            if self._endpoint is not None:
                self._select_egress_mode(self._endpoint)
            return self._route_mode

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
                new_ip = client.wait_for_ip(
                    timeout=20.0,
                    should_stop=self._stop_requested,
                )
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

            if self._endpoint is not None:
                self._select_egress_mode(self._endpoint)

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
        """Сменить WAN IP модема. Не бросает исключение — возвращает результат."""
        if not self._rotation_lock.acquire(blocking=False):
            logger.warning("Смена IP уже выполняется; повторный запрос пропущен")
            return RotationResult(
                ok=False,
                old_ip=None,
                new_ip=None,
                strategy="busy",
                error="Смена IP уже выполняется",
            )
        started = time.monotonic()
        try:
            return self._rotate_locked(reason, started)
        finally:
            self._rotation_lock.release()

    def _rotate_locked(self, reason: str, started: float) -> RotationResult:
        with self._lock:
            client = self._get_client()
            strategy = self.strategy
            if self._stop_requested():
                return self._failure(
                    started, None, strategy, "Смена IP остановлена пользователем"
                )

            try:
                old_ip = client.wan_ip()
            except ModemError as error:
                logger.warning(f"Не удалось прочитать исходный WAN IP: {error}")
                old_ip = None
            logger.info(
                f"Смена WAN IP модема (причина: {reason}), исходный IP: "
                f"{old_ip or 'нет'}, стратегия: {strategy}"
            )

            if strategy in {"fast", "fast_then_reboot"}:
                fast_ip = self._rotate_fast(client, old_ip)
                if self._stop_requested():
                    return self._failure(
                        started, old_ip, strategy,
                        "Смена IP остановлена пользователем",
                    )
                if fast_ip and fast_ip != old_ip:
                    return self._success(started, old_ip, fast_ip, "fast")
                if fast_ip == old_ip:
                    logger.warning(
                        f"Быстрое переподключение вернуло тот же WAN IP {fast_ip}"
                    )
                else:
                    logger.warning(
                        "Быстрое переподключение не вернуло WAN IP"
                    )
                if strategy == "fast":
                    return self._failure(
                        started, old_ip, strategy,
                        "Быстрое переподключение не изменило WAN IP; "
                        "перезагрузка отключена стратегией 'fast'",
                    )

            if not self.reboot_on_fail or self.max_reboots <= 0:
                return self._failure(
                    started, old_ip, strategy,
                    "Быстрый способ не дал новый WAN IP, автоматическая "
                    "перезагрузка отключена "
                    "(modem_reboot_on_fail=false или modem_max_reboots=0)",
                )

            last_error = ""
            for attempt in range(1, self.max_reboots + 1):
                if self._stop_requested():
                    return self._failure(
                        started, old_ip, strategy,
                        "Смена IP остановлена пользователем",
                    )
                logger.warning(
                    "Быстрый способ не дал новый WAN IP — перезагружаю модем "
                    f"(попытка {attempt}/{self.max_reboots})"
                )
                new_ip, error = self._reboot_once(client, old_ip, attempt)
                if self._stop_requested():
                    return self._failure(
                        started, old_ip, strategy,
                        "Смена IP остановлена пользователем",
                    )
                if new_ip and new_ip != old_ip:
                    return self._success(started, old_ip, new_ip, "reboot")
                if new_ip == old_ip:
                    last_error = (
                        "После перезагрузки оператор вернул тот же WAN IP "
                        f"{new_ip}"
                    )
                else:
                    last_error = error or (
                        "После перезагрузки не удалось получить WAN IP"
                    )
                logger.warning(last_error)
                if attempt < self.max_reboots:
                    time.sleep(1.0)

            return self._failure(
                started, old_ip, strategy,
                last_error or "Смена WAN IP не удалась",
            )

    def _success(
        self,
        started: float,
        old_ip: Optional[str],
        new_ip: str,
        strategy: str,
    ) -> RotationResult:
        elapsed = int((time.monotonic() - started) * 1000)
        logger.success(
            f"WAN IP модема изменён за {elapsed / 1000:.1f} с: "
            f"{old_ip or 'нет'} -> {new_ip} ({strategy})"
        )
        self._status = None
        return RotationResult(
            ok=True,
            old_ip=old_ip,
            new_ip=new_ip,
            strategy=strategy,
            elapsed_ms=elapsed,
        )

    def _failure(
        self,
        started: float,
        old_ip: Optional[str],
        strategy: str,
        error: str,
    ) -> RotationResult:
        elapsed = int((time.monotonic() - started) * 1000)
        logger.error(f"Смена WAN IP не выполнена за {elapsed / 1000:.1f} с: {error}")
        self._status = None
        return RotationResult(
            ok=False,
            old_ip=old_ip,
            new_ip=None,
            strategy=strategy,
            elapsed_ms=elapsed,
            error=error,
        )

    def _rotate_fast(self, client: ZTEClient, before: Optional[str]) -> Optional[str]:
        for attempt in range(1, self.fast_attempts + 1):
            if self._stop_requested():
                return None
            logger.info(
                "Быстрое переподключение PDN, попытка "
                f"{attempt}/{self.fast_attempts}"
            )
            try:
                client.disconnect()
                logger.info("DISCONNECT_NETWORK: команда принята")
            except ModemError as error:
                logger.warning(f"DISCONNECT_NETWORK: {error}")

            disconnected = client.wait_for_disconnect(
                timeout=_FAST_DISCONNECT_TIMEOUT,
                should_stop=self._stop_requested,
            )
            logger.info(
                "Отключение PDN: "
                + ("подтверждено" if disconnected else "не подтверждено")
            )
            if self._stop_requested():
                return None

            # Даём модему завершить отключение перед CONNECT_NETWORK.
            time.sleep(_FAST_SETTLE_SECONDS)

            try:
                client.connect()
                logger.info("CONNECT_NETWORK: команда принята")
            except ModemError as error:
                logger.warning(f"CONNECT_NETWORK: {error}")

            reconnect_started = time.monotonic()
            current_ip = client.wait_for_ip(
                previous=None,
                timeout=self.fast_timeout,
                require_change=False,
                should_stop=self._stop_requested,
            )
            elapsed = time.monotonic() - reconnect_started
            if current_ip:
                logger.info(
                    f"Соединение восстановлено за {elapsed:.1f} с, "
                    f"WAN IP: {current_ip}"
                )
                if current_ip != before:
                    return current_ip
                logger.warning(
                    "WAN IP после быстрого переподключения не изменился: "
                    f"{current_ip}"
                )
            else:
                logger.warning(
                    f"Соединение не восстановилось за {self.fast_timeout} с"
                )

            if attempt < self.fast_attempts:
                time.sleep(1.0)
        return None

    def _reboot_once(
        self, client: ZTEClient, before: Optional[str], attempt: int
    ) -> tuple[Optional[str], str]:
        reboot_started = time.monotonic()
        try:
            client.reboot()
            logger.info("REBOOT_DEVICE отправлен; ожидаю восстановление CGI-шлюза")
        except ModemError as error:
            return None, f"REBOOT_DEVICE не выполнен: {error}"

        deadline = time.monotonic() + self.reboot_timeout
        logger.info(
            f"Ожидание CGI-шлюза после перезагрузки (до {self.reboot_timeout} с)"
        )
        if not client.wait_for_cgi(
            timeout=self.reboot_timeout,
            interval=1.0,
            should_stop=self._stop_requested,
        ):
            if self._stop_requested():
                return None, "Смена IP остановлена пользователем"
            return None, (
                f"CGI-шлюз не восстановился за {self.reboot_timeout} с"
            )
        logger.success(
            "CGI-шлюз модема снова доступен через "
            f"{time.monotonic() - reboot_started:.1f} с"
        )

        remaining = max(0.0, deadline - time.monotonic())
        status = self._wait_for_lte(client, remaining)
        if self._stop_requested():
            return None, "Смена IP остановлена пользователем"
        if status is None:
            return None, (
                "Модем восстановился, но не зарегистрировался в LTE за "
                f"{self.reboot_timeout} с"
            )
        logger.success(
            "Модем зарегистрирован в LTE (оператор: "
            f"{status.operator or 'н/д'}) через "
            f"{time.monotonic() - reboot_started:.1f} с после перезагрузки"
        )

        remaining = max(0.0, deadline - time.monotonic())
        new_ip = client.wait_for_ip(
            previous=None,
            timeout=remaining,
            require_change=False,
            should_stop=self._stop_requested,
        )
        if self._stop_requested():
            return None, "Смена IP остановлена пользователем"
        if not new_ip:
            return None, "Модем восстановился, но WAN IP не получен"

        logger.info(f"WAN IP после перезагрузки: {new_ip}")
        self._refresh_route()
        return new_ip, ""

    def _wait_for_lte(
        self, client: ZTEClient, timeout: float
    ) -> Optional[ModemStatus]:
        deadline = time.monotonic() + max(0.0, timeout)
        last_network = ""
        while True:
            if self._stop_requested():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                status = client.status(timeout=min(2.0, max(0.3, remaining)))
            except ModemError as error:
                logger.debug(f"Ожидание LTE: статус модема недоступен: {error}")
            else:
                network = (status.network_type or "").upper()
                if "LTE" in network or "4G" in network:
                    return status
                last_network = status.network_type or "не определён"
                logger.debug(f"Ожидание регистрации LTE, сейчас: {last_network}")
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

        if last_network:
            logger.warning(
                f"LTE не зарегистрировался за {timeout:.0f} с; "
                f"последний стандарт: {last_network}"
            )
        return None

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

        if strategy == "fast":
            strategy_text = "только быстрое переподключение PDN (2-5 с)"
        elif strategy == "reboot":
            strategy_text = "сразу перезагрузка модема"
        else:
            strategy_text = (
                "быстрое переподключение PDN, при том же WAN IP — "
                "перезагрузка модема"
            )

        reboot_text = (
            f"да, до {self.max_reboots} раз"
            if self.reboot_on_fail and self.max_reboots > 0
            else "нет"
        )
        return (
            status.describe()
            + f"\nСтратегия смены WAN IP: {strategy_text}"
            + f"\nПерезагрузка при неудаче: {reboot_text}"
            + f"\nТаймаут восстановления после перезагрузки: {self.reboot_timeout} с"
            + (
                f"\nРежим привязки к модему: {self._route_mode}"
                if self._route_mode
                else "\nРежим привязки к модему: не проверялся"
            )
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._proxy is not None:
                self._proxy.stop()
                self._proxy = None


def preflight_modem(config, verify_egress: bool = True) -> str:
    """Проверить модем без запуска парсера (для GUI/CLI). Вернуть описание.

    verify_egress=False используется перед стартом из GUI: проверка выхода
    через модем может занимать несколько секунд (а при VPN — до таймаутов),
    поэтому её лучше выполнять в рабочем потоке парсера, а не в UI-потоке.
    """
    service = ModemService(config)
    try:
        status = service.probe()
        if verify_egress:
            service.check_egress()
        return (
            status.describe()
            + f"\nРежим привязки: {service.route_mode or 'не проверен'}"
        )
    finally:
        service.close()
