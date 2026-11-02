"""Investigación nov-2026 (1ª parte): ¿una señal de "medias alineadas" (EMA 10/20/50) mejora a la actual?

Pregunta de Oier (2026-11-02): más rentabilidad. Es la última candidata pendiente del estudio de patrones de
septiembre (results/pattern_study.txt: en diario, "medias alineadas" +30 % frente a +26 % de trend_ema, con
más caída). Aquí se prueba DENTRO del sistema real (v5): misma gestión v3 (stop 2 ATR, parciales 1.5/3 ATR,
reentrada en máximo nuevo), cartera BTC 42.5 % / ETH 42.5 % / ARB 15 %, 75 % por posición, largos y cortos.
Solo cambia la SEÑAL diaria (cuándo se está largo, corto o fuera).

Variantes de señal (diaria, calculada al cierre y aplicada al día siguiente, como en producción):
  S0  actual: EMA 20 > EMA 60 (o <) y ADX > 20
  R1  alineadas 10/20/50: EMA10 > EMA20 > EMA50 -> +1; al revés -> -1; si no, 0
  R2  R1 + ADX > 20
  R3  R1 con permanencia: entra al alinearse y SIGUE mientras EMA10 esté del mismo lado que EMA50
  R4  alineadas 8/20/50 + ADX > 20
  R5  alineadas 10/30/100 + ADX > 20
  R6  actual (20/60 + ADX) Y además alineadas 10/20/50 (filtro más estricto)
Costes: 0.10 %/lado + gas; cortos +0.10 %; interés cortos BTC/ETH 4 %/año, ARB 7 %/año.
Riesgo de sobreajuste: 7 variantes; se mira que gane en ambas mitades y en los tercios, no solo en total.
Salida: results/research_2026_11a.txt y .csv
"""
from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import research_2026_10 as base  # noqa: E402
import research_2026_10b as v3mod  # noqa: E402
from bot.indicators import adx, ema  # noqa: E402
from bot.strategies import trend_ema  # noqa: E402

V3 = v3mod.V2("v3", pt=(1.5, 3.0), rh=True, pb_atr=1.0, restop_k=1.5)
COST = {"BTC": 0.0010, "ETH": 0.0010, "ARB": 0.0010}
APR = {"BTC": 0.04, "ETH": 0.04, "ARB": 0.07}
WEIGHTS = {"BTC": 0.425, "ETH": 0.425, "ARB": 0.15}


def s_actual(d):
    return trend_ema.signals(d, fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True)


def _aligned(d, n1, n2, n3):
    a, b, c = ema(d.close, n1), ema(d.close, n2), ema(d.close, n3)
    sig = pd.Series(0, index=d.index, dtype=int)
    sig[(a > b) & (b > c)] = 1
    sig[(a < b) & (b < c)] = -1
    return sig


def _with_adx(sig, d, adx_min=20):
    a = adx(d, 14)
    return sig.where(a > adx_min, 0).astype(int)


def s_r1(d):
    return _aligned(d, 10, 20, 50)


def s_r2(d):
    return _with_adx(_aligned(d, 10, 20, 50), d)


def s_r3(d):
    al = _aligned(d, 10, 20, 50).values
    f, c = ema(d.close, 10).values, ema(d.close, 50).values
    out = np.zeros(len(d), dtype=int)
    cur = 0
    for i in range(len(d)):
        if cur == 1 and not f[i] > c[i]:
            cur = 0
        elif cur == -1 and not f[i] < c[i]:
            cur = 0
        if cur == 0 and al[i] != 0:
            cur = al[i]
        elif al[i] == -cur:
            cur = al[i]
        out[i] = cur
    return pd.Series(out, index=d.index)


def s_r4(d):
    return _with_adx(_aligned(d, 8, 20, 50), d)


def s_r5(d):
    return _with_adx(_aligned(d, 10, 30, 100), d)


def s_r6(d):
    a, r = s_actual(d), _aligned(d, 10, 20, 50)
    return a.where(a == r, 0).astype(int)


SIGNALS = [("S0 actual EMA20/60+ADX", s_actual), ("R1 alineadas 10/20/50", s_r1),
           ("R2 alineadas 10/20/50 + ADX", s_r2), ("R3 alineadas 10/20/50 permanencia", s_r3),
           ("R4 alineadas 8/20/50 + ADX", s_r4), ("R5 alineadas 10/30/100 + ADX", s_r5),
           ("R6 actual + alineadas", s_r6)]


