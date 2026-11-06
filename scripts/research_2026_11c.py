"""Investigación nov-2026 (3ª parte): ¿comprobar la señal diaria CADA HORA en vez de una vez al día?

Idea de Oier (2026-11-05): "el momentum se pierde si la revisión es con mucho tiempo de distancia; ¿se puede
reducir para pillar una bajada agresiva, hacer un corto o lo contrario?".
Hoy la señal (EMA 20/60 + ADX > 20, diaria) se calcula una vez al día (~00:05 UTC); stops y parciales ya van
cada 15 min. Aquí la MISMA señal diaria se recalcula cada hora con el precio actual como cierre provisional del
día (vela del día en curso: apertura, máximo y mínimo hasta esa hora, cierre = precio actual), así que si la
tendencia gira a media mañana se actúa entonces y no al día siguiente. NO es la señal de 4 h de research_2026_10c.
  A  actual: decide una vez al día (apertura UTC con la señal del cierre anterior)
  B  cada hora: actúa en cuanto la señal provisional cambia (abrir, cerrar o girar)
  C  cada hora con confirmación: la señal provisional nueva tiene que mantenerse 4 horas seguidas
  D  cada hora solo para SALIR antes (si la señal provisional deja de acompañar, cierra); entradas como A
  E  cada hora, confirmación de 4 h solo para entrar; para salir, en cuanto cambia
Gestión v3 idéntica (stop 2 ATR, parciales 1.5/3 ATR, stop a entrada, reentrada). Cartera v5 (BTC 42.5 / ETH 42.5 /
ARB 15 %), 75 % por posición, costes reales (0.10 %/lado + gas; cortos +0.10 %; interés BTC/ETH 4 %, ARB 7 %).
Criterio fijado antes: solo se propone si gana en ambas mitades y en >= 2 de 3 tercios sin más caída.
Salida: results/research_2026_11c.txt y .csv
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
from bot.indicators import adx, atr, ema  # noqa: E402
from bot.strategies import trend_ema  # noqa: E402

STOP_ATR, MIN_TRADE = base.STOP_ATR, base.MIN_TRADE
PT, PB_ATR, RESTOP_K = (1.5, 3.0), 1.0, 1.5
COST = {"BTC": 0.0010, "ETH": 0.0010, "ARB": 0.0010}
APR = {"BTC": 0.04, "ETH": 0.04, "ARB": 0.07}
WEIGHTS = {"BTC": 0.425, "ETH": 0.425, "ARB": 0.15}
FAST, SLOW, N, ADX_MIN = 20, 60, 14, 20


@dataclass(frozen=True)
class Var:
    name: str
    hourly_entry: bool = False
    hourly_exit: bool = False
    confirm_entry: int = 1      # horas seguidas que la señal provisional tiene que mantenerse para entrar
    confirm_exit: int = 1       # ... para salir


VARIANTS = [
    Var("A actual (1 vez al día)"),
    Var("B cada hora", hourly_entry=True, hourly_exit=True),
    Var("C cada hora + confirmación 4 h", hourly_entry=True, hourly_exit=True, confirm_entry=4, confirm_exit=4),
    Var("D cada hora solo para salir", hourly_exit=True),
    Var("E cada hora: entrar con 4 h, salir al momento", hourly_entry=True, hourly_exit=True, confirm_entry=4),
]


def provisional_signal(h: pd.DataFrame, d: pd.DataFrame) -> np.ndarray:
    """Señal diaria recalculada en cada hora con la vela del día en curso (sin mirar el futuro)."""
    c_d, h_d, l_d = d.close, d.high, d.low
    ef, es = ema(c_d, FAST), ema(c_d, SLOW)
    pc = c_d.shift(1)
    tr = pd.concat([h_d - l_d, (h_d - pc).abs(), (l_d - pc).abs()], axis=1).max(axis=1)
    atr_s = tr.ewm(alpha=1 / N, adjust=False).mean()
    up, dn = h_d.diff(), -l_d.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=d.index).ewm(alpha=1 / N, adjust=False).mean()
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=d.index).ewm(alpha=1 / N, adjust=False).mean()
    pdi, mdi = 100 * pdm / atr_s, 100 * mdm / atr_s
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx_s = dx.ewm(alpha=1 / N, adjust=False).mean()
    prev = pd.DataFrame({"ef": ef, "es": es, "atr": atr_s, "pdm": pdm, "mdm": mdm, "adx": adx_s,
                         "c": c_d, "h": h_d, "l": l_d}).shift(1)          # estado al cierre del día anterior
    day = h.index.normalize()
    pv = prev.reindex(day).to_numpy()
    g = pd.Series(range(len(h)), index=h.index).groupby(day)
    hh = h["high"].groupby(day).cummax().to_numpy()
    ll = h["low"].groupby(day).cummin().to_numpy()
    cc = h["close"].to_numpy()
    ef_p, es_p, atr_p, pdm_p, mdm_p, adx_p, c_p, h_p, l_p = (pv[:, i] for i in range(9))
    a_f, a_s, a_w = 2 / (FAST + 1), 2 / (SLOW + 1), 1 / N
    ef1 = ef_p + a_f * (cc - ef_p)
    es1 = es_p + a_s * (cc - es_p)
    tr1 = np.maximum.reduce([hh - ll, np.abs(hh - c_p), np.abs(ll - c_p)])
    atr1 = atr_p + a_w * (tr1 - atr_p)
    up1, dn1 = hh - h_p, l_p - ll
    pdm1 = pdm_p + a_w * (np.where((up1 > dn1) & (up1 > 0), up1, 0.0) - pdm_p)
    mdm1 = mdm_p + a_w * (np.where((dn1 > up1) & (dn1 > 0), dn1, 0.0) - mdm_p)
    pdi1, mdi1 = 100 * pdm1 / atr1, 100 * mdm1 / atr1
    with np.errstate(invalid="ignore", divide="ignore"):
        dx1 = 100 * np.abs(pdi1 - mdi1) / (pdi1 + mdi1)
    adx1 = adx_p + a_w * (dx1 - adx_p)
    sig = np.zeros(len(h), dtype=int)
    sig[(ef1 > es1) & (adx1 > ADX_MIN)] = 1
    sig[(ef1 < es1) & (adx1 > ADX_MIN)] = -1
    sig[np.isnan(adx1) | np.isnan(es1)] = 0
    del g
    return sig


def simulate(h, d, v: Var, capital, cost_pct, apr):
    sig_d = trend_ema.signals(d, fast=FAST, slow=SLOW, adx_n=N, adx_min=ADX_MIN, allow_short=True).shift(1)
    day_atr = atr(d, N).shift(1)
    sig_h = provisional_signal(h, d) if (v.hourly_entry or v.hourly_exit) else None
    s = base.Sleeve(cash=capital)
    s.ref, s.pb = 0.0, 0.0
    eq = np.empty(len(h))
    closes, opens = h["close"].values, h["open"].values
    last_day, cur_sig = None, 0
    run_sig, run_len = 0, 0
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

    def open_pos(px, side, a, t):
        usd = mtm(px) * base.POS_FRACTION
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

    for i, t in enumerate(h.index):
        day = t.normalize()
        px = closes[i]
        a_day = day_atr.get(day, np.nan)
        # --- decisión diaria (como ahora) ---
        if day != last_day:
            last_day = day
            sig = sig_d.get(day, np.nan)
            if not np.isnan(sig):
                cur_sig = int(sig)
                o = opens[i]
                if not v.hourly_exit and s.side and cur_sig != s.side:
                    close_part(o, 1.0, "señal", t)
                if not v.hourly_entry and cur_sig != 0 and s.side == 0 and s.stopped_day != day:
                    open_pos(o, cur_sig, a_day, t)
        # --- decisión horaria con la señal provisional (al cierre de la hora) ---
        if sig_h is not None:
            ps = int(sig_h[i])
            run_len = run_len + 1 if ps == run_sig else 1
            run_sig = ps
            if v.hourly_exit and s.side and ps != s.side and run_len >= v.confirm_exit:
                close_part(px, 1.0, "señal horaria", t)
            if v.hourly_entry and ps != 0 and s.side == 0 and run_len >= v.confirm_entry and s.stopped_day != day:
                open_pos(px, ps, a_day, t)
            if v.hourly_entry or v.hourly_exit:
                cur_sig = ps
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
                rebuy(px, a_day)
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
    start = t0 + pd.Timedelta(days=90)
    end = t1 + pd.Timedelta(hours=1)
    mid = start + (t1 - start) / 2
    th = [start + (t1 - start) * k / 3 for k in range(4)]
    th[-1] = end
    periods = [("PERIODO COMPLETO", start, end), ("1ª MITAD", start, mid), ("2ª MITAD", mid, end),
               ("TERCIO 1", th[0], th[1]), ("TERCIO 2 (lateral)", th[1], th[2]), ("TERCIO 3", th[2], th[3])]

    # comprobación: la señal provisional de la ÚLTIMA hora de cada día debe coincidir con la señal diaria
    h0, d0 = data["BTC"]
    ps = pd.Series(provisional_signal(h0, d0), index=h0.index)
    last_of_day = ps.groupby(ps.index.normalize()).last()
    sd = trend_ema.signals(d0, fast=FAST, slow=SLOW, adx_n=N, adx_min=ADX_MIN, allow_short=True)
    both = pd.concat([last_of_day, sd], axis=1, join="inner").dropna().iloc[100:]
    match = float((both.iloc[:, 0] == both.iloc[:, 1]).mean())

    lines = ["# Investigación nov-2026 (3ª parte) — ¿comprobar la señal diaria cada hora? (idea de Oier)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             "Cartera v5 (BTC 42.5 / ETH 42.5 / ARB 15 %), gestión v3 idéntica; solo cambia CUÁNDO se mira la señal.",
             "Costes 0.10 %/lado + gas; cortos +0.10 %; interés cortos BTC/ETH 4 %/año, ARB 7 %/año.",
             f"Comprobación del cálculo: la señal provisional de la última hora del día coincide con la diaria en {match:.1%} de los días.",
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
    if len(better):
        best = better.sort_values("sharpe", ascending=False).iloc[0]
        lines.append(f"CANDIDATA: {best.name}: {best.ret:+.1%}, caída {best.max_dd:+.1%}, {int(best.neg_months)} meses en rojo, "
                     f"Sharpe {best.sharpe:.2f} (actual: {b.ret:+.1%}, {b.max_dd:+.1%}, {int(b.neg_months)}, {b.sharpe:.2f}).")
    else:
        lines.append("Ninguna variante gana a la actual de forma consistente: se mantiene la decisión una vez al día.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_11c{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_11c{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
