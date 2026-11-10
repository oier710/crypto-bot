"""Investigación nov-2026 (6ª parte): las dos ideas que sobrevivieron al debate de expertos (2026-11-10).

T1  HISTÉRESIS DEL ADX. Hoy la señal no tiene memoria: (EMA20 > EMA60) y ADX > 20. Si el ADX baja a 19.9 se cierra y
    si al día siguiente vuelve a 20.1 se reabre (dos comisiones y stop nuevo). Pasó en real: corto de BTC cerrado el
    7-nov por "señal 0" y reabierto el 10-nov. Regla: ENTRAR igual (ADX > 20); MANTENER mientras las EMAs sigan del
    mismo lado y ADX > 15. Vecinos para comprobar que no es casualidad: salida con ADX 17 y "solo cruce de EMAs".
T2  BLOQUEO TRAS STOP. Hoy tras un stop solo se espera al día siguiente. En real: stop de ETH el 8-oct, recompra 9 h
    después, −6.7 %. Regla: tras un stop, no volver a entrar en esa dirección hasta que el cierre diario supere el
    máximo de los 10 días previos al stop (largo) o caiga bajo el mínimo (corto). Un giro de la señal lo anula.
Parámetros fijados ANTES de mirar resultados (15 y 10 días). Todo lo demás idéntico a producción (cartera v5, gestión
v3, costes). El simulador es el de research_2026_11c con dos ganchos; se comprueba que sin cambios da lo mismo.
Criterio: ganar a la actual en ambas mitades y en >= 2 de 3 tercios sin más caída.
Salida: results/research_2026_11f.txt y .csv
"""
from __future__ import annotations

import argparse
import inspect
import sys
import types
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import research_2026_10 as base  # noqa: E402
import research_2026_11c as m11c  # noqa: E402
from bot.indicators import adx, ema  # noqa: E402
from bot.strategies import trend_ema  # noqa: E402

COST, APR, WEIGHTS = m11c.COST, m11c.APR, m11c.WEIGHTS
BLOCK_DAYS = 10


@dataclass(frozen=True)
class Var:
    name: str
    exit_adx: float | None = 20.0     # 20 = actual (sin memoria); 15/17 = histéresis; None = solo cruce de EMAs
    block: bool = False               # bloqueo de reentrada tras stop


VARIANTS = [
    Var("A actual"),
    Var("T1 histéresis: salida ADX<15", exit_adx=15),
    Var("T1b histéresis: salida ADX<17", exit_adx=17),
    Var("T1c mantener hasta cruce de EMAs", exit_adx=None),
    Var("T2 bloqueo tras stop (10 días)", block=True),
    Var("T1 + T2", exit_adx=15, block=True),
]


def hyst_signal(d: pd.DataFrame, exit_adx) -> pd.Series:
    f, s, a = ema(d.close, 20).values, ema(d.close, 60).values, adx(d, 14).values
    out, cur = np.zeros(len(d), dtype=int), 0
    for i in range(len(d)):
        if np.isnan(a[i]) or np.isnan(s[i]):
            cur = 0
        else:
            dirn = 1 if f[i] > s[i] else (-1 if f[i] < s[i] else 0)
            if cur != 0 and (dirn != cur or (exit_adx is not None and a[i] <= exit_adx)):
                cur = 0
            if cur == 0 and dirn != 0 and a[i] > 20:
                cur = dirn
        out[i] = cur
    return pd.Series(out, index=d.index)


# ---- simulador de 11c con dos ganchos (bloqueo tras stop) --------------------------------------------------
_BLOCK = {"on": False}


def _block_level(d, day, side):
    w = d[d.index < day].tail(BLOCK_DAYS)
    return (side, float(w.high.max() if side == 1 else w.low.min()))


def _reentry_ok(s, d, day, sig):
    blk = getattr(s, "blk", None)
    if not blk:
        return True
    bside, level = blk
    if sig != bside:
        s.blk = None
        return True
    prev = d.close[d.index < day]
    if prev.empty:
        return False
    pc = float(prev.iloc[-1])
    ok = pc > level if bside == 1 else pc < level
    if ok:
        s.blk = None
    return ok


_src = inspect.getsource(m11c.simulate)
_a = "if not v.hourly_entry and cur_sig != 0 and s.side == 0 and s.stopped_day != day:"
_b = 'close_part(px, 1.0, "stop", t)\n                s.stopped_day = day'
assert _src.count(_a) == 1 and _src.count(_b) == 1, "el simulador de 11c ha cambiado"
_src = _src.replace(_a, _a[:-1] + " and _reentry_ok(s, d, day, cur_sig):")
_src = _src.replace(_b, _b + "\n                s.blk = _block_level(d, day, side) if _BLOCK['on'] else None")
_ns = dict(vars(m11c))
_ns.update(_reentry_ok=_reentry_ok, _block_level=_block_level, _BLOCK=_BLOCK)
exec(compile(_src.replace("def simulate(", "def simulate_x("), "<simulate_x>", "exec"), _ns)
simulate_x = _ns["simulate_x"]


def sim(h, d, v: Var, capital, cost, apr):
    _BLOCK["on"] = v.block
    if v.exit_adx == 20.0:
        _ns["trend_ema"] = trend_ema
    else:
        _ns["trend_ema"] = types.SimpleNamespace(signals=lambda df, **k: hyst_signal(df, v.exit_adx))
    try:
        return simulate_x(h, d, m11c.VARIANTS[0], capital, cost, apr)
    finally:
        _ns["trend_ema"] = trend_ema
        _BLOCK["on"] = False


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

    lines = ["# Investigación nov-2026 (6ª parte) — histéresis del ADX y bloqueo tras stop (debate de expertos)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             "Cartera v5, gestión v3 y costes idénticos a producción."]
    # comprobación: el simulador con ganchos, sin cambios, reproduce al de 11c
    h0, d0 = data["ETH"]
    hh = h0[(h0.index >= start) & (h0.index < end)]
    e1, _ = m11c.simulate(hh, d0, m11c.VARIANTS[0], 50.0, 0.001, 0.04)
    e2, _ = sim(hh, d0, VARIANTS[0], 50.0, 0.001, 0.04)
    lines.append(f"Comprobación del simulador: diferencia máxima con el de producción {float((e1 - e2).abs().max()):.6f} $ (debe ser 0).")
    # premisa de T1: ¿cuántos cierres por señal son parpadeos del ADX?
    lines.append("Premisa T1 — veces que la señal pasó a 0 SIN cruce de EMAs y volvió al mismo lado en <= 5 días:")
    for sym in WEIGHTS:
        d = data[sym][1]
        sg = trend_ema.signals(d, fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True)
        sg = sg[sg.index >= start].values
        exits = flick = 0
        for i in range(1, len(sg)):
            if sg[i - 1] != 0 and sg[i] == 0:
                exits += 1
                if any(x == sg[i - 1] for x in sg[i + 1:i + 6]):
                    flick += 1
        lines.append(f"  {sym}: {flick} de {exits} salidas a señal 0")
    lines.append("Columnas extra: stops = cierres por stop; señal = cierres por cambio de señal.")
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
        lines.append("Para T1 solo vale si sus vecinas (T1b, T1c) apuntan en la misma dirección; si solo gana un valor, es casualidad.")
    else:
        lines.append("Ninguna variante pasa el criterio: se mantiene el sistema actual.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_11f{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_11f{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
