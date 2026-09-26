"""Descarga y carga de velas OHLCV.

- Histórico largo: Binance USDⓈ-M futures (perpetuos), API pública sin clave.
  Precios prácticamente idénticos a Hyperliquid para BTC/ETH/SOL.
- Reciente / live: Hyperliquid info endpoint (candleSnapshot).

Formato CSV unificado: timestamp (UTC, ms), open, high, low, close, volume
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

BINANCE_FAPI = "https://fapi.binance.com/fapi/v1/klines"
HL_INFO = {
    "mainnet": "https://api.hyperliquid.xyz/info",
    "testnet": "https://api.hyperliquid-testnet.xyz/info",
}

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
COLS = ["timestamp", "open", "high", "low", "close", "volume"]


def csv_path(symbol: str, timeframe: str) -> Path:
    return DATA_DIR / f"{symbol}_{timeframe}.csv"


# ---------------------------------------------------------------- Binance ---
def fetch_binance(symbol: str, timeframe: str, days: int, verbose: bool = True) -> pd.DataFrame:
    pair = f"{symbol}USDT"
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    step = _TF_MS[timeframe]
    rows: list[list] = []
    cur = start
    while cur < end:
        params = dict(symbol=pair, interval=timeframe, startTime=cur, limit=1500)
        r = requests.get(BINANCE_FAPI, params=params, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend([[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in batch])
        cur = batch[-1][0] + step
        if verbose:
            print(f"  {pair} {timeframe}: {len(rows)} velas", end="\r")
        time.sleep(0.15)  # respetar rate limit
    if verbose:
        print()
    df = pd.DataFrame(rows, columns=COLS).drop_duplicates("timestamp").sort_values("timestamp")
    return df.reset_index(drop=True)


# ------------------------------------------------------------ Hyperliquid ---
def fetch_hyperliquid(symbol: str, timeframe: str, days: int, network: str = "mainnet") -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    payload = {"type": "candleSnapshot", "req": {"coin": symbol, "interval": timeframe, "startTime": start, "endTime": end}}
    r = requests.post(HL_INFO[network], json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    rows = [[int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"])] for c in data]
    return pd.DataFrame(rows, columns=COLS).sort_values("timestamp").reset_index(drop=True)


def fetch_hl_funding(symbol: str, days: int, network: str = "mainnet") -> pd.DataFrame:
    """Histórico de funding rate (por hora) de Hyperliquid."""
    start = int(time.time() * 1000) - days * 86_400_000
    payload = {"type": "fundingHistory", "coin": symbol, "startTime": start}
    r = requests.post(HL_INFO[network], json=payload, timeout=30)
    r.raise_for_status()
    rows = [[int(x["time"]), float(x["fundingRate"]), float(x["premium"])] for x in r.json()]
    return pd.DataFrame(rows, columns=["timestamp", "funding_rate", "premium"]).sort_values("timestamp")


# ------------------------------------------------------------------ disco ---
def save(df: pd.DataFrame, symbol: str, timeframe: str) -> Path:
    p = csv_path(symbol, timeframe)
    df.to_csv(p, index=False)
    return p


def load(symbol: str, timeframe: str) -> pd.DataFrame:
    p = csv_path(symbol, timeframe)
    if not p.exists():
        raise FileNotFoundError(f"No hay datos en {p}. Ejecuta scripts/fetch_data.py desde tu terminal.")
    df = pd.read_csv(p)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("dt")
