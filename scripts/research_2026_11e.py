"""Investigación nov-2026 (5ª parte): estrategia de RANGO para cuando no hay tendencia.

Objetivo de Oier (2026-11-10): "ser muy rentable y fallar muy poco, siendo muy ágil". El punto débil del sistema es
el mercado lateral: con la señal diaria en 0 (ADX <= 20) el bot está fuera y no gana nada. Aquí se prueba una
segunda estrategia que SOLO opera esos días (nunca a la vez que la de tendencia, mismo capital):
comprar cuando el precio cae al borde inferior de su rango y vender al volver al centro (y al revés en corto).
  B*  Bandas de Bollinger diarias (media 20 días ± k desviaciones, datos del día anterior), precio horario:
      entra al tocar la banda, sale en la media; stop a s x ATR; tiempo máximo 7 días.
  R*  RSI 14 en velas de 4 h: entra con RSI < 30 (largo) o > 70 (corto), sale al cruzar 50.
Si la señal de tendencia se activa (≠ 0), la posición de rango se cierra en la apertura del día y manda la tendencia.
Tamaño 75 % de la parte de cada moneda, como la de tendencia. Costes 0.10 %/lado + gas; cortos +0.10 % e interés.
Se mide la estrategia de rango sola y la CARTERA COMBINADA (tendencia v5 + rango) frente a la actual.
Criterio fijado antes: la combinada tiene que ganar a la actual en ambas mitades y en >= 2 de 3 tercios sin más caída.
Salida: results/research_2026_11e.txt y .csv
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import research_2026_10 as base  # noqa: E402
import research_2026_11c as m11c  # noqa: E402
from bot.indicators import atr  # noqa: E402
from bot.strategies import trend_ema  # noqa: E402

COST = {"BTC": 0.0010, "ETH": 0.0010, "ARB": 0.0010}
APR = {"BTC": 0.04, "ETH": 0.04, "ARB": 0.07}
WEIGHTS = {"BTC": 0.425, "ETH": 0.425, "ARB": 0.15}
MAX_HOURS = 7 * 24


@dataclass(frozen=True)
class Var:
    name: str
    kind: str = "bb"        # "bb" o "rsi"
    k: float = 2.0          # desviaciones de la banda
    stop_k: float = 1.5     # stop en ATR diarios
    lo: float = 30.0        # umbrales RSI
    hi: float = 70.0
    short: bool = True


VARIANTS = [
    Var("B1 Bollinger 2.0, stop 1.5 ATR, L+C"),
    Var("B2 Bollinger 2.0, stop 1.5 ATR, solo largo", short=False),
    Var("B3 Bollinger 1.5, stop 1.5 ATR, L+C", k=1.5),
    Var("B4 Bollinger 2.0, stop 2.5 ATR, L+C", stop_k=2.5),
    Var("B5 Bollinger 2.5, stop 2.0 ATR, L+C", k=2.5, stop_k=2.0),
    Var("R1 RSI 30/70, stop 1.5 ATR, L+C", kind="rsi"),
    Var("R2 RSI 25/75, stop 2.0 ATR, L+C", kind="rsi", lo=25, hi=75, stop_k=2.0),
    Var("R3 RSI 30/70, stop 1.5 ATR, solo largo", kind="rsi", short=False),
]


def rsi_wilder(close: pd.Series, n: int) -> pd.Series:
    dlt = close.diff()
    up = dlt.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-dlt.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def simulate_range(h, d, v: Var, capital, cost_pct, apr):
    sig_d = trend_ema.signals(d, fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True).shift(1)
    sma = d.close.rolling(20).mean().shift(1)
    sd = d.close.rolling(20).std().shift(1)
    atr_d = atr(d, 14).shift(1)
    # RSI 14 sobre velas de 4 h; se usa el de la última vela de 4 h ya cerrada (sin mirar el futuro)
    c4 = h.close.resample("4h").last().dropna()
    rsi = rsi_wilder(c4, 14).shift(1).reindex(h.index, method="ffill").to_numpy()
    closes, opens = h.close.to_numpy(), h.open.to_numpy()
    cash, side, qty, entry, stop, target, held = capital, 0, 0.0, 0.0, 0.0, 0.0, 0
    trades, fees, eq = [], 0.0, np.empty(len(h))
    last_day, sig, mid, dev, a, stopped_day = None, np.nan, np.nan, np.nan, np.nan, None
    realized = 0.0
    rate = apr / (365 * 24)

    def cost(usd, short):
        return abs(usd) * (cost_pct + (base.SHORT_EXTRA_PCT if short else 0.0)) + base.GAS_USD

    for i, t in enumerate(h.index):
        day = t.normalize()
        px = closes[i]
        if day != last_day:
            last_day = day
            sig, mid, dev, a = sig_d.get(day, np.nan), sma.get(day, np.nan), sd.get(day, np.nan), atr_d.get(day, np.nan)
            if side and not (sig == 0):          # se activa la tendencia: cerrar en la apertura
                o = opens[i]
                c = cost(qty * o, side == -1)
                cash += (qty * o - c) if side == 1 else (-(qty * o) - c)
                fees += c
                trades.append((t, "tendencia", realized + side * qty * (o - entry) - c))
                side, qty = 0, 0.0
        if side:
            held += 1
            if side == -1:
                c = qty * px * rate
                cash -= c
                fees += c
            hit_stop = (side == 1 and px <= stop) or (side == -1 and px >= stop)
            if v.kind == "bb":
                hit_tgt = (side == 1 and px >= mid) or (side == -1 and px <= mid)
            else:
                hit_tgt = (side == 1 and rsi[i] >= 50) or (side == -1 and rsi[i] <= 50)
            if hit_stop or hit_tgt or held >= MAX_HOURS:
                c = cost(qty * px, side == -1)
                cash += (qty * px - c) if side == 1 else (-(qty * px) - c)
                fees += c
                trades.append((t, "stop" if hit_stop else ("objetivo" if hit_tgt else "tiempo"),
                               realized + side * qty * (px - entry) - c))
                if hit_stop:
                    stopped_day = day
                side, qty = 0, 0.0
        elif sig == 0 and stopped_day != day and not (np.isnan(mid) or np.isnan(dev) or np.isnan(a)):
            new = 0
            if v.kind == "bb":
                if px <= mid - v.k * dev:
                    new = 1
                elif px >= mid + v.k * dev and v.short:
                    new = -1
            elif not np.isnan(rsi[i]):
                if rsi[i] < v.lo:
                    new = 1
                elif rsi[i] > v.hi and v.short:
                    new = -1
            if new:
                usd = cash * base.POS_FRACTION
                if usd >= base.MIN_TRADE:
                    c = cost(usd, new == -1)
                    qty = usd / px
                    cash += (-usd if new == 1 else usd) - c
                    fees += c
                    side, entry, held, realized = new, px, 0, -c
                    stop = px - new * v.stop_k * a
        eq[i] = cash + side * qty * px
    return pd.Series(eq, index=h.index), trades, fees


def run(data, v, start, end):
    """Devuelve métricas de: tendencia sola, rango solo y combinada."""
    eq_t, eq_r, tr_t, tr_r, f_t, f_r = [], [], [], [], 0.0, 0.0
    for sym, w in WEIGHTS.items():
        h, d = data[sym]
        hh = h[(h.index >= start) & (h.index < end)]
        cap = base.CAPITAL * w
        e, s = m11c.simulate(hh, d, m11c.VARIANTS[0], cap, COST[sym], APR[sym])
        eq_t.append(e); tr_t += s.trades; f_t += s.fees
        if v is not None:
            e2, tr, f = simulate_range(hh, d, v, cap, COST[sym], APR[sym])
            eq_r.append(e2); tr_r += tr; f_r += f
    et = pd.concat(eq_t, axis=1).ffill().bfill().sum(axis=1)
    out = {"trend": base.metrics(et, tr_t, f_t)}
    if v is not None:
        er = pd.concat(eq_r, axis=1).ffill().bfill().sum(axis=1)
        out["range"] = base.metrics(er, tr_r, f_r)
        out["comb"] = base.metrics(et + (er - base.CAPITAL), tr_t + tr_r, f_t + f_r)
    return out


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

    lines = ["# Investigación nov-2026 (5ª parte) — estrategia de rango para cuando no hay tendencia",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             "Cartera v5 (BTC 42.5 / ETH 42.5 / ARB 15 %). La de rango solo opera con la señal de tendencia en 0.",
             "Costes 0.10 %/lado + gas; cortos +0.10 %; interés cortos BTC/ETH 4 %/año, ARB 7 %/año."]
    for sym in WEIGHTS:
        d = data[sym][1]
        s = trend_ema.signals(d, fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True)
        s = s[s.index >= start]
        lines.append(f"{sym}: días sin tendencia (señal 0) {float((s == 0).mean()):.0%}")
    lines.append("")
    rows = []
    for lab, s0, s1 in periods:
        lines += [f"## {lab} ({s0.date()} -> {min(s1, t1).date()})", base.HEADER]
        first = True
        for v in VARIANTS:
            r = run(data, v, s0, s1)
            if first:
                t = dict(r["trend"], variant="ACTUAL (solo tendencia)")
                lines.append(base.fmt(t))
                rows.append(t | {"period": lab, "tipo": "actual"})
                first = False
            for tipo, pre in (("range", "solo rango: "), ("comb", "COMBINADA + ")):
                m = dict(r[tipo], variant=(pre + v.name)[:36])
                lines.append(base.fmt(m))
                rows.append(m | {"period": lab, "tipo": tipo, "var": v.name})
        lines.append("")

    df = pd.DataFrame(rows)
    act = df[df.tipo == "actual"].set_index("period")
    comb = df[df.tipo == "comb"]
    res = []
    for name, g in comb.groupby("var", sort=False):
        g = g.set_index("period")
        full = g.loc["PERIODO COMPLETO"]
        halves = all(g.loc[p, "ret"] > act.loc[p, "ret"] for p in ("1ª MITAD", "2ª MITAD"))
        thirds = sum(g.loc[p, "ret"] > act.loc[p, "ret"] for p in g.index if p.startswith("TERCIO"))
        res.append(dict(variante=name, ret=full.ret, max_dd=full.max_dd, neg_months=full.neg_months, sharpe=full.sharpe,
                        gana_ambas_mitades=halves, gana_tercios=int(thirds)))
    R = pd.DataFrame(res).set_index("variante")
    b = act.loc["PERIODO COMPLETO"]
    lines += ["## RESUMEN — cartera COMBINADA (tendencia + rango) frente a la actual",
              f"ACTUAL: retorno {b.ret:+.1%}, caída {b.max_dd:+.1%}, meses en rojo {int(b.neg_months)}, Sharpe {b.sharpe:.2f}",
              R.round(3).to_string(), ""]
    ok = R[R.gana_ambas_mitades & (R.gana_tercios >= 2) & (R.max_dd >= b.max_dd - 0.02) & (R.neg_months <= b.neg_months + 1)]
    if len(ok):
        best = ok.sort_values("sharpe", ascending=False).iloc[0]
        lines.append(f"CANDIDATA: {best.name}: {best.ret:+.1%}, caída {best.max_dd:+.1%}, {int(best.neg_months)} meses en rojo, "
                     f"Sharpe {best.sharpe:.2f}. Gana en ambas mitades y en {best.gana_tercios} de 3 tercios sin más caída.")
    else:
        lines.append("Ninguna variante de rango mejora a la cartera actual de forma consistente.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_11e{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_11e{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
