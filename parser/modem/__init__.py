"""Пакет поддержки USB 4G модемов (ZTE MF823D и совместимые)."""
from .models import (
    ModemAuthError,
    ModemCommandError,
    ModemEndpoint,
    ModemError,
    ModemNotFoundError,
    ModemStatus,
    ModemTransportError,
    NetAdapter,
    RotationResult,
)
from .service import ModemService, preflight_modem

__all__ = [
    "ModemAuthError",
    "ModemCommandError",
    "ModemEndpoint",
    "ModemError",
    "ModemNotFoundError",
    "ModemService",
    "ModemStatus",
    "ModemTransportError",
    "NetAdapter",
    "RotationResult",
    "preflight_modem",
]
