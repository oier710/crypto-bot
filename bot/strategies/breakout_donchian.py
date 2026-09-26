"""Ruptura de canal Donchian (estilo "turtle").

Reglas:
- Largo  si close > máximo de las últimas `entry` velas.
- Corto  si close < mínimo de las últimas `entry` velas.
- Salir  si el precio cruza el canal opuesto más corto (`exit` velas).

Hipótesis: los movimientos grandes en cripto empiezan con rupturas de rango.
Pocas operaciones, ratio ganadoras bajo, ganadoras grandes. Sensible a fees si
`entry` es demasiado corto.
"""
import numpy as np
import pandas as pd
from ..indicators import donchian

NAME = "breakout_donchian"
DEFAULT_PARAMS = dict(entry=40, exit=20, allow_short=True)
PARAM_GRID = dict(entry=[20, 40, 55], exit=[10, 20])


def signals(df: pd.DataFrame, entry=40, exit=20, allow_short=True) -> pd.Series:
    hi_e, lo_e = donchian(df, entry)
    hi_x, lo_x = donchian(df, exit)
    c = df["close"].values
    # canales desplazados una vela: la ruptura se compara con el canal ANTERIOR
    hie, loe = hi_e.shift(1).values, lo_e.shift(1).values
    hix, lox = hi_x.shift(1).values, lo_x.shift(1).values
    pos = 0
    out = np.zeros(len(df), dtype=int)
    for i in range(len(df)):
        if np.isnan(hie[i]) or np.isnan(hix[i]):
            continue
        if pos == 0:
            if c[i] > hie[i]:
                pos = 1
            elif allow_short and c[i] < loe[i]:
                pos = -1
        elif pos == 1 and c[i] < lox[i]:
            pos = 0
        elif pos == -1 and c[i] > hix[i]:
            pos = 0
        out[i] = pos
    return pd.Series(out, index=df.index)
