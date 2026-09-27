"""Estudio sistemático de patrones: ¿qué tiene ventaja real en BTC/ETH con fees de Uniswap?

    python scripts/pattern_study.py

Cada patrón se codifica como regla de entrada/salida, solo largos, y se prueba en diario y 4h
sobre BTC y ETH, con varias parametrizaciones (robustez), fees 0.05 % + slippage 0.05 % por lado.
Salida: results/pattern_study.csv y tabla resumen por patrón.
"""
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from bot import backtest, data, indicators as I
from bot.config import RISK, FEES

pd.set_option("display.width", 250)
object.__setattr__(RISK, "position_fraction", 0.75)
object.__setattr__(FEES, "taker", 0.0005)
object.__setattr__(FEES, "slippage", 0.0005)


def _hold(entry: pd.Series, exit_: pd.Series) -> pd.Series:
    """Convierte señales puntuales de entrada/salida en posición mantenida (0/1)."""
    pos = np.zeros(len(entry), dtype=int)
    p = 0
    e, x = entry.fillna(False).values, exit_.fillna(False).values
    for i in range(len(pos)):
        if p == 0 and e[i]:
            p = 1
        elif p == 1 and x[i]:
            p = 0
        pos[i] = p
    return pd.Series(pos, index=entry.index)


# ----------------------------------------------------------------- patrones
def trend_ema(df, fast=20, slow=60, adx_min=20):  # referencia (v1)
    f, s, a = I.ema(df.close, fast), I.ema(df.close, slow), I.adx(df, 14)
    return ((f > s) & (a > adx_min)).astype(int)


def breakout(df, n=40, exit_n=20):  # ruptura de máximos (turtle)
    hi, _ = I.donchian(df, n)
    _, lo = I.donchian(df, exit_n)
    return _hold(df.close > hi.shift(1), df.close < lo.shift(1))


def pullback(df, fast=20, slow=60, dip=0.98):  # retroceso a la media en tendencia
    f, s = I.ema(df.close, fast), I.ema(df.close, slow)
    up = f > s
    entry = up & (df.low <= f * dip) & (df.close > f * dip)
    return _hold(entry, ~up)


def rsi_dip(df, slow=100, rsi_n=14, low=35, high=60):  # RSI bajo en tendencia alcista
    s = I.ema(df.close, slow)
    r = I.rsi(df.close, rsi_n)
    return _hold((df.close > s) & (r < low), (r > high) & (r.shift(1) <= high) | (df.close < s))


def momentum(df, n=30, exit_n=10):  # ROC positivo
    roc = df.close.pct_change(n)
    roc_x = df.close.pct_change(exit_n)
    return _hold(roc > 0, roc_x < 0)


def ma_ribbon(df, n1=10, n2=20, n3=50):  # medias alineadas
    a, b, c = I.ema(df.close, n1), I.ema(df.close, n2), I.ema(df.close, n3)
    return ((a > b) & (b > c)).astype(int)


def engulfing(df, slow=60, hold=10):  # vela envolvente alcista en tendencia, salida por tiempo
    s = I.ema(df.close, slow)
    prev_red = df.close.shift(1) < df.open.shift(1)
    eng = (df.close > df.open) & (df.open <= df.close.shift(1)) & (df.close >= df.open.shift(1))
    entry = prev_red & eng & (df.close > s)
    pos = np.zeros(len(df), dtype=int)
    ent = entry.fillna(False).values
    left = 0
    for i in range(len(df)):
        if left == 0 and ent[i]:
            left = hold
        if left > 0:
            pos[i] = 1
            left -= 1
    return pd.Series(pos, index=df.index)


def volume_breakout(df, n=20, vol_mult=1.5, exit_n=10):  # ruptura confirmada por volumen
    hi, _ = I.donchian(df, n)
    _, lo = I.donchian(df, exit_n)
    vol_ok = df.volume > df.volume.rolling(n).mean() * vol_mult
    return _hold((df.close > hi.shift(1)) & vol_ok, df.close < lo.shift(1))


def bollinger_squeeze(df, n=20, k=2.0, sq=0.06):  # expansión tras compresión, al alza
    m, up, lo = I.bollinger(df.close, n, k)
    width = (up - lo) / m
    squeeze = width.rolling(n).min() < sq
    return _hold(squeeze.shift(1) & (df.close > up), df.close < m)


def macd_cross(df, fast=12, slow=26, sig=9, trend=100):  # cruce MACD con filtro de tendencia
    macd = I.ema(df.close, fast) - I.ema(df.close, slow)
    signal = I.ema(macd, sig)
    s = I.ema(df.close, trend)
    return _hold((macd > signal) & (macd.shift(1) <= signal.shift(1)) & (df.close > s), macd < signal)


