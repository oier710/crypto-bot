"""Ejecutor on-chain v2: Uniswap V3 + Aave V3 en Arbitrum One. Largos y CORTOS.

Modelo por activo (BTC, ETH), al cierre diario:
  señal +1 -> LARGO: mantener (capital/2 * 75 %) en el token (WETH/WBTC) en la wallet.
  señal -1 -> CORTO sintético vía Aave: depositar USDC como garantía, pedir prestado el token,
              venderlo por USDC (que también se deposita como garantía). Se gana si baja.
  señal  0 -> todo en USDC.
Cada ejecución (cada 15 min): gestión de la posición abierta, stops y factor de salud de Aave.

v3 (2026-10-10, investigación de octubre, idea de Oier): ventas parciales + reentrada en máximo nuevo.
  - Al entrar: stop fijo a 2xATR (como en v2).
  - +1.5 ATR a favor -> se vende 1/3 de la posición y el stop sube a precio de entrada (+costes).
  - +3 ATR a favor   -> se vende otro 1/3. El último tercio sigue hasta el stop o el cambio de señal.
  - Reentrada: si tras una venta parcial el precio corrige >= 1 ATR desde el máximo y después
    marca un MÁXIMO NUEVO (señal diaria aún a favor), se recompra lo vendido; el stop de toda la
    posición sube a precio - 1.5 ATR y los niveles de parciales se recalculan desde ese precio.
  - En cortos, todo en espejo (mínimos en vez de máximos).
  Backtest 3 años (results/research_2026_10b.txt): +68.7 % / caída máx. −16.2 % / 11 meses en rojo de 33,
  frente a +99.7 % / −24.1 % / 13 de la v2. Elegida por consistencia (criterio de Oier).

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
PNL_FILE = LOG_DIR / "pnl.csv"            # resultado de cada cierre (total o parcial)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG_DIR / "onchain.log"), logging.StreamHandler()])
log = logging.getLogger("onchain")

# ----------------------------------------------------------------- Arbitrum One
RPC_DEFAULT = "https://arb1.arbitrum.io/rpc"
CHAIN_ID = 42161
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"   # USDC nativo (Circle)
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
WBTC = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"
ARB = "0x912CE59144191C1204E64559FE8253a0e49E6548"
SWAP_ROUTER02 = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
AAVE_ADDRESSES_PROVIDER = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"  # Aave V3 Arbitrum (estable)
# Dolomite (Arbitrum One) — docs.dolomite.io/smart-contract-addresses; comprobación on-chain (mercados, operador,
# márgenes) en scripts/research_dolomite_check.py -> results/research_dolomite_check.txt. Solo se usa para el corto
# de ARB, con su garantía en la cuenta aislada nº 1 (no aparece en la web de Dolomite: recuperación manual =
# depositWei/withdrawWei sobre la cuenta 1 del DepositWithdrawalProxy; ver journal/2026-10-29-arb-dolomite.md).
DOLOMITE_MARGIN = "0x6Bd780E7fDf01D77e4d475c821f1e7AE05409072"
DOLOMITE_DEPOSIT_WITHDRAWAL_PROXY = "0xAdB9D68c613df4AA363B42161E1282117C7B9594"
DOLO_ACCOUNT = 1              # número de cuenta aislada en Dolomite para el corto (la 0 es la "por defecto")
DOLO_FLAG_FROM, DOLO_FLAG_NONE = 1, 3   # AccountBalanceLib.BalanceCheckFlag: Both=0, From=1, To=2, None=3
DOLO_MARGIN_RATIO_DEFAULT = 0.15        # margen mínimo global si no se puede leer (liquidación si garantía < 1.15 x deuda)
DOLO_BORROW_BUFFER = 1.30               # al pedir prestado (antes de depositar lo vendido) la garantía cubre deuda x k x 1.30 (> HF_MIN)
DOLO_DUST_USD = 0.05                    # deuda por debajo de esto se considera saldada (intereses de polvo)
DECIMALS = {USDC: 6, WETH: 18, WBTC: 8, ARB: 18}
ASSETS = {"ETH": WETH, "BTC": WBTC, "ARB": ARB}
TOKENS = (USDC, WETH, WBTC, ARB)
MAX_UINT = 2 ** 256 - 1
PATHS = {  # rutas Uniswap (fee 0.05 %)
    "ETH": {"buy": [(USDC, 500, WETH)], "sell": [(WETH, 500, USDC)]},
    # BTC directo contra USDC (pool WBTC/USDC 0.05 %, ~6 M$): un solo salto, mitad de comisión que vía WETH
    # (escaneo on-chain 2026-10-26: ida+vuelta 0.10 % frente a 0.20 %).
    "BTC": {"buy": [(USDC, 500, WBTC)], "sell": [(WBTC, 500, USDC)]},
    "ARB": {"buy": [(USDC, 500, WETH), (WETH, 500, ARB)], "sell": [(ARB, 500, WETH), (WETH, 500, USDC)]},
}

# ------------------------------------------------------------------ parámetros
STRATEGY_PARAMS = dict(fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True)
SYMBOLS = ("BTC", "ETH", "ARB")   # ARB desde 2026-10-29: largo en Uniswap, corto vía Dolomite (research_2026_10g)
# Reparto del capital de trading por moneda (research_2026_10g: ARB al 15 % sube la rentabilidad de +61 % a +79 %
# a 3 años con la misma caída máxima; a partir del 20 % la caída se pasa de −20 %). Sin entrada -> partes iguales.
WEIGHTS = {"BTC": 0.425, "ETH": 0.425, "ARB": 0.15}
# Dónde se pide prestada cada moneda para los cortos: Aave presta WBTC y WETH; ARB solo Dolomite.
SHORT_VENUE = {"BTC": "aave", "ETH": "aave", "ARB": "dolomite"}
POSITION_FRACTION = 0.75
# v4: acumulación. La base de trading se queda con el 70 % de cada ganancia realizada nueva (por encima del
# máximo anterior); el 30 % compra ACCUM_COINS a partes iguales y se aparta (nunca se vende, no cuenta para
# stops ni límites). Las compras se agrupan hasta alcanzar MIN_TRADE_USD por moneda. Oier, 2026-10-25.
ACCUM_SHARE = 0.30
ACCUM_COINS = ("BTC", "ETH")
CAPITAL_REF = 115.75
STOP_ATR = 2.0
ATR_N = 14
MIN_TRADE_USD = 3.0           # bajado de 5 a 3 (2026-10-29): la parcial (1/3) de una posición de ARB de ~12$ son ~4$
MAX_SLIPPAGE = 0.005
GAS_RESERVE_ETH = 0.0015
HF_MIN = 1.25                 # factor de salud mínimo en Aave; por debajo se cierran los cortos
HF_TARGET_MIN = 1.5           # al abrir un corto, el HF resultante debe quedar por encima
LIQ_THRESHOLD = 0.78          # umbral de liquidación del USDC como garantía en Aave (conservador)
PT_LEVELS = (1.5, 3.0)        # ventas parciales (1/3 cada una) a +1.5 y +3 ATR desde la referencia
RH_PULLBACK_ATR = 1.0         # corrección mínima tras una parcial para aceptar el máximo nuevo
RH_RESTOP_ATR = 1.5           # al recomprar, stop de toda la posición a precio - 1.5 ATR (nunca baja)
BE_COST = 0.002               # stop "a entrada" = entrada * (1 ± 0.2 %) para cubrir los costes
PARTIAL_OUT = 0.95            # la posición está "recortada" si queda < 95 % del tamaño completo
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
_ACC = '{"components":[{"name":"owner","type":"address"},{"name":"number","type":"uint256"}],"name":"account","type":"tuple"}'
_VAL = '{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}'
DOLO_MARGIN_ABI = json.loads('[{"name":"getMarketIdByTokenAddress","type":"function","stateMutability":"view","inputs":[{"name":"token","type":"address"}],"outputs":[{"type":"uint256"}]},'
                             '{"name":"getMarketIsClosing","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"type":"bool"}]},'
                             '{"name":"getMarginRatio","type":"function","stateMutability":"view","inputs":[],"outputs":[' + _VAL + ']},'
                             '{"name":"getMarketMarginPremium","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[' + _VAL + ']},'
                             '{"name":"getMinBorrowedValue","type":"function","stateMutability":"view","inputs":[],"outputs":[' + _VAL + ']},'
                             '{"name":"getIsGlobalOperator","type":"function","stateMutability":"view","inputs":[{"name":"o","type":"address"}],"outputs":[{"type":"bool"}]},'
                             '{"name":"getAccountWei","type":"function","stateMutability":"view","inputs":[' + _ACC + ',{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"sign","type":"bool"},{"name":"value","type":"uint256"}],"type":"tuple"}]},'
                             '{"name":"getAdjustedAccountValues","type":"function","stateMutability":"view","inputs":[' + _ACC + '],"outputs":[' + _VAL + ',' + _VAL + ']}]')
DOLO_PROXY_ABI = json.loads('[{"name":"depositWei","type":"function","stateMutability":"nonpayable","inputs":[{"name":"toAccountNumber","type":"uint256"},{"name":"marketId","type":"uint256"},{"name":"amountWei","type":"uint256"}],"outputs":[]},'
                            '{"name":"withdrawWei","type":"function","stateMutability":"nonpayable","inputs":[{"name":"fromAccountNumber","type":"uint256"},{"name":"marketId","type":"uint256"},{"name":"amountWei","type":"uint256"},{"name":"balanceCheckFlag","type":"uint8"}],"outputs":[]}]')


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
        for t in TOKENS:
            a, _, v = dp.functions.getReserveTokensAddresses(t).call()
            self.atoken[t], self.debt_token[t] = a, v
        self._init_dolomite()
        log.info("Arbitrum One | cuenta %s | Aave pool %s | Dolomite %s | dry_run=%s", self.address, self.pool_addr[:10],
                 "ok" if self.dolo_ok else "NO disponible", dry_run)

    def _init_dolomite(self):
        """Dolomite solo se usa para los cortos de ARB. Si algo falla al leerlo, el bot sigue (sin cortos de ARB)."""
        self.dolo_ok, self.dolo_market = False, {}
        self.dolo_margin_ratio, self.dolo_min_borrow_usd = DOLO_MARGIN_RATIO_DEFAULT, 0.0
        try:
            self.dolo = self.w3.eth.contract(address=DOLOMITE_MARGIN, abi=DOLO_MARGIN_ABI)
            self.dolo_proxy = self.w3.eth.contract(address=DOLOMITE_DEPOSIT_WITHDRAWAL_PROXY, abi=DOLO_PROXY_ABI)
            for t in (USDC,) + tuple(ASSETS[c] for c, v in SHORT_VENUE.items() if v == "dolomite"):
                self.dolo_market[t] = self.dolo.functions.getMarketIdByTokenAddress(t).call()
                if t != USDC and self.dolo.functions.getMarketIsClosing(self.dolo_market[t]).call():
                    raise RuntimeError(f"mercado {t[:8]} cerrándose en Dolomite")
            if not self.dolo.functions.getIsGlobalOperator(DOLOMITE_DEPOSIT_WITHDRAWAL_PROXY).call():
                raise RuntimeError("el DepositWithdrawalProxy no es operador global de DolomiteMargin")
            # requisito efectivo de garantía: margen global x primas de los mercados implicados (USDC y la moneda)
            k = 1 + self.dolo.functions.getMarginRatio().call()[0] / 1e18
            for t, mid in self.dolo_market.items():
                k *= 1 + self.dolo.functions.getMarketMarginPremium(mid).call()[0] / 1e18
            self.dolo_margin_ratio = k - 1
            self.dolo_min_borrow_usd = self.dolo.functions.getMinBorrowedValue().call()[0] / 1e36
            self.dolo_ok = True
        except Exception as e:  # noqa: BLE001
            st = load_state()
            open_pos = [c for c in SHORT_VENUE if SHORT_VENUE[c] == "dolomite"
                        and (c in st.get("pos", {}) or c in st.get("stops", {}))]
            if open_pos:
                # con una posición de ARB abierta no se puede fingir que no existe: mejor fallar esta ejecución
                raise RuntimeError(f"Dolomite no responde ({e}) y hay posición abierta de {open_pos}") from e
            log.error("Dolomite no disponible (%s): sin cortos de ARB hasta que vuelva", e)

    # ---- Dolomite (lectura) ----------------------------------------------
    def _dolo_wei(self, token, account: int = DOLO_ACCOUNT) -> float:
        """Saldo del token en la cuenta Dolomite: positivo = depositado, negativo = prestado."""
        sign, value = self.dolo.functions.getAccountWei((self.address, account), self.dolo_market[token]).call()
        return (value if sign else -value) / 10 ** DECIMALS[token]

    def dolo_balances(self) -> dict:
        """USDC depositado en Dolomite (cuentas 0 y 1) y deuda por moneda (solo las de Dolomite)."""
        out = {"dUSDC": 0.0}
        for c, v in SHORT_VENUE.items():
            if v == "dolomite":
                out[f"debt_{c}"] = 0.0
        if not self.dolo_ok:
            return out
        # si la lectura falla se deja propagar: una ejecución fallida es inofensiva (la siguiente es en 15 min);
        # fingir que no hay deuda borraría el estado de la posición de ARB
        out["dUSDC"] = max(self._dolo_wei(USDC, DOLO_ACCOUNT), 0.0)   # solo la cuenta 1: la 0 no respalda la 1
        for c, v in SHORT_VENUE.items():
            if v == "dolomite":
                out[f"debt_{c}"] = max(-self._dolo_wei(ASSETS[c], DOLO_ACCOUNT), 0.0)
        return out

    def dolo_health(self) -> float:
        """Equivalente al HF de Aave para la cuenta aislada: garantía / (deuda x (1 + margen mínimo)).
        1.0 = liquidable. Infinito sin deuda."""
        if not self.dolo_ok:
            return 1e9
        sup, bor = self.dolo.functions.getAdjustedAccountValues((self.address, DOLO_ACCOUNT)).call()
        if bor[0] <= 0:
            return 1e9
        return sup[0] / (bor[0] * (1 + self.dolo_margin_ratio))

    # ---- Dolomite (transacciones) ----------------------------------------
    def dolo_deposit_usdc(self, usd: float):
        amt = int(usd * 1e6)
        log.info("DOLOMITE deposit %.2f USDC (cuenta %d)", usd, DOLO_ACCOUNT)
        if not self.dry_run and amt > 0:
            self.ensure_allowance(USDC, DOLOMITE_MARGIN, amt)
            self._send(self.dolo_proxy.functions.depositWei(DOLO_ACCOUNT, self.dolo_market[USDC], amt))

    def dolo_withdraw_usdc(self, usd: float | None):
        amt = MAX_UINT if usd is None else int(usd * 1e6)
        log.info("DOLOMITE withdraw %s USDC (cuenta %d)", "todo" if usd is None else f"{usd:.2f}", DOLO_ACCOUNT)
        if not self.dry_run:
            self._send(self.dolo_proxy.functions.withdrawWei(DOLO_ACCOUNT, self.dolo_market[USDC], amt, DOLO_FLAG_FROM))

    def dolo_borrow(self, token, amount: int):
        """Retirar un token que la cuenta no tiene = pedirlo prestado contra la garantía de esa cuenta."""
        log.info("DOLOMITE borrow %s %s", amount, token[:8])
        if not self.dry_run:
            self._send(self.dolo_proxy.functions.withdrawWei(DOLO_ACCOUNT, self.dolo_market[token], amount, DOLO_FLAG_NONE))

    def dolo_repay(self, token, amount: int):
        """Depositar el token en la cuenta con deuda = devolverlo."""
        log.info("DOLOMITE repay %s %s", amount, token[:8])
        if not self.dry_run:
            self.ensure_allowance(token, DOLOMITE_MARGIN, amount)
            self._send(self.dolo_proxy.functions.depositWei(DOLO_ACCOUNT, self.dolo_market[token], amount))

    def dolo_repay_all(self, token):
        """Devuelve toda la deuda: deposita lo justo (la deuda actual) y, si sobra algo depositado, lo retira."""
        debt = max(-self._dolo_wei(token, DOLO_ACCOUNT), 0.0)
        have = self.bal(token)
        amt = int(min(debt * 1.0005, have) * 10 ** DECIMALS[token])
        log.info("DOLOMITE repay todo %s (deuda %.8f, tengo %.8f)", token[:8], debt, have)
        if not self.dry_run and amt > 0:
            self.ensure_allowance(token, DOLOMITE_MARGIN, amt)
            self._send(self.dolo_proxy.functions.depositWei(DOLO_ACCOUNT, self.dolo_market[token], amt))
            if self._dolo_wei(token, DOLO_ACCOUNT) > 0:
                self._send(self.dolo_proxy.functions.withdrawWei(DOLO_ACCOUNT, self.dolo_market[token], MAX_UINT, DOLO_FLAG_FROM))

    def erc20(self, token):
        return self.w3.eth.contract(address=token, abi=ERC20_ABI)

    def bal(self, token, who=None) -> float:
        return self.erc20(token).functions.balanceOf(who or self.address).call() / 10 ** DECIMALS[token]

    def balances(self) -> dict:
        b = {"ETH_native": self.w3.eth.get_balance(self.address) / 1e18}
        for t in TOKENS:
            b[t] = self.bal(t)
        b["aUSDC"] = self.erc20(self.atoken[USDC]).functions.balanceOf(self.address).call() / 1e6
        for c, t in ASSETS.items():
            if SHORT_VENUE.get(c, "aave") == "aave":
                b[f"debt_{c}"] = self.erc20(self.debt_token[t]).functions.balanceOf(self.address).call() / 10 ** DECIMALS[t]
        b.update(self.dolo_balances())
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

    def aave_repay(self, token, amount: int):
        log.info("AAVE repay %s %s", amount, token[:8])
        if not self.dry_run:
            self.ensure_allowance(token, self.pool_addr, amount)
            self._send(self.pool.functions.repay(token, amount, 2, self.address))

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


_DRY_RUN = False


def save_state(s):
    if _DRY_RUN:          # --dry-run no debe dejar parciales/stops que nunca ocurrieron
        return
    STATE_FILE.write_text(json.dumps(s, indent=2))


def record_equity(equity: float, pf: dict, prices: dict):
    new = not EQUITY_FILE.exists()
    b, v = pf["raw"], pf["val"]
    with EQUITY_FILE.open("a") as f:
        if new:
            f.write("time,equity_usd,usdc,eth_usd,btc_usd,eth_qty,btc_qty,eth_price,btc_price,gas_eth,ausdc,debt_eth_usd,debt_btc_usd,hf,arb_usd,arb_qty,arb_price,debt_arb_usd,accum_usd,equity_total,dusdc,hf_dolo\n")
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')},{equity:.2f},{b[USDC]:.2f},{v['ETH']:.2f},{v['BTC']:.2f},"
                f"{b[WETH] + max(b['ETH_native'] - GAS_RESERVE_ETH, 0):.6f},{b[WBTC]:.6f},{prices['ETH']:.2f},{prices['BTC']:.2f},"
                f"{min(b['ETH_native'], GAS_RESERVE_ETH):.5f},{b['aUSDC']:.2f},{b['debt_ETH'] * prices['ETH']:.2f},{b['debt_BTC'] * prices['BTC']:.2f},{pf['hf']:.2f},"
                f"{pf['val'].get('ARB', 0.0):.2f},{b.get(ARB, 0.0):.6f},{prices.get('ARB', 0.0):.4f},{b.get('debt_ARB', 0.0) * prices.get('ARB', 0.0):.2f},"
                f"{pf.get('accum_usd', 0.0):.2f},{pf.get('equity_total', equity):.2f},{b.get('dUSDC', 0.0):.2f},{pf.get('hf_dolo', 1e9):.2f}\n")


def record(coin, side, usd, price, reason, dry_run=False):
    if dry_run:
        return
    new = not TRADES_FILE.exists()
    with TRADES_FILE.open("a") as f:
        if new:
            f.write("time,coin,side,usd,price,reason\n")
        f.write(f"{datetime.now(timezone.utc).isoformat()},{coin},{side},{usd:.2f},{price:.2f},{reason}\n")


# ------------------------------------------------------- contabilidad (P&L)
PNL_HEADER = "time,coin,lado,evento,cantidad,entrada,salida,bruto_usd,costes_usd,neto_usd,neto_pct,operacion_usd,total_usd\n"


def trade_cost(usd: float, side: int) -> float:
    """Coste estimado de un swap: comisión Uniswap + deslizamiento (0.10 %) + gas; cortos +0.10 % de Aave."""
    return abs(usd) * (0.001 + (0.001 if side == -1 else 0.0)) + 0.03


def pnl_total() -> float:
    """Resultado realizado acumulado (última fila de logs/pnl.csv)."""
    if not PNL_FILE.exists():
        return 0.0
    rows = PNL_FILE.read_text().strip().splitlines()
    return float(rows[-1].split(",")[-1]) if len(rows) > 1 else 0.0


def realize(st: dict, coin: str, side: int, qty: float, exit_px: float, event: str, dry_run: bool,
            closes_op: bool):
    """Apunta el resultado de cerrar `qty` (parcial o total). Neto = bruto − coste de salida − parte proporcional
    de los costes de entrada/recompras de esa operación. Nunca lanza: la contabilidad no puede bloquear el bot."""
    if dry_run or qty <= 0:
        return
    try:
        _realize(st, coin, side, qty, exit_px, event, closes_op)
    except Exception:  # noqa: BLE001
        log.exception("no se pudo apuntar el resultado de %s", coin)


def _realize(st: dict, coin: str, side: int, qty: float, exit_px: float, event: str, closes_op: bool):
    pos = st.get("pos", {}).get(coin) or {}
    entry = pos.get("entry") or last_entry_price(coin, side) or exit_px
    held = max(pos.get("qty_now", qty), qty)
    pending = pos.get("cost_pending", trade_cost(qty * entry, side))
    entry_cost = pending * min(qty / held, 1.0) if held > 0 else pending
    gross = side * qty * (exit_px - entry)
    costs = trade_cost(qty * exit_px, side) + entry_cost
    net = gross - costs
    op = pos.get("op_pnl", 0.0) + net
    total = pnl_total() + net
    if pos:
        pos["cost_pending"] = max(pending - entry_cost, 0.0)
        pos["op_pnl"] = op
    new = not PNL_FILE.exists()
    with PNL_FILE.open("a") as f:
        if new:
            f.write(PNL_HEADER)
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')},{coin},{'largo' if side == 1 else 'corto'},"
                f"{event},{qty:.8f},{entry:.2f},{exit_px:.2f},{gross:.2f},{costs:.2f},{net:.2f},"
                f"{net / (qty * entry) * 100 if entry else 0:.2f},{op:.2f},{total:.2f}\n")
    log.warning("RESULTADO %s %s (%s): bruto %+.2f$ costes %.2f$ -> neto %+.2f$%s | realizado acumulado %+.2f$",
                coin, "largo" if side == 1 else "corto", event, gross, costs, net,
                f" | operación completa {op:+.2f}$" if closes_op else "", total)


# ------------------------------------------------------------------- cartera
def accum_qty(coin: str) -> float:
    """Monedas apartadas (acumulación): no se operan."""
    return float(load_state().get("accum", {}).get("qty", {}).get(coin, 0.0))


def portfolio(ch: Chain, prices: dict) -> tuple[float, dict]:
    """Equity DE TRADING = USDC (wallet + Aave) + largos − deuda de cortos − monedas acumuladas.
    val[c] = exposición neta operable en USD (+largo/−corto), sin las monedas apartadas."""
    b = ch.balances()
    eth_tradable = max(b["ETH_native"] - GAS_RESERVE_ETH, 0.0)
    held = {c: b[t] + (eth_tradable if c == "ETH" else 0.0) for c, t in ASSETS.items()}
    acc = {c: min(accum_qty(c), held[c]) for c in ASSETS}
    val = {c: (held[c] - acc[c] - b[f"debt_{c}"]) * prices[c] for c in ASSETS if c in prices}
    accum_usd = sum(acc[c] * prices[c] for c in ASSETS if c in prices)
    cash = b[USDC] + b["aUSDC"] + b.get("dUSDC", 0.0)
    equity = cash + sum(val.values())
    hf = ch.health_factor()
    hf_d = ch.dolo_health()
    log.info("USDC %.2f (wallet %.2f + Aave %.2f + Dolomite %.2f) | %s | deuda %s | HF %s / Dolomite %s | acumulado %.2f$ -> equity trading %.2f USD (total %.2f)",
             cash, b[USDC], b["aUSDC"], b.get("dUSDC", 0.0), " ".join(f"{c} {v:+.2f}$" for c, v in val.items()),
             " ".join(f"{c} {b[f'debt_{c}']:.6f}" for c in ASSETS), "∞" if hf > 1e6 else f"{hf:.2f}",
             "∞" if hf_d > 1e6 else f"{hf_d:.2f}", accum_usd, equity, equity + accum_usd)
    return equity, {"raw": b, "val": val, "cash": cash, "hf": hf, "hf_dolo": hf_d, "accum_usd": accum_usd, "accum_qty": acc,
                    "equity_total": equity + accum_usd, "prices": prices}


def usdc_in_wallet(ch: Chain, usd: float, pf: dict):
    """Garantiza `usd` USDC en la wallet, retirando de Aave (sin bajar su HF) y, si no basta, de Dolomite."""
    b = pf["raw"]
    if b[USDC] >= usd:
        return
    need = usd - b[USDC] + 0.5
    prices = pf["prices"]
    if b["aUSDC"] > 0.01:
        w = min(need, _max_withdraw(b, prices, HF_TARGET_MIN), b["aUSDC"])
        if w > 0.01:
            ch.aave_withdraw_usdc(w)
            need -= w
    if need > 0.01 and b.get("dUSDC", 0.0) > 0.01 and ch.dolo_ok:
        w = min(need, _max_withdraw_dolo(ch, b, prices, HF_TARGET_MIN), b["dUSDC"])
        if w > 0.01:
            ch.dolo_withdraw_usdc(w)


# ---------------------------------------------------------------------- largos
def sell_long(ch: Chain, coin: str, usd: float, prices: dict, reason: str):
    token = ASSETS[coin]
    raw = ch.balances()
    free = raw[token] + (max(raw["ETH_native"] - GAS_RESERVE_ETH, 0) if coin == "ETH" else 0.0) - accum_qty(coin)
    amt_tokens = max(min(usd / prices[coin], free), 0.0)
    if amt_tokens * prices[coin] < MIN_TRADE_USD:
        return False
    usd = amt_tokens * prices[coin]
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
        ch.swap_exact_in(PATHS[coin]["sell"], int(min(amt_tokens, raw[token]) * 10 ** DECIMALS[token]))
    record(coin, "SELL", usd, prices[coin], reason, ch.dry_run)
    return True


def buy_long(ch: Chain, coin: str, usd: float, prices: dict, pf: dict, reason: str):
    usdc_in_wallet(ch, usd, pf)
    usd = min(usd, ch.balances()[USDC]) if not ch.dry_run else usd
    if usd < MIN_TRADE_USD:
        return
    ch.swap_exact_in(PATHS[coin]["buy"], int(usd * 1e6))
    record(coin, "BUY", usd, prices[coin], reason, ch.dry_run)


# ---------------------------------------------------------------------- cortos
def venue(coin: str) -> str:
    return SHORT_VENUE.get(coin, "aave")


def aave_debt_usd(b: dict, prices: dict) -> float:
    return sum(b[f"debt_{c}"] * prices[c] for c in ASSETS if c in prices and venue(c) == "aave")


def dolo_debt_usd(b: dict, prices: dict) -> float:
    return sum(b.get(f"debt_{c}", 0.0) * prices[c] for c in ASSETS if c in prices and venue(c) == "dolomite")


def open_short(ch: Chain, coin: str, usd: float, prices: dict, pf: dict, reason: str):
    return (open_short_dolomite if venue(coin) == "dolomite" else open_short_aave)(ch, coin, usd, prices, pf, reason)


def open_short_aave(ch: Chain, coin: str, usd: float, prices: dict, pf: dict, reason: str):
    """Deposita todo el USDC libre en Aave, pide prestado `usd` del token y lo vende por USDC (que también deposita)."""
    b = pf["raw"]
    # tamaño máximo que deja HF >= HF_TARGET_MIN con la garantía disponible (USDC libre + USDC ya en Aave − deuda actual)
    debt_now = aave_debt_usd(b, prices)
    collateral = b[USDC] + b["aUSDC"]
    max_usd = max((collateral * LIQ_THRESHOLD - debt_now * HF_TARGET_MIN) / (HF_TARGET_MIN - LIQ_THRESHOLD), 0.0)
    if max_usd < usd:
        log.info("CORTO %s: tamaño limitado por garantía %.2f$ -> %.2f$", coin, usd, max_usd)
    usd = min(usd, SHORT_MAX_USD, max_usd)
    if usd < MIN_TRADE_USD:
        log.info("CORTO %s: sin garantía suficiente (%.2f$), no se abre", coin, usd)
        return
    token = ASSETS[coin]
    if b[USDC] > 1:
        ch.aave_supply_usdc(b[USDC] - 0.5)
    amount = int(usd / prices[coin] * 10 ** DECIMALS[token])
    ch.aave_borrow(token, amount)
    out = ch.swap_exact_in(PATHS[coin]["sell"], amount)
    ch.aave_supply_usdc(out / 1e6 - 0.5 if not ch.dry_run else out / 1e6)
    hf = ch.health_factor() if not ch.dry_run else 9.9
    log.info("CORTO %s abierto: %.2f$ | HF %.2f", coin, usd, hf)
    record(coin, "SHORT", usd, prices[coin], reason, ch.dry_run)
    if hf < HF_MIN:
        log.warning("HF %.2f tras abrir < %.2f: cerrando el corto por seguridad", hf, HF_MIN)
        _, pf2 = portfolio(ch, prices)
        close_short(ch, coin, prices, pf2, "HF insuficiente")


def open_short_dolomite(ch: Chain, coin: str, usd: float, prices: dict, pf: dict, reason: str):
    """Corto en Dolomite (cuenta aislada): deposita la garantía justa en USDC, retira el token (= préstamo),
    lo vende por USDC y deposita también lo vendido. La garantía de Aave (cortos BTC/ETH) no se toca
    más allá de lo que su HF permite."""
    if not ch.dolo_ok:
        log.warning("CORTO %s: Dolomite no disponible, no se abre", coin)
        return
    b = ch.balances() if not ch.dry_run else pf["raw"]   # saldos frescos: otras aperturas del ciclo han podido gastar USDC
    pf = dict(pf, raw=b)
    k = 1 + ch.dolo_margin_ratio
    debt_now = dolo_debt_usd(b, prices)
    # garantía necesaria para que la salud quede >= HF_TARGET_MIN contando lo vendido (que también se deposita):
    # (coll + usd) / ((debt_now + usd) * k) >= HF_TARGET_MIN  ->  coll >= (debt_now + usd) * k * HF_TARGET_MIN - usd
    # ... pero en el momento del préstamo lo vendido aún no está depositado: la cuenta tiene que aguantar
    # sola con margen (garantía >= deuda x k x DOLO_BORROW_BUFFER) o Dolomite revierte.
    def coll_needed(u):
        return max((debt_now + u) * k * HF_TARGET_MIN - u, (debt_now + u) * k * DOLO_BORROW_BUFFER)

    need_coll = coll_needed(usd)
    have_coll = b.get("dUSDC", 0.0)
    free = b[USDC] + (_max_withdraw(b, prices, HF_TARGET_MIN) if b["aUSDC"] > 0.01 else 0.0)
    if have_coll + free < need_coll:
        # reducir el tamaño a lo que permite la garantía alcanzable (la mayor de las dos condiciones)
        avail = have_coll + free
        max_usd = min((avail - debt_now * k * HF_TARGET_MIN) / (k * HF_TARGET_MIN - 1),
                      avail / (k * DOLO_BORROW_BUFFER) - debt_now)
        max_usd = max(max_usd, 0.0)
        log.info("CORTO %s: tamaño limitado por garantía %.2f$ -> %.2f$", coin, usd, max_usd)
        usd = max_usd
        need_coll = coll_needed(usd)
    usd = min(usd, SHORT_MAX_USD)
    if usd < max(MIN_TRADE_USD, ch.dolo_min_borrow_usd):
        log.info("CORTO %s: sin garantía suficiente o por debajo del mínimo de Dolomite (%.2f$), no se abre", coin, usd)
        return
    token = ASSETS[coin]
    add_coll = max(need_coll - have_coll, 0.0) + 0.5
    if add_coll > 0.5:
        usdc_in_wallet(ch, add_coll, pf)
        add_coll = min(add_coll, ch.balances()[USDC] - 0.2) if not ch.dry_run else add_coll
        if add_coll > 0.01:
            ch.dolo_deposit_usdc(add_coll)
    amount = int(usd / prices[coin] * 10 ** DECIMALS[token])
    ch.dolo_borrow(token, amount)
    try:
        usdc_before = ch.bal(USDC) if not ch.dry_run else 0.0
        out = ch.swap_exact_in(PATHS[coin]["sell"], amount)
        got = (ch.bal(USDC) - usdc_before - 0.05) if not ch.dry_run else out / 1e6
        if got > 0.1:
            ch.dolo_deposit_usdc(got)
    except Exception as e:  # noqa: BLE001 - préstamo hecho pero venta/depósito fallidos: deshacer, no dejar una posición a medias
        log.exception("CORTO %s: fallo tras pedir prestado (%s); devuelvo el préstamo y retiro la garantía", coin, e)
        try:
            ch.dolo_repay_all(token)
            ch.dolo_withdraw_usdc(None)
        except Exception:  # noqa: BLE001
            log.exception("CORTO %s: no se pudo deshacer del todo; se reintenta en la próxima ejecución", coin)
        raise
    hf = ch.dolo_health() if not ch.dry_run else 9.9
    log.info("CORTO %s abierto en Dolomite: %.2f$ | salud %.2f", coin, usd, hf)
    record(coin, "SHORT", usd, prices[coin], reason, ch.dry_run)
    if hf < HF_MIN:
        log.warning("Salud Dolomite %.2f tras abrir < %.2f: cerrando el corto por seguridad", hf, HF_MIN)
        _, pf2 = portfolio(ch, prices)
        close_short(ch, coin, prices, pf2, "salud insuficiente")


HF_WITHDRAW_MIN = 1.01        # al retirar garantía para recomprar, el HF no baja de aquí


def _max_withdraw(b: dict, prices: dict, min_hf: float = HF_WITHDRAW_MIN) -> float:
    """USDC que se puede sacar de Aave dejando HF >= min_hf (umbral conservador). Al recomprar un corto vale
    HF_WITHDRAW_MIN (cada repago restaura el HF); para financiar OTRA posición se usa HF_TARGET_MIN."""
    return max(b["aUSDC"] - aave_debt_usd(b, prices) * min_hf / LIQ_THRESHOLD, 0.0)


def _max_withdraw_dolo(ch: Chain, b: dict, prices: dict, min_hf: float = HF_WITHDRAW_MIN) -> float:
    """USDC que se puede sacar de la cuenta Dolomite dejando salud >= min_hf."""
    k = 1 + ch.dolo_margin_ratio
    return max(b.get("dUSDC", 0.0) - dolo_debt_usd(b, prices) * k * min_hf, 0.0)


def _cover(ch: Chain, coin: str, qty: float | None, prices: dict) -> float:
    """Recompra y devuelve `qty` tokens de deuda (None = toda). Si la garantía no permite sacar todo el
    USDC de una vez (el protocolo no deja bajar la salud de 1), lo hace por tramos: cada repago libera
    garantía. Devuelve los USD cubiertos. Funciona igual en Aave (BTC/ETH) y en Dolomite (ARB)."""
    token = ASSETS[coin]
    key = f"debt_{coin}"
    px = prices[coin]
    covered = 0.0
    dolo = venue(coin) == "dolomite"
    coll_key = "dUSDC" if dolo else "aUSDC"
    withdraw = ch.dolo_withdraw_usdc if dolo else ch.aave_withdraw_usdc
    repay = ch.dolo_repay if dolo else ch.aave_repay
    repay_all = ch.dolo_repay_all if dolo else ch.aave_repay_all
    max_withdraw = (lambda bb: _max_withdraw_dolo(ch, bb, prices)) if dolo else (lambda bb: _max_withdraw(bb, prices))
    for _ in range(8):
        b = ch.balances()
        debt = b[key]
        left = debt if qty is None else min(qty - covered / px, debt)
        if debt <= 0 or left * px < 0.01:
            break
        full = left >= debt * 0.98
        buy_q = debt * 1.005 if full else left
        need = buy_q * px * 1.02 - b[USDC]
        if need > 0 and b.get(coll_key, 0.0) > 0.01:
            w = min(need, max_withdraw(b), b[coll_key])
            if w > 0.01:
                withdraw(w)
                b = ch.balances()
        afford = b[USDC] / (px * 1.02)
        if ch.dry_run:
            afford = buy_q
        if afford < buy_q:          # tramo: lo que permite la garantía liberada
            full, buy_q = False, afford
        if buy_q * px < 0.5:
            log.error("CORTO %s: no se puede recomprar más (USDC %.2f, deuda %.6f)", coin, b[USDC], debt)
            break
        amount = int(buy_q * 10 ** DECIMALS[token])
        ch.swap_exact_out(PATHS[coin]["buy"], amount)
        if full:
            repay_all(token)
            covered += debt * px
        else:
            repay(token, amount)
            covered += buy_q * px
        if ch.dry_run:
            break
    return covered


def close_short(ch: Chain, coin: str, prices: dict, pf: dict, reason: str) -> bool:
    """Recompra toda la deuda del token y la repaga (por tramos si la garantía lo exige).
    Devuelve True si la deuda queda saldada."""
    key = f"debt_{coin}"
    if pf["raw"][key] <= 0:
        return True
    usd = _cover(ch, coin, None, prices)
    left = 0.0 if ch.dry_run else ch.balances()[key]
    if usd > 0:
        record(coin, "COVER", usd, prices[coin], reason, ch.dry_run)
    if left * prices[coin] > 0.5:
        log.error("CORTO %s NO cerrado del todo: queda deuda %.6f (%.2f$). Se reintenta en la próxima ejecución.",
                  coin, left, left * prices[coin])
        return False
    log.info("CORTO %s cerrado: %.2f$", coin, usd)
    return True


def cover_short_part(ch: Chain, coin: str, qty: float, prices: dict, pf: dict, reason: str):
    """Recompra y devuelve `qty` tokens de la deuda de un corto (cierre parcial)."""
    debt = pf["raw"][f"debt_{coin}"]
    qty = min(qty, debt)
    if qty * prices[coin] < MIN_TRADE_USD:
        return False
    if qty >= debt * 0.98:
        return close_short(ch, coin, prices, pf, reason)
    usd = _cover(ch, coin, qty, prices)
    log.info("CORTO %s recortado: %.2f$", coin, usd)
    record(coin, "COVER", usd, prices[coin], reason, ch.dry_run)
    return usd > 0


# --------------------------------------------- v3: gestión de la posición abierta
def last_entry_price(coin: str, side: int) -> float | None:
    """Precio de la última entrada registrada (BUY para largos, SHORT para cortos)."""
    if not TRADES_FILE.exists():
        return None
    want = "BUY" if side == 1 else "SHORT"
    px = None
    for line in TRADES_FILE.read_text().splitlines()[1:]:
        f = line.split(",")
        if len(f) >= 5 and f[1] == coin and f[2] == want:
            px = float(f[4])
    return px


def new_pos(side: int, price: float, atr: float, qty: float, sig: int) -> dict:
    return dict(side=side, entry=price, ref=price, atr=atr, atr_today=atr, full_qty=qty,
                pt_done=0, extreme=price, pb=0.0, sig=sig)


def adopt_position(st: dict, c: str, v: float, px: float) -> dict | None:
    """Posición abierta con la v2 (sin datos de v3): reconstruye entrada y ATR desde el stop fijo."""
    sp = st["stops"].get(c)
    if not sp:
        return None
    side = 1 if v > 0 else -1
    entry = last_entry_price(c, side) or px
    atr = abs(entry - sp) / STOP_ATR
    if atr <= 0:
        return None
    pos = new_pos(side, entry, atr, abs(v) / px, side)
    pos["extreme"] = max(entry, px) if side == 1 else min(entry, px)
    log.info("v3: adopto %s %s abierto (entrada %.2f, ATR %.2f, %.6f): parciales a %.2f y %.2f",
             c, "largo" if side == 1 else "corto", entry, atr, pos["full_qty"],
             entry + side * PT_LEVELS[0] * atr, entry + side * PT_LEVELS[1] * atr)
    return pos


def realize_close(st: dict, c: str, v: float, px: float, event: str, dry_run: bool):
    """Cierre total de la posición c (v = exposición neta en USD antes de cerrar)."""
    if abs(v) < MIN_TRADE_USD:
        return
    side = 1 if v > 0 else -1
    q = abs(v) / px
    st.setdefault("pos", {}).setdefault(c, {}).setdefault("qty_now", q)
    st["pos"][c]["qty_now"] = q
    realize(st, c, side, q, px, event, dry_run, closes_op=True)


def reduce_position(ch: Chain, c: str, side: int, qty: float, prices: dict, pf: dict, reason: str) -> bool:
    """True si de verdad se vendió/recompró algo (por debajo de MIN_TRADE_USD no se opera)."""
    if side == 1:
        return bool(sell_long(ch, c, qty * prices[c], prices, reason))
    return bool(cover_short_part(ch, c, qty, prices, pf, reason))


def add_position(ch: Chain, c: str, side: int, qty: float, prices: dict, pf: dict, reason: str):
    if side == 1:
        buy_long(ch, c, qty * prices[c], prices, pf, reason)
    else:
        open_short(ch, c, qty * prices[c], prices, pf, reason)


def manage_positions(ch: Chain, st: dict, prices: dict, pf: dict, today: str, allow_rebuy: bool = True):
    """Cada ejecución: parciales, stop a entrada, stop, reentrada en máximo nuevo (en este orden)."""
    pos_all = st.setdefault("pos", {})
    for c in SYMBOLS:
        v, px = pf["val"][c], prices[c]
        sp = st["stops"].get(c)
        if abs(v) < MIN_TRADE_USD:
            pos_all.pop(c, None)
            continue
        side = 1 if v > 0 else -1
        pos = pos_all.get(c)
        if pos is None or pos["side"] != side:
            pos = adopt_position(st, c, v, px)
            if pos is None:
                continue
            pos_all[c] = pos
        qty_now = abs(v) / px
        pos["qty_now"] = qty_now
        pos.setdefault("cost_pending", trade_cost(qty_now * pos["entry"], side))
        pos.setdefault("op_pnl", 0.0)
        new_extreme = side * (px - pos["extreme"]) > 0
        partial_out = qty_now < pos["full_qty"] * PARTIAL_OUT
        if partial_out and not new_extreme:
            pos["pb"] = max(pos["pb"], side * (pos["extreme"] - px) / pos["atr"])
        pos["extreme"] = max(pos["extreme"], px) if side == 1 else min(pos["extreme"], px)
        fav = side * (px - pos["ref"]) / pos["atr"]
        traded = False
        # 1) venta parcial
        if pos["pt_done"] < len(PT_LEVELS) and fav >= PT_LEVELS[pos["pt_done"]]:
            q = min(pos["full_qty"] / 3, qty_now)
            n = pos["pt_done"] + 1
            if qty_now <= pos["full_qty"] * (1 - n / 3) * 1.02:
                # ya está recortada (p. ej. ejecución anterior que vendió pero no llegó a guardar estado)
                log.warning("PARCIAL %d %s: ya hecha (quedan %.6f de %.6f), solo actualizo estado",
                            n, c, qty_now, pos["full_qty"])
            else:
                log.warning("PARCIAL %d %s %s: %.2f (+%.1f ATR desde %.2f) -> cierro 1/3 (%.2f$)",
                            n, "largo" if side == 1 else "corto", c, px, fav, pos["ref"], q * px)
                if reduce_position(ch, c, side, q, prices, pf, f"parcial {n}"):
                    realize(st, c, side, q, px, f"parcial {n}", ch.dry_run, closes_op=False)
                    pos["qty_now"] = qty_now - q
                    traded = True
                else:
                    log.warning("PARCIAL %d %s: %.2f$ está por debajo del mínimo operable; no se vende (solo se sube el stop)", n, c, q * px)
            pos["pt_done"], pos["pb"] = n, 0.0
            be = pos["entry"] * (1 + side * BE_COST)
            if sp is not None:
                st["stops"][c] = sp = max(sp, be) if side == 1 else min(sp, be)
                log.info("%s stop a entrada: %.2f", c, sp)
        # 2) stop
        if sp is not None and ((side == 1 and px <= sp) or (side == -1 and px >= sp)):
            if traded:
                _, pf = portfolio(ch, prices)
            v = pf["val"][c]
            log.warning("STOP %s %s: %.2f %s %.2f", "largo" if side == 1 else "corto", c, px,
                        "<=" if side == 1 else ">=", sp)
            if side == 1 and v >= MIN_TRADE_USD:
                sell_long(ch, c, v, prices, "stop")
            elif side == -1 and v <= -MIN_TRADE_USD:
                if not close_short(ch, c, prices, pf, "stop"):
                    continue          # deuda pendiente: se mantienen stop y posición y se reintenta
            realize_close(st, c, v, px, "stop", ch.dry_run)
            st["stops"].pop(c, None)
            pos_all.pop(c, None)
            st.setdefault("stopped_today", {})[c] = today
            continue
        # 3) reentrada en máximo nuevo tras corrección
        q = pos["full_qty"] - qty_now
        if (allow_rebuy and not traded and partial_out and new_extreme and pos["pb"] >= RH_PULLBACK_ATR
                and pos.get("sig") == side and q * px >= MIN_TRADE_USD):
            a = pos.get("atr_today") or pos["atr"]
            log.warning("REENTRADA %s en %s nuevo %.2f (corrección %.1f ATR): recompro %.2f$",
                        c, "máximo" if side == 1 else "mínimo", px, pos["pb"], q * px)
            add_position(ch, c, side, q, prices, pf, "reentrada máximo nuevo")
            _, pf = portfolio(ch, prices)
            added = q if ch.dry_run else abs(pf["val"][c]) / px - qty_now
            if added * px < MIN_TRADE_USD:
                log.warning("REENTRADA %s no ejecutada (sin USDC/garantía suficiente); estado sin cambios", c)
                continue
            pos["entry"] = (pos["entry"] * qty_now + px * added) / (qty_now + added)
            pos.update(ref=px, atr=a, pt_done=0, pb=0.0, qty_now=qty_now + added)
            pos["cost_pending"] = pos.get("cost_pending", 0.0) + trade_cost(added * px, side)
            if sp is not None:
                ns = px - side * RH_RESTOP_ATR * a
                st["stops"][c] = max(sp, ns) if side == 1 else min(sp, ns)
                log.info("%s stop sube a %.2f; parciales a %.2f y %.2f", c, st["stops"][c],
                         px + side * PT_LEVELS[0] * a, px + side * PT_LEVELS[1] * a)
        if traded:
            _, pf = portfolio(ch, prices)
    return pf


def tidy_aave(ch: Chain, prices: dict | None = None):
    """Sin deuda en un protocolo -> retirar todo su USDC para tenerlo disponible (Aave y Dolomite por separado).
    En Dolomite, una deuda de polvo (< DOLO_DUST_USD, intereses) se salda con lo que haya en la wallet y no bloquea."""
    b = ch.balances()
    if all(b[f"debt_{c}"] <= 0 for c in ASSETS if venue(c) == "aave") and b["aUSDC"] > 0.5:
        ch.aave_withdraw_usdc(None)
    if not ch.dolo_ok:
        return
    prices = prices or {}
    clear = True
    for c in ASSETS:
        if venue(c) != "dolomite":
            continue
        d = b.get(f"debt_{c}", 0.0)
        if d <= 0:
            continue
        if prices.get(c) and d * prices[c] <= DOLO_DUST_USD and b.get(ASSETS[c], 0.0) >= d:
            ch.dolo_repay_all(ASSETS[c])
        else:
            clear = False
    if clear and b.get("dUSDC", 0.0) > 0.5:
        ch.dolo_withdraw_usdc(None)


# ----------------------------------------------------------------------- ciclo
def run_cycle(dry_run: bool, signals: bool = True):
    global _DRY_RUN
    _DRY_RUN = dry_run
    ch = Chain(dry_run)
    st = load_state()
    prices = {s: spot(s) for s in SYMBOLS}
    equity, pf = portfolio(ch, prices)
    today = datetime.now(timezone.utc).date().isoformat()

    if equity < MIN_TRADE_USD * 2:
        log.warning("Sin fondos suficientes (equity %.2f USD).", equity)
        return
    record_equity(equity, pf, prices)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if st.get("last_run"):
        gap_h = (datetime.now(timezone.utc) - datetime.fromisoformat(st["last_run"])).total_seconds() / 3600
        if gap_h > 2:
            log.warning("HUECO: %.1f h desde la ejecución anterior (GitHub no ejecutó alguna hora)", gap_h)
            st["gaps"] = st.get("gaps", 0) + 1
    st["last_run"] = now_iso
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
    if not ch.dolo_ok:
        # sin lectura de Dolomite la equity puede ir sin su garantía: no se evalúan los límites con un dato incompleto
        log.warning("Dolomite no disponible: no se evalúan los límites de riesgo en esta ejecución")
    elif dd < -RISK.max_total_drawdown_pct or day < -RISK.max_daily_loss_pct:
        reason = f"drawdown {dd:+.1%} / día {day:+.1%} supera límites"
        log.warning("CERRANDO TODO: %s", reason)
        for c in SYMBOLS:
            if pf["val"][c] <= -MIN_TRADE_USD:
                close_short(ch, c, prices, pf, "límite de riesgo")
                realize_close(st, c, pf["val"][c], prices[c], "límite de riesgo", ch.dry_run)
            elif pf["val"][c] >= MIN_TRADE_USD:
                sell_long(ch, c, pf["val"][c], prices, "límite de riesgo")
                realize_close(st, c, pf["val"][c], prices[c], "límite de riesgo", ch.dry_run)
        tidy_aave(ch, prices)
        st["stops"], st["pos"] = {}, {}
        st.update(halted=True, halt_reason=reason)
        save_state(st)
        return

    # --- salud de Aave y de Dolomite (cada ejecución): se cierran solo los cortos del protocolo afectado ---
    low = [v for v, hf in (("aave", pf["hf"]), ("dolomite", pf.get("hf_dolo", 1e9))) if hf < HF_MIN]
    if low:
        log.warning("HF Aave %.2f / Dolomite %.2f: cerrando cortos de %s por seguridad", pf["hf"], pf.get("hf_dolo", 1e9), low)
        for c in SYMBOLS:
            if pf["raw"].get(f"debt_{c}", 0.0) * prices[c] > DOLO_DUST_USD and venue(c) in low:
                if close_short(ch, c, prices, pf, "factor de salud bajo"):
                    realize_close(st, c, pf["val"][c], prices[c], "factor de salud bajo", ch.dry_run)
                    st["stops"].pop(c, None)
                    st.get("pos", {}).pop(c, None)
        tidy_aave(ch, prices)
        equity, pf = portfolio(ch, prices)

    # --- posición abierta: parciales, stops, reentrada (cada ejecución) ---
    try:
        # en la primera ejecución del día no se recompra: primero manda la señal diaria nueva
        pf = manage_positions(ch, st, prices, pf, today, allow_rebuy=not signals)
    except Exception as e:  # noqa: BLE001 - si la v3 falla, los stops de siempre siguen protegiendo
        log.exception("ERROR en gestión v3 (%s): aplico solo los stops", e)
        _, pf = portfolio(ch, prices)
        for c in SYMBOLS:
            sp, v = st["stops"].get(c), pf["val"][c]
            if sp and v >= MIN_TRADE_USD and prices[c] <= sp:
                sell_long(ch, c, v, prices, "stop")
            elif sp and v <= -MIN_TRADE_USD and prices[c] >= sp:
                if not close_short(ch, c, prices, pf, "stop"):
                    continue
            else:
                continue
            try:
                realize_close(st, c, v, prices[c], "stop", ch.dry_run)
            except Exception:  # noqa: BLE001 - la contabilidad nunca debe impedir el stop
                log.exception("no se pudo apuntar el resultado de %s", c)
            st["stops"].pop(c, None)
            st.get("pos", {}).pop(c, None)
            st.setdefault("stopped_today", {})[c] = today
        _, pf = portfolio(ch, prices)
    if not signals:
        tidy_aave(ch, prices)
        save_state(st)
        log.info("Chequeo de stops completado.")
        return

    # --- señales diarias ---
    equity, pf = portfolio(ch, prices)
    plan = {}
    for c in SYMBOLS:
        per_asset = equity * WEIGHTS.get(c, 1 / len(SYMBOLS)) * POSITION_FRACTION
        df = coinbase_daily(c)
        sig = int(trend_ema.signals(df, **STRATEGY_PARAMS).iloc[-1])
        if sig == -1 and not SHORTS_ENABLED:
            sig = 0
        if sig == -1 and venue(c) == "dolomite" and not ch.dolo_ok:
            log.warning("%s: señal de corto pero Dolomite no disponible; se queda fuera", c)
            sig = 0
        atr = float(indicators.atr(df, ATR_N).iloc[-1])
        have = pf["val"][c]
        target = per_asset if sig == 1 else (-min(per_asset, SHORT_MAX_USD) if sig == -1 else 0.0)
        if st.get("stopped_today", {}).get(c) == today:
            target = 0.0
        pos = st.get("pos", {}).get(c)
        if pos and sig == pos["side"] and abs(have) >= MIN_TRADE_USD:
            # v3: la operación sigue abierta -> no se redimensiona (las parciales/recompras las gestiona
            # manage_positions); solo se actualizan señal y ATR del día para la próxima reentrada
            target = have
            pos.update(sig=sig, atr_today=atr)
        log.info("%s cierre=%.2f señal=%+d ATR=%.2f tengo=%+.2f$ objetivo=%+.2f$", c, df.close.iloc[-1], sig, atr, have, target)
        plan[c] = dict(sig=sig, atr=atr, have=have, target=target)

    # 1) cierres: largos que sobran o que pasan a corto/0; cortos que pasan a largo/0
    for c, p in plan.items():
        have, target = p["have"], p["target"]
        if have >= MIN_TRADE_USD and (target <= 0 or (target - have) < -MIN_TRADE_USD and abs(target - have) / have > 0.25):
            sell_long(ch, c, have if target <= 0 else have - target, prices, f"señal {p['sig']:+d}")
            if target <= 0:
                realize_close(st, c, have, prices[c], f"señal {p['sig']:+d}", ch.dry_run)
            else:
                realize(st, c, 1, (have - target) / prices[c], prices[c], f"ajuste señal {p['sig']:+d}", ch.dry_run, False)
            if target <= 0:
                st["stops"].pop(c, None)
                st.get("pos", {}).pop(c, None)
        elif have <= -MIN_TRADE_USD and target >= 0:
            if close_short(ch, c, prices, pf, f"señal {p['sig']:+d}"):
                realize_close(st, c, have, prices[c], f"señal {p['sig']:+d}", ch.dry_run)
                st["stops"].pop(c, None)
                st.get("pos", {}).pop(c, None)
            else:
                p["target"] = have    # no abrir nada nuevo mientras quede deuda
    # 2) aperturas
    equity, pf = portfolio(ch, prices)
    for c, p in plan.items():
        have, target = pf["val"][c], p["target"]
        try:
            if target > 0 and have > -MIN_TRADE_USD and target - max(have, 0) > MIN_TRADE_USD:
                buy_long(ch, c, target - max(have, 0), prices, pf, f"señal {p['sig']:+d}")
                equity, pf = portfolio(ch, prices)
            elif target < 0 and have > -MIN_TRADE_USD:
                open_short(ch, c, -target, prices, pf, f"señal {p['sig']:+d}")
                equity, pf = portfolio(ch, prices)
        except Exception as e:  # noqa: BLE001 - que una apertura fallida (p. ej. Dolomite) no deje sin stops al resto
            log.exception("ERROR abriendo %s (%s); el resto del ciclo sigue", c, e)
            equity, pf = portfolio(ch, prices)
    # 3) stops: toda posición tiene stop; nuevas entradas lo fijan hoy
    tidy_aave(ch, prices)
    equity, pf = portfolio(ch, prices)
    pos_all = st.setdefault("pos", {})
    for c, p in plan.items():
        v = pf["val"][c]
        side = 1 if v >= MIN_TRADE_USD and p["target"] > 0 else (-1 if v <= -MIN_TRADE_USD and p["target"] < 0 else 0)
        if side:
            st["stops"].setdefault(c, prices[c] - side * STOP_ATR * p["atr"])
            if c not in pos_all or pos_all[c]["side"] != side:
                pos_all[c] = new_pos(side, prices[c], p["atr"], abs(v) / prices[c], p["sig"])
                pos_all[c].update(qty_now=abs(v) / prices[c], cost_pending=trade_cost(abs(v), side), op_pnl=0.0)
                log.info("%s nueva operación %s: entrada %.2f stop %.2f parciales a %.2f y %.2f", c,
                         "larga" if side == 1 else "corta", prices[c], st["stops"][c],
                         prices[c] + side * PT_LEVELS[0] * p["atr"], prices[c] + side * PT_LEVELS[1] * p["atr"])
        elif abs(v) < MIN_TRADE_USD:
            st["stops"].pop(c, None)
            pos_all.pop(c, None)
    # 4) acumulación: el 30 % de la ganancia realizada nueva compra BTC/ETH y se aparta
    try:
        accumulate(ch, st, prices, pf)
    except Exception as e:  # noqa: BLE001 - la acumulación nunca debe bloquear el ciclo
        log.exception("ERROR en acumulación (%s); se reintenta mañana", e)
    st["last_signal_day"] = today
    save_state(st)
    equity, pf = portfolio(ch, prices)
    acc = st.get("accum", {})
    log.info("Resultado realizado acumulado: %+.2f$ | equity trading %.2f$ (%+.2f$ desde %.2f$) | acumulado %s = %.2f$ (coste %.2f$)",
             pnl_total(), equity, equity - CAPITAL_REF, CAPITAL_REF,
             {c: round(q, 6) for c, q in acc.get("qty", {}).items()}, pf["accum_usd"], acc.get("cost_usd", 0.0))
    log.info("Ciclo diario completado. Stops: %s | posiciones: %s", st["stops"], {c: dict(entrada=round(p["entry"], 2), parciales=p["pt_done"], ref=round(p["ref"], 2)) for c, p in st.get("pos", {}).items()})


# ------------------------------------------------------------- v4: acumulación
ACCUM_FILE = LOG_DIR / "accum.csv"


def accumulate(ch: Chain, st: dict, prices: dict, pf: dict):
    """Cada ganancia realizada NUEVA (por encima del máximo realizado anterior, hwm) se reparte:
    70 % se queda en la base de trading; 30 % se apunta como pendiente y, cuando hay al menos MIN_TRADE_USD
    por moneda, compra ACCUM_COINS a partes iguales y las aparta. Las pérdidas no restan lo acumulado:
    hay que recuperar el máximo anterior antes de volver a acumular."""
    acc = st.setdefault("accum", {"hwm": 0.0, "pending_usd": 0.0, "qty": {}, "cost_usd": 0.0})
    realized = pnl_total()
    if realized > acc["hwm"] + 0.005:
        gain = realized - acc["hwm"]
        acc["pending_usd"] += gain * ACCUM_SHARE
        acc["hwm"] = realized
        log.info("ACUMULACIÓN: ganancia nueva %+.2f$ -> %.2f$ pendientes de comprar (%.0f %%)", gain, acc["pending_usd"], ACCUM_SHARE * 100)
    per_coin = acc["pending_usd"] / len(ACCUM_COINS)
    if per_coin < MIN_TRADE_USD:
        return
    if any(pf["raw"].get(f"debt_{c}", 0.0) * prices.get(c, 1.0) > DOLO_DUST_USD for c in ASSETS):
        log.info("ACUMULACIÓN: %.2f$ pendientes; se espera a no tener cortos abiertos (no retirar garantía de Aave/Dolomite)", acc["pending_usd"])
        return
    for c in ACCUM_COINS:
        if c not in prices:
            continue
        _, pf = portfolio(ch, prices)
        before = ch.balances()
        tok = ASSETS[c]
        usdc_in_wallet(ch, per_coin, pf)
        usd = min(per_coin, ch.balances()[USDC]) if not ch.dry_run else per_coin
        if usd < MIN_TRADE_USD:
            log.warning("ACUMULACIÓN %s: sin USDC libre (%.2f$); se intenta en el próximo ciclo", c, usd)
            continue
        ch.swap_exact_in(PATHS[c]["buy"], int(usd * 1e6))
        got = (ch.balances()[tok] - before[tok]) if not ch.dry_run else usd / prices[c]
        if got <= 0:
            # el swap se confirmó (si no, _send habría lanzado): el saldo leído va con retraso; se estima
            got = usd / prices[c] * (1 - 0.002)
            log.error("ACUMULACIÓN %s: el saldo no refleja aún la compra; se apunta una estimación de %.8f", c, got)
        acc["qty"][c] = acc["qty"].get(c, 0.0) + got
        acc["cost_usd"] += usd
        acc["pending_usd"] = max(acc["pending_usd"] - usd, 0.0)
        # el dinero apartado sale de la equity de trading: los límites de riesgo (% desde máximo y % del día)
        # se reescalan en proporción para que la operación no cuente ni como pérdida ni como ganancia
        eq_before = pf["cash"] + sum(pf["val"].values())
        if eq_before > usd:
            for k in ("peak_equity", "day_start_equity"):
                if st.get(k):
                    st[k] = st[k] * (eq_before - usd) / eq_before
        record(c, "ACUMULAR", usd, prices[c], "acumulación 30 %", ch.dry_run)
        if not ch.dry_run:
            new = not ACCUM_FILE.exists()
            with ACCUM_FILE.open("a") as f:
                if new:
                    f.write("time,coin,usd,price,qty,qty_total,cost_total_usd\n")
                f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')},{c},{usd:.2f},{prices[c]:.2f},{got:.8f},{acc['qty'][c]:.8f},{acc['cost_usd']:.2f}\n")
        log.warning("ACUMULACIÓN: comprados %.8f %s por %.2f$ (apartados; total %s)", got, c, usd, {k: round(v, 6) for k, v in acc["qty"].items()})
        save_state(st)


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
    tidy_aave(ch, prices)
    time.sleep(5)
    equity2, pf = portfolio(ch, prices)
    log.info("=== PRUEBA TERMINADA: equity %.2f -> %.2f -> %.2f (coste %.2f$) ===", equity0, equity1, equity2, equity0 - equity2)
