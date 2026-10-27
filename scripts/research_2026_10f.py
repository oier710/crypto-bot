"""Investigación oct-2026 (6ª parte): ¿PARCIALES MÁS CERCANAS para ir más seguros?

Pregunta de Oier (2026-10-27): "los rangos de coger beneficios progresivamente, ¿podrían ser
menores, para ser un poco más seguros?".

Se compara la gestión v3 en real (parciales a 1.5 y 3 ATR, 1/3 cada una, stop a la entrada tras
la primera parcial, reentrada en máximo nuevo con corrección 1 ATR y stop nuevo 1.5 ATR) con
variantes que cogen beneficio antes, con más tramos, o que suben el stop a la entrada más tarde
(para que el ruido no saque de la operación a cero).

Mismo simulador, datos y costes que research_2026_10 / _10b. Salida: results/research_2026_10f.txt y .csv
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
from bot.indicators import atr  # noqa: E402
from bot.strategies import trend_ema  # noqa: E402

SWAP_PCT, STOP_ATR, MIN_TRADE = base.SWAP_PCT, base.STOP_ATR, base.MIN_TRADE


@dataclass(frozen=True)
class V3:
    name: str
    pt: tuple = (1.5, 3.0)      # niveles de parcial en ATR
    frac: float = 1 / 3         # fracción de la posición completa que se vende en cada parcial
    be_after: int = 1           # tras cuántas parciales sube el stop a la entrada (0 = nunca)
    pb_atr: float = 1.0         # reentrada: corrección mínima (ATR) antes de aceptar el máximo nuevo
    restop_k: float = 1.5       # reentrada: stop nuevo de toda la posición a k*ATR del precio


VARIANTS = [
    V3("v3 REAL: PT 1.5/3, 1/3, BE tras 1ª"),
    V3("PT 1.0/2.0, 1/3, BE tras 1ª", pt=(1.0, 2.0)),
    V3("PT 1.0/2.0, 1/3, BE tras 2ª", pt=(1.0, 2.0), be_after=2),
    V3("PT 1.25/2.5, 1/3, BE tras 1ª", pt=(1.25, 2.5)),
    V3("PT 1.0/2.0/3.0, 1/4, BE tras 1ª", pt=(1.0, 2.0, 3.0), frac=0.25),
    V3("PT 1.0/2.0/3.0, 1/4, BE tras 2ª", pt=(1.0, 2.0, 3.0), frac=0.25, be_after=2),
    V3("PT 0.75/1.5/3.0, 1/4, BE tras 2ª", pt=(0.75, 1.5, 3.0), frac=0.25, be_after=2),
    V3("PT 1.5/3, 1/3, BE tras 2ª", be_after=2),
    V3("PT 1.5/3, 1/3, BE nunca", be_after=0),
    V3("PT 2.0/4.0, 1/3, BE tras 1ª", pt=(2.0, 4.0)),
]


def simulate(h: pd.DataFrame, d: pd.DataFrame, v: V3, capital: float):
    sig_d = trend_ema.signals(d, fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True)
    day_sig = sig_d.shift(1)
    day_atr = atr(d, 14).shift(1)

    s = base.Sleeve(cash=capital)
    s.ref, s.pb, s.rebuys = 0.0, 0.0, 0
    eq = np.empty(len(h))
    closes, opens = h["close"].values, h["open"].values
    last_day = None
    cur_sig = 0
    hourly_short_rate = base.SHORT_APR / (365 * 24)
    swap_cost = base.swap_cost

    def mtm(px):
        return s.cash + s.side * s.qty * px if s.side else s.cash

    def close_part(px, frac, kind, t):
        q = s.qty * frac
        cost = swap_cost(q * px, s.side == -1)
        pnl = s.side * q * (px - s.entry) - cost
        s.cash += (q * px - cost) if s.side == 1 else (-(q * px) - cost)
        s.qty -= q
        s.fees += cost
        s.realized_trade += pnl
        if s.qty <= 1e-12 or frac >= 0.999:
            s.trades.append((t, kind, s.realized_trade))
            s.side, s.qty, s.full_qty, s.pt_done, s.realized_trade = 0, 0.0, 0.0, 0, 0.0

    def open_pos(px, side, a, t):
        usd = mtm(px) * base.POS_FRACTION
        if usd < MIN_TRADE or np.isnan(a):
            return
        cost = swap_cost(usd, side == -1)
        q = usd / px
        s.cash += (-usd if side == 1 else usd) - cost
        s.side, s.qty, s.full_qty, s.entry = side, q, q, px
        s.atr_e, s.extreme, s.pt_done, s.realized_trade = a, px, 0, -cost
        s.stop = px - side * STOP_ATR * a
        s.ref, s.pb = px, 0.0
        s.fees += cost

    def rebuy(px, a):
        q = s.full_qty - s.qty
        if q * px < MIN_TRADE:
            return False
        cost = swap_cost(q * px, s.side == -1)
        s.cash += (-(q * px) if s.side == 1 else q * px) - cost
        s.entry = (s.entry * s.qty + px * q) / (s.qty + q)
        s.qty += q
        s.fees += cost
        s.realized_trade -= cost
        s.pt_done, s.ref, s.pb = 0, px, 0.0
        if not np.isnan(a):
            s.atr_e = a
            new_stop = px - s.side * v.restop_k * a
            s.stop = max(s.stop, new_stop) if s.side == 1 else min(s.stop, new_stop)
        s.rebuys += 1
        return True

    for i, t in enumerate(h.index):
        day = t.normalize()
        px = closes[i]
        if day != last_day:
            last_day = day
            sig = day_sig.get(day, np.nan)
            a = day_atr.get(day, np.nan)
            cur_sig = 0 if np.isnan(sig) else int(sig)
            if not np.isnan(sig):
                o = opens[i]
                if s.side and cur_sig != s.side:
                    close_part(o, 1.0, "señal", t)
                if cur_sig != 0 and s.side == 0 and s.stopped_day != day:
                    open_pos(o, cur_sig, a, t)
        if s.side == -1:
            c = s.qty * px * hourly_short_rate
            s.cash -= c
            s.fees += c
        if s.side:
            side = s.side
            a_now = day_atr.get(day, np.nan)
            new_high = side * (px - s.extreme) > 0
            partial_out = s.qty < s.full_qty * 0.99
            if partial_out and not new_high and s.atr_e:
                s.pb = max(s.pb, side * (s.extreme - px) / s.atr_e)
            s.extreme = max(s.extreme, px) if side == 1 else min(s.extreme, px)
            fav = side * (px - s.ref) / s.atr_e if s.atr_e else 0.0
            be_px = s.entry * (1 + side * 2 * SWAP_PCT)
            # parciales: cada una vende `frac` de la posición completa
            if s.pt_done < len(v.pt) and fav >= v.pt[s.pt_done]:
                close_part(px, min(1.0, v.frac / (s.qty / s.full_qty)), f"parcial{s.pt_done + 1}", t)
                if s.side:
                    s.pt_done += 1
                    s.pb = 0.0
                    if v.be_after and s.pt_done >= v.be_after:
                        s.stop = max(s.stop, be_px) if side == 1 else min(s.stop, be_px)
            # stop primero
            if s.side and ((side == 1 and px <= s.stop) or (side == -1 and px >= s.stop)):
                close_part(px, 1.0, "stop", t)
                s.stopped_day = day
            # reentrada en máximo nuevo tras corrección
            elif (s.side and partial_out and new_high and s.pb >= v.pb_atr and cur_sig == side):
                rebuy(px, a_now)
        eq[i] = mtm(px)
    return pd.Series(eq, index=h.index), s


def run_portfolio(data, v, start, end):
    eqs, trades, fees, rebuys = [], [], 0.0, 0
    for sym, (h, d) in data.items():
        hh = h[(h.index >= start) & (h.index < end)]
        e, s = simulate(hh, d, v, base.CAPITAL / len(data))
        eqs.append(e)
        trades += s.trades
        fees += s.fees
        rebuys += s.rebuys
    eq = pd.concat(eqs, axis=1).ffill().sum(axis=1)
    out = base.metrics(eq, trades, fees)
    out["variant"] = v.name
    out["rebuys"] = rebuys
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    data = {}
    for sym in base.SYMS:
        h = base.load_hourly(sym, a.synthetic)
        data[sym] = (h, base.daily_from_hourly(h))
    t0, t1 = data["BTC"][0].index[0], data["BTC"][0].index[-1]
    start = t0 + pd.Timedelta(days=90)
    end = t1 + pd.Timedelta(hours=1)
    mid = start + (t1 - start) / 2
    thirds = [start + (t1 - start) * k / 3 for k in range(4)]
    thirds[-1] = end

    lines = ["# Investigación oct-2026 (6ª parte) — ¿parciales más cercanas para ir más seguros? (pregunta de Oier)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, {t0.date()} -> {t1.date()} "
             f"(evaluación desde {start.date()}). Capital {base.CAPITAL} $, BTC+ETH a medias, 75 % por posición.",
             f"Costes: {SWAP_PCT:.2%}/lado + {base.GAS_USD} $ gas; cortos +{base.SHORT_EXTRA_PCT:.2%} y "
             f"{base.SHORT_APR:.0%}/año. Todas con reentrada en máximo nuevo (corr 1 ATR, stop nuevo 1.5 ATR) como la v3 real.",
             "PT = niveles de parcial en ATR; 1/3 o 1/4 = parte de la posición que se vende en cada parcial; "
             "BE = cuándo sube el stop a la entrada.",
             "Columnas: m- = meses en negativo / meses; gana$/pierde$ = resultado medio por operación; rec = recompras.",
             ""]
    periods = [("PERIODO COMPLETO", start, end), ("1ª MITAD", start, mid), ("2ª MITAD", mid, end),
               ("TERCIO 1", thirds[0], thirds[1]), ("TERCIO 2", thirds[1], thirds[2]), ("TERCIO 3", thirds[2], thirds[3])]
    rows = []
    for label, s0, s1 in periods:
        lines += [f"## {label} ({s0.date()} -> {min(s1, t1).date()})", base.HEADER + f" {'rec':>4}"]
        for v in VARIANTS:
            r = run_portfolio(data, v, s0, s1)
            lines.append(base.fmt(r) + f" {r['rebuys']:>4}")
            rows.append(r | {"period": label})
        lines.append("")

    df = pd.DataFrame(rows)
    full = df[df.period == "PERIODO COMPLETO"].set_index("variant")
    b = full.iloc[0]
    full["score"] = (full.max_dd - b.max_dd) * 100 - (full.neg_months - b.neg_months) * 1.0 + (full.cagr - b.cagr) * 50
    sub = df[df.period.str.startswith("TERCIO")].pivot_table(index="variant", columns="period", values="ret")
    full["tercios_positivos"] = (sub > 0).sum(axis=1).reindex(full.index)
    full["gana_a_v3_en_tercios"] = [int((sub.loc[n] > sub.loc[b.name]).sum()) for n in full.index]
    halves = df[df.period.isin(["1ª MITAD", "2ª MITAD"])].pivot_table(index="variant", columns="period", values="ret")
    full["gana_a_v3_en_ambas_mitades"] = [bool((halves.loc[n] > halves.loc[b.name]).all()) for n in full.index]
    lines += ["## RANKING (consistencia: caída máx., meses en rojo y rentabilidad, frente a la v3 real)",
              full.sort_values("score", ascending=False)[
                  ["ret", "max_dd", "neg_months", "worst_month", "sharpe", "trades", "win_rate", "rebuys", "fees_usd",
                   "tercios_positivos", "gana_a_v3_en_tercios", "gana_a_v3_en_ambas_mitades", "score"]]
              .round(3).to_string(), ""]
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_10f{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_10f{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
