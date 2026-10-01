"""Informe de contabilidad del bot.

    python scripts/report.py            # imprime el informe y escribe results/report.md + results/equity_curve.png

Fuentes: logs/equity.csv (valor de cartera por ejecución), logs/onchain_trades.csv (operaciones).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LOGS, RESULTS = ROOT / "logs", ROOT / "results"
RESULTS.mkdir(exist_ok=True)
CAPITAL_INICIAL = 115.75   # 2026-09-26 17:44, tras el puente a Arbitrum
FECHA_INICIO = "2026-09-26"


def main():
    EQ_COLS = ["time", "equity_usd", "usdc", "eth_usd", "btc_usd", "eth_qty", "btc_qty", "eth_price", "btc_price", "gas_eth",
               "ausdc", "debt_eth_usd", "debt_btc_usd", "hf"]
    eq = (pd.read_csv(LOGS / "equity.csv", header=None, skiprows=1, names=EQ_COLS, parse_dates=["time"])
          if (LOGS / "equity.csv").exists() else pd.DataFrame())
    tr = pd.read_csv(LOGS / "onchain_trades.csv", parse_dates=["time"]) if (LOGS / "onchain_trades.csv").exists() else pd.DataFrame()
    tr = tr[~tr.reason.astype(str).str.contains("dry-run")] if len(tr) else tr

    lines = [f"# Informe crypto-bot — {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC", ""]
    if len(eq):
        last = eq.iloc[-1]
        equity = float(last.equity_usd)
        pnl = equity - CAPITAL_INICIAL
        peak = eq.equity_usd.cummax()
        dd = float(((eq.equity_usd - peak) / peak).min())
        days = max((eq.time.iloc[-1] - pd.Timestamp(FECHA_INICIO, tz="UTC")).days, 1)
        lines += [
            "## Resumen",
            f"- Capital inicial: **{CAPITAL_INICIAL:.2f} $** ({FECHA_INICIO})",
            f"- Valor actual: **{equity:.2f} $**  →  P&L **{pnl:+.2f} $ ({pnl / CAPITAL_INICIAL:+.2%})** en {days} días",
            f"- Máximo alcanzado: {peak.iloc[-1]:.2f} $ · Caída máxima desde máximo: {dd:+.2%}",
            f"- Cartera: USDC {last.usdc:.2f} $ · ETH neto {last.eth_usd:.2f} $ · BTC neto {last.btc_usd:.2f} $ · gas {last.gas_eth:.4f} ETH"
            + (f" · en Aave {last.ausdc:.2f} $ · deuda ETH {last.debt_eth_usd:.2f} $ / BTC {last.debt_btc_usd:.2f} $ · HF {('∞' if last.hf > 1e6 else f'{last.hf:.2f}')}" if pd.notna(last.get("hf")) else ""),
            f"- Precios: ETH {last.eth_price:,.0f} $ · BTC {last.btc_price:,.0f} $",
            "",
        ]
        # semana a semana
        w = eq.set_index("time").equity_usd.resample("W-SAT").last().dropna()
        if len(w) > 1:
            lines += ["## Por semana", "| Semana (cierre sábado) | Valor | Variación |", "|---|---|---|"]
            prev = CAPITAL_INICIAL
            for t, v in w.items():
                lines.append(f"| {t:%Y-%m-%d} | {v:.2f} $ | {v - prev:+.2f} $ ({(v / prev - 1):+.2%}) |")
                prev = v
            lines.append("")
    else:
        lines += ["(sin datos de equity todavía)", ""]

    if len(tr):
        lines += ["## Operaciones", f"- Total: {len(tr)} (compras {int((tr.side == 'BUY').sum())}, ventas {int((tr.side == 'SELL').sum())}, cortos {int((tr.side == 'SHORT').sum())}, cierres de corto {int((tr.side == 'COVER').sum())})",
                  f"- Volumen operado: {tr.usd.sum():.2f} $ · coste estimado fees+slippage (~0.1 %): {tr.usd.sum() * 0.001:.2f} $", "",
                  "| Fecha | Activo | Lado | USD | Precio | Motivo |", "|---|---|---|---|---|---|"]
        for _, r in tr.iterrows():
            lines.append(f"| {r.time:%Y-%m-%d %H:%M} | {r.coin} | {r.side} | {r.usd:.2f} | {r.price:,.2f} | {r.reason} |")
        lines.append("")
    else:
        lines += ["## Operaciones", "(ninguna)", ""]

    md = "\n".join(lines)
    (RESULTS / "report.md").write_text(md)
    print(md)

    if len(eq) > 1:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(eq.time, eq.equity_usd, lw=1.2)
        ax.axhline(CAPITAL_INICIAL, color="grey", ls="--", lw=0.8, label="capital inicial")
        ax.set_ylabel("USD")
        ax.grid(alpha=0.3)
        ax.legend()
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(RESULTS / "equity_curve.png", dpi=120)
        print(f"\nGráfico: {RESULTS / 'equity_curve.png'}")


if __name__ == "__main__":
    main()
