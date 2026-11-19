#!/usr/bin/env python
"""Headless-запуск парсера для автозагрузки Windows.

Запускается через pythonw.exe (без консольного окна), поэтому stdout/stderr
перенаправляются в logs/headless.log. Основной лог парсера — logs/app.log.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_log_stream = (LOG_DIR / "headless.log").open("a", encoding="utf-8", buffering=1)
sys.stdout = _log_stream
sys.stderr = _log_stream


def main() -> int:
    print(f"\n=== AvitoParser headless start {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print(f"CWD: {Path.cwd()}")
    print(f"Python: {sys.executable}")
    try:
        from parser_cls import run_forever

        run_forever()
    except KeyboardInterrupt:
        print("Остановлено пользователем")
        return 0
    except BaseException:
        traceback.print_exc()
        return 1
    finally:
        print(f"=== AvitoParser headless stop {datetime.now():%Y-%m-%d %H:%M:%S} ===")
        _log_stream.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
