"""Ejecuta el bot en Hyperliquid.

    python scripts/run_live.py --dry-run   # calcula y muestra, no envía órdenes
    python scripts/run_live.py --once      # un ciclo real y termina
    python scripts/run_live.py --loop      # espera a las 00:05 UTC de cada día y ejecuta un ciclo

La red (testnet/mainnet) y las claves se leen de .env
"""
import argparse
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import live  # noqa: E402

RUN_AT_UTC = (0, 5)  # 00:05 UTC: la vela diaria ya ha cerrado


def seconds_until_next_run() -> float:
    now = datetime.now(timezone.utc)
    nxt = now.replace(hour=RUN_AT_UTC[0], minute=RUN_AT_UTC[1], second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    a = ap.parse_args()

    if a.loop:
        live.log.info("Modo loop: siguiente ciclo a las %02d:%02d UTC", *RUN_AT_UTC)
        while True:
            time.sleep(seconds_until_next_run())
            try:
                live.run_cycle(dry_run=False)
            except Exception as e:  # noqa: BLE001
                live.log.exception("ciclo fallido: %s", e)
            time.sleep(120)
    else:
        live.run_cycle(dry_run=a.dry_run)
