"""Descarga y carga de velas OHLCV.

- Histórico largo: Coinbase Exchange (spot), API pública sin clave, accesible desde EE.UU.
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

HL_INFO = {
    "mainnet": "https://api.hyperliquid.xyz/info",
    "testnet": "https://api.hyperliquid-testnet.xyz/info",
}

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
COLS = ["timestamp", "open", "high", "low", "close", "volume"]


def csv_path(symbol: str, timeframe: str) -> Path:
    return DATA_DIR / f"{symbol}_{timeframe}.csv"


# --------------------------------------------------------------- Coinbase ---
# Binance bloquea IPs de EE.UU. (HTTP 451). Coinbase Exchange es público, sin clave
# y accesible desde EE.UU. Devuelve máximo 300 velas por petición: paginamos.
COINBASE = "https://api.exchange.coinbase.com/products/{pair}/candles"
_CB_GRAN = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": None, "1d": 86400}


def fetch_coinbase(symbol: str, timeframe: str, days: int, verbose: bool = True) -> pd.DataFrame:
    """Descarga velas de Coinbase. 4h no existe en su API: se construye a partir de 1h."""
    if timeframe == "4h":
        h1 = fetch_coinbase(symbol, "1h", days, verbose)
        return resample(h1, "4h")
    gran = _CB_GRAN[timeframe]
    pair = f"{symbol}-USD"
    end = int(time.time())
    start = end - days * 86_400
    rows: list[list] = []
    cur = start
    while cur < end:
        chunk_end = min(cur + 300 * gran, end)
        params = dict(granularity=gran, start=_iso(cur), end=_iso(chunk_end))
        r = requests.get(COINBASE.format(pair=pair), params=params, timeout=30,
                         headers={"User-Agent": "crypto-bot/0.1"})
        if r.status_code == 429:
            time.sleep(2)
            continue
        r.raise_for_status()
        # formato: [time, low, high, open, close, volume]
        rows.extend([[int(k[0]) * 1000, float(k[3]), float(k[2]), float(k[1]), float(k[4]), float(k[5])] for k in r.json()])
        cur = chunk_end
        if verbose:
            print(f"  {pair} {timeframe}: {len(rows)} velas", end="\r")
        time.sleep(0.25)  # ~4 req/s, bajo el límite público
    if verbose:
        print()
    df = pd.DataFrame(rows, columns=COLS).drop_duplicates("timestamp").sort_values("timestamp")
    return df.reset_index(drop=True)


def _iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    d = df.copy()
    d["dt"] = pd.to_datetime(d["timestamp"], unit="ms", utc=True)
    d = d.set_index("dt")
    rule = {"4h": "4h", "1d": "1D"}[timeframe]
    out = d.resample(rule).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                               close=("close", "last"), volume=("volume", "sum")).dropna()
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    out["timestamp"] = ((out.index - epoch) // pd.Timedelta(milliseconds=1)).astype("int64")
    return out.reset_index(drop=True)[COLS]


# alias histórico: la fuente de histórico largo es Coinbase
fetch_history = fetch_coinbase


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
