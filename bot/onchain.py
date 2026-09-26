"""Ejecutor on-chain: Uniswap V3 en Arbitrum One, con la cuenta de MetaMask del experimento.

Modelo: la cartera es {USDC, WETH, WBTC} en una sola dirección. Cada día, para BTC y ETH:
  señal +1 -> mantener (capital/2 * 75 %) en el activo;  señal 0 -> todo en USDC.
Se compra/vende solo la diferencia si supera MIN_TRADE_USD (evita gas y polvo).
Stop 2xATR comprobado cada hora (no hay stops en cadena): si se toca, se vende a USDC.

Seguridad:
  - La clave privada vive en .env (WALLET_PRIVATE_KEY) en el Mac. Solo la cuenta del experimento.
  - Se reserva GAS_RESERVE_ETH en ETH nativo para pagar gas; el resto se gestiona.
  - amountOutMinimum siempre = cotización * (1 - MAX_SLIPPAGE): protege de sandwich/MEV.
  - dry_run: calcula y muestra, no firma nada.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from . import indicators
from .config import RISK
from .strategies import trend_ema

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
STATE_FILE = LOG_DIR / "onchain_state.json"
TRADES_FILE = LOG_DIR / "onchain_trades.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG_DIR / "onchain.log"), logging.StreamHandler()])
log = logging.getLogger("onchain")

# ----------------------------------------------------------------- Arbitrum One
RPC_DEFAULT = "https://arb1.arbitrum.io/rpc"
CHAIN_ID = 42161
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"   # USDC nativo (Circle)
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
WBTC = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"
SWAP_ROUTER02 = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
DECIMALS = {USDC: 6, WETH: 18, WBTC: 8}
ASSETS = {"ETH": WETH, "BTC": WBTC}
POOL_FEE = 500  # 0.05 %
# rutas: ETH directo; BTC vía WETH (pool WBTC/WETH 0.05 % es la más líquida)
PATHS = {
    "ETH": {"buy": [(USDC, 500, WETH)], "sell": [(WETH, 500, USDC)]},
    "BTC": {"buy": [(USDC, 500, WETH), (WETH, 500, WBTC)], "sell": [(WBTC, 500, WETH), (WETH, 500, USDC)]},
}

# ------------------------------------------------------------------ parámetros
STRATEGY_PARAMS = dict(fast=20, slow=60, adx_n=14, adx_min=20, allow_short=False)
SYMBOLS = ("BTC", "ETH")
POSITION_FRACTION = 0.75
STOP_ATR = 2.0
ATR_N = 14
MIN_TRADE_USD = 5.0
MAX_SLIPPAGE = 0.005          # 0.5 % bajo la cotización
GAS_RESERVE_ETH = 0.0015      # ~4 $ para muchas operaciones en Arbitrum

ERC20_ABI = json.loads('[{"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"name":"a","type":"address"}],"outputs":[{"type":"uint256"}]},'
                       '{"name":"allowance","type":"function","stateMutability":"view","inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"outputs":[{"type":"uint256"}]},'
                       '{"name":"approve","type":"function","stateMutability":"nonpayable","inputs":[{"name":"s","type":"address"},{"name":"v","type":"uint256"}],"outputs":[{"type":"bool"}]}]')
ROUTER_ABI = json.loads('[{"name":"exactInput","type":"function","stateMutability":"payable","inputs":[{"name":"params","type":"tuple","components":[{"name":"path","type":"bytes"},{"name":"recipient","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"amountOutMinimum","type":"uint256"}]}],"outputs":[{"name":"amountOut","type":"uint256"}]}]')
QUOTER_ABI = json.loads('[{"name":"quoteExactInput","type":"function","stateMutability":"nonpayable","inputs":[{"name":"path","type":"bytes"},{"name":"amountIn","type":"uint256"}],"outputs":[{"name":"amountOut","type":"uint256"},{"name":"a","type":"uint160[]"},{"name":"b","type":"uint32[]"},{"name":"gasEstimate","type":"uint256"}]}]')


def encode_path(hops) -> bytes:
    out = b""
    for i, (tin, fee, tout) in enumerate(hops):
        if i == 0:
            out += bytes.fromhex(tin[2:])
        out += fee.to_bytes(3, "big") + bytes.fromhex(tout[2:])
    return out


# ---------------------------------------------------------------------- precios
def coinbase_daily(symbol: str, days: int = 220) -> pd.DataFrame:
    from .data import fetch_coinbase, resample
    h1 = fetch_coinbase(symbol, "1h", days, verbose=False)
    d1 = resample(h1, "1d")
    d1["dt"] = pd.to_datetime(d1["timestamp"], unit="ms", utc=True)
    d1 = d1.set_index("dt")
    if d1.index[-1] + pd.Timedelta(days=1) > pd.Timestamp.now(tz="UTC"):
        d1 = d1.iloc[:-1]  # vela de hoy aún abierta
    return d1


def spot(symbol: str) -> float:
    r = requests.get(f"https://api.exchange.coinbase.com/products/{symbol}-USD/ticker", timeout=15,
                     headers={"User-Agent": "crypto-bot/0.1"})
    r.raise_for_status()
    return float(r.json()["price"])


# ---------------------------------------------------------------------- cadena
class Chain:
    def __init__(self, dry_run: bool):
        load_dotenv(ROOT / ".env")
        from web3 import Web3
        self.w3 = Web3(Web3.HTTPProvider(os.getenv("ARB_RPC_URL", RPC_DEFAULT), request_kwargs={"timeout": 30}))
        assert self.w3.is_connected(), "no conecta al RPC de Arbitrum"
        self.dry_run = dry_run
        key = os.getenv("WALLET_PRIVATE_KEY", "").strip()
        addr = os.getenv("WALLET_ADDRESS", "").strip()
        if key:
            self.acct = self.w3.eth.account.from_key(key)
            self.address = self.acct.address
        else:
            if not addr or not dry_run:
                raise SystemExit("Falta WALLET_PRIVATE_KEY en .env (para --dry-run basta WALLET_ADDRESS)")
            self.acct, self.address = None, Web3.to_checksum_address(addr)
        self.router = self.w3.eth.contract(address=SWAP_ROUTER02, abi=ROUTER_ABI)
        self.quoter = self.w3.eth.contract(address=QUOTER_V2, abi=QUOTER_ABI)
        log.info("Arbitrum One | cuenta %s | dry_run=%s", self.address, dry_run)

    def erc20(self, token):
        return self.w3.eth.contract(address=token, abi=ERC20_ABI)

    def balances(self) -> dict:
        b = {"ETH_native": self.w3.eth.get_balance(self.address) / 1e18}
        for t in (USDC, WETH, WBTC):
            b[t] = self.erc20(t).functions.balanceOf(self.address).call() / 10 ** DECIMALS[t]
        return b

    def quote(self, hops, amount_in: int) -> int:
        return self.quoter.functions.quoteExactInput(encode_path(hops), amount_in).call()[0]

    def _send(self, fn, value: int = 0):
        tx = fn.build_transaction({"from": self.address, "value": value, "nonce": self.w3.eth.get_transaction_count(self.address),
                                   "chainId": CHAIN_ID})
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.3)
        signed = self.acct.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        rc = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if rc.status != 1:
            raise RuntimeError(f"tx fallida {h.hex()}")
        log.info("tx ok %s gas=%s", h.hex(), rc.gasUsed)
        return rc

    def ensure_allowance(self, token, amount: int):
        c = self.erc20(token)
        if c.functions.allowance(self.address, SWAP_ROUTER02).call() >= amount:
            return
        log.info("approve %s -> router", token)
        if not self.dry_run:
            self._send(c.functions.approve(SWAP_ROUTER02, 2 ** 256 - 1))

    def swap(self, hops, amount_in: int) -> int:
        """Swap exactInput por la ruta dada. Devuelve amountOut (o cotización en dry_run)."""
        q = self.quote(hops, amount_in)
        min_out = int(q * (1 - MAX_SLIPPAGE))
        tin = hops[0][0]
        log.info("SWAP %s -> %s | in=%s out~%s min=%s", tin[:8], hops[-1][2][:8], amount_in, q, min_out)
        if self.dry_run:
            return q
        self.ensure_allowance(tin, amount_in)
        params = (encode_path(hops), self.address, amount_in, min_out)
        self._send(self.router.functions.exactInput(params))
        return q


# ---------------------------------------------------------------------- estado
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"peak_equity": None, "day": None, "day_start_equity": None, "halted": False, "halt_reason": "", "stops": {}, "last_signal_day": None}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def record(coin, side, usd, price, reason):
    new = not TRADES_FILE.exists()
    with TRADES_FILE.open("a") as f:
        if new:
            f.write("time,coin,side,usd,price,reason\n")
        f.write(f"{datetime.now(timezone.utc).isoformat()},{coin},{side},{usd:.2f},{price:.2f},{reason}\n")


# ----------------------------------------------------------------------- ciclo
def portfolio(ch: Chain, prices: dict) -> tuple[float, dict]:
    b = ch.balances()
    eth_tradable = max(b["ETH_native"] - GAS_RESERVE_ETH, 0.0)
    val = {"USDC": b[USDC], "ETH": (b[WETH] + eth_tradable) * prices["ETH"], "BTC": b[WBTC] * prices["BTC"]}
    equity = sum(val.values())
    log.info("Balances: USDC %.2f | WETH %.5f | ETH nativo %.5f (reserva gas %.4f) | WBTC %.6f -> equity %.2f USD",
             b[USDC], b[WETH], b["ETH_native"], GAS_RESERVE_ETH, b[WBTC], equity)
    return equity, {"raw": b, "val": val}


def sell_to_usdc(ch: Chain, coin: str, usd: float, prices: dict, reason: str):
    token = ASSETS[coin]
    raw = ch.balances()
    amt_tokens = usd / prices[coin]
    if coin == "ETH":
        # primero WETH; si no alcanza, el ETH nativo se envuelve automáticamente al pasar value al router
        weth = raw[WETH]
        use_weth = min(weth, amt_tokens)
        rest = amt_tokens - use_weth
        if use_weth * prices["ETH"] >= MIN_TRADE_USD:
            ch.swap(PATHS["ETH"]["sell"], int(use_weth * 1e18))
        if rest * prices["ETH"] >= MIN_TRADE_USD:
            native_ok = max(raw["ETH_native"] - GAS_RESERVE_ETH, 0)
            rest = min(rest, native_ok)
            amount = int(rest * 1e18)
            q = ch.quote(PATHS["ETH"]["sell"], amount)
            log.info("SWAP ETH nativo -> USDC | in=%s out~%s", amount, q)
            if not ch.dry_run:
                params = (encode_path(PATHS["ETH"]["sell"]), ch.address, amount, int(q * (1 - MAX_SLIPPAGE)))
                ch._send(ch.router.functions.exactInput(params), value=amount)
    else:
        amount = int(min(amt_tokens, raw[WBTC]) * 10 ** DECIMALS[token])
        ch.swap(PATHS["BTC"]["sell"], amount)
    record(coin, "SELL", usd, prices[coin], reason)


def buy_from_usdc(ch: Chain, coin: str, usd: float, prices: dict, reason: str):
    raw = ch.balances()
    usd = min(usd, raw[USDC])
    if usd < MIN_TRADE_USD:
        return
    ch.swap(PATHS[coin]["buy"], int(usd * 1e6))
    record(coin, "BUY", usd, prices[coin], reason)


def run_cycle(dry_run: bool, signals: bool = True):
    """signals=True: ciclo diario completo. signals=False: solo comprobar stops (cada hora)."""
    ch = Chain(dry_run)
    st = load_state()
    prices = {s: spot(s) for s in SYMBOLS}
    equity, pf = portfolio(ch, prices)
    today = datetime.now(timezone.utc).date().isoformat()

    if st["peak_equity"] is None or equity > st["peak_equity"]:
        st["peak_equity"] = equity
    if st["day"] != today:
        st["day"], st["day_start_equity"] = today, equity
    if st["halted"]:
        log.error("BOT PARADO: %s. Revisa y borra 'halted' en logs/onchain_state.json", st["halt_reason"])
        save_state(st)
        return
    dd = equity / st["peak_equity"] - 1
    day = equity / st["day_start_equity"] - 1
    if dd < -RISK.max_total_drawdown_pct or day < -RISK.max_daily_loss_pct:
        reason = f"drawdown {dd:+.1%} / día {day:+.1%} supera límites"
        log.warning("CERRANDO TODO: %s", reason)
        for c in SYMBOLS:
            if pf["val"][c] >= MIN_TRADE_USD:
                sell_to_usdc(ch, c, pf["val"][c], prices, "límite de riesgo")
        st.update(halted=True, halt_reason=reason)
        save_state(st)
        return

    # --- stops (cada ejecución) ---
    for c in SYMBOLS:
        sp = st["stops"].get(c)
        if sp and pf["val"][c] >= MIN_TRADE_USD and prices[c] <= sp:
            log.warning("STOP %s: precio %.2f <= stop %.2f", c, prices[c], sp)
            sell_to_usdc(ch, c, pf["val"][c], prices, "stop")
            st["stops"].pop(c, None)
            st.setdefault("stopped_today", {})[c] = today
    if not signals:
        save_state(st)
        log.info("Chequeo de stops completado.")
        return

    # --- señales diarias ---
    equity, pf = portfolio(ch, prices)
    per_asset = equity / len(SYMBOLS) * POSITION_FRACTION
    plan = {}
    for c in SYMBOLS:
        df = coinbase_daily(c)
        sig = int(trend_ema.signals(df, **STRATEGY_PARAMS).iloc[-1])
        atr = float(indicators.atr(df, ATR_N).iloc[-1])
        have = pf["val"][c]
        target = per_asset if sig == 1 else 0.0
        if st.get("stopped_today", {}).get(c) == today:
            target = 0.0  # tras un stop, no reentrar el mismo día
        delta = target - have
        log.info("%s cierre=%.2f señal=%+d ATR=%.2f tengo=%.2f$ objetivo=%.2f$ delta=%+.2f$", c, df.close.iloc[-1], sig, atr, have, target, delta)
        plan[c] = dict(sig=sig, atr=atr, have=have, target=target, delta=delta)

    # 1) ventas (liberan USDC)
    for c, p in plan.items():
        if p["delta"] < -MIN_TRADE_USD and (p["target"] == 0 or abs(p["delta"]) / max(p["have"], 1) > 0.25):
            sell_to_usdc(ch, c, -p["delta"], prices, f"señal {p['sig']:+d}")
            if p["target"] == 0:
                st["stops"].pop(c, None)
    # 2) compras
    for c, p in plan.items():
        if p["delta"] > MIN_TRADE_USD:
            buy_from_usdc(ch, c, p["delta"], prices, f"señal {p['sig']:+d}")
    # 3) stops: toda posición mantenida tiene stop; el de una nueva entrada se fija hoy
    equity, pf = portfolio(ch, prices)
    for c, p in plan.items():
        if pf["val"][c] >= MIN_TRADE_USD and p["target"] > 0:
            st["stops"].setdefault(c, prices[c] - STOP_ATR * p["atr"])
        elif pf["val"][c] < MIN_TRADE_USD:
            st["stops"].pop(c, None)
    st["last_signal_day"] = today
    save_state(st)
    log.info("Ciclo diario completado. Stops: %s", st["stops"])
