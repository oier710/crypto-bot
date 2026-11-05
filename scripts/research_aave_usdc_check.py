"""Diagnóstico (solo lectura): el 2026-11-05 00:06 UTC el bot no pudo abrir un corto de ETH en Aave:
borrow revirtió con LtvValidationFailed() (0x5b263df7) justo después de depositar USDC.
Esa revisión salta cuando el LTV efectivo de la garantía del usuario es 0: o el USDC tiene LTV 0 en Aave
Arbitrum, o el depósito no quedó marcado como garantía.

Lee:
  - configuración de reserva de USDC, USDC.e, WETH y WBTC en Aave V3 Arbitrum (LTV, umbral, garantía activa,
    congelada, pausada, préstamo activo)
  - datos de la cuenta del bot (garantía, deuda, LTV) y si tiene USDC marcado como garantía (getUserConfiguration)
  - mercados WETH/WBTC/USDC en Dolomite (alternativa para los cortos de BTC/ETH)
Salida: results/research_aave_usdc_check.txt
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "research_aave_usdc_check.txt"
w3 = Web3(Web3.HTTPProvider(os.getenv("ARB_RPC_URL", "https://arb1.arbitrum.io/rpc"), request_kwargs={"timeout": 30}))

WALLET = "0x44Cc8F4417C5353686D93000431b240e9d187923"
PROVIDER = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
TOK = {"USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "USDC.e": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
       "WETH": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "WBTC": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"}
PROVIDER_ABI = json.loads('[{"name":"getPool","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]},'
                          '{"name":"getPoolDataProvider","type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"address"}]}]')
DP_ABI = json.loads('[{"name":"getReserveConfigurationData","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"name":"decimals","type":"uint256"},{"name":"ltv","type":"uint256"},{"name":"liquidationThreshold","type":"uint256"},{"name":"liquidationBonus","type":"uint256"},{"name":"reserveFactor","type":"uint256"},{"name":"usageAsCollateralEnabled","type":"bool"},{"name":"borrowingEnabled","type":"bool"},{"name":"stableBorrowRateEnabled","type":"bool"},{"name":"isActive","type":"bool"},{"name":"isFrozen","type":"bool"}]},'
                    '{"name":"getPaused","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"}],"outputs":[{"type":"bool"}]},'
                    '{"name":"getUserReserveData","type":"function","stateMutability":"view","inputs":[{"name":"asset","type":"address"},{"name":"user","type":"address"}],"outputs":[{"name":"currentATokenBalance","type":"uint256"},{"name":"currentStableDebt","type":"uint256"},{"name":"currentVariableDebt","type":"uint256"},{"name":"principalStableDebt","type":"uint256"},{"name":"scaledVariableDebt","type":"uint256"},{"name":"stableBorrowRate","type":"uint256"},{"name":"liquidityRate","type":"uint256"},{"name":"stableRateLastUpdated","type":"uint40"},{"name":"usageAsCollateralEnabled","type":"bool"}]}]')
POOL_ABI = json.loads('[{"name":"getUserAccountData","type":"function","stateMutability":"view","inputs":[{"name":"user","type":"address"}],"outputs":[{"name":"totalCollateralBase","type":"uint256"},{"name":"totalDebtBase","type":"uint256"},{"name":"availableBorrowsBase","type":"uint256"},{"name":"currentLiquidationThreshold","type":"uint256"},{"name":"ltv","type":"uint256"},{"name":"healthFactor","type":"uint256"}]},'
                      '{"name":"getUserEMode","type":"function","stateMutability":"view","inputs":[{"name":"user","type":"address"}],"outputs":[{"type":"uint256"}]}]')
DOLO = "0x6Bd780E7fDf01D77e4d475c821f1e7AE05409072"
DOLO_ABI = json.loads('[{"name":"getMarketIdByTokenAddress","type":"function","stateMutability":"view","inputs":[{"name":"t","type":"address"}],"outputs":[{"type":"uint256"}]},'
                      '{"name":"getMarketIsClosing","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"type":"bool"}]},'
                      '{"name":"getMarketTotalPar","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"borrow","type":"uint128"},{"name":"supply","type":"uint128"}],"type":"tuple"}]},'
                      '{"name":"getMarketInterestRate","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]},'
                      '{"name":"getMarketMarginPremium","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]},'
                      '{"name":"getMarketPrice","type":"function","stateMutability":"view","inputs":[{"name":"m","type":"uint256"}],"outputs":[{"components":[{"name":"value","type":"uint256"}],"type":"tuple"}]}]')
DEC = {"USDC": 6, "USDC.e": 6, "WETH": 18, "WBTC": 8}


def main():
    L = [f"bloque {w3.eth.block_number}"]
    prov = w3.eth.contract(address=PROVIDER, abi=PROVIDER_ABI)
    pool = w3.eth.contract(address=prov.functions.getPool().call(), abi=POOL_ABI)
    dp = w3.eth.contract(address=prov.functions.getPoolDataProvider().call(), abi=DP_ABI)
    L.append("\n## Aave V3 Arbitrum — configuración de reservas")
    for n, t in TOK.items():
        c = dp.functions.getReserveConfigurationData(t).call()
        L.append(f"{n}: LTV {c[1]/100:.2f}% | umbral liq {c[2]/100:.2f}% | garantía permitida {c[5]} | prestable {c[6]} | "
                 f"activa {c[8]} | congelada {c[9]} | pausada {dp.functions.getPaused(t).call()}")
    L.append("\n## Cuenta del bot en Aave")
    d = pool.functions.getUserAccountData(WALLET).call()
    L.append(f"garantía {d[0]/1e8:.2f} $ | deuda {d[1]/1e8:.2f} $ | disponible para pedir {d[2]/1e8:.2f} $ | "
             f"umbral {d[3]/100:.2f}% | LTV {d[4]/100:.2f}% | eMode {pool.functions.getUserEMode(WALLET).call()}")
    for n, t in TOK.items():
        u = dp.functions.getUserReserveData(t, WALLET).call()
        if u[0] or u[2] or u[8]:
            L.append(f"  {n}: depositado {u[0]/10**DEC[n]:.6f} | deuda {u[2]/10**DEC[n]:.8f} | marcado como garantía {u[8]}")
    L.append("\n## Dolomite (alternativa para cortos de BTC/ETH)")
    m = w3.eth.contract(address=DOLO, abi=DOLO_ABI)
    for n in ("USDC", "WETH", "WBTC"):
        mid = m.functions.getMarketIdByTokenAddress(TOK[n]).call()
        tp = m.functions.getMarketTotalPar(mid).call()
        px = m.functions.getMarketPrice(mid).call()[0] / 10 ** (36 - DEC[n])
        apr = m.functions.getMarketInterestRate(mid).call()[0] * 365 * 86400 / 1e18 * 100
        prem = m.functions.getMarketMarginPremium(mid).call()[0] / 1e18
        L.append(f"{n}: id {mid} | cerrando {m.functions.getMarketIsClosing(mid).call()} | disponible "
                 f"{(tp[1]-tp[0])/10**DEC[n]*px:,.0f} $ | interés {apr:.2f}%/año | prima margen {prem:.3f}")
    txt = "\n".join(L)
    OUT.write_text(txt)
    print(txt)


if __name__ == "__main__":
    main()