def sim_asset(data, sym, start, end, capital, sigfn):
    h, d = data[sym]
    hh = h[(h.index >= start) & (h.index < end)]
    old_c, old_a, old_t = base.SWAP_PCT, base.SHORT_APR, v3mod.trend_ema
    base.SWAP_PCT = v3mod.SWAP_PCT = COST[sym]
    base.SHORT_APR = APR[sym]
    v3mod.trend_ema = types.SimpleNamespace(signals=lambda df, **k: sigfn(df))
    try:
        e, s = v3mod.simulate(hh, d, V3, capital)
    finally:
        base.SWAP_PCT = v3mod.SWAP_PCT = old_c
        base.SHORT_APR = old_a
        v3mod.trend_ema = old_t
    return e, s.trades, s.fees


def portfolio(data, sigfn, start, end):
    eqs, trades, fees, per = [], [], 0.0, {}
    for sym, w in WEIGHTS.items():
        e, tr, f = sim_asset(data, sym, start, end, base.CAPITAL * w, sigfn)
        eqs.append(e)
        trades += tr
        fees += f
        per[sym] = float(e.iloc[-1] - e.iloc[0])
    eq = pd.concat(eqs, axis=1).ffill().bfill().sum(axis=1)
    r = base.metrics(eq, trades, fees)
    r.update({f"pnl_{k}": v for k, v in per.items()})
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    data = {}
    for sym in WEIGHTS:
        h = base.load_hourly(sym if not a.synthetic else ("BTC" if sym == "BTC" else "ETH"), a.synthetic)
        if a.synthetic and sym == "ARB":
            rng = np.random.default_rng(7)
            h = h * (1 + rng.normal(0, 0.0005, len(h))[:, None]).clip(0.99, 1.01)
        data[sym] = (h, base.daily_from_hourly(h))
    t0 = max(data[s][0].index[0] for s in data)
    t1 = min(data[s][0].index[-1] for s in data)
    start = t0 + pd.Timedelta(days=120)          # 100 días de calentamiento para la EMA 100
    end = t1 + pd.Timedelta(hours=1)
    mid = start + (t1 - start) / 2
    th = [start + (t1 - start) * k / 3 for k in range(4)]
    th[-1] = end
    periods = [("PERIODO COMPLETO", start, end), ("1ª MITAD", start, mid), ("2ª MITAD", mid, end),
               ("TERCIO 1", th[0], th[1]), ("TERCIO 2 (lateral)", th[1], th[2]), ("TERCIO 3", th[2], th[3])]

    lines = ["# Investigación nov-2026 (1ª parte) — ¿señal de medias alineadas (EMA 10/20/50) mejor que la actual?",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             f"Cartera v5: BTC 42.5 % / ETH 42.5 % / ARB 15 %, 75 % por posición, gestión v3 idéntica; solo cambia la señal.",
             "Costes 0.10 %/lado + gas; cortos +0.10 %; interés cortos BTC/ETH 4 %/año, ARB 7 %/año.",
             "Columnas: m- = meses en negativo / meses; BTC$/ETH$/ARB$ = lo que aporta cada moneda.", ""]
    rows = []
    for lab, s0, s1 in periods:
        lines += [f"## {lab} ({s0.date()} -> {min(s1, t1).date()})",
                  base.HEADER + f" {'BTC$':>7} {'ETH$':>7} {'ARB$':>7}"]
        for name, fn in SIGNALS:
            r = portfolio(data, fn, s0, s1)
            r["variant"] = name
            lines.append(base.fmt(r) + f" {r['pnl_BTC']:>+7.2f} {r['pnl_ETH']:>+7.2f} {r['pnl_ARB']:>+7.2f}")
            rows.append(r | {"period": lab})
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
    lines += ["## RESUMEN (periodo completo)",
              full[["ret", "cagr", "max_dd", "neg_months", "worst_month", "sharpe", "trades", "win_rate", "fees_usd",
                    "retorno_por_caida", "tercios_positivos", "gana_a_actual_en_tercios",
                    "gana_a_actual_en_ambas_mitades"]].round(3).to_string(), ""]
    better = full[(full.index != b.name) & full.gana_a_actual_en_ambas_mitades & (full.gana_a_actual_en_tercios >= 2)
                  & (full.max_dd >= b.max_dd - 0.02) & (full.neg_months <= b.neg_months + 1)]
    if len(better):
        best = better.sort_values("sharpe", ascending=False).iloc[0]
        lines.append(f"CANDIDATA: {best.name}: {best.ret:+.1%}, caída {best.max_dd:+.1%}, {int(best.neg_months)} meses en rojo, "
                     f"Sharpe {best.sharpe:.2f} (actual: {b.ret:+.1%}, {b.max_dd:+.1%}, {int(b.neg_months)}, {b.sharpe:.2f}). "
                     "Gana en ambas mitades y en >= 2 de 3 tercios sin más caída.")
    else:
        lines.append("Ninguna variante gana a la actual de forma consistente (ambas mitades, >= 2 tercios, sin más caída): "
                     "se mantiene la señal actual.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_11a{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_11a{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
