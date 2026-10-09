"""Investigación mensual oct-2026: proteger beneficios dentro de la tendencia.

Pregunta de Oier (2026-10-07/08): en vez de un stop fijo, ¿mejora el resultado ir asegurando
beneficios (ventas parciales) y/o mover el stop a favor (a precio de entrada, o dinámico detrás
del precio), tanto en largos como en cortos? Criterio acordado: CONSISTENCIA (caída máxima,
meses en rojo) por encima de la rentabilidad máxima, siempre neto de comisiones.

Simulación fiel al bot real (bot/onchain.py v2):
- Señal diaria trend_ema (EMA 20/60 + ADX>=20, L+S) calculada con velas diarias CERRADAS;
  se ejecuta en la primera hora del día siguiente (el bot hace el ciclo a ~00:05-00:25 UTC).
- Capital dividido en dos mitades (BTC, ETH); cada posición = 75 % de su mitad.
- Stops / tomas de beneficio comprobados UNA vez por hora con el cierre de la vela de 1h
  (el bot real mira el precio spot una vez por hora; no ve mechas intrahora).
- Tras un stop no se reentra el mismo día; al día siguiente, si la señal sigue, se reentra.
- Costes por operación (cada compra/venta): 0.05 % fee Uniswap + 0.05 % deslizamiento + 0.03 $ de gas.
  Cortos (Aave): +0.10 % extra al abrir y al cerrar (supply/borrow/repay) e interés 4 %/año.

Variantes (iguales para largos y cortos, en espejo):
  V0  actual: stop fijo a 2xATR desde la entrada.
  BE  stop a precio de entrada (+costes) cuando la operación va +X*ATR a favor.
  TR  stop dinámico (chandelier): máximo desde la entrada - k*ATR (en cortos: mínimo + k*ATR). Nunca retrocede.
  PT  ventas parciales: cierra 1/3 a +a*ATR y otro 1/3 a +b*ATR; tras la 1ª, stop a entrada.
      Con 'RE': si la señal sigue y el precio corrige hasta la EMA20 diaria, recompra lo vendido.
  Combinaciones PT+TR.

Uso (GitHub Actions lo ejecuta al subir este archivo):
    python scripts/research_2026_10.py            # descarga 3 años de Coinbase
    python scripts/research_2026_10.py --synthetic # prueba local sin red
Salida: results/research_2026_10.txt y results/research_2026_10.csv
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from bot.indicators import atr, ema
from bot.strategies import trend_ema

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
RESULTS.mkdir(exist_ok=True)

CAPITAL = 115.75
POS_FRACTION = 0.75
SWAP_PCT = 0.0010          # fee 0.05 % + slippage 0.05 % por lado
GAS_USD = 0.03             # por swap en Arbitrum
SHORT_EXTRA_PCT = 0.0010   # tx de Aave al abrir y al cerrar un corto
SHORT_APR = 0.04           # interés del préstamo
STOP_ATR = 2.0
MIN_TRADE = 5.0
SYMS = ("BTC", "ETH")
DAYS = 1095


# ------------------------------------------------------------------ datos
def load_hourly(sym: str, synthetic: bool) -> pd.DataFrame:
    if synthetic:
        rng = np.random.default_rng(7 if sym == "BTC" else 11)
        n = DAYS * 24
        # paseo aleatorio con regímenes de tendencia para probar la mecánica
        drift = np.repeat(rng.choice([-0.0004, 0.0, 0.0005], size=n // 500 + 1), 500)[:n]
        r = drift + rng.normal(0, 0.006, n)
        close = (30000 if sym == "BTC" else 2000) * np.exp(np.cumsum(r))
        idx = pd.date_range("2023-10-01", periods=n, freq="1h", tz="UTC")
        o = np.r_[close[0], close[:-1]]
        hi = np.maximum(o, close) * (1 + np.abs(rng.normal(0, 0.002, n)))
        lo = np.minimum(o, close) * (1 - np.abs(rng.normal(0, 0.002, n)))
        return pd.DataFrame(dict(open=o, high=hi, low=lo, close=close, volume=1.0), index=idx)
    from bot.data import fetch_coinbase
    df = fetch_coinbase(sym, "1h", DAYS, verbose=False)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("dt")[["open", "high", "low", "close", "volume"]]


def daily_from_hourly(h: pd.DataFrame) -> pd.DataFrame:
    d = h.resample("1D").agg(dict(open="first", high="max", low="min", close="last", volume="sum")).dropna()
    return d


# ------------------------------------------------------------- variantes
@dataclass(frozen=True)
class Variant:
    name: str
    be_at: float | None = None      # mover stop a entrada cuando va +be_at*ATR
    trail_k: float | None = None    # stop dinámico a k*ATR del extremo
    pt: tuple = ()                  # niveles de toma parcial en múltiplos de ATR (1/3 cada uno)
    reentry: bool = False           # recomprar lo vendido si corrige a la EMA20 diaria


VARIANTS = [
    Variant("V0 actual (stop fijo 2xATR)"),
    Variant("BE 1.0 (stop a entrada a +1 ATR)", be_at=1.0),
    Variant("BE 1.5", be_at=1.5),
    Variant("TR 2.0 (stop dinámico 2xATR)", trail_k=2.0),
    Variant("TR 2.5", trail_k=2.5),
    Variant("TR 3.0", trail_k=3.0),
    Variant("PT 1.5/3 (parciales)", pt=(1.5, 3.0)),
    Variant("PT 2/4", pt=(2.0, 4.0)),
    Variant("PT 1.5/3 + RE (recompra en EMA20)", pt=(1.5, 3.0), reentry=True),
    Variant("PT 2/4 + RE", pt=(2.0, 4.0), reentry=True),
    Variant("PT 2/4 + TR 3.0", pt=(2.0, 4.0), trail_k=3.0),
    Variant("PT 2/4 + RE + TR 3.0", pt=(2.0, 4.0), reentry=True, trail_k=3.0),
    Variant("BE 1.5 + TR 3.0", be_at=1.5, trail_k=3.0),
]


# ------------------------------------------------------------- simulador
@dataclass
class Sleeve:
    cash: float
    side: int = 0
    qty: float = 0.0          # unidades del activo (positivo; el signo lo da side)
    full_qty: float = 0.0     # tamaño completo de la posición (para recompras)
    entry: float = 0.0
    stop: float = 0.0
    atr_e: float = 0.0        # ATR al entrar (fija los niveles de la operación)
    extreme: float = 0.0      # máximo (largo) / mínimo (corto) desde la entrada
    pt_done: int = 0
    stopped_day: object = None
    fees: float = 0.0
    trades: list = field(default_factory=list)  # (fecha, tipo, pnl_usd)
    realized_trade: float = 0.0  # pnl acumulado de la operación en curso (parciales incluidos)


def swap_cost(usd: float, short: bool) -> float:
    return usd * (SWAP_PCT + (SHORT_EXTRA_PCT if short else 0.0)) + GAS_USD


def simulate(h: pd.DataFrame, d: pd.DataFrame, v: Variant, capital: float) -> tuple[pd.Series, Sleeve]:
    sig_d = trend_ema.signals(d, fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True)
    atr_d = atr(d, 14)
    ema20 = ema(d["close"], 20)
    # valores de la vela diaria cerrada el día anterior, disponibles durante todo el día actual
    day_sig = sig_d.shift(1)
    day_atr = atr_d.shift(1)
    day_ema = ema20.shift(1)

    s = Sleeve(cash=capital)
    eq = np.empty(len(h))
    hours = h.index
    closes = h["close"].values
    opens = h["open"].values
    last_day = None
    hourly_short_rate = SHORT_APR / (365 * 24)

    def mtm(px: float) -> float:
        return s.cash + s.side * s.qty * px if s.side else s.cash

    def close_part(px: float, frac: float, kind: str, t):
        """Cierra una fracción de la posición actual (1.0 = todo)."""
        q = s.qty * frac
        usd = q * px
        cost = swap_cost(usd, s.side == -1)
        pnl = s.side * q * (px - s.entry) - cost
        s.cash += s.side * q * px - cost if s.side == 1 else -(q * px) - cost
        # para cortos: al abrir sumamos el producto de la venta al cash; al cerrar recompramos
        s.qty -= q
        s.fees += cost
        s.realized_trade += pnl
        if s.qty <= 1e-12 or frac >= 0.999:
            s.trades.append((t, kind, s.realized_trade))
            s.side, s.qty, s.full_qty, s.pt_done, s.realized_trade = 0, 0.0, 0.0, 0, 0.0

    def open_pos(px: float, side: int, a: float, t):
        equity = mtm(px)
        usd = equity * POS_FRACTION
        if usd < MIN_TRADE or np.isnan(a):
            return
        cost = swap_cost(usd, side == -1)
        q = usd / px
        s.cash += (-usd if side == 1 else usd) - cost
        s.side, s.qty, s.full_qty, s.entry = side, q, q, px
        s.atr_e, s.extreme, s.pt_done, s.realized_trade = a, px, 0, -cost
        s.stop = px - side * STOP_ATR * a
        s.fees += cost

    def add_back(px: float, t):
        """Recompra (o re-vende en cortos) la parte tomada en parciales."""
        q = s.full_qty - s.qty
        if q * px < MIN_TRADE:
            return
        cost = swap_cost(q * px, s.side == -1)
        s.cash += (-(q * px) if s.side == 1 else q * px) - cost
        # nuevo precio medio
        s.entry = (s.entry * s.qty + px * q) / (s.qty + q)
        s.qty += q
        s.pt_done = 0
        s.fees += cost
        s.realized_trade -= cost

    for i, t in enumerate(hours):
        day = t.normalize()
        px = closes[i]
        # --- ciclo diario: primera hora del día, al open
        if day != last_day:
            last_day = day
            sig = day_sig.get(day, np.nan)
            a = day_atr.get(day, np.nan)
            if not np.isnan(sig):
                sig = int(sig)
                o = opens[i]
                if s.side and sig != s.side:
                    close_part(o, 1.0, "señal", t)
                if sig != 0 and s.side == 0 and s.stopped_day != day:
                    open_pos(o, sig, a, t)
        # --- intereses del corto
        if s.side == -1:
            s.cash -= s.qty * px * hourly_short_rate
            s.fees += s.qty * px * hourly_short_rate
        # --- gestión horaria (con el cierre de la hora, como el bot real)
        if s.side:
            side = s.side
            s.extreme = max(s.extreme, px) if side == 1 else min(s.extreme, px)
            fav = side * (px - s.entry) / s.atr_e if s.atr_e else 0.0  # ganancia en ATRs
            # stop dinámico
            if v.trail_k:
                cand = s.extreme - side * v.trail_k * s.atr_e
                s.stop = max(s.stop, cand) if side == 1 else min(s.stop, cand)
            # stop a entrada
            be_px = s.entry * (1 + side * (2 * SWAP_PCT))
            if v.be_at is not None and fav >= v.be_at:
                s.stop = max(s.stop, be_px) if side == 1 else min(s.stop, be_px)
            # parciales
            if v.pt and s.pt_done < len(v.pt) and fav >= v.pt[s.pt_done]:
                close_part(px, (1 / 3) / (s.qty / s.full_qty), f"parcial{s.pt_done + 1}", t)
                if s.side:
                    s.pt_done += 1
                    s.stop = max(s.stop, be_px) if side == 1 else min(s.stop, be_px)
            # stop (antes que la recompra: si salta, no se recompra)
            if s.side and ((side == 1 and px <= s.stop) or (side == -1 and px >= s.stop)):
                close_part(px, 1.0, "stop", t)
                s.stopped_day = day
            # recompra en la corrección a la EMA20 (solo si el precio sigue por encima del stop)
            if s.side and v.reentry and s.qty < s.full_qty * 0.99:
                e20 = day_ema.get(day, np.nan)
                if not np.isnan(e20) and ((side == 1 and px <= e20) or (side == -1 and px >= e20)):
                    add_back(px, t)
        eq[i] = mtm(px)
    return pd.Series(eq, index=hours), s


# ------------------------------------------------------------- métricas
def metrics(eq: pd.Series, trades: list, fees: float) -> dict:
    d = eq.resample("1D").last()
    m = eq.resample("ME").last()
    mret = m.pct_change().dropna()
    peak = d.cummax()
    dd = float(((d - peak) / peak).min())
    dr = d.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365
    pnl = [p for (_, k, p) in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    return dict(
        ret=eq.iloc[-1] / eq.iloc[0] - 1,
        cagr=(eq.iloc[-1] / eq.iloc[0]) ** (1 / max(years, 1e-9)) - 1,
        max_dd=dd,
        sharpe=float(dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0.0,
        months=len(mret),
        neg_months=int((mret < 0).sum()),
        worst_month=float(mret.min()) if len(mret) else 0.0,
        trades=len(pnl),
        win_rate=len(wins) / len(pnl) if pnl else 0.0,
        avg_win=float(np.mean(wins)) if wins else 0.0,
        avg_loss=float(np.mean(losses)) if losses else 0.0,
        fees_usd=fees,
    )


def run_portfolio(data: dict, v: Variant, start=None, end=None) -> dict:
    eqs, trades, fees = [], [], 0.0
    for sym, (h, d) in data.items():
        hh = h if start is None else h[(h.index >= start) & (h.index < end)]
        e, s = simulate(hh, d, v, CAPITAL / len(data))
        eqs.append(e)
        trades += s.trades
        fees += s.fees
    eq = pd.concat(eqs, axis=1).ffill().sum(axis=1)
    out = metrics(eq, trades, fees)
    out["variant"] = v.name
    out["_eq"] = eq
    return out


def fmt(r: dict) -> str:
    return (f"{r['variant']:<36} {r['ret']:>+8.1%} {r['cagr']:>+7.1%} {r['max_dd']:>+7.1%} {r['sharpe']:>6.2f} "
            f"{r['neg_months']:>3}/{r['months']:<3} {r['worst_month']:>+7.1%} {r['trades']:>5} {r['win_rate']:>6.0%} "
            f"{r['avg_win']:>+7.2f} {r['avg_loss']:>+7.2f} {r['fees_usd']:>7.2f}")


HEADER = (f"{'variante':<36} {'retorno':>8} {'CAGR':>7} {'maxDD':>7} {'Sharpe':>6} {'m-':>7} {'peor m':>7} "
          f"{'ops':>5} {'acierto':>6} {'gana$':>7} {'pierde$':>7} {'costes$':>7}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()

    data = {}
    for sym in SYMS:
        h = load_hourly(sym, a.synthetic)
        data[sym] = (h, daily_from_hourly(h))
    t0, t1 = data["BTC"][0].index[0], data["BTC"][0].index[-1]
    # los indicadores necesitan ~60 días; se evalúa desde el día 90
    start = t0 + pd.Timedelta(days=90)

    lines = [f"# Investigación oct-2026 — proteger beneficios dentro de la tendencia",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, {t0.date()} -> {t1.date()} "
             f"(evaluación desde {start.date()}). Capital {CAPITAL} $, BTC+ETH a medias, 75 % por posición.",
             f"Costes: {SWAP_PCT:.2%}/lado + {GAS_USD} $ gas; cortos +{SHORT_EXTRA_PCT:.2%} y {SHORT_APR:.0%}/año.",
             "Columnas: m- = meses en negativo / meses; gana$/pierde$ = resultado medio por operación ganadora/perdedora.",
             ""]
    rows = []
    halves = [(start, start + (t1 - start) / 2), (start + (t1 - start) / 2, t1 + pd.Timedelta(hours=1))]
    for label, (s0, s1) in [("PERIODO COMPLETO", (start, t1 + pd.Timedelta(hours=1))),
                            ("1ª MITAD", halves[0]), ("2ª MITAD", halves[1])]:
        lines += [f"## {label} ({s0.date()} -> {min(s1, t1).date()})", HEADER]
        for v in VARIANTS:
            r = run_portfolio(data, v, s0, s1)
            lines.append(fmt(r))
            rows.append({k: val for k, val in r.items() if k != "_eq"} | {"period": label})
        lines.append("")

    df = pd.DataFrame(rows)
    full = df[df.period == "PERIODO COMPLETO"].set_index("variant")
    base = full.iloc[0]
    # ranking de consistencia: mejor DD, menos meses en rojo, y que no gane menos que la base
    full["score"] = (full.max_dd - base.max_dd) * 100 - (full.neg_months - base.neg_months) * 1.0 \
        + (full.cagr - base.cagr) * 50
    both = df[df.period != "PERIODO COMPLETO"].pivot_table(index="variant", columns="period", values="ret")
    full["gana_en_ambas_mitades_vs_V0"] = [
        bool((both.loc[n] > both.loc[base.name]).all()) for n in full.index]
    lines += ["## RANKING (consistencia: caída máx., meses en rojo y rentabilidad, frente a V0)",
              full.sort_values("score", ascending=False)[["ret", "max_dd", "neg_months", "sharpe", "trades", "fees_usd",
                                                          "gana_en_ambas_mitades_vs_V0", "score"]]
              .round(3).to_string(), ""]
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (RESULTS / f"research_2026_10{suffix}.txt").write_text(txt)
    df.to_csv(RESULTS / f"research_2026_10{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
