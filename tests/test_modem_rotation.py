"""Тесты стратегий смены IP USB-модема (без реального оборудования)."""
from __future__ import annotations

from dto import AvitoConfig
from parser.modem.models import ModemStatus
from parser.modem.service import ModemService, alternate_mode


class FakeModem:
    def __init__(self, ip_queue, initial="100.64.0.1"):
        self.current = initial
        self.queue = list(ip_queue)
        self.modes: list[str] = []
        self.commands: list[str] = []
        self.rebooted = False

    def wan_ip(self, timeout=None):
        return self.current

    def status(self, timeout=None):
        return ModemStatus(
            gateway="192.168.0.1",
            wan_ip=self.current,
            connected=bool(self.current),
            network_type="LTE",
            operator="Test",
        )

    def disconnect(self):
        self.commands.append("disconnect")
        self.current = None

    def connect(self):
        self.commands.append("connect")
        if self.queue:
            self.current = self.queue.pop(0)
        elif self.current is None:
            self.current = "100.64.0.99"

    def wait_for_disconnect(self, timeout=1.0, interval=0.1):
        return True

    def wait_for_ip(
        self,
        previous=None,
        timeout=8.0,
        require_change=True,
        interval=0.2,
    ):
        if not self.current:
            return None
        if not require_change or previous is None or self.current != previous:
            return self.current
        return None

    def set_bearer(self, mode):
        self.modes.append(mode)

    def reboot(self):
        self.rebooted = True
        self.current = "100.64.0.200"

    def close(self):
        pass


def _config(**kwargs):
    defaults = dict(
        urls=[],
        use_usb_modem=True,
        modem_rotation_strategy="fast_then_mode",
        modem_preferred_mode="NETWORK_auto",
        modem_fast_timeout=2,
        modem_mode_timeout=2,
        modem_fast_attempts=2,
    )
    defaults.update(kwargs)
    return AvitoConfig(**defaults)


def test_alternate_mode_opposite_to_preferred():
    assert alternate_mode("NETWORK_auto") == "Only_WCDMA"
    assert alternate_mode("Only_LTE") == "Only_WCDMA"
    assert alternate_mode("Only_WCDMA") == "Only_LTE"
    assert alternate_mode("Only_GSM") == "Only_LTE"
    assert alternate_mode("WCDMA_AND_LTE") == "Only_WCDMA"
    assert alternate_mode("WCDMA_preferred") == "Only_LTE"
    assert alternate_mode("GSM_preferred") == "Only_LTE"


def test_fast_strategy_changes_ip_quickly():
    modem = FakeModem(["100.64.0.2"])
    service = ModemService(_config(modem_rotation_strategy="fast"), client=modem)

    result = service.rotate("test")

    assert result.ok is True
    assert result.old_ip == "100.64.0.1"
    assert result.new_ip == "100.64.0.2"
    assert result.strategy == "fast"
    assert modem.modes == []


def test_fast_only_fails_when_operator_keeps_same_ip():
    modem = FakeModem(["100.64.0.1", "100.64.0.1"])
    service = ModemService(_config(modem_rotation_strategy="fast"), client=modem)

    result = service.rotate("test")

    assert result.ok is False
    assert result.old_ip == "100.64.0.1"
    assert modem.commands.count("connect") == 2


def test_fast_then_mode_falls_back_to_network_mode_change():
    modem = FakeModem(
        ["100.64.0.1", "100.64.0.1", "100.64.0.50", "100.64.0.60"]
    )
    service = ModemService(
        _config(
            modem_rotation_strategy="fast_then_mode",
            modem_preferred_mode="NETWORK_auto",
            modem_fast_attempts=2,
        ),
        client=modem,
    )

    result = service.rotate("test")

    assert result.ok is True
    assert result.strategy == "mode"
    assert result.new_ip == "100.64.0.60"
    assert modem.modes == ["Only_WCDMA", "NETWORK_auto"]


def test_mode_strategy_restores_preferred_lte():
    modem = FakeModem(["100.64.0.50", "100.64.0.60"])
    service = ModemService(
        _config(
            modem_rotation_strategy="mode",
            modem_preferred_mode="Only_LTE",
        ),
        client=modem,
    )

    result = service.rotate("test")

    assert result.ok is True
    assert modem.modes == ["Only_WCDMA", "Only_LTE"]
    assert result.new_ip == "100.64.0.60"


def test_mode_strategy_returns_first_ip_if_restore_fails():
    modem = FakeModem(["100.64.0.50", None])
    service = ModemService(
        _config(
            modem_rotation_strategy="mode",
            modem_preferred_mode="Only_LTE",
        ),
        client=modem,
    )

    # После первого подключения модем не отдал адрес при восстановлении
    # режима — сервис должен вернуть уже полученный новый IP.
    result = service.rotate("test")

    assert result.ok is True
    assert result.new_ip == "100.64.0.50"


def test_rotation_result_measures_time():
    modem = FakeModem(["100.64.0.2"])
    service = ModemService(_config(modem_rotation_strategy="fast"), client=modem)

    result = service.rotate("block")

    assert result.elapsed_ms >= 0
    assert result.ok is True
