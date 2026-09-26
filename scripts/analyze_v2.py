"""Análisis v2: cartera BTC+ETH, walk-forward, funding en cortos.

    python scripts/analyze_v2.py
"""
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from bot import backtest, data, report
from bot.config import RISK
from bot.strategies import trend_ema

pd.set_option("display.width", 250)
SYMS = ("BTC", "ETH")
GRID = dict(fast=[10, 20, 30], slow=[50, 60, 100], adx_min=[15, 20, 25])


def daily(sym):
    d1 = data.resample(data.load(sym, "1h").reset_index(drop=True), "1d")
    d1["dt"] = pd.to_datetime(d1["timestamp"], unit="ms", utc=True)
    return d1.set_index("dt")


def funding_daily_est(sym):
    """Funding medio diario (90 días de Hyperliquid). Positivo = los largos pagan a los cortos."""
    p = data.DATA_DIR / f"{sym}_funding.csv"
    if not p.exists():
        return 0.0
    f = pd.read_csv(p)
    return float(f.funding_rate.mean() * 24)  # funding es por hora


def run_portfolio(dfs, params, stop=2.0, capital=RISK.initial_capital, funding=None):
    """Capital dividido a partes iguales; cada activo corre independiente. Devuelve equity total."""
    eqs = []
    trades = 0
    for sym, df in dfs.items():
        sig = trend_ema.signals(df, **params)
        r = backtest.run(df, sig, strategy="trend_ema", symbol=sym, timeframe="1d", params=params,
                         stop_atr=stop, capital=capital / len(dfs))
        eq = r.equity.copy()
        if funding:
            # coste de funding: largos pagan f, cortos cobran f (si f>0). Aplicado a la exposición diaria.
            pos = sig.shift(1).fillna(0).reindex(eq.index).fillna(0)
            f = funding[sym]
            cost = -pos * f * eq  # largo con f>0 paga
            eq = eq + cost.cumsum()
        eqs.append(eq)
        trades += len(r.trades)
    total = pd.concat(eqs, axis=1).ffill().sum(axis=1)
    return total, trades


def metrics(eq, trades):
    peak = eq.cummax()
    dd = float(((eq - peak) / peak).min())
    daily_r = eq.pct_change().dropna()
    sharpe = float(daily_r.mean() / daily_r.std() * np.sqrt(365)) if daily_r.std() > 0 else 0
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    return dict(ret=eq.iloc[-1] / eq.iloc[0] - 1, cagr=(eq.iloc[-1] / eq.iloc[0]) ** (365 / days) - 1,
                dd=dd, sharpe=sharpe, trades=trades)


def main():
    dfs = {s: daily(s) for s in SYMS}
    fund = {s: funding_daily_est(s) for s in SYMS}
    print("Funding medio diario (HL, 90d):", {k: f"{v:+.4%}" for k, v in fund.items()},
          "-> anualizado", {k: f"{v*365:+.1%}" for k, v in fund.items()})

    # --- 1) cartera con todos los parámetros, periodo completo, con funding
    rows = []
    for fast, slow, adx_min in itertools.product(GRID["fast"], GRID["slow"], GRID["adx_min"]):
        p = dict(fast=fast, slow=slow, adx_n=14, adx_min=adx_min, allow_short=True)
        eq, n = run_portfolio(dfs, p, funding=fund)
        m = metrics(eq, n)
        m.update(p)
        rows.append(m)
    full = pd.DataFrame(rows)
    print("\n=== CARTERA BTC+ETH, diario, L+S, con funding — periodo completo (27 combinaciones) ===")
    print(full.describe().loc[["min", "25%", "50%", "75%", "max"], ["ret", "cagr", "dd", "sharpe", "trades"]].round(3).to_string())
    print(f"% combinaciones positivas: {(full.ret > 0).mean():.0%}   % con DD mejor que -25%: {(full.dd > -0.25).mean():.0%}")

    # --- 2) walk-forward: elegir params con año 1, evaluar a ciegas en año 2
    mid = dfs["BTC"].index[0] + pd.Timedelta(days=365)
    tr = {s: d[d.index < mid] for s, d in dfs.items()}
    te = {s: d[d.index >= mid - pd.Timedelta(days=120)] for s, d in dfs.items()}  # 120 días de calentamiento de indicadores
    best, best_s = None, -9
    for fast, slow, adx_min in itertools.product(GRID["fast"], GRID["slow"], GRID["adx_min"]):
        p = dict(fast=fast, slow=slow, adx_n=14, adx_min=adx_min, allow_short=True)
        eq, n = run_portfolio(tr, p, funding=fund)
        m = metrics(eq, n)
        if m["sharpe"] > best_s:
            best_s, best = m["sharpe"], p
    eq_te, n_te = run_portfolio(te, best, funding=fund)
    eq_te = eq_te[eq_te.index >= mid]
    eq_te = eq_te / eq_te.iloc[0] * RISK.initial_capital
    m_te = metrics(eq_te, n_te)
    bh = {s: d.close[d.index >= mid].iloc[-1] / d.close[d.index >= mid].iloc[0] - 1 for s, d in dfs.items()}
    print("\n=== WALK-FORWARD ===")
    print(f"Optimizado en año 1 ({dfs['BTC'].index[0].date()} -> {mid.date()}): mejores params {best}, Sharpe in-sample {best_s:.2f}")
    print(f"Test a ciegas año 2 ({mid.date()} -> {dfs['BTC'].index[-1].date()}):")
    print(f"  retorno {m_te['ret']:+.1%}   max DD {m_te['dd']:+.1%}   Sharpe {m_te['sharpe']:.2f}   trades {m_te['trades']}")
    print(f"  buy&hold mismo periodo: BTC {bh['BTC']:+.1%}  ETH {bh['ETH']:+.1%}")

    # --- 3) tamaño de posición: 95 % vs 60 % del capital por activo
    print("\n=== Sensibilidad al tamaño (params mediana fast=20, slow=60, adx=20, periodo completo) ===")
    p = dict(fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True)
    for frac in (0.95, 0.75, 0.6, 0.5):
        RISK_frac = frac
        import bot.config as cfg
        object.__setattr__(cfg.RISK, "position_fraction", frac)
        eq, n = run_portfolio(dfs, p, funding=fund)
        m = metrics(eq, n)
        print(f"  {frac:.0%} por activo: retorno {m['ret']:+.1%}  CAGR {m['cagr']:+.1%}  DD {m['dd']:+.1%}  Sharpe {m['sharpe']:.2f}")
    object.__setattr__(cfg.RISK, "position_fraction", 0.95)

    eq, _ = run_portfolio(dfs, p, funding=fund)
    eq.to_csv(report.RESULTS_DIR / "portfolio_equity_v2.csv")


if __name__ == "__main__":
    main()
