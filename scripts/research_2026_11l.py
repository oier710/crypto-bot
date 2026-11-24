"""Investigación nov-2026 (12ª parte): ¿PARÁMETROS PROPIOS PARA CADA MONEDA? (pregunta de Oier, 2026-11-24)

"¿Siempre para todas las monedas valen los mismos criterios? Históricamente no todas funcionan igual."
Hoy BTC, ETH y ARB usan la misma señal: EMA 20/60 + ADX > 20 (stops y parciales ya se adaptan a cada moneda vía ATR).
Riesgo: con ~50 operaciones por moneda, buscar la mejor combinación casi siempre encuentra algo que "funcionó" por
casualidad. Por eso se mide FUERA DE MUESTRA (fijado antes de ver resultados):
  * Rejilla por moneda: EMA rápida {10, 15, 20, 30} x EMA lenta {40, 60, 90, 120} x ADX mínimo {15, 20, 25} (48).
  * Pliegue A: se elige con la 1ª mitad (mejor Sharpe de la moneda sola, con >= 8 operaciones) y se prueba en la 2ª.
    Pliegue B: al revés (se elige con la 2ª y se prueba en la 1ª).
  * Se compara en la mitad NO vista: producción (20/60/20 en todas) vs POR MONEDA (cada una con lo suyo) vs COMÚN
    (una sola combinación reoptimizada para la cartera; separa "reoptimizar" de "por moneda").
Criterio para cambiar: POR MONEDA gana a producción en la mitad no vista en LOS DOS pliegues (retorno y Sharpe), sin más
caída máxima, y también gana a COMÚN (si no, la ventaja no es de "cada moneda lo suyo").
Además: en qué puesto queda 20/60/20 dentro de las 48 combinaciones de cada moneda en el periodo completo.
Resto idéntico a producción v6 (cartera BTC 42.5 / ETH 42.5 / ARB 15 %, 75 %, stop 2 ATR, parciales, reentrada, stop que
persigue 3 ATR desde la parcial 1, costes reales). Simulador = research_2026_11k con tamaño fijo 0.75 (= producción).
Salida: results/research_2026_11l.txt y .csv
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import research_2026_10 as base  # noqa: E402
import research_2026_11k as m11k  # noqa: E402

WEIGHTS, COST, APR = m11k.WEIGHTS, m11k.COST, m11k.APR
PROD = (20, 60, 20)
GRID = [(f, s, a) for f, s, a in itertools.product((10, 15, 20, 30), (40, 60, 90, 120), (15, 20, 25)) if f < s]
MIN_TRADES = 8
VAR = m11k.REFS[2]          # tamaño fijo 0.75 = producción


def sleeve(data, sym, params, start, end):
    """Simula una moneda con (rápida, lenta, ADX mínimo). Devuelve (curva de equity, métricas)."""
    m11k.FAST, m11k.SLOW, m11k.ADX_MIN = params
    try:
        h, d = data[sym]
        hh = h[(h.index >= start) & (h.index < end)]
        e, s = m11k.simulate(hh, d, VAR, base.CAPITAL * WEIGHTS[sym], COST[sym], APR[sym])
    finally:
        m11k.FAST, m11k.SLOW, m11k.ADX_MIN = PROD
    return e, s, base.metrics(e, s.trades, s.fees)


def portfolio(runs: dict) -> dict:
    """runs = {sym: (e, s, m)} -> métricas de la cartera."""
    eq = pd.concat([r[0] for r in runs.values()], axis=1).ffill().bfill().sum(axis=1)
    trades = sum((r[1].trades for r in runs.values()), [])
    fees = sum(r[1].fees for r in runs.values())
    return base.metrics(eq, trades, fees)


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
    start = t0 + pd.Timedelta(days=150)          # las EMA de 120 necesitan historia
    end = t1 + pd.Timedelta(hours=1)
    mid = start + (t1 - start) / 2
    halves = {"1ª MITAD": (start, mid), "2ª MITAD": (mid, end), "COMPLETO": (start, end)}

    lines = ["# Investigación nov-2026 (12ª parte) — ¿parámetros propios para cada moneda? (pregunta de Oier)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()} "
             f"(1ª mitad hasta {mid.date()}). Producción = EMA 20/60 + ADX > 20 en las tres. Rejilla de {len(GRID)} combinaciones por moneda."]
    # comprobación: con 20/60/20 el simulador da lo mismo que producción (11g S4)
    import research_2026_11g as m11g
    h0, d0 = data["ETH"]
    hh = h0[(h0.index >= start) & (h0.index < end)]
    e1, _ = m11g.sim(hh, d0, m11g.Var("prod", k=3.0, after=1), 50.0, 0.001, 0.04)
    e2, _ = m11k.simulate(hh, d0, VAR, 50.0, 0.001, 0.04)
    lines += [f"Comprobación del simulador: diferencia máxima con producción {float((e1 - e2).abs().max()):.6f} $ (debe ser 0).", ""]

    # todas las simulaciones por moneda, combinación y tramo
    res = {}
    rows = []
    for sym in WEIGHTS:
        for p in GRID:
            for lab, (s0, s1) in halves.items():
                e, s, m = sleeve(data, sym, p, s0, s1)
                res[(sym, p, lab)] = (e, s, m)
                rows.append({"coin": sym, "fast": p[0], "slow": p[1], "adx": p[2], "period": lab, **m})
    df = pd.DataFrame(rows)

    # puesto de producción en el periodo completo, por moneda
    lines.append("## ¿Dónde queda 20/60/20 entre las 48 combinaciones de cada moneda? (periodo completo, por Sharpe)")
    for sym in WEIGHTS:
        c = df[(df.coin == sym) & (df.period == "COMPLETO")].sort_values("sharpe", ascending=False).reset_index(drop=True)
        rank = int(c.index[(c.fast == PROD[0]) & (c.slow == PROD[1]) & (c.adx == PROD[2])][0]) + 1
        best = c.iloc[0]
        pr = c.iloc[rank - 1]
        lines.append(f"{sym}: puesto {rank}/{len(c)} (Sharpe {pr.sharpe:.2f}, retorno {pr.ret:+.1%}) | mejor {int(best.fast)}/{int(best.slow)}/ADX {int(best.adx)}: "
                     f"Sharpe {best.sharpe:.2f}, retorno {best.ret:+.1%} | mediana de las 48: Sharpe {c.sharpe.median():.2f}")
    lines.append("")

    def pick(sym, lab):
        c = df[(df.coin == sym) & (df.period == lab) & (df.trades >= MIN_TRADES)]
        if c.empty:
            return PROD
        b = c.sort_values("sharpe", ascending=False).iloc[0]
        return int(b.fast), int(b.slow), int(b.adx)

    def pick_common(lab):
        best, bp = -9e9, PROD
        for p in GRID:
            m = portfolio({sym: res[(sym, p, lab)] for sym in WEIGHTS})
            if m["sharpe"] > best:
                best, bp = m["sharpe"], p
        return bp

    out = []
    for fold, train, test in (("A (elige 1ª, prueba 2ª)", "1ª MITAD", "2ª MITAD"), ("B (elige 2ª, prueba 1ª)", "2ª MITAD", "1ª MITAD")):
        per = {sym: pick(sym, train) for sym in WEIGHTS}
        com = pick_common(train)
        m_prod = portfolio({sym: res[(sym, PROD, test)] for sym in WEIGHTS})
        m_per = portfolio({sym: res[(sym, per[sym], test)] for sym in WEIGHTS})
        m_com = portfolio({sym: res[(sym, com, test)] for sym in WEIGHTS})
        m_per_in = portfolio({sym: res[(sym, per[sym], train)] for sym in WEIGHTS})
        m_prod_in = portfolio({sym: res[(sym, PROD, train)] for sym in WEIGHTS})
        lines += [f"## Pliegue {fold}",
                  "Elegido por moneda: " + ", ".join(f"{s} {p[0]}/{p[1]}/ADX {p[2]}" for s, p in per.items())
                  + f" | común: {com[0]}/{com[1]}/ADX {com[2]}",
                  f"En la mitad usada para elegir ({train}): por moneda {m_per_in['ret']:+.1%} (Sharpe {m_per_in['sharpe']:.2f}) "
                  f"vs producción {m_prod_in['ret']:+.1%} ({m_prod_in['sharpe']:.2f})  <- aquí siempre gana: es lo que se ha optimizado",
                  f"En la mitad NO vista ({test}):"]
        for lab, m in (("producción 20/60/20", m_prod), ("POR MONEDA", m_per), ("COMÚN reoptimizado", m_com)):
            lines.append(f"  {lab:<22} retorno {m['ret']:+7.1%}  caída {m['max_dd']:+6.1%}  Sharpe {m['sharpe']:5.2f}  "
                         f"meses en rojo {m['neg_months']}/{m['months']}  operaciones {m['trades']}")
        lines.append("  por moneda, cada una sola en la mitad no vista: " + "; ".join(
            f"{s} {res[(s, per[s], test)][2]['ret']:+.1%} vs prod {res[(s, PROD, test)][2]['ret']:+.1%}" for s in WEIGHTS))
        lines.append("")
        out.append(dict(fold=fold, prod=m_prod, per=m_per, com=m_com))

    ok = all(o["per"]["ret"] > o["prod"]["ret"] and o["per"]["sharpe"] > o["prod"]["sharpe"]
             and o["per"]["max_dd"] >= o["prod"]["max_dd"] for o in out)
    beats_common = all(o["per"]["sharpe"] > o["com"]["sharpe"] for o in out)
    lines.append("## CONCLUSIÓN")
    if ok and beats_common:
        lines.append("POR MONEDA PASA: gana a producción fuera de muestra en los dos pliegues (retorno, Sharpe, sin más caída) "
                     "y también a la reoptimización común. Candidata a revisar con más detalle antes de desplegar.")
    elif ok:
        lines.append("Por moneda gana a producción fuera de muestra, pero NO a la reoptimización común: la ventaja no viene de "
                     "'cada moneda lo suyo' sino de otra combinación general. No se cambia; se apunta para el 16-ene.")
    else:
        lines.append("POR MONEDA NO PASA: elegir parámetros para cada moneda no mejora fuera de muestra (lo que gana en la mitad "
                     "optimizada no se repite en la otra). Se mantiene 20/60/20 para todas.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_11l{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_11l{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
