"""Motor de backtesting.

Reglas del motor (deliberadamente simples y conservadoras):
- La señal se calcula al cierre de la vela t; se ejecuta al OPEN de t+1.
- Cada cambio de posición paga fee taker + slippage por lado.
- Tamaño: fracción fija del capital, sin apalancamiento (config.RISK).
- Stop-loss opcional por ATR: si la vela toca el stop, se cierra a ese precio
  (más slippage) dentro de la misma vela. Conservador: si open ya está más allá
  del stop, se sale al open.
- Equity marcada a mercado cada vela.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .config import FEES, RISK
from .indicators import atr


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: int              # +1 / -1
    entry_price: float
    exit_price: float
    size_usd: float
    pnl_usd: float         # neto de fees
    fees_usd: float
    ret_pct: float         # sobre size_usd
    bars: int
    exit_reason: str       # signal | stop


@dataclass
class Result:
    strategy: str
    symbol: str
    timeframe: str
    params: dict
    equity: pd.Series
    trades: list

    # --- métricas -------------------------------------------------------
    def metrics(self) -> dict:
        eq = self.equity
        tr = pd.DataFrame([asdict(t) for t in self.trades]) if self.trades else pd.DataFrame()
        n = len(tr)
        out = dict(strategy=self.strategy, symbol=self.symbol, timeframe=self.timeframe,
                   params=str(self.params), trades=n)
        out["net_return_pct"] = eq.iloc[-1] / eq.iloc[0] - 1
        peak = eq.cummax()
        out["max_drawdown_pct"] = float(((eq - peak) / peak).min())
        # sharpe anualizado sobre retornos diarios
        daily = eq.resample("1D").last().dropna().pct_change().dropna()
        out["sharpe"] = float(daily.mean() / daily.std() * np.sqrt(365)) if daily.std() > 0 else 0.0
        days = max((eq.index[-1] - eq.index[0]).days, 1)
        out["cagr_pct"] = (eq.iloc[-1] / eq.iloc[0]) ** (365 / days) - 1
        if n:
            gp = tr.loc[tr.pnl_usd > 0, "pnl_usd"].sum()
            gl = -tr.loc[tr.pnl_usd < 0, "pnl_usd"].sum()
            out["profit_factor"] = float(gp / gl) if gl > 0 else float("inf")
            out["win_rate"] = float((tr.pnl_usd > 0).mean())
            out["avg_trade_pct"] = float(tr.ret_pct.mean())
            out["total_fees_usd"] = float(tr.fees_usd.sum())
            out["fees_pct_of_capital"] = out["total_fees_usd"] / eq.iloc[0]
            out["avg_bars"] = float(tr.bars.mean())
        else:
            out.update(profit_factor=0, win_rate=0, avg_trade_pct=0, total_fees_usd=0,
                       fees_pct_of_capital=0, avg_bars=0)
        return out


def run(df: pd.DataFrame, signal: pd.Series, *, strategy: str, symbol: str, timeframe: str,
        params: dict, stop_atr: float | None = 2.0, atr_n: int = 14,
        capital: float = RISK.initial_capital) -> Result:
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    a = atr(df, atr_n).values
    sig = signal.shift(1).fillna(0).astype(int).values  # ejecutar en la vela siguiente
    idx = df.index

    cost_side = FEES.taker + FEES.slippage
    equity = np.empty(len(df))
    cash = capital
    pos = 0
    qty = 0.0
    entry_px = 0.0
    entry_i = 0
    stop_px = None
    fees_acc = 0.0
    trades: list[Trade] = []

    def close_position(i: int, px: float, reason: str):
        nonlocal cash, pos, qty, entry_px, stop_px, fees_acc
        fee = abs(qty) * px * cost_side
        pnl = qty * (px - entry_px) - fee - fees_acc
        cash += qty * (px - entry_px) - fee
        size = abs(qty) * entry_px
        trades.append(Trade(idx[entry_i], idx[i], pos, entry_px, px, size, pnl, fee + fees_acc,
                            pnl / size, i - entry_i, reason))
        pos, qty, stop_px, fees_acc = 0, 0.0, None, 0.0

    def open_position(i: int, side: int, px: float):
        nonlocal cash, pos, qty, entry_px, entry_i, stop_px, fees_acc
        size_usd = cash * RISK.position_fraction * min(RISK.max_leverage, 1.0)
        if size_usd < 10:  # Hyperliquid: orden mínima ~10 USD
            return
        fee = size_usd * cost_side
        cash -= fee
        fees_acc = fee
        qty = side * size_usd / px
        pos, entry_px, entry_i = side, px, i
        if stop_atr and not np.isnan(a[i]):
            stop_px = px - side * stop_atr * a[i]

    for i in range(len(df)):
        target = sig[i]
        # 1) cambio de posición al open
        if target != pos:
            if pos != 0:
                close_position(i, o[i], "signal")
            if target != 0:
                open_position(i, target, o[i])
        # 2) stop intra-vela
        if pos != 0 and stop_px is not None:
            hit = (pos == 1 and l[i] <= stop_px) or (pos == -1 and h[i] >= stop_px)
            if hit:
                px = min(o[i], stop_px) if pos == 1 else max(o[i], stop_px)
                close_position(i, px, "stop")
        # 3) marcar a mercado
        equity[i] = cash + (qty * (c[i] - entry_px) if pos != 0 else 0.0)

    return Result(strategy, symbol, timeframe, params, pd.Series(equity, index=idx, name="equity"), trades)
