"""Diagnóstico (solo lectura, sin firmar nada): el 2026-11-06 00:07 UTC, con el corto de BTC ya abierto en la
cuenta 1 de Dolomite, el depósito de 17.37 USDC para el corto de ETH falló al estimar gas con
"execution reverted" sin motivo (0x). 8 s antes, un depósito de 34.93 USDC en la misma cuenta funcionó.

Repite AHORA, como simulación (eth_call / eth_estimateGas desde la wallet del bot, sin enviar nada):
  1. depositWei(cuenta 1, USDC, 17.37) y otros importes
  2. withdrawWei(cuenta 1, WETH, ~5 $, None)  -> pedir prestado ETH con el corto de BTC abierto
  3. lo mismo en la cuenta 2 (cuenta limpia) para comparar
y muestra el dato crudo del error (motivo) de cada una, más saldos, permisos, topes de mercado y estado de la cuenta.
Salida: results/research_dolomite_diag.txt
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "research_dolomite_diag.txt"
w3 = Web3(Web3.HTTPProvider(os.getenv("ARB_RPC_URL", "https://arb1.arbitrum.io/rpc"), request_kwargs={"timeout": 30}))

WALLET = "0x44Cc8F4417C5353686D93000431b240e9d187923"
DOLO = "0x6Bd780E7fDf01D77e4d475c821f1e7AE05409072"
PROXY = "0xAdB9D68c613df4AA363B42161E1282117C7B9594"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
WBTC = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"
DEC = {USDC: 6, WETH: 18, WBTC: 8}
NAME = {USDC: "USDC", WETH: "WETH", WBTC: "WBTC"}

_ACC = '{"components":[{"name":"owner","type":"address"},{"name":"number","type":"uint256"}],"name":"a","type":"tuple"}'
_VAL = '{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}'
_WEI = '{"components":[{"name":"sign","type":"bool"},{"name":"value","type":"uint256"}],"type":"tuple"}'
DOLO_ABI = json.loads('[{"name":"getMarketIdByTokenAddress","type":"function","stateMutability":"view","inputs":[{"name":"t","type":"address"}],"outputs":[{"type":"uint256"}]},'
                      '{"name":"getMarketMaxWei","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[' + _WEI + ']},'
                      '{"name":"getMarketMaxBorrowWei","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[' + _WEI + ']},'
                      '{"name":"getMarketTotalWei","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"borrow","type":"uint128"},{"name":"supply","type":"uint128"}],"type":"tuple"}]},'
                      '{"name":"getMarketIsClosing","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"type":"bool"}]},'
                      '{"name":"getMarketPrice","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[' + _VAL + ']},'
                      '{"name":"getAccountWei","type":"function","stateMutability":"view","inputs":[' + _ACC + ',{"name":"m","type":"uint256"}],"outputs":[' + _WEI + ']},'
                      '{"name":"getAccountStatus","type":"function","stateMutability":"view","inputs":[' + _ACC + '],"outputs":[{"type":"uint8"}]},'
                      '{"name":"getAccountMarketsWithBalances","type":"function","stateMutability":"view","inputs":[' + _ACC + '],"outputs":[{"type":"uint256[]"}]},'
                      '{"name":"getAccountNumberOfMarketsWithDebt","type":"function","stateMutability":"view","inputs":[' + _ACC + '],"outputs":[{"type":"uint256"}]},'
                      '{"name":"getAccountMaxNumberOfMarketsWithBalances","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint256"}]},'
                      '{"name":"getAdjustedAccountValues","type":"function","stateMutability":"view","inputs":[' + _ACC + '],"outputs":[' + _VAL + ',' + _VAL + ']},'
                      '{"name":"getIsLocalOperator","type":"function","stateMutability":"view","inputs":[{"name":"o","type":"address"},{"name":"op","type":"address"}],"outputs":[{"type":"bool"}]},'
                      '{"name":"getIsGlobalOperator","type":"function","stateMutability":"view","inputs":[{"name":"o","type":"address"}],"outputs":[{"type":"bool"}]}]')
PROXY_ABI = json.loads('[{"name":"depositWei","type":"function","stateMutability":"nonpayable","inputs":[{"name":"a","type":"uint256"},{"name":"m","type":"uint256"},{"name":"w","type":"uint256"}],"outputs":[]},'
                       '{"name":"withdrawWei","type":"function","stateMutability":"nonpayable","inputs":[{"name":"a","type":"uint256"},{"name":"m","type":"uint256"},{"name":"w","type":"uint256"},{"name":"f","type":"uint8"}],"outputs":[]}]')
ERC = json.loads('[{"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"name":"a","type":"address"}],"outputs":[{"type":"uint256"}]},'
                 '{"name":"allowance","type":"function","stateMutability":"view","inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"outputs":[{"type":"uint256"}]}]')


def decode(data_hex: str) -> str:
    if not data_hex or data_hex in ("0x", "0x0"):
        return "(sin motivo: datos vacíos)"
    d = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if d.startswith("08c379a0"):          # Error(string)
        try:
            raw = bytes.fromhex(d[8:])
            ln = int.from_bytes(raw[32:64], "big")
            return "Error(string): " + raw[64:64 + ln].decode(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    return f"datos crudos {data_hex[:200]}"


def sim(label, fn):
    call = {"from": WALLET, "to": fn.address, "data": fn._encode_transaction_data()}
    out = []
    for method, params in (("eth_call", [call, "latest"]), ("eth_estimateGas", [call])):
        r = w3.provider.make_request(method, params)
        if "error" in r:
            e = r["error"]
            out.append(f"{method}: REVIERTE — {e.get('message')} | {decode(e.get('data', ''))}")
        else:
            out.append(f"{method}: OK ({int(r['result'], 16) if method == 'eth_estimateGas' else 'sin error'})")
    return [f"- {label}"] + [f"    {x}" for x in out]


def safe(L, label, f):
    try:
        f()
    except Exception as e:  # noqa: BLE001
        L.append(f"[{label}] no se pudo leer: {type(e).__name__}: {str(e)[:200]}")


def main():
    L = []
    try:
        _main(L)
    finally:
        OUT.write_text("\n".join(L))
        print("\n".join(L))


def _main(L):
    m = w3.eth.contract(address=DOLO, abi=DOLO_ABI)
    p = w3.eth.contract(address=PROXY, abi=PROXY_ABI)
    mid = {t: m.functions.getMarketIdByTokenAddress(t).call() for t in (USDC, WETH, WBTC)}
    L += [f"bloque {w3.eth.block_number}", f"ids de mercado: { {NAME[t]: i for t, i in mid.items()} }"]
    L.append("\n## Simulaciones (no se envía nada) — lo importante")
    eth_px = 2300.0
    try:
        eth_px = m.functions.getMarketPrice(mid[WETH]).call()[0] / 1e18
    except Exception as e:  # noqa: BLE001
        L.append(f"[precio WETH] {e}")
    weth5 = int(5 / eth_px * 1e18)
    for usd in (17.37, 5.0, 1.0):
        safe(L, "sim", lambda usd=usd: L.extend(sim(f"depositWei(cuenta 1, USDC, {usd})", p.functions.depositWei(1, mid[USDC], int(usd * 1e6)))))
    safe(L, "sim", lambda: L.extend(sim("depositWei(cuenta 2, USDC, 17.37)  [cuenta limpia]", p.functions.depositWei(2, mid[USDC], 17_370_000))))
    safe(L, "sim", lambda: L.extend(sim(f"withdrawWei(cuenta 1, WETH, {weth5/1e18:.6f} ≈ 5 $, None)  [pedir prestado ETH con BTC abierto]",
                                        p.functions.withdrawWei(1, mid[WETH], weth5, 3))))
    safe(L, "sim", lambda: L.extend(sim("withdrawWei(cuenta 1, WBTC, pequeño, None)  [pedir más BTC]", p.functions.withdrawWei(1, mid[WBTC], 500, 3))))
    L.append("\n## Wallet")
    for t in (USDC, WETH, WBTC):
        c = w3.eth.contract(address=t, abi=ERC)
        safe(L, NAME[t], lambda c=c, t=t: L.append(f"{NAME[t]}: saldo {c.functions.balanceOf(WALLET).call() / 10**DEC[t]:.8f} | permiso a DolomiteMargin "
                 f"{c.functions.allowance(WALLET, DOLO).call() / 10**DEC[t]:.3g} | permiso al proxy {c.functions.allowance(WALLET, PROXY).call() / 10**DEC[t]:.3g}"))
    safe(L, "operador", lambda: L.append(f"proxy operador global: {m.functions.getIsGlobalOperator(PROXY).call()}"))
    safe(L, "operador local", lambda: L.append(f"operador local de la wallet: {m.functions.getIsLocalOperator(WALLET, PROXY).call()}"))
    L.append("\n## Mercados")
    def market(t):
        i = mid[t]
        tw = m.functions.getMarketTotalWei(i).call()
        mx = m.functions.getMarketMaxWei(i).call()[1]
        try:
            mb = m.functions.getMarketMaxBorrowWei(i).call()[1]
        except Exception:  # noqa: BLE001
            mb = -1
        px = m.functions.getMarketPrice(i).call()[0] / 10 ** (36 - DEC[t])
        L.append(f"{NAME[t]}: suministro {tw[1]/10**DEC[t]:,.4f} | prestado {tw[0]/10**DEC[t]:,.4f} | tope suministro "
                 f"{'sin tope' if mx == 0 else f'{mx/10**DEC[t]:,.4f}'} | tope préstamo "
                 f"{'sin tope' if mb == 0 else ('n/d' if mb < 0 else f'{mb/10**DEC[t]:,.4f}')} | cerrando "
                 f"{m.functions.getMarketIsClosing(i).call()} | precio {px:,.2f}")
        if mx and tw[1] + 17_370_000 * (t == USDC) > mx:
            L.append(f"   ¡¡ {NAME[t]} SIN HUECO bajo su tope de suministro: un depósito revierte !!")
    for t in (USDC, WETH, WBTC):
        safe(L, f"mercado {NAME[t]}", lambda t=t: market(t))
    L.append("\n## Cuentas de la wallet en Dolomite")
    def account(n):
        acc = (WALLET, n)
        sup, bor = m.functions.getAdjustedAccountValues(acc).call()
        try:
            mk = m.functions.getAccountMarketsWithBalances(acc).call()
            nd = m.functions.getAccountNumberOfMarketsWithDebt(acc).call()
        except Exception:  # noqa: BLE001
            mk, nd = "n/d", "n/d"
        bals = []
        for t in (USDC, WETH, WBTC):
            sgn, val = m.functions.getAccountWei(acc, mid[t]).call()
            if val:
                bals.append(f"{NAME[t]} {'+' if sgn else '-'}{val/10**DEC[t]:.8f}")
        try:
            stt = m.functions.getAccountStatus(acc).call()
        except Exception:  # noqa: BLE001
            stt = "n/d"
        L.append(f"cuenta {n}: estado {stt} | garantía ajustada {sup[0]/1e36:.4f} $ | "
                 f"deuda ajustada {bor[0]/1e36:.4f} $ | mercados con saldo {mk} | con deuda {nd} | {' '.join(bals) or 'vacía'}")
    for n in (0, 1, 2):
        safe(L, f"cuenta {n}", lambda n=n: account(n))
    try:
        L.append(f"máximo de mercados con saldo por cuenta: {m.functions.getAccountMaxNumberOfMarketsWithBalances().call()}")
    except Exception:  # noqa: BLE001
        pass



if __name__ == "__main__":
    main()
