"""Escaneo on-chain (solo lectura) de TODAS las monedas de Aave V3 en Arbitrum One: cuáles se pueden pedir
prestadas (cortos), con qué disponibilidad e interés, y qué liquidez tienen en Uniswap V3 contra WETH/USDC.
Objetivo: candidatas para operar en largo y en corto con la estrategia actual.
Salida: results/research_aave_scan.txt
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "research_aave_scan.txt"
w3 = Web3(Web3.HTTPProvider(os.getenv("ARB_RPC_URL", "https://arb1.arbitrum.io/rpc"), request_kwargs={"timeout": 30}))

WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
ERC = json.loads('[{"name":"symbol","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"string"}]},'
                 '{"name":"decimals","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"uint8"}]},'
                 '{"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"name":"a","type":"address"}],"outputs":[{"type":"uint256"}]}]')
PROVIDER_ABI = json.loads('[{"name":"getPoolDataProvider","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},'
                          '{"name":"getPriceOracle","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]}]')
DP_ABI = json.loads('[{"name":"getAllReservesTokens","type":"function","stateMutability":"view","inputs":[],"outputs":[{"components":[{"name":"symbol","type":"string"},{"name":"tokenAddress","type":"address"}],"type":"tuple[]"}]},'
                    '{"name":"getReserveConfigurationData","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"name":"decimals","type":"uint256"},{"name":"ltv","type":"uint256"},{"name":"liquidationThreshold","type":"uint256"},{"name":"liquidationBonus","type":"uint256"},{"name":"reserveFactor","type":"uint256"},{"name":"usageAsCollateralEnabled","type":"bool"},{"name":"borrowingEnabled","type":"bool"},{"name":"stableBorrowRateEnabled","type":"bool"},{"name":"isActive","type":"bool"},{"name":"isFrozen","type":"bool"}]},'
                    '{"name":"getReserveCaps","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"name":"borrowCap","type":"uint256"},{"name":"supplyCap","type":"uint256"}]},'
                    '{"name":"getReserveData","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"name":"unbacked","type":"uint256"},{"name":"accruedToTreasuryScaled","type":"uint256"},{"name":"totalAToken","type":"uint256"},{"name":"totalStableDebt","type":"uint256"},{"name":"totalVariableDebt","type":"uint256"},{"name":"liquidityRate","type":"uint256"},{"name":"variableBorrowRate","type":"uint256"},{"name":"stableBorrowRate","type":"uint256"},{"name":"averageStableBorrowRate","type":"uint256"},{"name":"liquidityIndex","type":"uint256"},{"name":"variableBorrowIndex","type":"uint256"},{"name":"lastUpdateTimestamp","type":"uint40"}]},'
                    '{"name":"getPaused","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"type":"bool"}]},'
                    '{"name":"getSiloedBorrowing","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"type":"bool"}]}]')
ORACLE_ABI = json.loads('[{"name":"getAssetPrice","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"type":"uint256"}]}]')
FACTORY_ABI = json.loads('[{"name":"getPool","type":"function","stateMutability":"view","inputs":[{"type":"address"},{"type":"address"},{"type":"uint24"}],"outputs":[{"type":"address"}]}]')
QUOTER_ABI = json.loads('[{"name":"quoteExactInput","type":"function","stateMutability":"nonpayable","inputs":[{"name":"path","type":"bytes"},{"name":"amountIn","type":"uint256"}],"outputs":[{"name":"amountOut","type":"uint256"},{"name":"a","type":"uint160[]"},{"name":"b","type":"uint32[]"},{"name":"gasEstimate","type":"uint256"}]}]')
STABLES = {"USDC", "USDC.e", "USDT", "DAI", "FRAX", "LUSD", "GHO", "USDe", "sUSDe", "USD₮0", "USDT0", "MAI", "EURS", "AUSD"}


def path(hops):
    out = b""
    for i, (tin, fee, tout) in enumerate(hops):
        if i == 0:
            out += bytes.fromhex(tin[2:])
        out += fee.to_bytes(3, "big") + bytes.fromhex(tout[2:])
    return out


def main():
    L = [f"bloque {w3.eth.block_number}", ""]
    prov = w3.eth.contract(address="0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb", abi=PROVIDER_ABI)
    dp = w3.eth.contract(address=prov.functions.getPoolDataProvider().call(), abi=DP_ABI)
    oracle = w3.eth.contract(address=prov.functions.getPriceOracle().call(), abi=ORACLE_ABI)
    factory = w3.eth.contract(address="0x1F98431c8aD98523631AE4a59f267346ea31F984", abi=FACTORY_ABI)
    quoter = w3.eth.contract(address="0x61fFE014bA17989E743c5F6cB21bF9697530B21e", abi=QUOTER_ABI)
    reserves = dp.functions.getAllReservesTokens().call()
    L.append(f"## Aave V3 Arbitrum: {len(reserves)} monedas")
    L.append(f"{'moneda':<10} {'prestable':<9} {'disp.$':>12} {'tope$':>12} {'interés':>8} {'silo':>5} {'congelada':>9}")
    cands = []
    for sym, addr in reserves:
        try:
            cfg = dp.functions.getReserveConfigurationData(addr).call()
            caps = dp.functions.getReserveCaps(addr).call()
            rd = dp.functions.getReserveData(addr).call()
            d = cfg[0]
            px = oracle.functions.getAssetPrice(addr).call() / 1e8
            avail = (rd[2] - rd[4]) / 10 ** d
            cap_left = (caps[0] - rd[4] / 10 ** d) if caps[0] else avail
            borrowable = cfg[6] and cfg[8] and not cfg[9] and not dp.functions.getPaused(addr).call() and caps[0] != 1
            room = min(avail, max(cap_left, 0)) * px
            silo = dp.functions.getSiloedBorrowing(addr).call()
            apr = rd[6] / 1e27 * 100
            L.append(f"{sym:<10} {'sí' if borrowable else 'no':<9} {room:>12,.0f} {caps[0] * px if caps[0] else 0:>12,.0f} {apr:>7.2f}% {'sí' if silo else 'no':>5} {'sí' if cfg[9] else 'no':>9}")
            if borrowable and sym not in STABLES and room > 5_000 and not silo:
                cands.append((sym, addr, d, px, apr))
        except Exception as e:  # noqa: BLE001
            L.append(f"{sym:<10} error {type(e).__name__}")
    L.append("")
    L.append("## Candidatas para cortos (prestables, no estables, > 5.000 $ disponibles): liquidez y coste en Uniswap V3")
    L.append("Coste = ida y vuelta de 40 $ por la mejor ruta encontrada (USDC->WETH->X o USDC->X), fees 0.05 % / 0.30 % / 1 %.")
    for sym, addr, d, px, apr in cands:
        best = None
        pools = []
        for other, oname, od in ((WETH, "WETH", 18), (USDC, "USDC", 6)):
            for fee in (500, 3000, 10000):
                p = factory.functions.getPool(addr, other, fee).call()
                if int(p, 16) == 0:
                    continue
                bal_x = w3.eth.contract(address=addr, abi=ERC).functions.balanceOf(p).call() / 10 ** d * px
                bal_o = w3.eth.contract(address=other, abi=ERC).functions.balanceOf(p).call() / 10 ** od
                pools.append(f"{sym}/{oname} {fee / 1e4:.2f}% ({bal_x:,.0f}$ + {bal_o:,.0f} {oname})")
                buy = [(USDC, 500, WETH), (WETH, fee, addr)] if other == WETH else [(USDC, fee, addr)]
                sell = [(addr, fee, WETH), (WETH, 500, USDC)] if other == WETH else [(addr, fee, USDC)]
                try:
                    out = quoter.functions.quoteExactInput(path(buy), 40 * 10 ** 6).call()[0]
                    back = quoter.functions.quoteExactInput(path(sell), out).call()[0] / 1e6
                    cost = (40 - back) / 40 * 100
                    if best is None or cost < best[0]:
                        best = (cost, f"via {oname} {fee / 1e4:.2f}%")
                except Exception:  # noqa: BLE001
                    pass
        L.append(f"- {sym} (precio {px:,.4f} $, interés préstamo {apr:.2f}%/año): "
                 f"{('mejor ruta ' + best[1] + f', coste ida+vuelta {best[0]:.2f}%') if best else 'SIN cotización en Uniswap'}")
        for ptxt in pools:
            L.append(f"    pool {ptxt}")
    txt = "\n".join(L)
    OUT.write_text(txt)
    print(txt)


if __name__ == "__main__":
    main()
