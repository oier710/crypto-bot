"""Comprobación on-chain (solo lectura, sin claves) de DOLOMITE en Arbitrum One antes de usarlo para
cortos de ARB (decisión de Oier 2026-10-29: ARB al 15 % del capital, largo y corto).

Qué mira:
  - Que la dirección de DolomiteMargin es la buena y que los proxies de depósito/préstamo apuntan a ella.
  - Mercados USDC, ARB, WETH, WBTC: id, abierto/cerrando, suministrado, prestado, disponible, interés de préstamo,
    prima de margen, precio del oráculo, tope de suministro.
  - Parámetros globales: margen mínimo (ratio de liquidación) y penalización de liquidación.
Salida: results/research_dolomite_check.txt
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "research_dolomite_check.txt"
w3 = Web3(Web3.HTTPProvider(os.getenv("ARB_RPC_URL", "https://arb1.arbitrum.io/rpc"), request_kwargs={"timeout": 30}))

DOLOMITE_MARGIN = "0x6Bd780E7fDf01D77e4d475c821f1e7AE05409072"
DEPOSIT_WITHDRAWAL_PROXY = "0xAdB9D68c613df4AA363B42161E1282117C7B9594"
BORROW_POSITION_PROXY_V2 = "0x38E49A617305101216eC6306e3a18065D14Bf3a7"
TOKENS = {
    "USDC": ("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
    "ARB": ("0x912CE59144191C1204E64559FE8253a0e49E6548", 18),
    "WETH": ("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18),
    "WBTC": ("0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", 8),
}
WALLET = "0x44Cc8F4417C5353686D93000431b240e9d187923"

MARGIN_ABI = json.loads("""[
 {"name":"getNumMarkets","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint256"}]},
 {"name":"getMarketIdByTokenAddress","type":"function","stateMutability":"view","inputs":[{"name":"token","type":"address"}],"outputs":[{"type":"uint256"}]},
 {"name":"getMarketTokenAddress","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"type":"address"}]},
 {"name":"getMarketIsClosing","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"type":"bool"}]},
 {"name":"getMarketTotalPar","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"borrow","type":"uint128"},{"name":"supply","type":"uint128"}],"type":"tuple"}]},
 {"name":"getMarketInterestRate","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]},
 {"name":"getMarketMarginPremium","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]},
 {"name":"getMarketPrice","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]},
 {"name":"getMarketMaxWei","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"sign","type":"bool"},{"name":"value","type":"uint256"}],"type":"tuple"}]},
 {"name":"getMarginRatio","type":"function","stateMutability":"view","inputs":[],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]},
 {"name":"getMinBorrowedValue","type":"function","stateMutability":"view","inputs":[],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]},
 {"name":"getIsGlobalOperator","type":"function","stateMutability":"view","inputs":[{"name":"o","type":"address"}],"outputs":[{"type":"bool"}]},
 {"name":"getLiquidationSpread","type":"function","stateMutability":"view","inputs":[],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]},
 {"name":"getAccountWei","type":"function","stateMutability":"view","inputs":[{"components":[{"name":"owner","type":"address"},{"name":"number","type":"uint256"}],"name":"account","type":"tuple"},{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"sign","type":"bool"},{"name":"value","type":"uint256"}],"type":"tuple"}]},
 {"name":"getAccountValues","type":"function","stateMutability":"view","inputs":[{"components":[{"name":"owner","type":"address"},{"name":"number","type":"uint256"}],"name":"account","type":"tuple"}],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"},{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]},
 {"name":"getAdjustedAccountValues","type":"function","stateMutability":"view","inputs":[{"components":[{"name":"owner","type":"address"},{"name":"number","type":"uint256"}],"name":"account","type":"tuple"}],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"},{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]}
]""")
PROXY_ABI = json.loads('[{"name":"DOLOMITE_MARGIN","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]}]')
ERC = json.loads('[{"name":"symbol","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"string"}]}]')


def main():
    L = [f"bloque {w3.eth.block_number}"]
    m = w3.eth.contract(address=DOLOMITE_MARGIN, abi=MARGIN_ABI)
    code = w3.eth.get_code(DOLOMITE_MARGIN)
    L.append(f"DolomiteMargin {DOLOMITE_MARGIN}: código {len(code)} bytes, mercados {m.functions.getNumMarkets().call()}")
    for name, addr in [("DepositWithdrawalProxy", DEPOSIT_WITHDRAWAL_PROXY), ("BorrowPositionProxyV2", BORROW_POSITION_PROXY_V2)]:
        try:
            dm = w3.eth.contract(address=addr, abi=PROXY_ABI).functions.DOLOMITE_MARGIN().call()
            L.append(f"{name} {addr}: DOLOMITE_MARGIN = {dm}  {'OK' if dm.lower() == DOLOMITE_MARGIN.lower() else '¡NO COINCIDE!'}")
        except Exception as e:  # noqa: BLE001
            L.append(f"{name} {addr}: sin getter DOLOMITE_MARGIN ({type(e).__name__}); código {len(w3.eth.get_code(addr))} bytes")

    for name, addr in [("DepositWithdrawalProxy", DEPOSIT_WITHDRAWAL_PROXY), ("BorrowPositionProxyV2", BORROW_POSITION_PROXY_V2)]:
        L.append(f"{name} es operador global de DolomiteMargin: {m.functions.getIsGlobalOperator(addr).call()}")
    L.append(f"Préstamo mínimo (getMinBorrowedValue): {m.functions.getMinBorrowedValue().call()[0] / 1e36:.4f} $")
    ratio = m.functions.getMarginRatio().call()[0] / 1e18
    spread = m.functions.getLiquidationSpread().call()[0] / 1e18
    L.append(f"\nMargen mínimo global: {ratio:.3f} -> liquidación si garantía < {1+ratio:.3f} x deuda (HF equivalente 1.0). "
             f"Penalización de liquidación: {spread:.3f}")

    L.append("\n## Mercados")
    for name, (addr, dec) in TOKENS.items():
        try:
            mid = m.functions.getMarketIdByTokenAddress(addr).call()
        except Exception as e:  # noqa: BLE001
            L.append(f"{name}: SIN MERCADO en Dolomite ({type(e).__name__})")
            continue
        tok = m.functions.getMarketTokenAddress(mid).call()
        closing = m.functions.getMarketIsClosing(mid).call()
        tp = m.functions.getMarketTotalPar(mid).call()
        rate = m.functions.getMarketInterestRate(mid).call()[0]
        prem = m.functions.getMarketMarginPremium(mid).call()[0] / 1e18
        price = m.functions.getMarketPrice(mid).call()[0] / 10 ** (36 - dec)
        maxw = m.functions.getMarketMaxWei(mid).call()
        supply, borrow = tp[1] / 10 ** dec, tp[0] / 10 ** dec
        apr = rate * 365 * 86400 / 1e18 * 100
        L.append(f"{name}: id {mid} token {tok} {'CERRANDO (no se puede pedir prestado)' if closing else 'abierto'}")
        L.append(f"   precio oráculo {price:,.4f} $ | suministrado {supply:,.2f} ({supply*price:,.0f} $) | prestado {borrow:,.2f} "
                 f"({borrow*price:,.0f} $) | disponible {(supply-borrow)*price:,.0f} $ | interés préstamo {apr:.2f}%/año | "
                 f"prima margen {prem:.3f} | tope suministro {maxw[1]/10**dec:,.0f}{' (sin tope)' if maxw[1] == 0 else ''}")

    L.append("\n## Cuenta del bot en Dolomite (debería estar vacía)")
    for n in (0, 1):
        sup, bor = m.functions.getAccountValues((WALLET, n)).call()
        L.append(f"cuenta {n}: suministrado {sup[0]/1e36:,.4f} $ | prestado {bor[0]/1e36:,.4f} $")

    L.append("\n## Simulación de margen para un corto de ARB de 17 $ con 60 $ de USDC de garantía (cuenta aislada)")
    L.append(f"   garantía/deuda = {60/17:.2f} -> margen {60/17-1:.2f} frente al mínimo {ratio:.3f}: "
             f"{'muy holgado' if 60/17-1 > 3*ratio else 'justo'}. ARB tendría que subir x{60/17/(1+ratio):.2f} para liquidar.")
    txt = "\n".join(L)
    OUT.write_text(txt)
    print(txt)


if __name__ == "__main__":
    main()
