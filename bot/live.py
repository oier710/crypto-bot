"""Ejecutor en vivo para Hyperliquid (testnet o mainnet).

Ciclo (una vez al día, tras el cierre de la vela diaria 00:00 UTC):
  1. Guardas de riesgo: si la cuenta ha perdido más del límite diario o total -> cerrar todo y parar.
  2. Por cada activo: descargar velas diarias, calcular señal al último cierre COMPLETO.
  3. Comparar con la posición real. Si difiere -> orden a mercado para ajustar.
  4. Si hay posición -> colocar/actualizar stop-loss en el exchange (2xATR desde la entrada).
  5. Registrar todo en logs/.

Seguridad:
  - Firma con la AGENT WALLET (solo puede operar, no retirar).
  - Nunca apalancamiento > 1x: el tamaño es una fracción del capital asignado.
  - `dry_run=True` calcula y muestra, pero NO envía nada.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from . import indicators
from .config import RISK
from .strategies import trend_ema

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
STATE_FILE = LOG_DIR / "state.json"
TRADES_FILE = LOG_DIR / "trades.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "live.log"), logging.StreamHandler()],
)
log = logging.getLogger("live")

# ------------------------------------------------------------ estrategia v1
STRATEGY_PARAMS = dict(fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True)
SYMBOLS = ("BTC", "ETH")
POSITION_FRACTION = 0.75   # del capital asignado a cada activo (decisión de analyze_v2)
STOP_ATR = 2.0
ATR_N = 14
WARMUP_DAYS = 200
MIN_NOTIONAL_USD = 10.5    # mínimo de Hyperliquid es 10 $; margen para redondeos
MARKET_SLIPPAGE = 0.01     # tolerancia de precio para la orden a mercado del SDK (1 %)


@dataclass
class Account:
    address: str
    network: str
    info: object
    exchange: object | None


def connect(dry_run: bool = False) -> Account:
    load_dotenv(ROOT / ".env")
    address = os.getenv("HL_ACCOUNT_ADDRESS", "").strip()
    key = os.getenv("HL_AGENT_PRIVATE_KEY", "").strip()
    network = os.getenv("HL_NETWORK", "testnet").strip().lower()
    if not address:
        raise SystemExit("Falta HL_ACCOUNT_ADDRESS en .env")
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    base = constants.TESTNET_API_URL if network == "testnet" else constants.MAINNET_API_URL
    info = Info(base, skip_ws=True)
    exchange = None
    if not dry_run:
        if not key:
            raise SystemExit("Falta HL_AGENT_PRIVATE_KEY en .env (o usa --dry-run)")
        from eth_account import Account as EthAccount
        from hyperliquid.exchange import Exchange
        agent = EthAccount.from_key(key)
        if agent.address.lower() == address.lower():
            raise SystemExit("SEGURIDAD: la clave del .env es la de la cuenta principal, no una agent wallet. Abortando.")
        exchange = Exchange(agent, base, account_address=address)
    log.info("Conectado a %s como %s (dry_run=%s)", network, address, dry_run)
    return Account(address, network, info, exchange)


# ------------------------------------------------------------------ estado
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"peak_equity": None, "day": None, "day_start_equity": None, "halted": False, "halt_reason": "", "stops": {}}


def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2))


# ------------------------------------------------------------------- datos
def daily_candles(acc: Account, coin: str) -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - WARMUP_DAYS * 86_400_000
    raw = acc.info.candles_snapshot(coin, "1d", start, end)
    rows = [[int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"])] for c in raw]
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("dt").sort_index()
    # descartar la vela del día en curso (aún no cerrada)
    if df.index[-1] + pd.Timedelta(days=1) > pd.Timestamp.now(tz="UTC"):
        df = df.iloc[:-1]
    return df


def account_snapshot(acc: Account) -> tuple[float, dict[str, float], dict[str, float]]:
    """Devuelve (valor de cuenta USD, {coin: tamaño con signo}, {coin: precio de entrada})."""
    st = acc.info.user_state(acc.address)
    equity = float(st["marginSummary"]["accountValue"])
    pos, entry = {}, {}
    for ap in st.get("assetPositions", []):
        p = ap["position"]
        szi = float(p["szi"])
        if szi != 0:
            pos[p["coin"]] = szi
            entry[p["coin"]] = float(p["entryPx"]) if p.get("entryPx") else 0.0
    return equity, pos, entry


def sz_decimals(acc: Account) -> dict[str, int]:
    meta = acc.info.meta()
    return {a["name"]: int(a["szDecimals"]) for a in meta["universe"]}


def mid_price(acc: Account, coin: str) -> float:
    return float(acc.info.all_mids()[coin])


# ----------------------------------------------------------------- órdenes
def market_to_target(acc: Account, coin: str, current: float, target: float, decimals: int, dry_run: bool) -> float:
    """Ajusta la posición de `current` a `target` (tamaños con signo, en unidades del activo)."""
    delta = round(target - current, decimals)
    if delta == 0:
        return 0.0
    is_buy = delta > 0
    log.info("ORDEN %s %s %.6f (posición %.6f -> %.6f)", coin, "COMPRA" if is_buy else "VENTA", abs(delta), current, target)
    if dry_run or acc.exchange is None:
        return delta
    res = acc.exchange.market_open(coin, is_buy, abs(delta), None, MARKET_SLIPPAGE)
    log.info("respuesta: %s", json.dumps(res)[:400])
    if res.get("status") != "ok":
        raise RuntimeError(f"orden rechazada: {res}")
    for st in res["response"]["data"]["statuses"]:
        if "error" in st:
            raise RuntimeError(f"orden con error: {st['error']}")
    return delta


def replace_stop(acc: Account, coin: str, size: float, stop_px: float, decimals: int, dry_run: bool):
    """Cancela stops previos del activo y coloca uno nuevo (reduce-only, trigger a mercado)."""
    side_close_is_buy = size < 0
    px_dec = 6 - decimals  # Hyperliquid: precio con máx 5 cifras significativas / (6 - szDecimals) decimales
    stop_px = float(f"{stop_px:.5g}")
    stop_px = round(stop_px, max(px_dec, 0))
    log.info("STOP %s a %s (cierra %s %.6f)", coin, stop_px, "comprando" if side_close_is_buy else "vendiendo", abs(size))
    if dry_run or acc.exchange is None:
        return
    for o in acc.info.open_orders(acc.address):
        if o["coin"] == coin:
            acc.exchange.cancel(coin, o["oid"])
    res = acc.exchange.order(coin, side_close_is_buy, abs(size), stop_px,
                             {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}}, reduce_only=True)
    log.info("respuesta stop: %s", json.dumps(res)[:300])


def close_all(acc: Account, pos: dict[str, float], decimals: dict[str, int], dry_run: bool, reason: str):
    log.warning("CERRANDO TODO: %s", reason)
    for coin, szi in pos.items():
        market_to_target(acc, coin, szi, 0.0, decimals[coin], dry_run)
    if not dry_run and acc.exchange is not None:
        for o in acc.info.open_orders(acc.address):
            acc.exchange.cancel(o["coin"], o["oid"])


def record_trade(coin: str, delta: float, price: float, reason: str):
    new = not TRADES_FILE.exists()
    with TRADES_FILE.open("a") as f:
        if new:
            f.write("time,coin,delta,price,notional_usd,reason\n")
        f.write(f"{datetime.now(timezone.utc).isoformat()},{coin},{delta},{price},{abs(delta)*price:.2f},{reason}\n")


# ------------------------------------------------------------------- ciclo
def run_cycle(dry_run: bool = False):
    acc = connect(dry_run)
    state = load_state()
    decimals = sz_decimals(acc)
    equity, pos, entry = account_snapshot(acc)
    today = datetime.now(timezone.utc).date().isoformat()
    log.info("Equity %.2f USD | posiciones %s", equity, pos or "ninguna")

    # --- guardas de riesgo ------------------------------------------------
    if state["peak_equity"] is None or equity > state["peak_equity"]:
        state["peak_equity"] = equity
    if state["day"] != today:
        state["day"], state["day_start_equity"] = today, equity
    if state.get("halted"):
        log.error("BOT PARADO (%s). Borra 'halted' en logs/state.json tras revisar para reanudar.", state["halt_reason"])
        save_state(state)
        return
    dd = equity / state["peak_equity"] - 1
    day_pnl = equity / state["day_start_equity"] - 1
    if dd < -RISK.max_total_drawdown_pct or day_pnl < -RISK.max_daily_loss_pct:
        reason = f"drawdown {dd:+.1%} / día {day_pnl:+.1%} supera límites"
        close_all(acc, pos, decimals, dry_run, reason)
        state.update(halted=True, halt_reason=reason)
        save_state(state)
        return

    # --- señales y ajuste ---------------------------------------------------
    capital_per_asset = equity / len(SYMBOLS)
    for coin in SYMBOLS:
        df = daily_candles(acc, coin)
        sig = int(trend_ema.signals(df, **STRATEGY_PARAMS).iloc[-1])
        atr = float(indicators.atr(df, ATR_N).iloc[-1])
        px = mid_price(acc, coin)
        current = pos.get(coin, 0.0)
        notional = capital_per_asset * POSITION_FRACTION
        target = 0.0 if sig == 0 or notional < MIN_NOTIONAL_USD else round(sig * notional / px, decimals[coin])
        # no re-dimensionar una posición ya abierta en la misma dirección (evita fees por ruido)
        if current != 0 and target != 0 and (current > 0) == (target > 0):
            target = current
        log.info("%s cierre=%.2f señal=%+d ATR=%.2f posición=%.6f objetivo=%.6f", coin, df.close.iloc[-1], sig, atr, current, target)

        delta = market_to_target(acc, coin, current, target, decimals[coin], dry_run)
        if delta:
            record_trade(coin, delta, px, f"señal {sig:+d}" + (" (dry-run)" if dry_run else ""))

        if target != 0:
            if (coin not in state["stops"]) or delta:
                entry_px = px if delta else entry.get(coin, px)
                state["stops"][coin] = entry_px - (1 if target > 0 else -1) * STOP_ATR * atr
            replace_stop(acc, coin, target, state["stops"][coin], decimals[coin], dry_run)
        else:
            state["stops"].pop(coin, None)
            if not dry_run and acc.exchange is not None:
                for o in acc.info.open_orders(acc.address):
                    if o["coin"] == coin:
                        acc.exchange.cancel(coin, o["oid"])

    save_state(state)
    log.info("Ciclo completado.")
