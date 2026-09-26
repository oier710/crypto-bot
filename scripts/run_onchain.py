"""Bot on-chain (Uniswap en Arbitrum).

    python scripts/run_onchain.py --dry-run    # muestra balances, señales y qué haría; no firma nada
    python scripts/run_onchain.py --once       # un ciclo diario real
    python scripts/run_onchain.py --loop       # cada hora comprueba stops; a las 00:05 UTC ciclo diario
"""
import argparse
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import onchain  # noqa: E402

DAILY_HOUR_UTC = 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--once", action="store_true", help="un ciclo diario completo (señales + stops)")
    g.add_argument("--stops", action="store_true", help="solo comprobar stops (para ejecución horaria externa)")
    g.add_argument("--auto", action="store_true", help="stops siempre; ciclo diario solo si es la hora 00 UTC (para cron)")
    g.add_argument("--loop", action="store_true")
    a = ap.parse_args()
    if a.stops:
        onchain.run_cycle(dry_run=False, signals=False)
        return
    if a.auto:
        daily = datetime.now(timezone.utc).hour == DAILY_HOUR_UTC
        onchain.run_cycle(dry_run=False, signals=daily)
        return
    if not a.loop:
        onchain.run_cycle(dry_run=a.dry_run, signals=True)
        return
    onchain.log.info("Modo loop: stops cada hora, señales a las %02d:05 UTC", DAILY_HOUR_UTC)
    last_daily = None
    while True:
        now = datetime.now(timezone.utc)
        try:
            do_daily = now.hour == DAILY_HOUR_UTC and last_daily != now.date()
            onchain.run_cycle(dry_run=False, signals=do_daily)
            if do_daily:
                last_daily = now.date()
        except Exception as e:  # noqa: BLE001
            onchain.log.exception("ciclo fallido: %s", e)
        # dormir hasta el minuto 05 de la próxima hora
        nxt = now.replace(minute=5, second=0, microsecond=0)
        while nxt <= datetime.now(timezone.utc):
            nxt += timedelta(hours=1)
        time.sleep(max((nxt - datetime.now(timezone.utc)).total_seconds(), 60))


if __name__ == "__main__":
    main()
