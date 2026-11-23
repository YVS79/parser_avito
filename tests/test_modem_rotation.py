"""Тесты стратегий смены WAN IP USB-модема (без реального оборудования).

Проверяются только безопасные сценарии: быстрое переподключение PDN и
перезагрузка модема. Переход на 3G/Only_WCDMA не используется.
"""
from __future__ import annotations

import threading

import pytest

from dto import AvitoConfig
from parser.modem.models import ModemCommandError, ModemStatus
from parser.modem.service import ModemService


class FakeModem:
    def __init__(
        self,
        fast_ips=None,
        reboot_ips=None,
        initial="100.64.0.1",
        network_type="LTE",
    ):
        self.current = initial
        self.fast_ips = list(fast_ips or [])
        self.reboot_ips = list(reboot_ips or [])
        self.commands: list[str] = []
        self.modes: list[str] = []
        self.rebooted = False
        self.reboot_count = 0
        self.reboot_error = None
        self.gateway_timeout = False
        self.network_type = network_type
        self.on_reboot = None
        self.block_disconnect: threading.Event | None = None
        self.disconnect_started = threading.Event()

    def wan_ip(self, timeout=None):
        return self.current

    def status(self, timeout=None):
        if self.gateway_timeout:
            raise ModemCommandError("gateway is down")
        return ModemStatus(
            gateway="192.168.4.1",
            wan_ip=self.current,
            connected=bool(self.current),
            network_type=self.network_type,
            operator="MTS",
        )

    def disconnect(self):
        self.disconnect_started.set()
        if self.block_disconnect is not None:
            self.block_disconnect.wait(timeout=5)
        self.commands.append("disconnect")
        self.current = None

    def connect(self):
        self.commands.append("connect")
        self.current = self.fast_ips.pop(0) if self.fast_ips else None

    def wait_for_disconnect(self, timeout=1.0, interval=0.1, should_stop=None):
        if should_stop is not None and should_stop():
            return False
        return self.current is None

    def wait_for_ip(
        self,
        previous=None,
        timeout=8.0,
        require_change=True,
        interval=0.2,
        should_stop=None,
    ):
        if should_stop is not None and should_stop():
            return None
        if not self.current:
            return None
        if not require_change or previous is None or self.current != previous:
            return self.current
        return None

    def wait_for_cgi(self, timeout=120.0, interval=1.0, should_stop=None):
        if should_stop is not None and should_stop():
            return False
        return not self.gateway_timeout

    def reboot(self):
        if self.reboot_error is not None:
            raise self.reboot_error
        self.rebooted = True
        self.reboot_count += 1
        self.commands.append("reboot")
        if self.on_reboot is not None:
            self.on_reboot()
        self.current = self.reboot_ips.pop(0) if self.reboot_ips else None
        return True

    def set_bearer(self, mode):
        # Сервис не должен управлять стандартом сети при смене IP.
        self.modes.append(mode)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def no_fast_settle_delay(monkeypatch):
    monkeypatch.setattr("parser.modem.service._FAST_SETTLE_SECONDS", 0.0)


def _config(**kwargs):
    defaults = dict(
        urls=[],
        use_usb_modem=True,
        modem_rotation_strategy="fast_then_reboot",
        modem_fast_timeout=2,
        modem_fast_attempts=1,
        modem_reboot_on_fail=True,
        modem_reboot_timeout=1,
        modem_max_reboots=1,
    )
    defaults.update(kwargs)
    return AvitoConfig(**defaults)


def _service(modem, **kwargs):
    return ModemService(_config(**kwargs), client=modem)


def test_fast_strategy_changes_ip_quickly():
    modem = FakeModem(fast_ips=["100.64.0.2"])
    service = _service(modem, modem_rotation_strategy="fast")

    result = service.rotate("test")

    assert result.ok is True
    assert result.old_ip == "100.64.0.1"
    assert result.new_ip == "100.64.0.2"
    assert result.strategy == "fast"
    assert modem.rebooted is False
    assert modem.modes == []


def test_fast_only_fails_when_operator_keeps_same_ip():
    modem = FakeModem(fast_ips=["100.64.0.1", "100.64.0.1"])
    service = _service(
        modem, modem_rotation_strategy="fast", modem_fast_attempts=2
    )

    result = service.rotate("test")

    assert result.ok is False
    assert result.old_ip == "100.64.0.1"
    assert modem.commands.count("connect") == 2
    assert modem.rebooted is False
    assert "не изменило WAN IP" in result.error


def test_fast_then_reboot_gets_new_ip_after_reboot():
    modem = FakeModem(
        fast_ips=["100.64.0.1"], reboot_ips=["100.64.0.200"]
    )
    service = _service(modem, modem_rotation_strategy="fast_then_reboot")

    result = service.rotate("test")

    assert result.ok is True
    assert result.strategy == "reboot"
    assert result.new_ip == "100.64.0.200"
    assert modem.reboot_count == 1
    assert modem.modes == []


def test_reboot_same_ip_is_not_success():
    modem = FakeModem(
        fast_ips=["100.64.0.1"], reboot_ips=["100.64.0.1"]
    )
    service = _service(modem, modem_rotation_strategy="fast_then_reboot")

    result = service.rotate("test")

    assert result.ok is False
    assert modem.reboot_count == 1
    assert "тот же WAN IP" in result.error


