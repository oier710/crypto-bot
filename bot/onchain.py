"""Ejecutor on-chain v2: Uniswap V3 + Aave V3 en Arbitrum One. Largos y CORTOS.

Modelo por activo (BTC, ETH), al cierre diario:
  señal +1 -> LARGO: mantener (capital/2 * 75 %) en el token (WETH/WBTC) en la wallet.
  señal -1 -> CORTO sintético vía Aave: depositar USDC como garantía, pedir prestado el token,
              venderlo por USDC (que también se deposita como garantía). Se gana si baja.
  señal  0 -> todo en USDC.
Cada hora: stops (largos: precio <= stop; cortos: precio >= stop) y factor de salud de Aave.

Seguridad:
  - Clave privada en .env / secreto de GitHub. Solo la cuenta del experimento.
  - Reserva de gas en ETH nativo (GAS_RESERVE_ETH).
  - amountOutMinimum / amountInMaximum siempre acotados por cotización ± MAX_SLIPPAGE.
  - Cortos: si el factor de salud de Aave cae por debajo de HF_MIN se cierran todos.
  - SHORT_MAX_USD limita el tamaño de cada corto (para la fase de prueba).
  - dry_run: lee todo, calcula, no firma nada.
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
EQUITY_FILE = LOG_DIR / "equity.csv"

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
AAVE_ADDRESSES_PROVIDER = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"  # Aave V3 Arbitrum (estable)
DECIMALS = {USDC: 6, WETH: 18, WBTC: 8}
ASSETS = {"ETH": WETH, "BTC": WBTC}
MAX_UINT = 2 ** 256 - 1
PATHS = {  # rutas Uniswap (fee 0.05 %)
    "ETH": {"buy": [(USDC, 500, WETH)], "sell": [(WETH, 500, USDC)]},
    "BTC": {"buy": [(USDC, 500, WETH), (WETH, 500, WBTC)], "sell": [(WBTC, 500, WETH), (WETH, 500, USDC)]},
}

# ------------------------------------------------------------------ parámetros
STRATEGY_PARAMS = dict(fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True)
SYMBOLS = ("BTC", "ETH")
POSITION_FRACTION = 0.75
STOP_ATR = 2.0
ATR_N = 14
MIN_TRADE_USD = 5.0
MAX_SLIPPAGE = 0.005
GAS_RESERVE_ETH = 0.0015
HF_MIN = 1.35                 # factor de salud mínimo en Aave; por debajo se cierran los cortos
HF_TARGET_MIN = 1.6           # al abrir un corto, el HF resultante debe quedar por encima
SHORTS_ENABLED = os.getenv("SHORTS_ENABLED", "1") == "1"
SHORT_MAX_USD = float(os.getenv("SHORT_MAX_USD", "1e9"))   # tope por corto (fase de prueba)

ERC20_ABI = json.loads('[{"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"name":"a","type":"address"}],"outputs":[{"type":"uint256"}]},'
                       '{"name":"allowance","type":"function","stateMutability":"view","inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"outputs":[{"type":"uint256"}]},'
                       '{"name":"approve","type":"function","stateMutability":"nonpayable","inputs":[{"name":"s","type":"address"},{"name":"v","type":"uint256"}],"outputs":[{"type":"bool"}]}]')
ROUTER_ABI = json.loads('[{"name":"exactInput","type":"function","stateMutability":"payable","inputs":[{"name":"params","type":"tuple","components":[{"name":"path","type":"bytes"},{"name":"recipient","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"amountOutMinimum","type":"uint256"}]}],"outputs":[{"name":"amountOut","type":"uint256"}]},'
                        '{"name":"exactOutput","type":"function","stateMutability":"payable","inputs":[{"name":"params","type":"tuple","components":[{"name":"path","type":"bytes"},{"name":"recipient","type":"address"},{"name":"amountOut","type":"uint256"},{"name":"amountInMaximum","type":"uint256"}]}],"outputs":[{"name":"amountIn","type":"uint256"}]}]')
QUOTER_ABI = json.loads('[{"name":"quoteExactInput","type":"function","stateMutability":"nonpayable","inputs":[{"name":"path","type":"bytes"},{"name":"amountIn","type":"uint256"}],"outputs":[{"name":"amountOut","type":"uint256"},{"name":"a","type":"uint160[]"},{"name":"b","type":"uint32[]"},{"name":"gasEstimate","type":"uint256"}]},'
                        '{"name":"quoteExactOutput","type":"function","stateMutability":"nonpayable","inputs":[{"name":"path","type":"bytes"},{"name":"amountOut","type":"uint256"}],"outputs":[{"name":"amountIn","type":"uint256"},{"name":"a","type":"uint160[]"},{"name":"b","type":"uint32[]"},{"name":"gasEstimate","type":"uint256"}]}]')
PROVIDER_ABI = json.loads('[{"name":"getPool","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},'
                          '{"name":"getPoolDataProvider","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]}]')
DATA_PROVIDER_ABI = json.loads('[{"name":"getReserveTokensAddresses","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"name":"aTokenAddress","type":"address"},{"name":"stableDebtTokenAddress","type":"address"},{"name":"variableDebtTokenAddress","type":"address"}]}]')
POOL_ABI = json.loads('[{"name":"supply","type":"function","stateMutability":"nonpayable","inputs":[{"name":"asset","type":"address"},{"name":"amount","type":"uint256"},{"name":"onBehalfOf","type":"address"},{"name":"referralCode","type":"uint16"}],"outputs":[]},'
                      '{"name":"withdraw","type":"function","stateMutability":"nonpayable","inputs":[{"name":"asset","type":"address"},{"name":"amount","type":"uint256"},{"name":"to","type":"address"}],"outputs":[{"type":"uint256"}]},'
                      '{"name":"borrow","type":"function","stateMutability":"nonpayable","inputs":[{"name":"asset","type":"address"},{"name":"amount","type":"uint256"},{"name":"interestRateMode","type":"uint256"},{"name":"referralCode","type":"uint16"},{"name":"onBehalfOf","type":"address"}],"outputs":[]},'
                      '{"name":"repay","type":"function","stateMutability":"nonpayable","inputs":[{"name":"asset","type":"address"},{"name":"amount","type":"uint256"},{"name":"interestRateMode","type":"uint256"},{"name":"onBehalfOf","type":"address"}],"outputs":[{"type":"uint256"}]},'
                      '{"name":"getUserAccountData","type":"function","stateMutability":"view","inputs":[{"name":"user","type":"address"}],"outputs":[{"name":"totalCollateralBase","type":"uint256"},{"name":"totalDebtBase","type":"uint256"},{"name":"availableBorrowsBase","type":"uint256"},{"name":"currentLiquidationThreshold","type":"uint256"},{"name":"ltv","type":"uint256"},{"name":"healthFactor","type":"uint256"}]}]')


def encode_path(hops) -> bytes:
    out = b""
    for i, (tin, fee, tout) in enumerate(hops):
        if i == 0:
            out += bytes.fromhex(tin[2:])
        out += fee.to_bytes(3, "big") + bytes.fromhex(tout[2:])
    return out


def reverse_hops(hops):
    """Ruta para exactOutput: se codifica del token de salida al de entrada."""
    return [(tout, fee, tin) for (tin, fee, tout) in reversed(hops)]


# ---------------------------------------------------------------------- precios
def coinbase_daily(symbol: str, days: int = 220) -> pd.DataFrame:
    from .data import fetch_coinbase, resample
    h1 = fetch_coinbase(symbol, "1h", days, verbose=False)
    d1 = resample(h1, "1d")
    d1["dt"] = pd.to_datetime(d1["timestamp"], unit="ms", utc=True)
    d1 = d1.set_index("dt")
    if d1.index[-1] + pd.Timedelta(days=1) > pd.Timestamp.now(tz="UTC"):
        d1 = d1.iloc[:-1]
    return d1


def spot(symbol: str) -> float:
    r = requests.get(f"https://api.exchange.coinbase.com/products/{symbol}-USD/ticker", timeout=15,
                     headers={"User-Agent": "crypto-bot/0.2"})
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
        prov = self.w3.eth.contract(address=AAVE_ADDRESSES_PROVIDER, abi=PROVIDER_ABI)
        self.pool_addr = prov.functions.getPool().call()
        self.pool = self.w3.eth.contract(address=self.pool_addr, abi=POOL_ABI)
        dp = self.w3.eth.contract(address=prov.functions.getPoolDataProvider().call(), abi=DATA_PROVIDER_ABI)
        self.atoken, self.debt_token = {}, {}
        for t in (USDC, WETH, WBTC):
            a, _, v = dp.functions.getReserveTokensAddresses(t).call()
            self.atoken[t], self.debt_token[t] = a, v
        log.info("Arbitrum One | cuenta %s | Aave pool %s | dry_run=%s", self.address, self.pool_addr[:10], dry_run)

    def erc20(self, token):
        return self.w3.eth.contract(address=token, abi=ERC20_ABI)

    def bal(self, token, who=None) -> float:
        return self.erc20(token).functions.balanceOf(who or self.address).call() / 10 ** DECIMALS[token]

    def balances(self) -> dict:
        b = {"ETH_native": self.w3.eth.get_balance(self.address) / 1e18}
        for t in (USDC, WETH, WBTC):
            b[t] = self.bal(t)
        b["aUSDC"] = self.erc20(self.atoken[USDC]).functions.balanceOf(self.address).call() / 1e6
        b["debt_WETH"] = self.erc20(self.debt_token[WETH]).functions.balanceOf(self.address).call() / 1e18
        b["debt_WBTC"] = self.erc20(self.debt_token[WBTC]).functions.balanceOf(self.address).call() / 1e8
        return b

    def health_factor(self) -> float:
        hf = self.pool.functions.getUserAccountData(self.address).call()[5]
        return 1e9 if hf >= MAX_UINT // 2 else hf / 1e18

    # ---- transacciones ---------------------------------------------------
    def _send(self, fn, value: int = 0):
        tx = fn.build_transaction({"from": self.address, "value": value, "nonce": self.w3.eth.get_transaction_count(self.address), "chainId": CHAIN_ID})
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.3)
        signed = self.acct.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        rc = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if rc.status != 1:
            raise RuntimeError(f"tx fallida {h.hex()}")
        log.info("tx ok %s gas=%s", h.hex(), rc.gasUsed)
        return rc

    def ensure_allowance(self, token, spender, amount: int):
        c = self.erc20(token)
        if c.functions.allowance(self.address, spender).call() >= amount:
            return
        log.info("approve %s -> %s", token[:8], spender[:8])
        if not self.dry_run:
            self._send(c.functions.approve(spender, MAX_UINT))

    def swap_exact_in(self, hops, amount_in: int) -> int:
        q = self.quoter.functions.quoteExactInput(encode_path(hops), amount_in).call()[0]
        min_out = int(q * (1 - MAX_SLIPPAGE))
        log.info("SWAP %s -> %s | in=%s out~%s", hops[0][0][:8], hops[-1][2][:8], amount_in, q)
        if not self.dry_run:
            self.ensure_allowance(hops[0][0], SWAP_ROUTER02, amount_in)
            self._send(self.router.functions.exactInput((encode_path(hops), self.address, amount_in, min_out)))
        return q

    def swap_exact_out(self, hops, amount_out: int) -> int:
        """Compra exactamente amount_out del token final pagando como máximo cotización*(1+slippage)."""
        rpath = encode_path(reverse_hops(hops))
        q = self.quoter.functions.quoteExactOutput(rpath, amount_out).call()[0]
        max_in = int(q * (1 + MAX_SLIPPAGE))
        log.info("SWAP exactOut %s -> %s | out=%s in~%s", hops[0][0][:8], hops[-1][2][:8], amount_out, q)
        if not self.dry_run:
            self.ensure_allowance(hops[0][0], SWAP_ROUTER02, max_in)
            self._send(self.router.functions.exactOutput((rpath, self.address, amount_out, max_in)))
        return q

    # ---- Aave ------------------------------------------------------------
    def aave_supply_usdc(self, usd: float):
        amt = int(usd * 1e6)
        log.info("AAVE supply %.2f USDC", usd)
        if not self.dry_run and amt > 0:
            self.ensure_allowance(USDC, self.pool_addr, amt)
            self._send(self.pool.functions.supply(USDC, amt, self.address, 0))

    def aave_withdraw_usdc(self, usd: float | None):
        amt = MAX_UINT if usd is None else int(usd * 1e6)
        log.info("AAVE withdraw %s USDC", "todo" if usd is None else f"{usd:.2f}")
        if not self.dry_run:
            self._send(self.pool.functions.withdraw(USDC, amt, self.address))

    def aave_borrow(self, token, amount: int):
        log.info("AAVE borrow %s %s", amount, token[:8])
        if not self.dry_run:
            self._send(self.pool.functions.borrow(token, amount, 2, 0, self.address))

    def aave_repay_all(self, token):
        log.info("AAVE repay todo %s", token[:8])
        if not self.dry_run:
            self.ensure_allowance(token, self.pool_addr, MAX_UINT)
            self._send(self.pool.functions.repay(token, MAX_UINT, 2, self.address))


# ---------------------------------------------------------------------- estado
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"peak_equity": None, "day": None, "day_start_equity": None, "halted": False, "halt_reason": "", "stops": {}, "last_signal_day": None}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def record_equity(equity: float, pf: dict, prices: dict):
    new = not EQUITY_FILE.exists()
    b, v = pf["raw"], pf["val"]
    with EQUITY_FILE.open("a") as f:
        if new:
            f.write("time,equity_usd,usdc,eth_usd,btc_usd,eth_qty,btc_qty,eth_price,btc_price,gas_eth,ausdc,debt_eth_usd,debt_btc_usd,hf\n")
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')},{equity:.2f},{b[USDC]:.2f},{v['ETH']:.2f},{v['BTC']:.2f},"
                f"{b[WETH] + max(b['ETH_native'] - GAS_RESERVE_ETH, 0):.6f},{b[WBTC]:.6f},{prices['ETH']:.2f},{prices['BTC']:.2f},"
                f"{min(b['ETH_native'], GAS_RESERVE_ETH):.5f},{b['aUSDC']:.2f},{b['debt_WETH'] * prices['ETH']:.2f},{b['debt_WBTC'] * prices['BTC']:.2f},{pf['hf']:.2f}\n")


def record(coin, side, usd, price, reason, dry_run=False):
    if dry_run:
        return
    new = not TRADES_FILE.exists()
    with TRADES_FILE.open("a") as f:
        if new:
            f.write("time,coin,side,usd,price,reason\n")
        f.write(f"{datetime.now(timezone.utc).isoformat()},{coin},{side},{usd:.2f},{price:.2f},{reason}\n")


# ------------------------------------------------------------------- cartera
def portfolio(ch: Chain, prices: dict) -> tuple[float, dict]:
    """Equity = USDC (wallet + Aave) + largos − deuda de cortos. val[c] = exposición neta en USD (+largo/−corto)."""
    b = ch.balances()
    eth_tradable = max(b["ETH_native"] - GAS_RESERVE_ETH, 0.0)
    val = {"ETH": (b[WETH] + eth_tradable - b["debt_WETH"]) * prices["ETH"],
           "BTC": (b[WBTC] - b["debt_WBTC"]) * prices["BTC"]}
    cash = b[USDC] + b["aUSDC"]
    equity = cash + val["ETH"] + val["BTC"]
    hf = ch.health_factor()
    log.info("USDC %.2f (wallet %.2f + Aave %.2f) | ETH neto %.2f$ | BTC neto %.2f$ | deuda ETH %.5f BTC %.6f | HF %s -> equity %.2f USD",
             cash, b[USDC], b["aUSDC"], val["ETH"], val["BTC"], b["debt_WETH"], b["debt_WBTC"], "∞" if hf > 1e6 else f"{hf:.2f}", equity)
    return equity, {"raw": b, "val": val, "cash": cash, "hf": hf}


def usdc_in_wallet(ch: Chain, usd: float, pf: dict):
    """Garantiza `usd` USDC en la wallet, retirando de Aave si hace falta."""
    b = pf["raw"]
    if b[USDC] >= usd or b["aUSDC"] <= 0.01:
        return
    need = min(usd - b[USDC] + 0.5, b["aUSDC"])
    ch.aave_withdraw_usdc(need)


# ---------------------------------------------------------------------- largos
def sell_long(ch: Chain, coin: str, usd: float, prices: dict, reason: str):
    token = ASSETS[coin]
    raw = ch.balances()
    amt_tokens = usd / prices[coin]
    if coin == "ETH":
        use_weth = min(raw[WETH], amt_tokens)
        rest = amt_tokens - use_weth
        if use_weth * prices["ETH"] >= MIN_TRADE_USD:
            ch.swap_exact_in(PATHS["ETH"]["sell"], int(use_weth * 1e18))
        if rest * prices["ETH"] >= MIN_TRADE_USD:
            rest = min(rest, max(raw["ETH_native"] - GAS_RESERVE_ETH, 0))
            amount = int(rest * 1e18)
            q = ch.quoter.functions.quoteExactInput(encode_path(PATHS["ETH"]["sell"]), amount).call()[0]
            log.info("SWAP ETH nativo -> USDC | in=%s out~%s", amount, q)
            if not ch.dry_run:
                ch._send(ch.router.functions.exactInput((encode_path(PATHS["ETH"]["sell"]), ch.address, amount, int(q * (1 - MAX_SLIPPAGE)))), value=amount)
    else:
        ch.swap_exact_in(PATHS["BTC"]["sell"], int(min(amt_tokens, raw[WBTC]) * 10 ** DECIMALS[token]))
    record(coin, "SELL", usd, prices[coin], reason, ch.dry_run)


def buy_long(ch: Chain, coin: str, usd: float, prices: dict, pf: dict, reason: str):
    usdc_in_wallet(ch, usd, pf)
    usd = min(usd, ch.balances()[USDC]) if not ch.dry_run else usd
    if usd < MIN_TRADE_USD:
        return
    ch.swap_exact_in(PATHS[coin]["buy"], int(usd * 1e6))
    record(coin, "BUY", usd, prices[coin], reason, ch.dry_run)


# ---------------------------------------------------------------------- cortos
def open_short(ch: Chain, coin: str, usd: float, prices: dict, pf: dict, reason: str):
    """Deposita todo el USDC libre en Aave, pide prestado `usd` del token y lo vende por USDC (que también deposita)."""
    usd = min(usd, SHORT_MAX_USD)
    if usd < MIN_TRADE_USD:
        return
    token = ASSETS[coin]
    b = pf["raw"]
    if b[USDC] > 1:
        ch.aave_supply_usdc(b[USDC] - 0.5)
    amount = int(usd / prices[coin] * 10 ** DECIMALS[token])
    ch.aave_borrow(token, amount)
    out = ch.swap_exact_in(PATHS[coin]["sell"], amount)
    ch.aave_supply_usdc(out / 1e6 - 0.5 if not ch.dry_run else out / 1e6)
    hf = ch.health_factor() if not ch.dry_run else 9.9
    log.info("CORTO %s abierto: %.2f$ | HF %.2f", coin, usd, hf)
    record(coin, "SHORT", usd, prices[coin], reason, ch.dry_run)


def close_short(ch: Chain, coin: str, prices: dict, pf: dict, reason: str):
    """Retira USDC, recompra exactamente la deuda (+0.5 %), repaga todo."""
    token = ASSETS[coin]
    debt = pf["raw"][f"debt_{'WETH' if coin == 'ETH' else 'WBTC'}"]
    if debt <= 0:
        return
    usd = debt * prices[coin]
    need_usdc = usd * 1.02
    ch.aave_withdraw_usdc(min(need_usdc, pf["raw"]["aUSDC"]))
    amount_out = int(debt * 1.005 * 10 ** DECIMALS[token])
    ch.swap_exact_out(PATHS[coin]["buy"], amount_out)
    ch.aave_repay_all(token)
    log.info("CORTO %s cerrado: %.2f$", coin, usd)
    record(coin, "COVER", usd, prices[coin], reason, ch.dry_run)


def tidy_aave(ch: Chain):
    """Sin deuda -> retirar todo el USDC de Aave para tenerlo disponible."""
    b = ch.balances()
    if b["debt_WETH"] <= 0 and b["debt_WBTC"] <= 0 and b["aUSDC"] > 0.5:
        ch.aave_withdraw_usdc(None)


# ----------------------------------------------------------------------- ciclo
def run_cycle(dry_run: bool, signals: bool = True):
    ch = Chain(dry_run)
    st = load_state()
    prices = {s: spot(s) for s in SYMBOLS}
    equity, pf = portfolio(ch, prices)
    today = datetime.now(timezone.utc).date().isoformat()

    if equity < MIN_TRADE_USD * 2:
        log.warning("Sin fondos suficientes (equity %.2f USD).", equity)
        return
    record_equity(equity, pf, prices)

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
            if pf["val"][c] <= -MIN_TRADE_USD:
                close_short(ch, c, prices, pf, "límite de riesgo")
            elif pf["val"][c] >= MIN_TRADE_USD:
                sell_long(ch, c, pf["val"][c], prices, "límite de riesgo")
        tidy_aave(ch)
        st.update(halted=True, halt_reason=reason)
        save_state(st)
        return

    # --- salud de Aave (cada ejecución) ---
    if pf["hf"] < HF_MIN:
        log.warning("HF %.2f < %.2f: cerrando cortos por seguridad", pf["hf"], HF_MIN)
        for c in SYMBOLS:
            if pf["val"][c] <= -MIN_TRADE_USD:
                close_short(ch, c, prices, pf, "factor de salud bajo")
                st["stops"].pop(c, None)
        tidy_aave(ch)
        equity, pf = portfolio(ch, prices)

    # --- stops (cada ejecución) ---
    for c in SYMBOLS:
        sp = st["stops"].get(c)
        v = pf["val"][c]
        if not sp:
            continue
        if v >= MIN_TRADE_USD and prices[c] <= sp:
            log.warning("STOP largo %s: %.2f <= %.2f", c, prices[c], sp)
            sell_long(ch, c, v, prices, "stop")
            st["stops"].pop(c, None); st.setdefault("stopped_today", {})[c] = today
        elif v <= -MIN_TRADE_USD and prices[c] >= sp:
            log.warning("STOP corto %s: %.2f >= %.2f", c, prices[c], sp)
            close_short(ch, c, prices, pf, "stop")
            st["stops"].pop(c, None); st.setdefault("stopped_today", {})[c] = today
    if not signals:
        tidy_aave(ch)
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
        if sig == -1 and not SHORTS_ENABLED:
            sig = 0
        atr = float(indicators.atr(df, ATR_N).iloc[-1])
        have = pf["val"][c]
        target = per_asset if sig == 1 else (-min(per_asset, SHORT_MAX_USD) if sig == -1 else 0.0)
        if st.get("stopped_today", {}).get(c) == today:
            target = 0.0
        log.info("%s cierre=%.2f señal=%+d ATR=%.2f tengo=%+.2f$ objetivo=%+.2f$", c, df.close.iloc[-1], sig, atr, have, target)
        plan[c] = dict(sig=sig, atr=atr, have=have, target=target)

    # 1) cierres: largos que sobran o que pasan a corto/0; cortos que pasan a largo/0
    for c, p in plan.items():
        have, target = p["have"], p["target"]
        if have >= MIN_TRADE_USD and (target <= 0 or (target - have) < -MIN_TRADE_USD and abs(target - have) / have > 0.25):
            sell_long(ch, c, have if target <= 0 else have - target, prices, f"señal {p['sig']:+d}")
            if target <= 0:
                st["stops"].pop(c, None)
        elif have <= -MIN_TRADE_USD and target >= 0:
            close_short(ch, c, prices, pf, f"señal {p['sig']:+d}")
            st["stops"].pop(c, None)
    # 2) aperturas
    equity, pf = portfolio(ch, prices)
    for c, p in plan.items():
        have, target = pf["val"][c], p["target"]
        if target > 0 and target - max(have, 0) > MIN_TRADE_USD:
            buy_long(ch, c, target - max(have, 0), prices, pf, f"señal {p['sig']:+d}")
        elif target < 0 and have > -MIN_TRADE_USD:
            open_short(ch, c, -target, prices, pf, f"señal {p['sig']:+d}")
            equity, pf = portfolio(ch, prices)
    # 3) stops: toda posición tiene stop; nuevas entradas lo fijan hoy
    tidy_aave(ch)
    equity, pf = portfolio(ch, prices)
    for c, p in plan.items():
        v = pf["val"][c]
        if v >= MIN_TRADE_USD and p["target"] > 0:
            st["stops"].setdefault(c, prices[c] - STOP_ATR * p["atr"])
        elif v <= -MIN_TRADE_USD and p["target"] < 0:
            st["stops"].setdefault(c, prices[c] + STOP_ATR * p["atr"])
        elif abs(v) < MIN_TRADE_USD:
            st["stops"].pop(c, None)
    st["last_signal_day"] = today
    save_state(st)
    log.info("Ciclo diario completado. Stops: %s", st["stops"])


# ---------------------------------------------------------- prueba de cortos
def test_short(coin: str = "ETH", usd: float = 10.0):
    """Abre un corto pequeño y lo cierra a continuación. Verifica toda la mecánica de Aave con poco dinero."""
    ch = Chain(dry_run=False)
    prices = {s: spot(s) for s in SYMBOLS}
    equity0, pf = portfolio(ch, prices)
    log.info("=== PRUEBA DE CORTO %s %.2f$ ===", coin, usd)
    open_short(ch, coin, usd, prices, pf, "prueba")
    time.sleep(5)
    equity1, pf = portfolio(ch, prices)
    close_short(ch, coin, prices, pf, "prueba")
    tidy_aave(ch)
    time.sleep(5)
    equity2, pf = portfolio(ch, prices)
    log.info("=== PRUEBA TERMINADA: equity %.2f -> %.2f -> %.2f (coste %.2f$) ===", equity0, equity1, equity2, equity0 - equity2)
