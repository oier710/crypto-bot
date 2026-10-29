"""Investigación oct-2026 (7ª parte): ¿cuánto peso darle a ARB (largo y corto vía Dolomite)?

Contexto (2026-10-29): Dolomite en Arbitrum presta ARB (~0.28 M$ disponibles, ~7 %/año), así que ya se
pueden hacer cortos de ARB. En research_2026_10d, BTC+ETH+ARB a partes iguales (1/3 cada una) daba
+134 % frente a +64 % de BTC+ETH, pero con caída máxima −28 % frente a −16 %.
Oier: "vamos a probar y si no es rentable lo cambiamos de vuelta". Criterio: consistencia primero.

Pregunta: ¿qué peso de ARB sube la rentabilidad sin que la caída máxima pase de ~−20 %?
- Mismo simulador v3 que en producción (research_2026_10b, PT 1.5/3 + reentrada 1.0/1.5).
- Capital 115.75 $: ARB recibe w del capital; BTC y ETH (1-w)/2 cada una. 75 % por posición, como ahora.
- Costes: 0.10 %/lado (BTC directo, ETH, ARB vía WETH: pools 0.05 %) + gas; cortos +0.10 %.
  Interés de los cortos: BTC/ETH 4 %/año (Aave); ARB 7 %/año (Dolomite) y prueba de estrés al 15 %/año.
Salida: results/research_2026_10g.txt y .csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import research_2026_10 as base  # noqa: E402
import research_2026_10b as v3mod  # noqa: E402

V3 = v3mod.V2("v3", pt=(1.5, 3.0), rh=True, pb_atr=1.0, restop_k=1.5)
COST = {"BTC": 0.0010, "ETH": 0.0010, "ARB": 0.0010}
APR = {"BTC": 0.04, "ETH": 0.04, "ARB": 0.07}
WEIGHTS = [0.0, 0.10, 0.15, 0.20, 0.25, 1 / 3]


def sim_asset(data, sym, start, end, capital, apr):
    h, d = data[sym]
    hh = h[(h.index >= start) & (h.index < end)]
    old_c, old_a = base.SWAP_PCT, base.SHORT_APR
    base.SWAP_PCT = v3mod.SWAP_PCT = COST[sym]
    base.SHORT_APR = apr
    try:
        e, s = v3mod.simulate(hh, d, V3, capital)
    finally:
        base.SWAP_PCT = v3mod.SWAP_PCT = old_c
        base.SHORT_APR = old_a
    return e, s.trades, s.fees


def portfolio(data, w_arb, start, end, arb_apr):
    alloc = {"BTC": (1 - w_arb) / 2, "ETH": (1 - w_arb) / 2, "ARB": w_arb}
    eqs, trades, fees, per = [], [], 0.0, {}
    for sym, w in alloc.items():
        if w <= 0:
            continue
        e, tr, f = sim_asset(data, sym, start, end, base.CAPITAL * w, arb_apr if sym == "ARB" else APR[sym])
        eqs.append(e)
        trades += tr
        fees += f
        per[sym] = float(e.iloc[-1] - e.iloc[0])
    eq = pd.concat(eqs, axis=1).ffill().bfill().sum(axis=1)
    r = base.metrics(eq, trades, fees)
    r["pnl_arb"] = per.get("ARB", 0.0)
    return r


def label(w, apr):
    return f"ARB {w:.0%}" + (" (actual)" if w == 0 else "") + (f" [interés {apr:.0%}]" if apr != APR["ARB"] else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    data = {}
    for sym in ("BTC", "ETH", "ARB"):
        h = base.load_hourly(sym if not a.synthetic else ("BTC" if sym == "BTC" else "ETH"), a.synthetic)
        if a.synthetic and sym == "ARB":
            rng = np.random.default_rng(7)
            h = h * (1 + rng.normal(0, 0.0005, len(h))[:, None]).clip(0.99, 1.01)
        data[sym] = (h, base.daily_from_hourly(h))
    t0 = max(data[s][0].index[0] for s in data)
    t1 = min(data[s][0].index[-1] for s in data)
    start = t0 + pd.Timedelta(days=90)
    end = t1 + pd.Timedelta(hours=1)
    mid = start + (t1 - start) / 2
    th = [start + (t1 - start) * k / 3 for k in range(4)]
    th[-1] = end
    periods = [("PERIODO COMPLETO", start, end), ("1ª MITAD", start, mid), ("2ª MITAD", mid, end),
               ("TERCIO 1", th[0], th[1]), ("TERCIO 2 (lateral)", th[1], th[2]), ("TERCIO 3", th[2], th[3])]
    variants = [(w, APR["ARB"]) for w in WEIGHTS] + [(0.15, 0.15), (0.20, 0.15), (0.25, 0.15)]

    lines = ["# Investigación oct-2026 (7ª parte) — ¿cuánto peso a ARB (largo + corto vía Dolomite)?",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             f"Capital {base.CAPITAL} $: ARB = w, BTC y ETH = (1-w)/2 cada una; 75 % por posición.",
             "Costes 0.10 %/lado + gas; cortos +0.10 %; interés cortos BTC/ETH 4 %/año, ARB 7 %/año (estrés 15 %).",
             "Columnas: m- = meses en negativo / meses; ARB$ = lo que aporta ARB en el periodo.", ""]
    rows = []
    for lab, s0, s1 in periods:
        lines += [f"## {lab} ({s0.date()} -> {min(s1, t1).date()})", base.HEADER + f" {'ARB$':>7}"]
        for w, apr in variants:
            r = portfolio(data, w, s0, s1, apr)
            r["variant"] = label(w, apr)
            lines.append(base.fmt(r) + f" {r['pnl_arb']:>+7.2f}")
            rows.append(r | {"period": lab, "w_arb": w, "arb_apr": apr})
        lines.append("")

    df = pd.DataFrame(rows)
    full = df[df.period == "PERIODO COMPLETO"].set_index("variant")
    b = full.iloc[0]
    sub = df[df.period.str.startswith("TERCIO")].pivot_table(index="variant", columns="period", values="ret")
    halves = df[df.period.isin(["1ª MITAD", "2ª MITAD"])].pivot_table(index="variant", columns="period", values="ret")
    full["tercios_positivos"] = (sub > 0).sum(axis=1).reindex(full.index)
    full["gana_a_actual_en_tercios"] = [int((sub.loc[n] > sub.loc[b.name]).sum()) for n in full.index]
    full["gana_a_actual_en_ambas_mitades"] = [bool((halves.loc[n] > halves.loc[b.name]).all()) for n in full.index]
    full["retorno_por_caida"] = full.ret / full.max_dd.abs()
    full["cumple_caida_20"] = full.max_dd >= -0.20
    lines += ["## RESUMEN (periodo completo)",
              full[["ret", "cagr", "max_dd", "neg_months", "worst_month", "sharpe", "trades", "pnl_arb",
                    "retorno_por_caida", "cumple_caida_20", "tercios_positivos", "gana_a_actual_en_tercios",
                    "gana_a_actual_en_ambas_mitades"]].round(3).to_string(), ""]
    ok = full[full.cumple_caida_20 & (full.index != b.name) & (full.arb_apr == APR["ARB"])]
    if len(ok):
        best = ok.sort_values("sharpe", ascending=False).iloc[0]
        lines.append(f"Mejor con caída <= 20 % (por Sharpe): {best.name}: {best.ret:+.1%}, caída {best.max_dd:+.1%}, "
                     f"{int(best.neg_months)} meses en rojo (actual: {b.ret:+.1%}, {b.max_dd:+.1%}, {int(b.neg_months)}).")
    else:
        lines.append("Ningún peso de ARB mantiene la caída máxima dentro de −20 %.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_10g{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_10g{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