def test_gateway_not_recovered_after_reboot_fails():
    modem = FakeModem(
        fast_ips=["100.64.0.1"], reboot_ips=["100.64.0.200"]
    )
    modem.gateway_timeout = True
    service = _service(modem, modem_rotation_strategy="fast_then_reboot")

    result = service.rotate("test")

    assert result.ok is False
    assert "CGI-шлюз не восстановился" in result.error
    assert modem.reboot_count == 1


def test_modem_recovered_without_wan_ip_is_failure():
    modem = FakeModem(fast_ips=["100.64.0.1"], reboot_ips=[None])
    service = _service(modem, modem_rotation_strategy="fast_then_reboot")

    result = service.rotate("test")

    assert result.ok is False
    assert "WAN IP не получен" in result.error
    assert result.new_ip is None


def test_modem_recovered_in_lte_is_success():
    modem = FakeModem(
        fast_ips=["100.64.0.1"],
        reboot_ips=["100.64.0.201"],
        network_type="LTE",
    )
    service = _service(modem, modem_rotation_strategy="fast_then_reboot")

    result = service.rotate("test")

    assert result.ok is True
    assert modem.network_type == "LTE"
    assert result.new_ip == "100.64.0.201"


def test_modem_recovered_without_lte_is_failure():
    modem = FakeModem(
        fast_ips=["100.64.0.1"],
        reboot_ips=["100.64.0.202"],
        network_type="WCDMA",
    )
    service = _service(modem, modem_rotation_strategy="fast_then_reboot")

    result = service.rotate("test")

    assert result.ok is False
    assert "не зарегистрировался в LTE" in result.error


def test_reboot_command_error_is_reported():
    modem = FakeModem(
        fast_ips=["100.64.0.1"], reboot_ips=["100.64.0.203"]
    )
    modem.reboot_error = ModemCommandError("REBOOT_DEVICE rejected")
    service = _service(modem, modem_rotation_strategy="fast_then_reboot")

    result = service.rotate("test")

    assert result.ok is False
    assert "REBOOT_DEVICE" in result.error
    assert modem.reboot_count == 0


def test_stop_event_aborts_rotation_before_start():
    stop_event = threading.Event()
    stop_event.set()
    modem = FakeModem(fast_ips=["100.64.0.2"])
    service = ModemService(_config(), client=modem, stop_event=stop_event)

    result = service.rotate("test")

    assert result.ok is False
    assert "остановлена" in result.error
    assert modem.commands == []


def test_stop_event_aborts_rotation_during_reboot():
    stop_event = threading.Event()
    modem = FakeModem(
        fast_ips=["100.64.0.1"], reboot_ips=["100.64.0.204"]
    )
    modem.on_reboot = stop_event.set
    service = ModemService(_config(), client=modem, stop_event=stop_event)

    result = service.rotate("test")

    assert result.ok is False
    assert "остановлена" in result.error
    assert modem.reboot_count == 1


def test_concurrent_rotation_is_rejected():
    modem = FakeModem(fast_ips=["100.64.0.2"])
    modem.block_disconnect = threading.Event()
    service = _service(modem, modem_rotation_strategy="fast")
    first_result = []

    def run_first():
        first_result.append(service.rotate("first"))

    thread = threading.Thread(target=run_first)
    thread.start()
    assert modem.disconnect_started.wait(timeout=2) is True

    second = service.rotate("second")
    assert second.ok is False
    assert second.strategy == "busy"
    assert "уже выполняется" in second.error

    modem.block_disconnect.set()
    thread.join(timeout=5)
    assert first_result and first_result[0].ok is True


def test_legacy_strategy_is_mapped_without_3g():
    modem = FakeModem(fast_ips=["100.64.0.2"])
    service = _service(modem, modem_rotation_strategy="fast_then_mode")

    assert service.strategy == "fast_then_reboot"
    result = service.rotate("test")

    assert result.ok is True
    assert modem.modes == []


def test_reboot_can_be_disabled():
    modem = FakeModem(fast_ips=["100.64.0.1"])
    service = _service(
        modem,
        modem_rotation_strategy="fast_then_reboot",
        modem_reboot_on_fail=False,
    )

    result = service.rotate("test")

    assert result.ok is False
    assert modem.reboot_count == 0
    assert "перезагрузка отключена" in result.error


def test_rotation_result_measures_time():
    modem = FakeModem(fast_ips=["100.64.0.2"])
    service = _service(modem, modem_rotation_strategy="fast")

    result = service.rotate("block")

    assert result.elapsed_ms >= 0
    assert result.ok is True


def test_max_reboots_limits_attempts():
    modem = FakeModem(
        fast_ips=["100.64.0.1"],
        reboot_ips=["100.64.0.1", "100.64.0.1"],
    )
    service = _service(
        modem,
        modem_rotation_strategy="fast_then_reboot",
        modem_max_reboots=2,
    )

    result = service.rotate("test")

    assert result.ok is False
    assert modem.reboot_count == 2
    assert "тот же WAN IP" in result.error
