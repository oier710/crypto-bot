"""Escaneo de protocolos de préstamo (oct-2026): ¿dónde se pueden pedir prestadas monedas que no sean
BTC/ETH para poder ponerse en CORTO?

Pregunta de Oier (2026-10-28): "¿no tenemos corto para otras monedas?". Aave solo presta WBTC y WETH
(research_aave_scan*.txt). Aquí se miran TODOS los protocolos de préstamo que lista DefiLlama
(Dolomite, Silo, Compound, Radiant, Lodestar, Morpho, Euler, Fluid, ...) en Arbitrum, Optimism y Base.

Fuente: API pública de DefiLlama (sin claves):
  - https://yields.llama.fi/pools        -> mercados (proyecto, red, moneda, TVL, estable o no)
  - https://yields.llama.fi/lendBorrow   -> por mercado: interés de préstamo, total suministrado y prestado,
                                            si se puede pedir prestado, LTV, techo de deuda
Disponible para pedir prestado ~= total suministrado - total prestado (limitado por techo de deuda si lo hay).

Salida: results/research_lending_scan.txt y .csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import requests

RESULTS = Path(__file__).resolve().parent.parent / "results"
CHAINS = ("Arbitrum", "Optimism", "Base")
MIN_AVAIL_USD = 20_000      # por debajo, no merece la pena ni para 40 $ (puede vaciarse en un momento)
MAJORS = {"WBTC", "CBBTC", "TBTC", "LBTC", "BTC", "WETH", "ETH", "WSTETH", "WEETH", "RETH", "CBETH",
          "EZETH", "RSETH", "WRSETH", "OSETH", "SFRXETH", "FRXETH", "SUPEROETHB", "WOETH", "STETH"}
# Monedas cuya historia de precios en Coinbase permitiría hacer el backtest
COINBASE = {"ARB", "OP", "LINK", "UNI", "GMX", "PENDLE", "AAVE", "CRV", "LDO", "SOL", "DOGE", "AERO",
            "COMP", "SNX", "MKR", "RDNT", "MAGIC", "GRT", "WIF", "PEPE", "VIRTUAL", "MORPHO", "ZRO", "EIGEN",
            "ENA", "W", "STG", "BAL", "YFI", "SUSHI", "XRP", "ADA", "AVAX", "DEGEN", "BRETT", "TOSHI", "WELL"}


def get(url):
    r = requests.get(url, timeout=90, headers={"User-Agent": "crypto-bot-research"})
    r.raise_for_status()
    j = r.json()
    return j["data"] if isinstance(j, dict) and "data" in j else j


def main():
    pools = pd.DataFrame(get("https://yields.llama.fi/pools"))
    lend = pd.DataFrame(get("https://yields.llama.fi/lendBorrow"))
    df = pools.merge(lend, on="pool", how="inner", suffixes=("", "_lb"))
    df = df[df.chain.isin(CHAINS)].copy()
    for c in ("totalSupplyUsd", "totalBorrowUsd", "debtCeilingUsd", "apyBaseBorrow", "apyRewardBorrow", "ltv", "tvlUsd"):
        if c not in df:
            df[c] = None
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["sym"] = df.symbol.astype(str).str.upper().str.replace(".E", "", regex=False)
    stable = df.get("stablecoin", False)
    df["stable"] = stable.fillna(False).astype(bool) if hasattr(stable, "fillna") else False
    df["borrowable"] = df.get("borrowable", True)
    df["borrowable"] = df["borrowable"].fillna(True).astype(bool)
    avail = (df.totalSupplyUsd.fillna(df.tvlUsd) - df.totalBorrowUsd.fillna(0)).clip(lower=0)
    room = (df.debtCeilingUsd - df.totalBorrowUsd.fillna(0)).clip(lower=0)
    df["avail_usd"] = avail.where(df.debtCeilingUsd.isna() | (df.debtCeilingUsd <= 0), pd.concat([avail, room], axis=1).min(axis=1))
    df["borrow_apy"] = df.apyBaseBorrow.fillna(0) - df.apyRewardBorrow.fillna(0)  # %/año neto
    df["major"] = df.sym.isin(MAJORS)
    df["coinbase"] = df.sym.isin(COINBASE)

    cols = ["chain", "project", "sym", "borrowable", "totalSupplyUsd", "totalBorrowUsd", "avail_usd",
            "debtCeilingUsd", "borrow_apy", "ltv", "poolMeta"]
    for c in cols:
        if c not in df:
            df[c] = None
    df[cols + ["stable", "major", "coinbase", "pool"]].sort_values(["chain", "project", "sym"]).to_csv(
        RESULTS / "research_lending_scan.csv", index=False)

    lines = ["# Escaneo de protocolos de préstamo — ¿dónde se puede poner en corto algo que no sea BTC/ETH?",
             f"Fuente: DefiLlama (yields.llama.fi/pools + /lendBorrow), redes {', '.join(CHAINS)}. "
             f"Mercados con datos de préstamo: {len(df)}.",
             "disp.$ = suministrado - prestado (y dentro del techo de deuda). interés = %/año neto de recompensas.",
             ""]

    lines.append("## Protocolos de préstamo encontrados (mercados por red)")
    lines.append(df.groupby(["chain", "project"]).size().rename("mercados").to_string())
    lines.append("")

    cand = df[(~df.stable) & (~df.major) & df.borrowable & (df.avail_usd >= MIN_AVAIL_USD)]
    cand = cand.sort_values("avail_usd", ascending=False)
    lines.append(f"## CANDIDATAS PARA CORTO: no estables, no BTC/ETH, prestables, disponible >= {MIN_AVAIL_USD:,} $")
    if cand.empty:
        lines.append("(ninguna)")
    else:
        lines.append(f"{'red':<9} {'protocolo':<22} {'moneda':<10} {'suministrado$':>14} {'prestado$':>12} "
                     f"{'disp.$':>12} {'interés':>8} {'LTV':>5} {'Coinbase':>8}  nota")
        for _, r in cand.iterrows():
            lines.append(f"{r.chain:<9} {str(r.project)[:22]:<22} {r.sym[:10]:<10} {r.totalSupplyUsd or 0:>14,.0f} "
                         f"{r.totalBorrowUsd or 0:>12,.0f} {r.avail_usd:>12,.0f} {r.borrow_apy:>7.2f}% "
                         f"{(r.ltv or 0):>5.2f} {'sí' if r.coinbase else 'no':>8}  {r.poolMeta if isinstance(r.poolMeta, str) else ''}")
    lines.append("")

    lines.append("## Resumen por moneda (suma de todos los protocolos, solo candidatas)")
    if not cand.empty:
        g = cand.groupby("sym").agg(protocolos=("project", lambda s: ", ".join(sorted(set(f"{p}" for p in s)))),
                                    redes=("chain", lambda s: ", ".join(sorted(set(s)))),
                                    disp_usd=("avail_usd", "sum"), interes_min=("borrow_apy", "min"),
                                    coinbase=("coinbase", "first")).sort_values("disp_usd", ascending=False)
        lines.append(g.round(2).to_string())
    lines.append("")

    for proj in ("dolomite", "silo"):
        sub = df[df.project.astype(str).str.contains(proj)].sort_values("avail_usd", ascending=False)
        lines.append(f"## Detalle {proj.upper()} (todos los mercados con datos)")
        if sub.empty:
            lines.append("(DefiLlama no da datos de préstamo de este protocolo)")
        else:
            for _, r in sub.head(40).iterrows():
                lines.append(f"{r.chain:<9} {str(r.project)[:18]:<18} {r.sym[:12]:<12} prestable={'sí' if r.borrowable else 'no':<3} "
                             f"sumin.={r.totalSupplyUsd or 0:>12,.0f} prest.={r.totalBorrowUsd or 0:>11,.0f} "
                             f"disp.={r.avail_usd:>11,.0f} interés={r.borrow_apy:>6.2f}% {r.poolMeta if isinstance(r.poolMeta, str) else ''}")
        lines.append("")

    txt = "\n".join(lines)
    (RESULTS / "research_lending_scan.txt").write_text(txt)
    print(txt)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # que el resultado diga qué falló en vez de quedar vacío
        RESULTS.mkdir(exist_ok=True)
        (RESULTS / "research_lending_scan.txt").write_text(f"ERROR {type(e).__name__}: {e}\n")
        print(e, file=sys.stderr)
        raise
