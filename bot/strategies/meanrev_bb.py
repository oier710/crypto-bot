"""Reversión a la media: Bandas de Bollinger + RSI.

Reglas:
- Entrar largo  si close < banda inferior y RSI < rsi_low.
- Entrar corto  si close > banda superior y RSI > rsi_high.
- Salir cuando el precio cruza la media (banda central).

Hipótesis: en rangos, los extremos revierten. Funciona mal en tendencias fuertes,
por eso compite con trend_ema: no esperamos que ambas ganen en el mismo régimen.
"""
import numpy as np
import pandas as pd
from ..indicators import bollinger, rsi

NAME = "meanrev_bb"
DEFAULT_PARAMS = dict(n=20, k=2.0, rsi_n=14, rsi_low=30, rsi_high=70, allow_short=True)
PARAM_GRID = dict(n=[20, 30], k=[2.0, 2.5], rsi_low=[25, 30], rsi_high=[70, 75])


def signals(df: pd.DataFrame, n=20, k=2.0, rsi_n=14, rsi_low=30, rsi_high=70, allow_short=True) -> pd.Series:
    mid, up, lo = bollinger(df["close"], n, k)
    r = rsi(df["close"], rsi_n)
    c = df["close"]
    pos = 0
    out = np.zeros(len(df), dtype=int)
    cv, midv, upv, lov, rv = c.values, mid.values, up.values, lo.values, r.values
    for i in range(len(df)):
        if np.isnan(midv[i]) or np.isnan(rv[i]):
            out[i] = 0
            continue
        if pos == 0:
            if cv[i] < lov[i] and rv[i] < rsi_low:
                pos = 1
            elif allow_short and cv[i] > upv[i] and rv[i] > rsi_high:
                pos = -1
        elif pos == 1 and cv[i] >= midv[i]:
            pos = 0
        elif pos == -1 and cv[i] <= midv[i]:
            pos = 0
        out[i] = pos
    return pd.Series(out, index=df.index)
