"""Descarga datos históricos a data/.

EJECUTAR DESDE LA TERMINAL DEL MAC (necesita acceso a internet sin proxy):

    cd ~/Desktop/crypto-bot
    source .venv/bin/activate
    python scripts/fetch_data.py

Descarga ~2 años de velas 1h y 4h de BTC, ETH y SOL desde Binance futures
(histórico profundo) y el funding rate de Hyperliquid (para modelar su coste).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import data
from bot.config import UNIVERSE

if __name__ == "__main__":
    for sym in UNIVERSE.symbols:
        for tf in UNIVERSE.timeframes:
            print(f"Descargando {sym} {tf} ({UNIVERSE.history_days} días)…")
            df = data.fetch_binance(sym, tf, UNIVERSE.history_days)
            p = data.save(df, sym, tf)
            print(f"  -> {p.name}: {len(df)} velas, {df.timestamp.min()} … {df.timestamp.max()}")
        try:
            f = data.fetch_hl_funding(sym, 90)
            fp = data.DATA_DIR / f"{sym}_funding.csv"
            f.to_csv(fp, index=False)
            print(f"  -> {fp.name}: {len(f)} registros de funding (Hyperliquid, 90 días)")
        except Exception as e:  # noqa: BLE001
            print(f"  ! funding {sym} no descargado: {e}")
    print("\nListo. Ahora ejecuta: python scripts/run_backtest.py")
