"""Seguimiento de tendencia: cruce de EMAs filtrado por ADX.

Reglas:
- Largo  si EMA rápida > EMA lenta  y ADX > umbral (hay tendencia).
- Corto  si EMA rápida < EMA lenta  y ADX > umbral.
- Fuera  si ADX < umbral (mercado lateral: no operar, ahorrar fees).

Hipótesis: BTC/ETH/SOL tienen tendencias largas; las EMAs las capturan y el ADX
evita el sierra-dientes lateral, que es donde una cuenta pequeña se desangra en comisiones.
"""
import pandas as pd
from ..indicators import ema, adx

NAME = "trend_ema"
DEFAULT_PARAMS = dict(fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True)
PARAM_GRID = dict(fast=[10, 20, 30], slow=[50, 60, 100], adx_min=[15, 20, 25])


def signals(df: pd.DataFrame, fast=20, slow=60, adx_n=14, adx_min=20, allow_short=True) -> pd.Series:
    f, s = ema(df["close"], fast), ema(df["close"], slow)
    a = adx(df, adx_n)
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[(f > s) & (a > adx_min)] = 1
    if allow_short:
        sig[(f < s) & (a > adx_min)] = -1
    return sig
