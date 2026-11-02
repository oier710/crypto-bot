"""Investigación nov-2026 (2ª parte): usar la VOLATILIDAD para decidir cuánto y cuándo entrar.

Idea de Oier (2026-11-02): "anticipar la volatilidad y hacer entradas y salidas en ese sentido". La volatilidad
se agrupa en rachas (es predecible en parte), la dirección no. Así que no se intenta adivinar hacia dónde va el
precio: la señal sigue siendo la tendencia diaria actual (EMA 20/60 + ADX), y la volatilidad decide:
  V1  TAMAÑO según volatilidad: tamaño = 75 % x (volatilidad normal / volatilidad actual), entre 0.5x y 1.33x
      (máximo el 100 % de su parte). Mercado tranquilo -> más grande; agitado -> más pequeño.
  V1b igual pero solo reduce (0.5x-1.0x): nunca más grande que ahora.
  V2  ENTRAR TRAS LA CALMA: solo abre si en los últimos 10 días hubo compresión (anchura de Bollinger en el
      25 % más bajo de los últimos 120 días). La señal sigue mandando; esto solo filtra la entrada.
  V3  NO ENTRAR EN PLENA TORMENTA: no abre si la volatilidad (ATR %) está por encima de 1.5x su mediana de 120
      días; espera a que se calme (si la señal sigue, entra después).
  V4  V1 + V3        V5  V1b + V3        V6  V1 + V2
"Volatilidad normal" = mediana del ATR % de los últimos 365 días. Todo con datos del día anterior (sin mirar
el futuro). Cierres, stops, parciales y reentradas idénticos a la v3 en producción.
Cartera v5: BTC 42.5 % / ETH 42.5 % / ARB 15 %. Costes 0.10 %/lado + gas; cortos +0.10 %; interés cortos
BTC/ETH 4 %/año, ARB 7 %/año.
Criterio fijado antes: solo se propone cambio si gana en ambas mitades y en >= 2 de 3 tercios sin más caída.
Salida: results/research_2026_11b.txt y .csv
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

STOP_ATR, MIN_TRADE = base.STOP_ATR, base.MIN_TRADE
PT, PB_ATR, RESTOP_K = (1.5, 3.0), 1.0, 1.5
COST = {"BTC": 0.0010, "ETH": 0.0010, "ARB": 0.0010}
APR = {"BTC": 0.04, "ETH": 0.04, "ARB": 0.07}
WEIGHTS = {"BTC": 0.425, "ETH": 0.425, "ARB": 0.15}


@dataclass(frozen=True)
class Var:
    name: str
    size: str = ""          # "", "vt" (0.5-1.33x), "vt_down" (0.5-1.0x)
    squeeze: bool = False   # entrar solo tras compresión reciente
    storm: bool = False     # no entrar con volatilidad > 1.5x lo normal


VARIANTS = [
    Var("S0 actual"),
    Var("V1 tamaño según volatilidad", size="vt"),
    Var("V1b tamaño (solo reduce)", size="vt_down"),
    Var("V2 entrar tras la calma", squeeze=True),
    Var("V3 no entrar en tormenta", storm=True),
    Var("V4 V1 + V3", size="vt", storm=True),
    Var("V5 V1b + V3", size="vt_down", storm=True),
    Var("V6 V1 + V2", size="vt", squeeze=True),
]


def vol_features(d: pd.DataFrame) -> pd.DataFrame:
    a = atr(d, 14)
    atr_pct = a / d.close
    sma = d.close.rolling(20).mean()
    bbw = 4 * d.close.rolling(20).std() / sma
    pct = bbw.rolling(120, min_periods=60).apply(lambda x: (x[-1] >= x).mean(), raw=True)
    f = pd.DataFrame({
        "atr": a,
        "atr_pct": atr_pct,
        "normal": atr_pct.rolling(365, min_periods=120).median(),
        "med120": atr_pct.rolling(120, min_periods=60).median(),
        "squeeze_recent": pct.rolling(10, min_periods=1).min() <= 0.25,
    })
    return f.shift(1)          # datos del día anterior


def simulate(h: pd.DataFrame, d: pd.DataFrame, v: Var, capital: float, cost_pct: float, apr: float):
    sig_d = trend_ema.signals(d, fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True).shift(1)
    vf = vol_features(d)
    s = base.Sleeve(cash=capital)
    s.ref, s.pb = 0.0, 0.0
    eq = np.empty(len(h))
    closes, opens = h["close"].values, h["open"].values
    last_day, cur_sig = None, 0
    hourly_rate = apr / (365 * 24)

    def swap_cost(usd, short):
        return abs(usd) * (cost_pct + (base.SHORT_EXTRA_PCT if short else 0.0)) + base.GAS_USD

    def mtm(px):
        return s.cash + s.side * s.qty * px if s.side else s.cash

    def close_part(px, frac, kind, t):
        q = s.qty * frac
        c = swap_cost(q * px, s.side == -1)
        s.cash += (q * px - c) if s.side == 1 else (-(q * px) - c)
        s.realized_trade += s.side * q * (px - s.entry) - c
        s.qty -= q
        s.fees += c
        if s.qty <= 1e-12 or frac >= 0.999:
            s.trades.append((t, kind, s.realized_trade))
            s.side, s.qty, s.full_qty, s.pt_done, s.realized_trade = 0, 0.0, 0.0, 0, 0.0

    def size_mult(row):
        if not v.size or np.isnan(row.normal) or np.isnan(row.atr_pct) or row.atr_pct <= 0:
            return 1.0
        m = row.normal / row.atr_pct
        return float(np.clip(m, 0.5, 1.0 if v.size == "vt_down" else 4 / 3))

    def entry_ok(row):
        if v.storm and not np.isnan(row.med120) and row.atr_pct > 1.5 * row.med120:
            return False
        if v.squeeze and not bool(row.squeeze_recent):
            return False
        return True

    def open_pos(px, side, row, t):
        a = row.atr
        frac = min(base.POS_FRACTION * size_mult(row), 1.0)
        usd = mtm(px) * frac
        if usd < MIN_TRADE or np.isnan(a):
            return
        c = swap_cost(usd, side == -1)
        q = usd / px
        s.cash += (-usd if side == 1 else usd) - c
        s.side, s.qty, s.full_qty, s.entry = side, q, q, px
        s.atr_e, s.extreme, s.pt_done, s.realized_trade = a, px, 0, -c
        s.stop = px - side * STOP_ATR * a
        s.ref, s.pb = px, 0.0
        s.fees += c

    def rebuy(px, a):
        q = s.full_qty - s.qty
        if q * px < MIN_TRADE:
            return
        c = swap_cost(q * px, s.side == -1)
        s.cash += (-(q * px) if s.side == 1 else q * px) - c
        s.entry = (s.entry * s.qty + px * q) / (s.qty + q)
        s.qty += q
        s.fees += c
        s.realized_trade -= c
        s.pt_done, s.ref, s.pb = 0, px, 0.0
        if not np.isnan(a):
            s.atr_e = a
            ns = px - s.side * RESTOP_K * a
            s.stop = max(s.stop, ns) if s.side == 1 else min(s.stop, ns)

    row = None
    for i, t in enumerate(h.index):
        day = t.normalize()
        px = closes[i]
        if day != last_day:
            last_day = day
            sig = sig_d.get(day, np.nan)
            row = vf.loc[day] if day in vf.index else None
            cur_sig = 0 if np.isnan(sig) else int(sig)
            if not np.isnan(sig) and row is not None:
                o = opens[i]
                if s.side and cur_sig != s.side:
                    close_part(o, 1.0, "señal", t)
                if cur_sig != 0 and s.side == 0 and s.stopped_day != day and entry_ok(row):
                    open_pos(o, cur_sig, row, t)
        if s.side == -1:
            c = s.qty * px * hourly_rate
            s.cash -= c
            s.fees += c
        if s.side:
            side = s.side
            new_high = side * (px - s.extreme) > 0
            partial_out = s.qty < s.full_qty * 0.99
            if partial_out and not new_high and s.atr_e:
                s.pb = max(s.pb, side * (s.extreme - px) / s.atr_e)
            s.extreme = max(s.extreme, px) if side == 1 else min(s.extreme, px)
            fav = side * (px - s.ref) / s.atr_e if s.atr_e else 0.0
            be_px = s.entry * (1 + side * 2 * cost_pct)
            if s.pt_done < len(PT) and fav >= PT[s.pt_done]:
                close_part(px, (1 / 3) / (s.qty / s.full_qty), f"parcial{s.pt_done + 1}", t)
                if s.side:
                    s.pt_done += 1
                    s.pb = 0.0
                    s.stop = max(s.stop, be_px) if side == 1 else min(s.stop, be_px)
            if s.side and ((side == 1 and px <= s.stop) or (side == -1 and px >= s.stop)):
                close_part(px, 1.0, "stop", t)
                s.stopped_day = day
            elif s.side and partial_out and new_high and s.pb >= PB_ATR and cur_sig == side:
                rebuy(px, row.atr if row is not None else np.nan)
        eq[i] = mtm(px)
    return pd.Series(eq, index=h.index), s


def portfolio(data, v, start, end):
    eqs, trades, fees, per = [], [], 0.0, {}
    for sym, w in WEIGHTS.items():
        h, d = data[sym]
        hh = h[(h.index >= start) & (h.index < end)]
        e, s = simulate(hh, d, v, base.CAPITAL * w, COST[sym], APR[sym])
        eqs.append(e)
        trades += s.trades
        fees += s.fees
        per[sym] = float(e.iloc[-1] - e.iloc[0])
    eq = pd.concat(eqs, axis=1).ffill().bfill().sum(axis=1)
    r = base.metrics(eq, trades, fees)
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
    start = t0 + pd.Timedelta(days=150)        # calentamiento de los indicadores de 120 días
    end = t1 + pd.Timedelta(hours=1)
    mid = start + (t1 - start) / 2
    th = [start + (t1 - start) * k / 3 for k in range(4)]
    th[-1] = end
    periods = [("PERIODO COMPLETO", start, end), ("1ª MITAD", start, mid), ("2ª MITAD", mid, end),
               ("TERCIO 1", th[0], th[1]), ("TERCIO 2 (lateral)", th[1], th[2]), ("TERCIO 3", th[2], th[3])]

    lines = ["# Investigación nov-2026 (2ª parte) — volatilidad para decidir cuánto y cuándo entrar (idea de Oier)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             "Cartera v5 (BTC 42.5 / ETH 42.5 / ARB 15 %), señal y gestión v3 idénticas; solo cambia tamaño o filtro de entrada.",
             "Costes 0.10 %/lado + gas; cortos +0.10 %; interés cortos BTC/ETH 4 %/año, ARB 7 %/año.",
             "Columnas: m- = meses en negativo / meses; BTC$/ETH$/ARB$ = lo que aporta cada moneda.", ""]
    rows = []
    for lab, s0, s1 in periods:
        lines += [f"## {lab} ({s0.date()} -> {min(s1, t1).date()})",
                  base.HEADER + f" {'BTC$':>7} {'ETH$':>7} {'ARB$':>7}"]
        for v in VARIANTS:
            r = portfolio(data, v, s0, s1)
            r["variant"] = v.name
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
    safer = full[(full.index != b.name) & (full.max_dd > b.max_dd + 0.02) & (full.ret >= b.ret * 0.9)
                 & (full.neg_months <= b.neg_months)]
    if len(better):
        best = better.sort_values("sharpe", ascending=False).iloc[0]
        lines.append(f"CANDIDATA (más rentable y consistente): {best.name}: {best.ret:+.1%}, caída {best.max_dd:+.1%}, "
                     f"{int(best.neg_months)} meses en rojo, Sharpe {best.sharpe:.2f} "
                     f"(actual: {b.ret:+.1%}, {b.max_dd:+.1%}, {int(b.neg_months)}, {b.sharpe:.2f}).")
    if len(safer):
        best = safer.sort_values("max_dd", ascending=False).iloc[0]
        lines.append(f"CANDIDATA (más segura, >= 90 % de la rentabilidad): {best.name}: {best.ret:+.1%}, caída "
                     f"{best.max_dd:+.1%}, {int(best.neg_months)} meses en rojo (actual: {b.ret:+.1%}, {b.max_dd:+.1%}).")
    if not len(better) and not len(safer):
        lines.append("Ninguna variante mejora a la actual de forma consistente: se mantiene el sistema actual.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_11b{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_11b{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
