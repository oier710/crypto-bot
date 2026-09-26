"""Análisis v1: benchmark buy&hold, timeframe diario, long-only, barrido de parámetros.

    python scripts/analyze_v1.py
"""
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot import backtest, data, report
from bot.config import UNIVERSE
from bot.strategies import trend_ema, breakout_donchian

pd.set_option("display.width", 250)


def load_all():
    out = {}
    for sym in UNIVERSE.symbols:
        out[(sym, "4h")] = data.load(sym, "4h")
        d1 = data.resample(data.load(sym, "1h").reset_index(drop=True), "1d")
        d1["dt"] = pd.to_datetime(d1["timestamp"], unit="ms", utc=True)
        out[(sym, "1d")] = d1.set_index("dt")
    return out


def main():
    dfs = load_all()

    print("=== BENCHMARK buy & hold (mismo periodo) ===")
    for sym in UNIVERSE.symbols:
        df = dfs[(sym, "4h")]
        bh = df.close.iloc[-1] / df.close.iloc[0] - 1
        peak = df.close.cummax()
        dd = ((df.close - peak) / peak).min()
        print(f"  {sym}: {bh:+.1%}   max drawdown {dd:+.1%}   ({df.index[0].date()} -> {df.index[-1].date()})")

    metrics = []
    for (sym, tf), df in dfs.items():
        for mod in (trend_ema, breakout_donchian):
            keys = list(mod.PARAM_GRID)
            for vals in itertools.product(*(mod.PARAM_GRID[k] for k in keys)):
                for allow_short in (True, False):
                    for stop in (None, 2.0, 3.0):
                        p = dict(mod.DEFAULT_PARAMS)
                        p.update(dict(zip(keys, vals)))
                        p["allow_short"] = allow_short
                        sig = mod.signals(df, **p)
                        r = backtest.run(df, sig, strategy=mod.NAME, symbol=sym, timeframe=tf, params=p, stop_atr=stop)
                        m = r.metrics()
                        m["stop_atr"] = stop
                        m["long_only"] = not allow_short
                        metrics.append(m)
        print(f"{sym} {tf}: {len(metrics)} combinaciones acumuladas")

    df = pd.DataFrame(metrics)
    df["PASS"] = df.apply(report.passes, axis=1)
    df.to_csv(report.RESULTS_DIR / "analyze_v1.csv", index=False)

    cols = ["strategy", "symbol", "timeframe", "long_only", "stop_atr", "trades", "net_return_pct", "max_drawdown_pct",
            "sharpe", "profit_factor", "win_rate", "fees_pct_of_capital", "PASS", "params"]
    print(f"\n=== PASAN: {int(df.PASS.sum())} / {len(df)} ===")
    print(report.fmt(df[df.PASS].sort_values("sharpe", ascending=False)[cols].head(25)) if df.PASS.any() else "ninguna")

    print("\n=== TOP 15 por Sharpe (pasen o no) ===")
    print(report.fmt(df.sort_values("sharpe", ascending=False)[cols].head(15)))

    print("\n=== Medias por grupo (robustez: ¿el grupo entero es positivo o solo un punto?) ===")
    g = df.groupby(["strategy", "timeframe", "long_only"]).agg(
        n=("sharpe", "size"), sharpe_med=("sharpe", "median"), ret_med=("net_return_pct", "median"),
        dd_med=("max_drawdown_pct", "median"), pf_med=("profit_factor", "median"), pct_positive=("net_return_pct", lambda s: (s > 0).mean()))
    print(g.round(3).to_string())


if __name__ == "__main__":
    main()
