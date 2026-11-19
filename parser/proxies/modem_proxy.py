"""Прокси-провайдер для работы парсера через USB 4G модем."""
from __future__ import annotations

import threading
from typing import Optional

from loguru import logger

from parser.modem.service import ModemService
from parser.proxies.proxy import Proxy


class UsbModemProxy(Proxy):
    """Направляет трафик парсера в модем и меняет IP при блокировке.

    В отличие от обычного прокси, здесь нет внешнего адреса: парсер ходит
    через локальный прокси (127.0.0.1), а тот привязывает каждый исходящий
    сокет к интерфейсу USB-модема и управляет модемом по CGI-API.
    """

    def __init__(self, config):
        self.config = config
        self._service: Optional[ModemService] = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> ModemService:
        if self._service is not None:
            return self._service
        with self._lock:
            if self._service is None:
                service = ModemService(self.config)
                try:
                    url = service.start()
                except Exception:
                    service.close()
                    raise
                logger.info(f"Трафик парсера направлен через USB-модем ({url})")
                self._service = service
            return self._service

    def get_httpx_proxy(self) -> str:
        return self._ensure_started().proxy_url

    def handle_block(self) -> None:
        self._ensure_started().rotate(reason="HTTP-блокировка Avito")

    def status_text(self) -> str:
        if self._service is None:
            return "Модем ещё не инициализирован"
        return self._service.describe()

    def close(self) -> None:
        with self._lock:
            service = self._service
            self._service = None
        if service is not None:
            service.close()
