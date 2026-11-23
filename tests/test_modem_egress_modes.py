"""Проверка строгого выбора режима выхода через модем (без сети)."""
from __future__ import annotations

import pytest

from dto import AvitoConfig
from parser.modem import service as service_module
from parser.modem.models import ModemEndpoint, ModemError, NetAdapter


class _FakeSocket:
    def __init__(self, source_ip: str):
        self.source_ip = source_ip
        self.closed = False

    def getsockname(self):
        return (self.source_ip, 40000)

    def close(self):
        self.closed = True


class _FakeBinder:
    succeed_mode = "bind_then_iface"
    calls: list[str] = []

    def __init__(self, ifindex=None, local_ip=None, interface_name=None, mode="iface_then_bind"):
        self.ifindex = ifindex
        self.local_ip = local_ip
        self.interface_name = interface_name
        self.mode = mode

    def connect(self, host, port, timeout=15.0):
        _FakeBinder.calls.append(self.mode)
        if self.succeed_mode is None or self.mode != self.succeed_mode:
            raise OSError(f"mode {self.mode} failed")
        return _FakeSocket(self.local_ip or "192.168.4.202")


def _endpoint():
    return ModemEndpoint(
        gateway="192.168.4.1",
        adapter=NetAdapter(
            name="Ethernet 3",
            ipv4="192.168.4.202",
            gateway="192.168.4.1",
            ifindex=20,
        ),
    )


def test_select_egress_mode_uses_first_working_mode(monkeypatch):
    _FakeBinder.calls = []
    _FakeBinder.succeed_mode = "bind_then_iface"
    monkeypatch.setattr(service_module, "RouteBinder", _FakeBinder)

    service = service_module.ModemService(
        AvitoConfig(urls=[], use_usb_modem=True),
        endpoint=_endpoint(),
    )
    try:
        mode = service.check_egress()
    finally:
        service.close()

    assert mode == "bind_then_iface"
    assert service.route_mode == "bind_then_iface"
    assert _FakeBinder.calls[-2:] == ["iface_then_bind", "bind_then_iface"]


def test_select_egress_mode_fails_closed(monkeypatch):
    _FakeBinder.calls = []
    _FakeBinder.succeed_mode = None
    monkeypatch.setattr(service_module, "RouteBinder", _FakeBinder)

    service = service_module.ModemService(
        AvitoConfig(urls=[], use_usb_modem=True),
        endpoint=_endpoint(),
    )
    try:
        with pytest.raises(ModemError) as error:
            service.check_egress()
    finally:
        service.close()

    assert "ни один режим" in str(error.value)
    assert service.route_mode is None
