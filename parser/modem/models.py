"""Модели и исключения для работы с USB 4G модемом."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class ModemError(RuntimeError):
    """Базовая ошибка работы с модемом (понятное пользователю сообщение)."""


class ModemNotFoundError(ModemError):
    """Модем не найден: нет ни веб-интерфейса, ни подходящего сетевого адаптера."""


class ModemAuthError(ModemError):
    """Веб-интерфейс модема требует авторизацию."""


class ModemCommandError(ModemError):
    """Модем отклонил команду управления."""


class ModemTransportError(ModemCommandError):
    """Связь с модемом прервалась (например, во время перезагрузки)."""


@dataclass(frozen=True)
class NetAdapter:
    """Сетевой адаптер, через который модем раздаёт интернет."""

    name: str
    description: str = ""
    ipv4: Optional[str] = None
    gateway: Optional[str] = None
    ifindex: Optional[int] = None

    def __str__(self) -> str:
        parts = [self.name or "адаптер"]
        if self.description and self.description != self.name:
            parts.append(f"({self.description})")
        if self.ipv4:
            parts.append(f"IP {self.ipv4}")
        if self.gateway:
            parts.append(f"шлюз {self.gateway}")
        if self.ifindex is not None:
            parts.append(f"ifindex {self.ifindex}")
        return ", ".join(parts)


@dataclass(frozen=True)
class ModemEndpoint:
    """Найденная точка управления модемом."""

    gateway: str
    adapter: Optional[NetAdapter] = None
    auth_required: bool = False

    @property
    def local_ip(self) -> Optional[str]:
        return self.adapter.ipv4 if self.adapter else None

    @property
    def ifindex(self) -> Optional[int]:
        return self.adapter.ifindex if self.adapter else None


@dataclass
class ModemStatus:
    """Состояние модема, полученное из его CGI-API."""

    gateway: str
    wan_ip: Optional[str] = None
    connected: bool = False
    network_type: str = ""
    operator: str = ""
    signal: str = ""
    imei: str = ""
    model: str = ""
    firmware: str = ""
    local_ip: Optional[str] = None
    ifindex: Optional[int] = None
    raw: dict = field(default_factory=dict)

    def describe(self) -> str:
        lines = [
            f"Модем: {self.model or 'ZTE'} (IMEI: {self.imei or 'н/д'})",
            f"CGI-шлюз: {self.gateway}"
            + (f", локальный IP: {self.local_ip}" if self.local_ip else "")
            + (f", ifindex: {self.ifindex}" if self.ifindex is not None else ""),
            f"Оператор: {self.operator or 'н/д'}",
            f"Сеть: {self.network_type or 'н/д'}",
            f"Сигнал: {self.signal or 'н/д'}",
            f"WAN IP: {self.wan_ip or 'нет'}",
            f"Подключение: {'да' if self.connected else 'нет'}",
        ]
        return "\n".join(lines)


@dataclass
class RotationResult:
    """Результат смены IP."""

    ok: bool
    old_ip: Optional[str]
    new_ip: Optional[str]
    strategy: str = ""
    elapsed_ms: int = 0
    error: str = ""
