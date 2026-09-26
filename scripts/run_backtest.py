"""Backtest de todas las estrategias sobre todos los activos y timeframes.

    python scripts/run_backtest.py            # parámetros por defecto
    python scripts/run_backtest.py --grid     # además, barrido de parámetros (más lento)

Escribe results/summary.csv, results/summary.txt y results/equity.png
"""
import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import backtest, data, report
from bot.config import UNIVERSE
from bot.strategies import REGISTRY


def param_combos(mod):
    keys = list(mod.PARAM_GRID)
    for vals in itertools.product(*(mod.PARAM_GRID[k] for k in keys)):
        p = dict(mod.DEFAULT_PARAMS)
        p.update(dict(zip(keys, vals)))
        yield p


def main(grid: bool):
    metrics, results = [], []
    for sym in UNIVERSE.symbols:
        for tf in UNIVERSE.timeframes:
            try:
                df = data.load(sym, tf)
            except FileNotFoundError as e:
                print(e)
                return
            for name, mod in REGISTRY.items():
                combos = list(param_combos(mod)) if grid else [dict(mod.DEFAULT_PARAMS)]
                for params in combos:
                    sig = mod.signals(df, **params)
                    r = backtest.run(df, sig, strategy=name, symbol=sym, timeframe=tf, params=params)
                    metrics.append(r.metrics())
                    if not grid:
                        results.append(r)
            print(f"{sym} {tf}: hecho")

    table = report.summary_table(metrics)
    table.to_csv(report.RESULTS_DIR / ("summary_grid.csv" if grid else "summary.csv"), index=False)
    txt = report.fmt(table.head(40))
    (report.RESULTS_DIR / ("summary_grid.txt" if grid else "summary.txt")).write_text(txt)
    print("\n" + txt)
    if results:
        report.plot_equity(results, report.RESULTS_DIR / "equity.png")
    print(f"\nEstrategias que PASAN los umbrales de aceptación: {int(table.PASS.sum())} / {len(table)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", action="store_true")
    main(ap.parse_args().grid)
