"""Тесты автопоиска модема и выбора прокси-провайдера."""
from __future__ import annotations

import json

import pytest

from dto import AvitoConfig
from parser.modem import discovery
from parser.modem.models import ModemNotFoundError, NetAdapter
from parser.proxies.proxy import MobileProxy, NoProxy, ServerProxy
from parser.proxies.proxy_factory import build_proxy


def test_windows_adapters_parse_powershell_json(monkeypatch):
    payload = json.dumps(
        [
            {
                "ifindex": 12,
                "name": "Ethernet 2",
                "description": "ZTE Mobile Broadband Device",
                "ipv4": "192.168.0.100",
                "gateway": "192.168.0.1",
            }
        ]
    )
    monkeypatch.setattr(discovery, "_run", lambda *args, **kwargs: payload)

    adapters = discovery._windows_adapters()

    assert len(adapters) == 1
    assert adapters[0].ifindex == 12
    assert adapters[0].ipv4 == "192.168.0.100"
    assert adapters[0].gateway == "192.168.0.1"


def test_find_modem_endpoint_prefers_manual_gateway(monkeypatch):
    adapter = NetAdapter(
        name="Ethernet 2",
        description="ZTE Mobile Broadband Device",
        ipv4="192.168.0.100",
        gateway="192.168.0.1",
        ifindex=12,
    )
    monkeypatch.setattr(discovery, "list_adapters", lambda: [adapter])
    monkeypatch.setattr(discovery, "_probe_zte", lambda gateway, timeout=1.5: (True, False))
    discovery.clear_cache()

    config = AvitoConfig(urls=[], use_usb_modem=True, modem_gateway="192.168.0.1")
    endpoint = discovery.find_modem_endpoint(config)

    assert endpoint.gateway == "192.168.0.1"
    assert endpoint.local_ip == "192.168.0.100"
    assert endpoint.ifindex == 12


def test_find_modem_endpoint_raises_helpful_error(monkeypatch):
    monkeypatch.setattr(discovery, "list_adapters", lambda: [])
    monkeypatch.setattr(discovery, "_probe_zte", lambda gateway, timeout=1.5: (False, False))
    discovery.clear_cache()

    config = AvitoConfig(urls=[], use_usb_modem=True)
    with pytest.raises(ModemNotFoundError) as error:
        discovery.find_modem_endpoint(config)

    assert "USB 4G модем" in str(error.value)


def test_proxy_factory_uses_usb_modem_when_enabled():
    config = AvitoConfig(
        urls=[],
        use_usb_modem=True,
        proxy_string="user:pass@proxy.site:1000",
    )
    proxy = build_proxy(config)
    assert proxy.__class__.__name__ == "UsbModemProxy"
    # Конструктор не должен ходить в сеть и искать модем.
    assert proxy._service is None


def test_proxy_factory_keeps_legacy_behaviour():
    assert isinstance(build_proxy(AvitoConfig(urls=[])), NoProxy)
    assert isinstance(
        build_proxy(
            AvitoConfig(
                urls=[],
                proxy_string="user:pass@proxy.site:1000",
                proxy_change_url="https://change.example",
            )
        ),
        MobileProxy,
    )
    assert isinstance(
        build_proxy(
            AvitoConfig(urls=[], proxy_string="user:pass@proxy.site:1000")
        ),
        ServerProxy,
    )


def test_match_adapter_by_interface_index():
    adapters = [
        NetAdapter(name="Wi-Fi", ipv4="192.168.1.10", gateway="192.168.1.1", ifindex=5),
        NetAdapter(
            name="Ethernet 2",
            ipv4="192.168.0.100",
            gateway="192.168.0.1",
            ifindex=12,
        ),
    ]
    matched = discovery._match_adapter(adapters, interface="12")
    assert matched is not None
    assert matched.gateway == "192.168.0.1"


def test_find_modem_endpoint_allows_auth_when_password_set(monkeypatch):
    adapter = NetAdapter(
        name="Ethernet 2",
        ipv4="192.168.0.100",
        gateway="192.168.0.1",
        ifindex=12,
    )
    monkeypatch.setattr(discovery, "list_adapters", lambda: [adapter])
    monkeypatch.setattr(discovery, "_probe_zte", lambda gateway, timeout=1.5: (False, True))
    discovery.clear_cache()

    config = AvitoConfig(
        urls=[],
        use_usb_modem=True,
        modem_gateway="192.168.0.1",
        modem_password="admin",
    )
    endpoint = discovery.find_modem_endpoint(config)

    assert endpoint.gateway == "192.168.0.1"
    assert endpoint.auth_required is True
    assert endpoint.local_ip == "192.168.0.100"


def test_gateway_url_is_normalized(monkeypatch):
    monkeypatch.setattr(discovery, "list_adapters", lambda: [])
    monkeypatch.setattr(discovery, "_probe_zte", lambda gateway, timeout=1.5: (True, False))
    discovery.clear_cache()

    config = AvitoConfig(
        urls=[],
        use_usb_modem=True,
        modem_gateway="http://192.168.0.1/",
    )
    endpoint = discovery.find_modem_endpoint(config)
    assert endpoint.gateway == "192.168.0.1"
