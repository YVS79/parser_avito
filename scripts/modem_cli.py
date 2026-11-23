#!/usr/bin/env python
"""Консольная диагностика USB 4G модема ZTE MF823D.

Примеры:
    python scripts/modem_cli.py --status
    python scripts/modem_cli.py --rotate
    python scripts/modem_cli.py --rotate --strategy fast_then_reboot
    python scripts/modem_cli.py --rotate --repeat 3 --delay 2
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dto import AvitoConfig  # noqa: E402
from load_config import load_avito_config  # noqa: E402
from parser.modem import ModemService  # noqa: E402


def _load_config(path: str) -> AvitoConfig:
    try:
        config = load_avito_config(path)
    except Exception as error:
        print(f"Не удалось прочитать {path}: {error}")
        config = AvitoConfig(urls=[])
    config.use_usb_modem = True
    return config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверка и смена IP USB 4G модема (ZTE MF823D)"
    )
    parser.add_argument("--config", default="config.toml", help="путь к config.toml")
    parser.add_argument("--gateway", default=None, help="адрес модема, например 192.168.4.1")
    parser.add_argument("--password", default=None, help="пароль веб-интерфейса модема")
    parser.add_argument(
        "--strategy",
        choices=("fast_then_reboot", "fast", "reboot"),
        default=None,
        help="способ смены IP",
    )
    parser.add_argument("--rotate", action="store_true", help="сменить IP")
    parser.add_argument("--repeat", type=int, default=1, help="сколько раз сменить IP")
    parser.add_argument("--delay", type=float, default=2.0, help="пауза между сменами, сек")
    parser.add_argument("--status", action="store_true", help="показать состояние (по умолчанию)")
    args = parser.parse_args()

    config = _load_config(args.config)
    if args.gateway:
        config.modem_gateway = args.gateway
    if args.password is not None:
        config.modem_password = args.password
    if args.strategy:
        config.modem_rotation_strategy = args.strategy
    service = ModemService(config)
    try:
        if not args.rotate:
            print(service.describe())
            return 0

        exit_code = 0
        for attempt in range(1, max(1, args.repeat) + 1):
            print(f"\n--- Смена IP {attempt}/{max(1, args.repeat)} ---")
            result = service.rotate(reason="cli")
            print(
                f"ok={result.ok} strategy={result.strategy} "
                f"{result.old_ip} -> {result.new_ip} "
                f"({result.elapsed_ms / 1000:.1f} с)"
            )
            if result.error:
                print(f"Ошибка: {result.error}")
            if not result.ok:
                exit_code = 1
                break
            if attempt < args.repeat:
                time.sleep(max(0.0, args.delay))

        print("\nИтоговое состояние:")
        print(service.describe())
        return exit_code
    except Exception as error:
        print(f"Ошибка модема: {error}")
        return 2
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
