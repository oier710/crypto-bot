"""Investigación nov-2026 (10ª parte): INDICADORES DE GIRO (idea de Oier, 2026-11-23).

Idea: "usar otros indicadores para detectar la reversión de la tendencia y poder cerrar el corto y abrir un largo
antes de que salte el stop loss del corto". Hoy la posición solo se cierra por stop (2 ATR, o el que persigue) o
cuando cambia la señal lenta (EMA 20/60 + ADX), que llega tarde.

Indicadores de giro, en diario, calculados con el cierre del día anterior (sin mirar el futuro). Fijados ANTES de ver
resultados con Oier:
  R1 EMA 9/21: la rápida cruza en contra de la posición.
  R2 divergencia RSI(14): el precio marca máximo (mínimo) de 20 días pero el RSI no marca el suyo, viniendo de
     sobrecompra > 65 (sobreventa < 35). La alerta dura 10 días.
  R3 MACD 12/26/9: la línea MACD cruza su señal en contra de la posición.
  R4 combinado: al menos 2 de los 3 coinciden.
Cuando el indicador de giro va en contra de la posición, a la apertura del día:
  * se cierra la posición entera ("giro");
  * GIRAR: si además el ADX(14) > 20 (hay tendencia), se abre la contraria con la gestión normal (stop 2 ATR,
    parciales, stop que persigue). Esa posición girada se mantiene mientras el indicador de giro la acompañe; si la
    señal lenta pasa a su lado, se convierte en una posición normal.
  * no se vuelve a entrar en la dirección de antes mientras el indicador de giro siga en contra (si no, el bot
    reabriría al día siguiente con la señal lenta).
Para cada indicador hay dos versiones: "girar" (lo que propone Oier) y "solo cerrar" (diagnóstico: separa si lo que
ayuda o perjudica es salir antes o darse la vuelta).

Base = producción v6: cartera BTC 42.5 / ETH 42.5 / ARB 15 %, 75 % por posición, stop 2 ATR, parciales 1.5/3 ATR,
reentrada, stop que persigue 3 ATR desde la parcial 1. Se comprueba que con los giros apagados el simulador da lo
mismo que el de 11g (producción). Criterio (el de siempre): ganar a la actual en ambas mitades y en >= 2 de 3 tercios,
sin más caída máxima ni más meses en rojo.
Salida: results/research_2026_11j.txt y .csv
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
import research_2026_11g as m11g  # noqa: E402
from bot.indicators import adx, atr, ema, rsi  # noqa: E402
from bot.strategies import trend_ema  # noqa: E402

COST, APR, WEIGHTS = m11c.COST, m11c.APR, m11c.WEIGHTS
STOP_ATR, MIN_TRADE, PT, PB_ATR, RESTOP_K = m11c.STOP_ATR, m11c.MIN_TRADE, m11c.PT, m11c.PB_ATR, m11c.RESTOP_K
FAST, SLOW, N, ADX_MIN = m11c.FAST, m11c.SLOW, m11c.N, m11c.ADX_MIN
TRAIL_K, TRAIL_AFTER = 3.0, 1          # producción
DIV_LOOK, DIV_HOLD, RSI_HI, RSI_LO = 20, 10, 65, 35


@dataclass(frozen=True)
class Var:
    name: str
    rev: str | None = None   # None (actual) | "ema" | "rsi" | "macd" | "combo"
    flip: bool = False       # True = abrir la contraria si ADX > 20; False = solo cerrar


VARIANTS = [
    Var("A actual (producción v6)"),
    Var("R1 EMA 9/21: girar", "ema", True),
    Var("R2 divergencia RSI: girar", "rsi", True),
    Var("R3 MACD: girar", "macd", True),
    Var("R4 2 de 3: girar", "combo", True),
    Var("C1 EMA 9/21: solo cerrar", "ema", False),
    Var("C2 divergencia RSI: solo cerrar", "rsi", False),
    Var("C3 MACD: solo cerrar", "macd", False),
    Var("C4 2 de 3: solo cerrar", "combo", False),
]


# ---- indicadores de giro (diarios; valor del día t = calculado con el cierre de t-1) ------------------------
def _rsi_divergence(d: pd.DataFrame) -> pd.Series:
    c = d.close
    r = rsi(c, 14)
    prev_hi_c = c.shift(1).rolling(DIV_LOOK - 1).max()
    prev_lo_c = c.shift(1).rolling(DIV_LOOK - 1).min()
    prev_hi_r = r.shift(1).rolling(DIV_LOOK - 1).max()
    prev_lo_r = r.shift(1).rolling(DIV_LOOK - 1).min()
    bear = (c > prev_hi_c) & (r < prev_hi_r) & (prev_hi_r > RSI_HI)   # máximo nuevo de precio, RSI no
    bull = (c < prev_lo_c) & (r > prev_lo_r) & (prev_lo_r < RSI_LO)   # mínimo nuevo de precio, RSI no
    out = np.zeros(len(d), dtype=int)
    cur, left = 0, 0
    for i, (b1, b2) in enumerate(zip(bear.to_numpy(), bull.to_numpy())):
        if b1:
            cur, left = -1, DIV_HOLD
        elif b2:
            cur, left = 1, DIV_HOLD
        elif left > 0:
            left -= 1
        if left == 0:
            cur = 0
        out[i] = cur
    return pd.Series(out, index=d.index)


def reversal(d: pd.DataFrame, kind: str) -> pd.Series:
    """Dirección que indica el indicador de giro: +1 alcista, -1 bajista, 0 nada. Ya desplazada un día."""
    c = d.close
    s_ema = np.sign(ema(c, 9) - ema(c, 21))
    m = ema(c, 12) - ema(c, 26)
    s_macd = np.sign(m - ema(m, 9))
    if kind == "ema":
        s = s_ema
    elif kind == "macd":
        s = s_macd
    elif kind == "rsi":
        s = _rsi_divergence(d)
    elif kind == "combo":
        tot = s_ema + s_macd + _rsi_divergence(d)
        s = pd.Series(np.where(tot >= 2, 1, np.where(tot <= -2, -1, 0)), index=d.index)
    else:
        raise ValueError(kind)
    return s.fillna(0).astype(int).shift(1)


# ---- simulador: el de producción (11c + stop que persigue de 11g) con los giros --------------------------
def simulate(h, d, v: Var, capital, cost_pct, apr):
    sig_d = trend_ema.signals(d, fast=FAST, slow=SLOW, adx_n=N, adx_min=ADX_MIN, allow_short=True).shift(1)
    day_atr = atr(d, N).shift(1)
    day_adx = adx(d, N).shift(1)
    rev_d = reversal(d, v.rev) if v.rev else None
    s = base.Sleeve(cash=capital)
    s.ref, s.pb = 0.0, 0.0
    st = {"flip": False}
    eq = np.empty(len(h))
    closes, opens = h["close"].values, h["open"].values
    last_day, cur_sig = None, 0
    hourly_rate = apr / (365 * 24)
    n_rev = {"giro": 0, "girada": 0}

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
            st["flip"] = False

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
        # --- decisión diaria ---
        if day != last_day:
            last_day = day
            sig = sig_d.get(day, np.nan)
            if not np.isnan(sig):
                cur_sig = int(sig)
                o = opens[i]
                rv = int(rev_d.get(day, 0)) if rev_d is not None and not pd.isna(rev_d.get(day, np.nan)) else 0
                if st["flip"] and s.side:
                    if cur_sig == s.side:                   # la señal lenta ya acompaña: posición normal
                        st["flip"] = False
                    elif rv != s.side:                      # el giro ya no acompaña: se cierra la girada
                        close_part(o, 1.0, "fin giro", t)
                if s.side and not st["flip"] and cur_sig != s.side:
                    close_part(o, 1.0, "señal", t)
                if rev_d is not None and s.side and not st["flip"] and rv == -s.side:
                    old = s.side
                    close_part(o, 1.0, "giro", t)
                    n_rev["giro"] += 1
                    ax = day_adx.get(day, np.nan)
                    if v.flip and not np.isnan(ax) and ax > ADX_MIN and s.stopped_day != day:
                        open_pos(o, -old, a_day, t)
                        if s.side:
                            st["flip"] = True
                            n_rev["girada"] += 1
                if (cur_sig != 0 and s.side == 0 and s.stopped_day != day
                        and (rev_d is None or rv != -cur_sig)):
                    open_pos(o, cur_sig, a_day, t)
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
            if s.side and s.pt_done >= TRAIL_AFTER and s.atr_e:
                tr = s.extreme - side * TRAIL_K * s.atr_e
                s.stop = max(s.stop, tr) if side == 1 else min(s.stop, tr)
            if s.side and ((side == 1 and px <= s.stop) or (side == -1 and px >= s.stop)):
                close_part(px, 1.0, "stop", t)
                s.stopped_day = day
            elif s.side and partial_out and new_high and s.pb >= PB_ATR and (cur_sig == side or st["flip"]):
                rebuy(px, a_day)
        eq[i] = mtm(px)
    s.n_rev = n_rev
    return pd.Series(eq, index=h.index), s


def portfolio(data, v, start, end):
    eqs, trades, fees, per, giros, giradas = [], [], 0.0, {}, 0, 0
    for sym, w in WEIGHTS.items():
        h, d = data[sym]
        hh = h[(h.index >= start) & (h.index < end)]
        e, s = simulate(hh, d, v, base.CAPITAL * w, COST[sym], APR[sym])
        eqs.append(e)
        trades += s.trades
        fees += s.fees
        giros += s.n_rev["giro"]
        giradas += s.n_rev["girada"]
        per[sym] = float(e.iloc[-1] - e.iloc[0])
    eq = pd.concat(eqs, axis=1).ffill().bfill().sum(axis=1)
    r = base.metrics(eq, trades, fees)
    r["giros"], r["giradas"] = giros, giradas
    r["pnl_giradas"] = sum(p for _, k, p in trades if k in ("fin giro",)) if trades else 0.0
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

    lines = ["# Investigación nov-2026 (10ª parte) — indicadores de giro: cerrar y darse la vuelta antes del stop (idea de Oier)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             "Base = producción v6 (cartera v5, gestión v3, stop que persigue 3 ATR tras parcial 1), costes reales."]
    h0, d0 = data["ETH"]
    hh = h0[(h0.index >= start) & (h0.index < end)]
    e1, _ = m11g.sim(hh, d0, m11g.Var("prod", k=TRAIL_K, after=TRAIL_AFTER), 50.0, 0.001, 0.04)
    e2, _ = simulate(hh, d0, VARIANTS[0], 50.0, 0.001, 0.04)
    lines.append(f"Comprobación del simulador: diferencia máxima con el de producción (11g S4) {float((e1 - e2).abs().max()):.6f} $ (debe ser 0).")
    lines.append("Columnas extra: giros = posiciones cerradas por el indicador; giradas = veces que se abrió la contraria.")
    lines.append("")
    rows = []
    for lab, s0, s1 in periods:
        lines += [f"## {lab} ({s0.date()} -> {min(s1, t1).date()})", base.HEADER + f" {'giros':>6} {'girad':>6}"]
        for v in VARIANTS:
            r = portfolio(data, v, s0, s1)
            r["variant"] = v.name
            lines.append(base.fmt(r) + f" {r['giros']:>6} {r['giradas']:>6}")
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
              full[["ret", "max_dd", "neg_months", "worst_month", "sharpe", "trades", "win_rate", "fees_usd", "giros",
                    "giradas", "gana_tercios", "gana_ambas_mitades"]].round(3).to_string(), ""]
    ok = full[(full.index != b.name) & full.gana_ambas_mitades & (full.gana_tercios >= 2) & (full.max_dd >= b.max_dd)
              & (full.neg_months <= b.neg_months)]
    if len(ok):
        lines.append("PASAN el criterio (ambas mitades, >= 2 tercios, sin más caída ni más meses en rojo): " + "; ".join(
            f"{n}: {r.ret:+.1%}, caída {r.max_dd:+.1%}, Sharpe {r.sharpe:.2f}" for n, r in ok.iterrows())
            + f"  (actual: {b.ret:+.1%}, {b.max_dd:+.1%}, {b.sharpe:.2f})")
        lines.append("Solo vale si su versión 'solo cerrar'/'girar' y los otros indicadores apuntan en la misma dirección.")
    else:
        lines.append("Ninguna variante pasa el criterio: se mantiene el sistema actual.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_11j{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_11j{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
