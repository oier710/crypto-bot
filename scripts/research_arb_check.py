"""Comprobación on-chain (solo lectura, sin claves) de ARB en Arbitrum One antes de añadirlo al bot:
  - Aave V3: ¿se puede pedir prestado ARB (para cortos)? tope de préstamo, interés actual, liquidez.
  - Uniswap V3: pools ARB/WETH y ARB/USDC (0.05 % y 0.30 %) y su liquidez, para elegir la ruta de swap.
  - Cotización real de 40 $ -> ARB -> 40 $ (ida y vuelta) por la mejor ruta, para medir el coste.
Salida: results/research_arb_check.txt
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "research_arb_check.txt"
w3 = Web3(Web3.HTTPProvider(os.getenv("ARB_RPC_URL", "https://arb1.arbitrum.io/rpc"), request_kwargs={"timeout": 30}))

ARB = "0x912CE59144191C1204E64559FE8253a0e49E6548"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
WBTC = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"
DEC = {ARB: 18, WETH: 18, USDC: 6, WBTC: 8}
ERC = json.loads('[{"name":"symbol","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"string"}]},'
                 '{"name":"decimals","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint8"}]},'
                 '{"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"name":"a","type":"address"}],"outputs":[{"type":"uint256"}]}]')
PROVIDER_ABI = json.loads('[{"name":"getPool","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},'
                          '{"name":"getPoolDataProvider","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]}]')
DP_ABI = json.loads('[{"name":"getReserveConfigurationData","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"name":"decimals","type":"uint256"},{"name":"ltv","type":"uint256"},{"name":"liquidationThreshold","type":"uint256"},{"name":"liquidationBonus","type":"uint256"},{"name":"reserveFactor","type":"uint256"},{"name":"usageAsCollateralEnabled","type":"bool"},{"name":"borrowingEnabled","type":"bool"},{"name":"stableBorrowRateEnabled","type":"bool"},{"name":"isActive","type":"bool"},{"name":"isFrozen","type":"bool"}]},'
                    '{"name":"getReserveCaps","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"name":"borrowCap","type":"uint256"},{"name":"supplyCap","type":"uint256"}]},'
                    '{"name":"getReserveData","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"name":"unbacked","type":"uint256"},{"name":"accruedToTreasuryScaled","type":"uint256"},{"name":"totalAToken","type":"uint256"},{"name":"totalStableDebt","type":"uint256"},{"name":"totalVariableDebt","type":"uint256"},{"name":"liquidityRate","type":"uint256"},{"name":"variableBorrowRate","type":"uint256"},{"name":"stableBorrowRate","type":"uint256"},{"name":"averageStableBorrowRate","type":"uint256"},{"name":"liquidityIndex","type":"uint256"},{"name":"variableBorrowIndex","type":"uint256"},{"name":"lastUpdateTimestamp","type":"uint40"}]},'
                    '{"name":"getReserveTokensAddresses","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"name":"a","type":"address"},{"name":"s","type":"address"},{"name":"v","type":"address"}]},'
                    '{"name":"getPaused","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"type":"bool"}]}]')
FACTORY_ABI = json.loads('[{"name":"getPool","type":"function","stateMutability":"view","inputs":[{"type":"address"},{"type":"address"},{"type":"uint24"}],"outputs":[{"type":"address"}]}]')
QUOTER_ABI = json.loads('[{"name":"quoteExactInput","type":"function","stateMutability":"nonpayable","inputs":[{"name":"path","type":"bytes"},{"name":"amountIn","type":"uint256"}],"outputs":[{"name":"amountOut","type":"uint256"},{"name":"a","type":"uint160[]"},{"name":"b","type":"uint32[]"},{"name":"gasEstimate","type":"uint256"}]}]')


def path(hops):
    out = b""
    for i, (tin, fee, tout) in enumerate(hops):
        if i == 0:
            out += bytes.fromhex(tin[2:])
        out += fee.to_bytes(3, "big") + bytes.fromhex(tout[2:])
    return out


def main():
    L = []
    L.append(f"bloque {w3.eth.block_number}")
    arb = w3.eth.contract(address=ARB, abi=ERC)
    L.append(f"ARB token: {arb.functions.symbol().call()} decimales {arb.functions.decimals().call()}")

    prov = w3.eth.contract(address="0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", abi=PROVIDER_ABI)
    dp = w3.eth.contract(address=prov.functions.getPoolDataProvider().call(), abi=DP_ABI)
    L.append("\n## Aave V3 Arbitrum")
    for name, addr in [("ARB", ARB), ("WETH", WETH), ("WBTC", WBTC)]:
        cfg = dp.functions.getReserveConfigurationData(addr).call()
        caps = dp.functions.getReserveCaps(addr).call()
        rd = dp.functions.getReserveData(addr).call()
        d = DEC[addr]
        supplied, debt = rd[2] / 10 ** d, rd[4] / 10 ** d
        L.append(f"{name}: activo={cfg[8]} congelado={cfg[9]} pausado={dp.functions.getPaused(addr).call()} "
                 f"prestable={cfg[6]} LTV={cfg[1]/100:.0f}% umbral_liq={cfg[2]/100:.0f}%")
        L.append(f"   suministrado={supplied:,.0f} deuda={debt:,.0f} disponible={supplied-debt:,.0f} "
                 f"tope_prestamo={caps[0]:,} tope_suministro={caps[1]:,} interes_variable={rd[6]/1e27*100:.2f}%/año")
        L.append(f"   tokens (aToken, stableDebt, varDebt): {dp.functions.getReserveTokensAddresses(addr).call()}")

    L.append("\n## Uniswap V3 pools")
    factory = w3.eth.contract(address="0x1F98431c8aD98523631AE4a59f267346ea31F984", abi=FACTORY_ABI)
    pools = {}
    for a, b, n in [(ARB, WETH, "ARB/WETH"), (ARB, USDC, "ARB/USDC")]:
        for fee in (500, 3000, 10000):
            p = factory.functions.getPool(a, b, fee).call()
            if int(p, 16) == 0:
                continue
            ba = w3.eth.contract(address=a, abi=ERC).functions.balanceOf(p).call() / 10 ** DEC[a]
            bb = w3.eth.contract(address=b, abi=ERC).functions.balanceOf(p).call() / 10 ** DEC[b]
            pools[(n, fee)] = p
            L.append(f"{n} fee {fee/1e4:.2f}%: {p}  ARB={ba:,.0f}  {'WETH' if b == WETH else 'USDC'}={bb:,.2f}")

    L.append("\n## Cotización ida y vuelta de 40 $ (quoter real)")
    quoter = w3.eth.contract(address="0x61fFE014bA17989E743c5F6cB21bF9697530B21e", abi=QUOTER_ABI)
    routes = {
        "USDC->WETH(0.05)->ARB(0.05)": ([(USDC, 500, WETH), (WETH, 500, ARB)], [(ARB, 500, WETH), (WETH, 500, USDC)]),
        "USDC->WETH(0.05)->ARB(0.30)": ([(USDC, 500, WETH), (WETH, 3000, ARB)], [(ARB, 3000, WETH), (WETH, 500, USDC)]),
        "USDC->ARB(0.05)": ([(USDC, 500, ARB)], [(ARB, 500, USDC)]),
        "USDC->ARB(0.30)": ([(USDC, 3000, ARB)], [(ARB, 3000, USDC)]),
    }
    best = None
    for name, (buy, sell) in routes.items():
        try:
            arb_out = quoter.functions.quoteExactInput(path(buy), 40 * 10 ** 6).call()[0]
            usdc_back = quoter.functions.quoteExactInput(path(sell), arb_out).call()[0]
            cost = (40 - usdc_back / 1e6) / 40 * 100
            L.append(f"{name}: 40 USDC -> {arb_out/1e18:.4f} ARB -> {usdc_back/1e6:.4f} USDC  (coste ida+vuelta {cost:.3f}%)")
            if best is None or cost < best[1]:
                best = (name, cost)
        except Exception as e:  # noqa: BLE001
            L.append(f"{name}: sin cotización ({type(e).__name__})")
    if best:
        L.append(f"\nMEJOR RUTA: {best[0]} (coste ida+vuelta {best[1]:.3f}% ≈ {best[1]/2:.3f}%/lado)")
    txt = "\n".join(L)
    OUT.write_text(txt)
    print(txt)


if __name__ == "__main__":
    main()
