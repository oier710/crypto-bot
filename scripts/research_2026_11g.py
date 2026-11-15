"""Investigación nov-2026 (7ª parte): STOP QUE PERSIGUE en el último tercio (idea de Oier, 2026-11-14).

Problema: el último tercio de cada operación corre con el stop fijo en la entrada. En un giro del mercado se devuelve
toda la ganancia no cobrada (hoy ~8 $ de 117 $). Idea: tras la parcial 2, el stop sigue al precio a K×ATR del extremo
(mínimo alcanzado en cortos, máximo en largos) y solo se aprieta, nunca se afloja. Si el precio gira, se cobra una parte.
Variantes (K fijados ANTES de mirar resultados): K = 2, 3, 4 tras la parcial 2; K = 3 ya tras la parcial 1; K = 2 tras la 1.
Todo lo demás idéntico a producción (cartera v5, gestión v3, costes). Simulador = research_2026_11c con un gancho;
se comprueba que sin cambios da lo mismo. Criterio (el de siempre): ganar a la actual en ambas mitades y en >= 2 de 3
tercios, sin más caída máxima ni más meses en rojo.
Salida: results/research_2026_11g.txt y .csv
"""
from __future__ import annotations

import argparse
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import research_2026_10 as base  # noqa: E402
import research_2026_11c as m11c  # noqa: E402

COST, APR, WEIGHTS = m11c.COST, m11c.APR, m11c.WEIGHTS


@dataclass(frozen=True)
class Var:
    name: str
    k: float | None = None     # None = sin stop que persigue (actual)
    after: int = 2             # nº de parciales hechas a partir del cual persigue (2 = solo el último tercio)


VARIANTS = [
    Var("A actual (stop fijo en entrada)"),
    Var("S1 persigue 2 ATR tras parcial 2", k=2.0, after=2),
    Var("S2 persigue 3 ATR tras parcial 2", k=3.0, after=2),
    Var("S3 persigue 4 ATR tras parcial 2", k=4.0, after=2),
    Var("S4 persigue 3 ATR tras parcial 1", k=3.0, after=1),
    Var("S5 persigue 2 ATR tras parcial 1", k=2.0, after=1),
]

# ---- simulador de 11c con un gancho: stop que persigue -----------------------------------------------------
_TRAIL = {"k": None, "after": 2}

_src = inspect.getsource(m11c.simulate)
_a = "            if s.side and ((side == 1 and px <= s.stop) or (side == -1 and px >= s.stop)):"
assert _src.count(_a) == 1, "el simulador de 11c ha cambiado"
_hook = (
    "            if _TRAIL['k'] and s.side and s.pt_done >= _TRAIL['after'] and s.atr_e:\n"
    "                _tr = s.extreme - side * _TRAIL['k'] * s.atr_e\n"
    "                s.stop = max(s.stop, _tr) if side == 1 else min(s.stop, _tr)\n"
)
_src = _src.replace(_a, _hook + _a)
_ns = dict(vars(m11c))
_ns["_TRAIL"] = _TRAIL
exec(compile(_src.replace("def simulate(", "def simulate_x("), "<simulate_x>", "exec"), _ns)
simulate_x = _ns["simulate_x"]


def sim(h, d, v: Var, capital, cost, apr):
    _TRAIL["k"], _TRAIL["after"] = v.k, v.after
    try:
        return simulate_x(h, d, m11c.VARIANTS[0], capital, cost, apr)
    finally:
        _TRAIL["k"], _TRAIL["after"] = None, 2


