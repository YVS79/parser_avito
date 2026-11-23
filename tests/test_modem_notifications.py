"""Уведомления о блокировках и ошибках модема."""
from __future__ import annotations

from unittest.mock import MagicMock

from dto import AvitoConfig
from parser.modem.models import RotationResult
from parser.proxies.modem_proxy import UsbModemProxy


def _proxy(events):
    proxy = UsbModemProxy(
        AvitoConfig(urls=[], use_usb_modem=True),
        notify_event=events.append,
    )
    proxy._service = MagicMock()
    return proxy


def test_rotation_failure_is_sent_as_push():
    events = []
    proxy = _proxy(events)
    proxy._service.rotate.return_value = RotationResult(
        ok=False,
        old_ip="100.1.1.1",
        new_ip=None,
        strategy="fast_then_reboot",
        elapsed_ms=1000,
        error="оператор вернул тот же IP",
    )

    proxy.handle_block()

    assert len(events) == 1
    assert "Не удалось сменить WAN IP" in events[0]
    assert "оператор вернул тот же IP" in events[0]


def test_successful_rotation_is_sent_as_push():
    events = []
    proxy = _proxy(events)
    proxy._service.rotate.return_value = RotationResult(
        ok=True,
        old_ip="100.1.1.1",
        new_ip="100.2.2.2",
        strategy="reboot",
        elapsed_ms=1500,
    )

    proxy.handle_block()

    assert len(events) == 1
    assert "WAN IP модема изменён" in events[0]
    assert "100.1.1.1" in events[0]
    assert "100.2.2.2" in events[0]