PATTERNS = {
    "v1 trend_ema (referencia)": (trend_ema, dict(fast=[20, 30], slow=[60, 100], adx_min=[15, 20, 25])),
    "ruptura Donchian": (breakout, dict(n=[20, 40, 55], exit_n=[10, 20])),
    "retroceso a la media": (pullback, dict(fast=[20], slow=[60, 100], dip=[0.97, 0.985])),
    "RSI bajo en tendencia": (rsi_dip, dict(slow=[100], low=[30, 35, 40], high=[55, 60])),
    "momentum (ROC)": (momentum, dict(n=[20, 30, 60], exit_n=[10, 20])),
    "medias alineadas": (ma_ribbon, dict(n1=[8, 10], n2=[20, 30], n3=[50, 100])),
    "vela envolvente": (engulfing, dict(slow=[60, 100], hold=[5, 10, 20])),
    "ruptura + volumen": (volume_breakout, dict(n=[20, 40], vol_mult=[1.3, 1.5, 2.0])),
    "compresión Bollinger": (bollinger_squeeze, dict(n=[20], sq=[0.05, 0.08, 0.12])),
    "cruce MACD filtrado": (macd_cross, dict(trend=[60, 100, 200])),
}


def load(sym, tf):
    if tf == "1d":
        d = data.resample(data.load(sym, "1h").reset_index(drop=True), "1d")
        d["dt"] = pd.to_datetime(d["timestamp"], unit="ms", utc=True)
        return d.set_index("dt")
    return data.load(sym, tf)


def main():
    rows = []
    for tf in ("1d", "4h"):
        dfs = {s: load(s, tf) for s in ("BTC", "ETH")}
        mid = dfs["BTC"].index[0] + pd.Timedelta(days=365)
        for name, (fn, grid) in PATTERNS.items():
            keys = list(grid)
            for vals in itertools.product(*(grid[k] for k in keys)):
                p = dict(zip(keys, vals))
                eqs, n_tr, eqs_oos = [], 0, []
                for s, df in dfs.items():
                    sig = fn(df, **p)
                    r = backtest.run(df, sig, strategy=name, symbol=s, timeframe=tf, params=p, stop_atr=2.0, capital=50)
                    eqs.append(r.equity)
                    n_tr += len(r.trades)
                    eqs_oos.append(r.equity[r.equity.index >= mid])
                eq = pd.concat(eqs, axis=1).ffill().sum(axis=1)
                oos = pd.concat(eqs_oos, axis=1).ffill().sum(axis=1)
                dr = eq.resample("1D").last().pct_change().dropna()
                sharpe = dr.mean() / dr.std() * np.sqrt(365) if dr.std() > 0 else 0
                rows.append(dict(pattern=name, tf=tf, params=str(p), ret=eq.iloc[-1] / eq.iloc[0] - 1,
                                 dd=((eq - eq.cummax()) / eq.cummax()).min(), sharpe=sharpe, trades=n_tr,
                                 ret_y2=oos.iloc[-1] / oos.iloc[0] - 1))
            print(f"{tf} {name}: ok")

    df = pd.DataFrame(rows)
    df.to_csv(Path(__file__).resolve().parent.parent / "results" / "pattern_study.csv", index=False)
    g = df.groupby(["tf", "pattern"]).agg(
        variantes=("ret", "size"), ret_med=("ret", "median"), dd_med=("dd", "median"), sharpe_med=("sharpe", "median"),
        trades_med=("trades", "median"), pct_pos=("ret", lambda s: (s > 0).mean()), ret_y2_med=("ret_y2", "median"),
        pct_pos_y2=("ret_y2", lambda s: (s > 0).mean()))
    g = g.sort_values(["tf", "sharpe_med"], ascending=[True, False])
    out = g.copy()
    for c in ("ret_med", "dd_med", "ret_y2_med", "pct_pos", "pct_pos_y2"):
        out[c] = (out[c] * 100).map("{:+.0f}%".format)
    out["sharpe_med"] = out["sharpe_med"].map("{:.2f}".format)
    print("\n=== Estudio de patrones — BTC+ETH, solo largos, fees Uniswap, 2 años (medianas sobre variantes) ===")
    print(out.to_string())
    (Path(__file__).resolve().parent.parent / "results" / "pattern_study.txt").write_text(out.to_string())


if __name__ == "__main__":
    main()
