"""Registro de estrategias.

Contrato: cada estrategia expone

    signals(df: pd.DataFrame, **params) -> pd.Series

que devuelve, para cada vela, la posición deseada AL CIERRE de esa vela:
    +1 largo, -1 corto, 0 sin posición.
El motor ejecuta el cambio al OPEN de la vela siguiente. Así no se mira el futuro.
"""
from . import trend_ema, meanrev_bb, breakout_donchian

REGISTRY = {
    "trend_ema": trend_ema,
    "meanrev_bb": meanrev_bb,
    "breakout_donchian": breakout_donchian,
}
