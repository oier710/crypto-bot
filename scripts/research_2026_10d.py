"""Investigación oct-2026 (4ª parte): MÁS MONEDAS con la misma estrategia v3.

Petición de Oier (2026-10-25): más rentabilidad sin operar más a menudo; monedas con tendencias más fuertes
para ganar dólares, y BTC/ETH/LINK para acumular.

Pregunta: ¿aplicar la v3 (tendencia diaria EMA 20/60 + ADX, largos y cortos, parciales + reentrada en máximo
nuevo) a más monedas mejora la cartera (más rentabilidad y/o más consistencia) frente a solo BTC+ETH?

- Mismo simulador que la v3 en producción (scripts/research_2026_10b.py, variante PT 1.5/3 + RH 1.0/1.5).
- 3 años de velas 1h reales de Coinbase. El capital (115.75 $) se reparte a partes iguales entre las monedas
  de cada cartera; cada posición = 75 % de su parte (como ahora).
- Costes por moneda según la liquidez real en Uniswap (Arbitrum):
    BTC, ETH, ARB: 0.10 %/lado (pools de 0.05 %);  LINK, UNI: 0.35 %/lado (pools de 0.30 %);
    SOL, AVAX, DOGE: 0.35 %/lado y SOLO INFORMATIVAS (no hay mercado líquido para ellas en Arbitrum).
  + 0.03 $ de gas por operación; cortos +0.10 % y 4 %/año de interés.
Salida: results/research_2026_10d.txt y .csv
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
COST = {"BTC": 0.0010, "ETH": 0.0010, "ARB": 0.0010, "LINK": 0.0035, "UNI": 0.0035,
        "SOL": 0.0035, "AVAX": 0.0035, "DOGE": 0.0035}
TRADABLE = ("BTC", "ETH", "LINK", "ARB", "UNI")
INFO_ONLY = ("SOL", "AVAX", "DOGE")
PORTFOLIOS = [
    ("BTC+ETH (actual)", ("BTC", "ETH")),
    ("BTC+ETH+LINK", ("BTC", "ETH", "LINK")),
    ("BTC+ETH+ARB", ("BTC", "ETH", "ARB")),
    ("BTC+ETH+LINK+ARB", ("BTC", "ETH", "LINK", "ARB")),
    ("BTC+ETH+LINK+ARB+UNI", ("BTC", "ETH", "LINK", "ARB", "UNI")),
    ("solo alts LINK+ARB+UNI", ("LINK", "ARB", "UNI")),
    ("BTC+ETH+SOL (informativa)", ("BTC", "ETH", "SOL")),
]


def sim_asset(data, sym, start, end, capital):
    h, d = data[sym]
    hh = h[(h.index >= start) & (h.index < end)]
    if len(hh) < 24 * 30:
        return None, [], 0.0
    old = base.SWAP_PCT
    base.SWAP_PCT = COST[sym]
    v3mod.SWAP_PCT = COST[sym]
    try:
        e, s = v3mod.simulate(hh, d, V3, capital)
    finally:
        base.SWAP_PCT = old
        v3mod.SWAP_PCT = old
    return e, s.trades, s.fees


def portfolio(data, syms, start, end):
    syms = [s for s in syms if s in data]
    eqs, trades, fees = [], [], 0.0
    for sym in syms:
        e, tr, f = sim_asset(data, sym, start, end, base.CAPITAL / len(syms))
        if e is None:
            return None
        eqs.append(e)
        trades += tr
        fees += f
    eq = pd.concat(eqs, axis=1).ffill().bfill().sum(axis=1)
    return base.metrics(eq, trades, fees), eqs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    data = {}
    for sym in TRADABLE + INFO_ONLY:
        try:
            h = base.load_hourly(sym if not a.synthetic else ("BTC" if sym == "BTC" else "ETH"), a.synthetic)
            if a.synthetic:   # variar series sintéticas por moneda
                rng = np.random.default_rng(abs(hash(sym)) % 1000)
                h = h * (1 + rng.normal(0, 0.0005, len(h))[:, None]).clip(0.99, 1.01)
            if len(h) < 24 * 200:
                print(f"{sym}: pocos datos ({len(h)} velas), se omite")
                continue
            data[sym] = (h, base.daily_from_hourly(h))
            print(f"{sym}: {len(h)} velas desde {h.index[0].date()}")
        except Exception as e:  # noqa: BLE001
            print(f"{sym}: sin datos de Coinbase ({e}), se omite")
    t0 = max(data[s][0].index[0] for s in ("BTC", "ETH", "LINK", "ARB") if s in data)
    t1 = min(data[s][0].index[-1] for s in data)
    start = t0 + pd.Timedelta(days=90)
    end = t1 + pd.Timedelta(hours=1)
    mid = start + (t1 - start) / 2
    th = [start + (t1 - start) * k / 3 for k in range(4)]
    th[-1] = end
    periods = [("PERIODO COMPLETO", start, end), ("1ª MITAD", start, mid), ("2ª MITAD", mid, end),
               ("TERCIO 1", th[0], th[1]), ("TERCIO 2 (lateral)", th[1], th[2]), ("TERCIO 3", th[2], th[3])]

    lines = ["# Investigación oct-2026 (4ª parte) — la v3 con más monedas (petición de Oier, 25-oct)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             f"Capital {base.CAPITAL} $ repartido a partes iguales; 75 % por posición.",
             "Costes/lado: BTC, ETH, ARB 0.10 %; LINK, UNI, SOL, AVAX, DOGE 0.35 %; + 0.03 $ gas; cortos +0.10 % y 4 %/año.",
             f"Monedas con datos: {', '.join(data)}. SOL/AVAX/DOGE solo informativas (sin mercado líquido en Arbitrum).",
             "Columnas: m- = meses en negativo / meses; gana$/pierde$ = resultado medio por operación.", ""]

    # 1) cada moneda sola (con todo el capital) para ver qué tendencias son más rentables
    lines += ["## CADA MONEDA SOLA (todo el capital en esa moneda, periodo completo)", base.HEADER]
    rows = []
    for sym in data:
        r, _ = portfolio(data, (sym,), start, end) or (None, None)
        if r is None:
            continue
        r["variant"] = sym + ("" if sym in TRADABLE else " (info)")
        lines.append(base.fmt(r))
        rows.append(r | {"period": "MONEDA SOLA"})
    lines.append("")

    # 2) carteras por periodo
    equity_full = {}
    for label, s0, s1 in periods:
        lines += [f"## CARTERAS — {label} ({s0.date()} -> {min(s1, t1).date()})", base.HEADER]
        for name, syms in PORTFOLIOS:
            if not all(s in data for s in syms):
                continue
            out = portfolio(data, syms, s0, s1)
            if out is None:
                continue
            r, eqs = out
            r["variant"] = name
            lines.append(base.fmt(r))
            rows.append(r | {"period": label})
            if label == "PERIODO COMPLETO":
                equity_full[name] = eqs
        lines.append("")

    # 3) correlación de los resultados diarios por moneda (dentro de la cartera más grande)
    big = [s for s in TRADABLE if s in data]
    _, eqs = portfolio(data, big, start, end)
    dr = pd.concat([e.resample("1D").last().pct_change() for e in eqs], axis=1)
    dr.columns = big
    lines += ["## CORRELACIÓN de los resultados diarios de la estrategia por moneda (1 = se mueven igual)",
              dr.corr().round(2).to_string(), ""]

    df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    full = df[df.period == "PERIODO COMPLETO"].set_index("variant")
    if "BTC+ETH (actual)" in full.index:
        b = full.loc["BTC+ETH (actual)"]
        sub = df[df.period.str.startswith("TERCIO")].pivot_table(index="variant", columns="period", values="ret")
        full["tercios_positivos"] = (sub > 0).sum(axis=1).reindex(full.index)
        full["mejora_actual_en_tercios"] = [int((sub.loc[n] > sub.loc[b.name]).sum()) if n in sub.index else 0
                                            for n in full.index]
        full["score_vs_actual"] = ((full.max_dd - b.max_dd) * 100 - (full.neg_months - b.neg_months)
                                   + (full.cagr - b.cagr) * 50)
        lines += ["## RESUMEN CARTERAS (periodo completo)",
                  full[["ret", "cagr", "max_dd", "neg_months", "worst_month", "sharpe", "trades", "fees_usd",
                        "tercios_positivos", "mejora_actual_en_tercios", "score_vs_actual"]].round(3).to_string(), ""]
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_10d{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_10d{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
