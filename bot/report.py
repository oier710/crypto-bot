"""Tablas y gráficos de resultados."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import ACCEPTANCE

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def passes(m: dict) -> bool:
    return (m["trades"] >= ACCEPTANCE["min_trades"]
            and m["profit_factor"] >= ACCEPTANCE["min_profit_factor"]
            and m["max_drawdown_pct"] >= -ACCEPTANCE["max_drawdown_pct"]
            and m["net_return_pct"] >= ACCEPTANCE["min_net_return_pct"]
            and m["sharpe"] >= ACCEPTANCE["min_sharpe"])


def summary_table(metrics: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(metrics)
    df["PASS"] = df.apply(passes, axis=1)
    cols = ["strategy", "symbol", "timeframe", "trades", "net_return_pct", "cagr_pct", "max_drawdown_pct",
            "sharpe", "profit_factor", "win_rate", "avg_trade_pct", "fees_pct_of_capital", "PASS", "params"]
    return df[cols].sort_values(["PASS", "sharpe"], ascending=[False, False]).reset_index(drop=True)


def fmt(df: pd.DataFrame) -> str:
    d = df.copy()
    for col in ["net_return_pct", "cagr_pct", "max_drawdown_pct", "win_rate", "avg_trade_pct", "fees_pct_of_capital"]:
        if col in d:
            d[col] = (d[col] * 100).map("{:+.1f}%".format)
    d["sharpe"] = d["sharpe"].map("{:.2f}".format)
    d["profit_factor"] = d["profit_factor"].map(lambda x: "inf" if x == float("inf") else f"{x:.2f}")
    return d.to_string(index=False)


def plot_equity(results, path: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(11, 5))
    for r in results:
        ax.plot(r.equity.index, r.equity.values, label=f"{r.strategy} {r.symbol} {r.timeframe}", lw=1)
    ax.axhline(results[0].equity.iloc[0], color="grey", ls="--", lw=0.8)
    ax.set_ylabel("Equity (USD)")
    ax.legend(fontsize=7, ncol=2)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
