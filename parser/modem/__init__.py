"""Пакет поддержки USB 4G модемов (ZTE MF823D и совместимые)."""
from .models import (
    ModemAuthError,
    ModemCommandError,
    ModemEndpoint,
    ModemError,
    ModemNotFoundError,
    ModemStatus,
    NetAdapter,
    RotationResult,
)
from .service import ModemService, alternate_mode, preflight_modem

__all__ = [
    "ModemAuthError",
    "ModemCommandError",
    "ModemEndpoint",
    "ModemError",
    "ModemNotFoundError",
    "ModemService",
    "ModemStatus",
    "NetAdapter",
    "RotationResult",
    "alternate_mode",
    "preflight_modem",
]
