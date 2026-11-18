"""Investigación nov-2026 (8ª parte): CIERRE DEL ÚLTIMO TERCIO (crítica de Oier, 2026-11-17).

Crítica: tras las dos parciales, el último tercio solo sale por el stop en la entrada o por la señal diaria (lenta).
Si el precio vuelve a la entrada, la tendencia ya estaba agotada mucho antes: se devuelve todo lo ganado. Hay que
definir CUÁNDO se cobra el resto. Ideas (parámetros fijados ANTES de mirar resultados):
E1  ESCALÓN: al hacer la parcial 2 (+3 ATR), el stop pasa al nivel de la parcial 1 (+1.5 ATR): el resto ya no puede
    cerrar sin beneficio.
E2  ESCALÓN corto: al hacer la parcial 2, stop a +1 ATR.
E3  ESCALÓN + PERSIGUE: como E1 y además el stop sigue al extremo a 4 ATR (el más ajustado de los dos).
F1  SALIDA RÁPIDA: tras la parcial 2, el resto se cierra en el ciclo diario si el cierre de ayer cruza la EMA20 en
    contra (la tendencia corta se ha girado), sin esperar al ADX ni al cruce 20/60.
F2  E1 + F1.
Referencia del estudio anterior (11g): S3 = persigue 4 ATR tras parcial 2.
Todo lo demás idéntico a producción (cartera v5, gestión v3, costes). Simulador = research_2026_11c con ganchos; se
comprueba que sin cambios da lo mismo. Criterio: ganar a la actual en ambas mitades y en >= 2 de 3 tercios, sin más
caída máxima ni más meses en rojo.
Salida: results/research_2026_11h.txt y .csv
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
from bot.indicators import ema  # noqa: E402

COST, APR, WEIGHTS = m11c.COST, m11c.APR, m11c.WEIGHTS


@dataclass(frozen=True)
class Var:
    name: str
    lock: float | None = None    # tras la parcial 2, stop a entrada + lock×ATR de beneficio
    trail: float | None = None   # tras la parcial 2, stop a trail×ATR del extremo
    ema_exit: bool = False       # tras la parcial 2, cerrar si el cierre de ayer cruza la EMA20 en contra


VARIANTS = [
    Var("A actual (stop fijo en entrada)"),
    Var("E1 escalón: stop a +1.5 ATR tras parcial 2", lock=1.5),
    Var("E2 escalón: stop a +1 ATR tras parcial 2", lock=1.0),
    Var("E3 escalón +1.5 y persigue 4 ATR", lock=1.5, trail=4.0),
    Var("F1 salida rápida EMA20 tras parcial 2", ema_exit=True),
    Var("F2 escalón +1.5 y salida rápida EMA20", lock=1.5, ema_exit=True),
    Var("S3 (11g) persigue 4 ATR tras parcial 2", trail=4.0),
]

# ---- simulador de 11c con ganchos ---------------------------------------------------------------------------
_X = {"lock": None, "trail": None, "ema_exit": False}

_src = inspect.getsource(m11c.simulate)
_a = "            if s.side and ((side == 1 and px <= s.stop) or (side == -1 and px >= s.stop)):"
_b = "    day_atr = atr(d, N).shift(1)\n"
_c = "                    open_pos(o, cur_sig, a_day, t)\n"
assert _src.count(_a) == 1 and _src.count(_b) == 1 and _src.count(_c) == 1, "el simulador de 11c ha cambiado"
_hook_stop = (
    "            if s.side and s.pt_done >= 2 and s.atr_e:\n"
    "                if _X['lock']:\n"
    "                    _lk = s.ref + side * _X['lock'] * s.atr_e\n"
    "                    s.stop = max(s.stop, _lk) if side == 1 else min(s.stop, _lk)\n"
    "                if _X['trail']:\n"
    "                    _tr = s.extreme - side * _X['trail'] * s.atr_e\n"
    "                    s.stop = max(s.stop, _tr) if side == 1 else min(s.stop, _tr)\n"
)
_hook_pre = "    _ema20 = _ema(d.close, 20).shift(1)\n    _pc = d.close.shift(1)\n"
_hook_day = (
    "                if _X['ema_exit'] and s.side and s.pt_done >= 2:\n"
    "                    _e, _p = _ema20.get(day, np.nan), _pc.get(day, np.nan)\n"
    "                    if not np.isnan(_e) and not np.isnan(_p) and s.side * (_p - _e) < 0:\n"
    "                        close_part(o, 1.0, 'ema20', t)\n"
)
_src = _src.replace(_a, _hook_stop + _a).replace(_b, _b + _hook_pre).replace(_c, _c + _hook_day)
_ns = dict(vars(m11c))
_ns.update(_X=_X, _ema=ema)
exec(compile(_src.replace("def simulate(", "def simulate_x("), "<simulate_x>", "exec"), _ns)
simulate_x = _ns["simulate_x"]


def sim(h, d, v: Var, capital, cost, apr):
    _X.update(lock=v.lock, trail=v.trail, ema_exit=v.ema_exit)
    try:
        return simulate_x(h, d, m11c.VARIANTS[0], capital, cost, apr)
    finally:
        _X.update(lock=None, trail=None, ema_exit=False)


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
    r["ema20"] = sum(1 for t in trades if t[1] == "ema20")
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

    lines = ["# Investigación nov-2026 (8ª parte) — cierre del último tercio: escalón, persigue y salida rápida (crítica de Oier)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             "Cartera v5, gestión v3 y costes idénticos a producción."]
    h0, d0 = data["ETH"]
    hh = h0[(h0.index >= start) & (h0.index < end)]
    e1, _ = m11c.simulate(hh, d0, m11c.VARIANTS[0], 50.0, 0.001, 0.04)
    e2, _ = sim(hh, d0, VARIANTS[0], 50.0, 0.001, 0.04)
    lines.append(f"Comprobación del simulador: diferencia máxima con el de producción {float((e1 - e2).abs().max()):.6f} $ (debe ser 0).")
    lines.append("Columnas extra: stops = cierres por stop; señal = cierres por cambio de señal; ema20 = cierres por salida rápida.")
    lines.append("")
    rows = []
    for lab, s0, s1 in periods:
        lines += [f"## {lab} ({s0.date()} -> {min(s1, t1).date()})", base.HEADER + f" {'stops':>6} {'señal':>6} {'ema20':>6}"]
        for v in VARIANTS:
            r = portfolio(data, v, s0, s1)
            r["variant"] = v.name
            lines.append(base.fmt(r) + f" {r['stops']:>6} {r['senal']:>6} {r['ema20']:>6}")
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
                    "ema20", "gana_tercios", "gana_ambas_mitades"]].round(3).to_string(), ""]
    ok = full[(full.index != b.name) & full.gana_ambas_mitades & (full.gana_tercios >= 2) & (full.max_dd >= b.max_dd)
              & (full.neg_months <= b.neg_months)]
    if len(ok):
        lines.append("PASAN el criterio (ambas mitades, >= 2 tercios, sin más caída ni más meses en rojo): " + "; ".join(
            f"{n}: {r.ret:+.1%}, caída {r.max_dd:+.1%}, Sharpe {r.sharpe:.2f}" for n, r in ok.iterrows())
            + f"  (actual: {b.ret:+.1%}, {b.max_dd:+.1%}, {b.sharpe:.2f})")
    else:
        lines.append("Ninguna variante pasa el criterio estricto.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_11h{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_11h{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