def portfolio(data, v, start, end):
    eqs, trades, fees, per = [], [], 0.0, {}
    for sym, w in WEIGHTS.items():
        h, d = data[sym]
        hh = h[(h.index >= start) & (h.index < end)]
        e, s = sim(hh, d, v, base.CAPITAL * w, COST[sym], APR[sym])
        eqs.append(e)
        trades += s.trades
        fees += s.fees
        per[sym] = float(e.iloc[-1] - e.iloc[0])
    eq = pd.concat(eqs, axis=1).ffill().bfill().sum(axis=1)
    r = base.metrics(eq, trades, fees)
    r["stops"] = sum(1 for t in trades if t[1] == "stop")
    r["senal"] = sum(1 for t in trades if t[1] == "señal")
    r.update({f"pnl_{k}": val for k, val in per.items()})
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
    start = t0 + pd.Timedelta(days=90)
    end = t1 + pd.Timedelta(hours=1)
    mid = start + (t1 - start) / 2
    th = [start + (t1 - start) * k / 3 for k in range(4)]
    th[-1] = end
    periods = [("PERIODO COMPLETO", start, end), ("1ª MITAD", start, mid), ("2ª MITAD", mid, end),
               ("TERCIO 1", th[0], th[1]), ("TERCIO 2 (lateral)", th[1], th[2]), ("TERCIO 3", th[2], th[3])]

    lines = ["# Investigación nov-2026 (7ª parte) — stop que persigue en el último tercio (idea de Oier)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             "Cartera v5, gestión v3 y costes idénticos a producción."]
    h0, d0 = data["ETH"]
    hh = h0[(h0.index >= start) & (h0.index < end)]
    e1, _ = m11c.simulate(hh, d0, m11c.VARIANTS[0], 50.0, 0.001, 0.04)
    e2, _ = sim(hh, d0, VARIANTS[0], 50.0, 0.001, 0.04)
    lines.append(f"Comprobación del simulador: diferencia máxima con el de producción {float((e1 - e2).abs().max()):.6f} $ (debe ser 0).")
    lines.append("Columnas extra: stops = cierres por stop (incluye el stop que persigue); señal = cierres por cambio de señal.")
    lines.append("")
    rows = []
    for lab, s0, s1 in periods:
        lines += [f"## {lab} ({s0.date()} -> {min(s1, t1).date()})", base.HEADER + f" {'stops':>6} {'señal':>6}"]
        for v in VARIANTS:
            r = portfolio(data, v, s0, s1)
            r["variant"] = v.name
            lines.append(base.fmt(r) + f" {r['stops']:>6} {r['senal']:>6}")
            rows.append(r | {"period": lab})
        lines.append("")
    df = pd.DataFrame(rows)
    full = df[df.period == "PERIODO COMPLETO"].set_index("variant")
    b = full.iloc[0]
    sub = df[df.period.str.startswith("TERCIO")].pivot_table(index="variant", columns="period", values="ret")
    halves = df[df.period.isin(["1ª MITAD", "2ª MITAD"])].pivot_table(index="variant", columns="period", values="ret")
    full["gana_tercios"] = [int((sub.loc[n] > sub.loc[b.name]).sum()) for n in full.index]
    full["gana_ambas_mitades"] = [bool((halves.loc[n] > halves.loc[b.name]).all()) for n in full.index]
    lines += ["## RESUMEN (periodo completo)",
              full[["ret", "max_dd", "neg_months", "worst_month", "sharpe", "trades", "win_rate", "fees_usd", "stops", "senal",
                    "gana_tercios", "gana_ambas_mitades"]].round(3).to_string(), ""]
    ok = full[(full.index != b.name) & full.gana_ambas_mitades & (full.gana_tercios >= 2) & (full.max_dd >= b.max_dd)
              & (full.neg_months <= b.neg_months)]
    if len(ok):
        lines.append("PASAN el criterio (ambas mitades, >= 2 tercios, sin más caída ni más meses en rojo): " + "; ".join(
            f"{n}: {r.ret:+.1%}, caída {r.max_dd:+.1%}, Sharpe {r.sharpe:.2f}" for n, r in ok.iterrows())
            + f"  (actual: {b.ret:+.1%}, {b.max_dd:+.1%}, {b.sharpe:.2f})")
        lines.append("Solo vale si las variantes vecinas (otros K) apuntan en la misma dirección; si solo gana un valor, es casualidad.")
    else:
        lines.append("Ninguna variante pasa el criterio: se mantiene el sistema actual.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_11g{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_11g{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
