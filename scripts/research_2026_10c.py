"""Investigación oct-2026 (3ª parte): MICRO-TENDENCIAS ("surfear las olas") además de la tendencia diaria.

Petición de Oier (2026-10-17): aparte de las macro tendencias, coger micro tendencias para surfear las olas.

Idea probada: una capa "micro" que opera en 4h/1h SOLO a favor de la tendencia diaria (largos si la diaria es
alcista, cortos si es bajista) y que usa el USDC que la v3 deja parado (~25 % del capital). Se compara:
  - v3 sola (lo que corre hoy)
  - micro sola (sobre su 25 %)
  - v3 + micro (cartera completa)
con 3 años de velas 1h reales de Coinbase y costes reales (0.10 %/lado + 0.03 $ gas; cortos +0.10 % y 4 %/año).

Reglas micro (todas con stop 2×ATR(14) de 4h comprobado cada hora, y cierre si la diaria cambia):
  E10/30  4h EMA 10/30: dentro cuando la 4h apunta en la misma dirección que la diaria.
  E20/50  igual con EMA 20/50 (más lenta, menos operaciones).
  RUP24   ruptura del máximo (mínimo) de las últimas 24 h a favor de la diaria; salida al romper el
          mínimo (máximo) de las últimas 12 h.
  RET35   retroceso: en diaria alcista compra cuando el RSI(14) de 4h < 35, vende cuando > 55
          (en bajista: corto con RSI > 65, cierra < 45). Máximo 5 días por operación.
Salida: results/research_2026_10c.txt y .csv
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
import research_2026_10b as v3mod  # noqa: E402
from bot.indicators import atr, ema, rsi  # noqa: E402
from bot.strategies import trend_ema  # noqa: E402

MICRO_FRAC = 0.25          # la capa micro usa el USDC que la v3 deja libre (25 % del capital)
STOP_ATR = 2.0
MAX_HOLD_H = 5 * 24
V3 = v3mod.V2("v3", pt=(1.5, 3.0), rh=True, pb_atr=1.0, restop_k=1.5)


@dataclass(frozen=True)
class Micro:
    name: str
    kind: str          # ema | rup | ret
    a: int = 0
    b: int = 0


MICROS = [Micro("E10/30 (4h EMA a favor de la diaria)", "ema", 10, 30),
          Micro("E20/50 (4h EMA lenta)", "ema", 20, 50),
          Micro("RUP24 (ruptura 24 h a favor)", "rup", 24, 12),
          Micro("RET35 (retroceso RSI 4h)", "ret", 35, 55)]


def h4_from_h(h):
    return h.resample("4h").agg(dict(open="first", high="max", low="min", close="last", volume="sum")).dropna()


def to_hourly(s4: pd.Series, idx) -> pd.Series:
    """Valor de la última vela de 4h CERRADA, disponible en cada hora."""
    s = s4.copy()
    s.index = s.index + pd.Timedelta(hours=4)        # disponible al cerrar la vela
    return s.reindex(idx, method="ffill")


def sim_micro(h: pd.DataFrame, d: pd.DataFrame, m: Micro, capital: float):
    idx = h.index
    o, c = h["open"].values, h["close"].values
    dsig = trend_ema.signals(d, fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True).shift(1)
    dsig_h = dsig.reindex(idx.normalize()).values
    h4 = h4_from_h(h)
    atr4 = to_hourly(atr(h4, 14), idx).values
    if m.kind == "ema":
        f, s_ = ema(h4["close"], m.a), ema(h4["close"], m.b)
        micro = to_hourly(np.sign(f - s_), idx).values
    elif m.kind == "ret":
        r4 = to_hourly(rsi(h4["close"], 14), idx).values
    else:
        # canal de las velas ANTERIORES a la última cerrada (t-1): ruptura = cierre de t-1 fuera del canal
        hi = h["high"].rolling(m.a).max().shift(2).values
        lo = h["low"].rolling(m.a).min().shift(2).values
        xhi = h["high"].rolling(m.b).max().shift(2).values
        xlo = h["low"].rolling(m.b).min().shift(2).values
        cprev = h["close"].shift(1).values

    cash, pos, qty, entry, stop, t_in = capital, 0, 0.0, 0.0, 0.0, 0
    blocked = 0          # dirección bloqueada tras un stop hasta que la regla se "resetee"
    fees, trades = 0.0, []
    pnl_trade = 0.0
    eq = np.empty(len(idx))
    rate_h = base.SHORT_APR / (365 * 24)

    def close(px, t, why):
        nonlocal cash, pos, qty, fees, pnl_trade
        cost = base.swap_cost(qty * px, pos == -1)
        cash += (qty * px if pos == 1 else -qty * px) - cost
        pnl_trade += pos * qty * (px - entry) - cost
        fees += cost
        trades.append((t, why, pnl_trade))
        pos, qty = 0, 0.0

    def open_(px, side, i, t):
        nonlocal cash, pos, qty, entry, stop, t_in, fees, pnl_trade
        a = atr4[i]
        usd = cash                              # todo su sleeve (sin apalancar)
        if usd < base.MIN_TRADE or np.isnan(a):
            return
        cost = base.swap_cost(usd, side == -1)
        if side == 1:                           # compra: paga usd, recibe (usd - coste) en token
            qty = (usd - cost) / px
            cash -= usd
        else:                                   # corto: vende usd prestados, recibe usd - coste
            qty = usd / px
            cash += usd - cost
        fees += cost
        pos, entry, t_in, pnl_trade = side, px, i, -cost
        stop = px - side * STOP_ATR * a

    for i, t in enumerate(idx):
        ds = dsig_h[i]
        ds = 0 if np.isnan(ds) else int(ds)
        px_o = o[i]
        # --- deseo de la regla (con info de velas cerradas)
        if m.kind == "ema":
            mv = micro[i]
            want = int(mv) if not np.isnan(mv) and int(mv) == ds else 0
            if blocked and want != blocked:
                blocked = 0
            if want == blocked:
                want = 0
        elif m.kind == "ret":
            rv = r4[i]
            want = pos
            if pos == 0 and not np.isnan(rv):
                if ds == 1 and rv < m.a:
                    want = 1
                elif ds == -1 and rv > 100 - m.a:
                    want = -1
            elif pos == 1 and (rv > m.b or ds != 1 or i - t_in > MAX_HOLD_H):
                want = 0
            elif pos == -1 and (rv < 100 - m.b or ds != -1 or i - t_in > MAX_HOLD_H):
                want = 0
        else:
            want = pos
            if not np.isnan(hi[i]):
                if pos == 0:
                    if ds == 1 and cprev[i] > hi[i]:
                        want = 1
                    elif ds == -1 and cprev[i] < lo[i]:
                        want = -1
                elif pos == 1 and (cprev[i] < xlo[i] or ds != 1):
                    want = 0
                elif pos == -1 and (cprev[i] > xhi[i] or ds != -1):
                    want = 0
        if pos and ds != pos:
            want = 0
        # --- ejecutar al open de la hora
        if pos != want:
            if pos:
                close(px_o, t, "señal")
            if want:
                open_(px_o, want, i, t)
        # --- intereses y stop con el cierre de la hora
        px = c[i]
        if pos == -1:
            cash -= qty * px * rate_h
            fees += qty * px * rate_h
        if pos and ((pos == 1 and px <= stop) or (pos == -1 and px >= stop)):
            side = pos
            close(px, t, "stop")
            blocked = side
        eq[i] = cash + (qty * px if pos == 1 else -qty * px if pos == -1 else 0.0)
    return pd.Series(eq, index=idx), trades, fees


def portfolio(data, start, end, micro: Micro | None, with_v3: bool):
    eqs, trades, fees = [], [], 0.0
    for sym, (h, d) in data.items():
        hh = h[(h.index >= start) & (h.index < end)]
        parts = []
        if with_v3:
            e, s = v3mod.simulate(hh, d, V3, base.CAPITAL / len(data))
            parts.append(e)
            trades += s.trades
            fees += s.fees
        if micro is not None:
            mc = base.CAPITAL * MICRO_FRAC / len(data)
            e, tr, f = sim_micro(hh, d, micro, mc)
            parts.append(e - (mc if with_v3 else 0))   # usa el USDC libre de la v3
            trades += tr
            fees += f
        eqs.append(sum(parts))
    eq = pd.concat(eqs, axis=1).ffill().sum(axis=1)
    return base.metrics(eq, trades, fees)


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
    th = [start + (t1 - start) * k / 3 for k in range(4)]
    th[-1] = end
    periods = [("PERIODO COMPLETO", start, end), ("1ª MITAD", start, mid), ("2ª MITAD", mid, end),
               ("TERCIO 1", th[0], th[1]), ("TERCIO 2 (lateral)", th[1], th[2]), ("TERCIO 3", th[2], th[3])]
    configs = [("v3 sola (actual)", None, True)]
    for m in MICROS:
        configs.append((f"micro sola: {m.name}", m, False))
    for m in MICROS:
        configs.append((f"v3 + {m.name}", m, True))

    lines = ["# Investigación oct-2026 (3ª parte) — micro-tendencias a favor de la diaria (idea de Oier, 17-oct)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, {t0.date()} -> {t1.date()} "
             f"(evaluación desde {start.date()}). Capital {base.CAPITAL} $; la micro usa el 25 % que la v3 deja en USDC.",
             f"Costes: {base.SWAP_PCT:.2%}/lado + {base.GAS_USD} $ gas; cortos +{base.SHORT_EXTRA_PCT:.2%} y "
             f"{base.SHORT_APR:.0%}/año. 'micro sola' = solo los {base.CAPITAL * MICRO_FRAC:.2f} $ de la capa micro.",
             "Columnas: m- = meses en negativo / meses; gana$/pierde$ = resultado medio por operación.", ""]
    rows = []
    for label, s0, s1 in periods:
        lines += [f"## {label} ({s0.date()} -> {min(s1, t1).date()})", base.HEADER]
        for name, m, with_v3 in configs:
            r = portfolio(data, s0, s1, m, with_v3)
            r["variant"] = name
            lines.append(base.fmt(r))
            rows.append(r | {"period": label})
        lines.append("")
    df = pd.DataFrame(rows)
    full = df[df.period == "PERIODO COMPLETO"].set_index("variant")
    b = full.loc["v3 sola (actual)"]
    sub = df[df.period.str.startswith("TERCIO")].pivot_table(index="variant", columns="period", values="ret")
    full["tercios_positivos"] = (sub > 0).sum(axis=1).reindex(full.index)
    full["mejora_v3_en_tercios"] = [int((sub.loc[n] > sub.loc[b.name]).sum()) for n in full.index]
    full["score_vs_v3"] = (full.max_dd - b.max_dd) * 100 - (full.neg_months - b.neg_months) + (full.cagr - b.cagr) * 50
    lines += ["## RESUMEN (periodo completo)",
              full[["ret", "max_dd", "neg_months", "worst_month", "sharpe", "trades", "win_rate", "fees_usd",
                    "tercios_positivos", "mejora_v3_en_tercios", "score_vs_v3"]].round(3).to_string(), ""]
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_10c{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_10c{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
