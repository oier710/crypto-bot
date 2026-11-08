"""Investigación nov-2026 (4ª parte): ¿activos del MUNDO REAL tokenizados (oro, acciones, índices)?

Pregunta de Oier (2026-11-08): "invertir en algún activo tokenizado del mundo real, donde sea más predecible: una
acción, un índice... no limitarnos a crypto".
Dos partes:
  1. ESCANEO (DefiLlama, sin claves): qué activos del mundo real tokenizados tienen liquidez en DEX y/o se pueden pedir
     prestados (para cortos) en Arbitrum, Base, Optimism y Ethereum: oro (PAXG, XAUT), acciones/índices tokenizados
     (xStocks, Backed, Ondo...), bonos del Tesoro (USDY, OUSG, BUIDL, USTB...).
  2. BACKTEST de la estrategia actual (señal diaria EMA 20/60 + ADX, gestión v3) sobre:
       - ORO: PAXG-USD de Coinbase (cotiza 24/7 como el token).
       - S&P 500: SPY horario de Yahoo (2 años; solo horas de mercado, aproximación del token).
     Solo largo y largo+corto; costes 0.30 %/lado (pools pequeños) + gas; cortos +0.10 % e interés 5 %/año.
     Y la cartera v5 actual frente a v5 + oro (15 % del capital) para ver si suma o resta.
Salida: results/research_2026_11d.txt (+ .csv del escaneo)
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import requests

import research_2026_10 as base  # noqa: E402
import research_2026_11c as m11c  # noqa: E402
from bot.strategies import trend_ema  # noqa: E402

RESULTS = base.RESULTS
RWA_RE = re.compile(r"(PAXG|XAUT|XAUT0|XAU|GOLD|KAU|CACHE|SPY|QQQ|CSPX|IVV|VOO|TSLA|AAPL|NVDA|MSFT|AMZN|GOOG|META|COIN|"
                    r"MSTR|HOOD|IB01|IBTA|TBILL|USTB|USDY|OUSG|BUIDL|BENJI|STBT|USYC|WTGXX|TBIL|SPXA|XSTOCK)", re.I)
CHAINS = ("Arbitrum", "Base", "Optimism", "Ethereum")
UA = {"User-Agent": "crypto-bot-research"}


# ---------------------------------------------------------------- 1. escaneo
def scan(lines):
    lines.append("## 1. ESCANEO de activos del mundo real tokenizados (DefiLlama)")
    try:
        pools = pd.DataFrame(requests.get("https://yields.llama.fi/pools", timeout=90, headers=UA).json()["data"])
        lend = pd.DataFrame(requests.get("https://yields.llama.fi/lendBorrow", timeout=90, headers=UA).json())
    except Exception as e:  # noqa: BLE001
        lines.append(f"(no se pudo leer DefiLlama: {type(e).__name__}: {e})")
        return
    df = pools[pools.chain.isin(CHAINS) & pools.symbol.astype(str).str.contains(RWA_RE)].copy()
    df = df.merge(lend, on="pool", how="left")
    for c in ("tvlUsd", "totalSupplyUsd", "totalBorrowUsd", "apyBaseBorrow"):
        if c not in df:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["avail_usd"] = (df.totalSupplyUsd - df.totalBorrowUsd.fillna(0)).clip(lower=0)
    df["borrowable"] = df.get("borrowable", pd.Series(False, index=df.index)).fillna(False)
    df = df.sort_values("tvlUsd", ascending=False)
    df[["chain", "project", "symbol", "tvlUsd", "totalSupplyUsd", "totalBorrowUsd", "avail_usd", "apyBaseBorrow",
        "borrowable", "poolMeta"]].to_csv(RESULTS / "research_2026_11d_scan.csv", index=False)
    for ch in CHAINS:
        sub = df[df.chain == ch]
        lines.append(f"\n### {ch}: {len(sub)} mercados con activos del mundo real")
        if sub.empty:
            continue
        lines.append(f"{'protocolo':<24} {'símbolo':<26} {'TVL $':>14} {'prestable':>9} {'disp. préstamo $':>17} {'interés':>8}")
        for _, r in sub.head(25).iterrows():
            lines.append(f"{str(r.project)[:24]:<24} {str(r.symbol)[:26]:<26} {r.tvlUsd or 0:>14,.0f} "
                         f"{'sí' if r.borrowable and (r.avail_usd or 0) > 0 else 'no':>9} {r.avail_usd if r.avail_usd == r.avail_usd else 0:>17,.0f} "
                         f"{(r.apyBaseBorrow if r.apyBaseBorrow == r.apyBaseBorrow else 0):>7.2f}%")
    lines.append("")


# ---------------------------------------------------------------- 2. datos
def load_paxg():
    from bot.data import fetch_coinbase
    df = fetch_coinbase("PAXG", "1h", base.DAYS, verbose=False)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("dt")[["open", "high", "low", "close", "volume"]]


def load_yahoo(ticker):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                     params={"interval": "1h", "range": "730d"}, timeout=60,
                     headers={"User-Agent": "Mozilla/5.0 (crypto-bot-research)"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).floor("h")
    df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")}, index=idx).dropna()
    return df[~df.index.duplicated(keep="last")]


# ---------------------------------------------------------------- 3. backtest
def sim(h, d, capital, cost, apr, allow_short):
    old = m11c.trend_ema
    if not allow_short:
        m11c.trend_ema = types.SimpleNamespace(
            signals=lambda df, **k: trend_ema.signals(df, **k).clip(lower=0))
    try:
        e, s = m11c.simulate(h, d, m11c.VARIANTS[0], capital, cost, apr)
    finally:
        m11c.trend_ema = old
    return e, s


def portfolio(data, spec, start, end):
    """spec: {sym: (peso, coste, interés, permite_corto)}"""
    eqs, trades, fees, per = [], [], 0.0, {}
    for sym, (w, cost, apr, short) in spec.items():
        h, d = data[sym]
        hh = h[(h.index >= start) & (h.index < end)]
        if hh.empty:
            continue
        e, s = sim(hh, d, base.CAPITAL * w, cost, apr, short)
        eqs.append(e)
        trades += s.trades
        fees += s.fees
        per[sym] = float(e.iloc[-1] - e.iloc[0])
    eq = pd.concat(eqs, axis=1).sort_index().ffill().bfill().sum(axis=1)
    r = base.metrics(eq, trades, fees)
    r["per"] = per
    return r, eq


def main():
    lines = ["# Investigación nov-2026 (4ª parte) — activos del mundo real tokenizados (idea de Oier)", ""]
    scan(lines)

    lines.append("## 2. BACKTEST de la estrategia actual sobre oro y S&P 500")
    data = {}
    for sym in ("BTC", "ETH", "ARB"):
        h = base.load_hourly(sym, False)
        data[sym] = (h, base.daily_from_hourly(h))
    notes = []
    try:
        h = load_paxg()
        data["PAXG"] = (h, base.daily_from_hourly(h))
        notes.append(f"PAXG (oro) Coinbase 1h: {h.index[0].date()} -> {h.index[-1].date()}, {len(h)} horas")
    except Exception as e:  # noqa: BLE001
        notes.append(f"PAXG no disponible en Coinbase: {type(e).__name__}: {e}")
    try:
        h = load_yahoo("SPY")
        data["SPY"] = (h, base.daily_from_hourly(h))
        notes.append(f"SPY (S&P 500) Yahoo 1h: {h.index[0].date()} -> {h.index[-1].date()}, {len(h)} horas (solo horario de mercado)")
    except Exception as e:  # noqa: BLE001
        notes.append(f"SPY no disponible en Yahoo: {type(e).__name__}: {e}")
    try:
        h = load_yahoo("GLD")
        data["GLD"] = (h, base.daily_from_hourly(h))
        notes.append(f"GLD (oro, ETF) Yahoo 1h: {h.index[0].date()} -> {h.index[-1].date()} (comprobación del oro)")
    except Exception as e:  # noqa: BLE001
        notes.append(f"GLD no disponible: {type(e).__name__}: {e}")
    lines += notes + [""]

    t1 = min(data[s][0].index[-1] for s in data)
    rows = []

    def block(title, start, end, specs):
        lines.append(f"### {title} ({start.date()} -> {min(end, t1).date()})")
        lines.append(base.HEADER + "  aporte por activo")
        for name, spec in specs:
            if any(s not in data for s in spec):
                lines.append(f"{name}: sin datos")
                continue
            r, _ = portfolio(data, spec, start, end)
            r["variant"] = name
            lines.append(base.fmt(r) + "  " + " ".join(f"{k} {v:+.2f}$" for k, v in r["per"].items()))
            rows.append({k: v for k, v in r.items() if k != "per"} | {"period": title})
        lines.append("")

    RWA_COST, RWA_APR = 0.0030, 0.05
    v5 = {"BTC": (0.425, 0.001, 0.04, True), "ETH": (0.425, 0.001, 0.04, True), "ARB": (0.15, 0.001, 0.07, True)}
    gold_specs, spy_specs, mix_specs = [], [], [("v5 actual (BTC/ETH/ARB)", v5)]
    for g in ("PAXG", "GLD"):
        gold_specs += [(f"{g} solo largo", {g: (1.0, RWA_COST, RWA_APR, False)}),
                       (f"{g} largo y corto", {g: (1.0, RWA_COST, RWA_APR, True)})]
    spy_specs += [("SPY solo largo", {"SPY": (1.0, RWA_COST, RWA_APR, False)}),
                  ("SPY largo y corto", {"SPY": (1.0, RWA_COST, RWA_APR, True)})]
    for w in (0.15, 0.25):
        k = 1 - w
        mix = {s: (wt * k, c, a, sh) for s, (wt, c, a, sh) in v5.items()}
        mix_specs += [(f"v5 + oro {w:.0%} solo largo", mix | {"PAXG": (w, RWA_COST, RWA_APR, False)}),
                      (f"v5 + oro {w:.0%} largo y corto", mix | {"PAXG": (w, RWA_COST, RWA_APR, True)})]
    for w in (0.15,):
        k = 1 - w
        mix = {s: (wt * k, c, a, sh) for s, (wt, c, a, sh) in v5.items()}
        mix_specs += [(f"v5 + S&P {w:.0%} solo largo", mix | {"SPY": (w, RWA_COST, RWA_APR, False)})]

    # periodo común largo para oro (PAXG) y para la cartera; corto (2 años) para SPY
    starts = [data[s][0].index[0] for s in ("BTC", "ETH", "ARB", "PAXG") if s in data]
    s_long = max(starts) + pd.Timedelta(days=90)
    end = t1 + pd.Timedelta(hours=1)
    mid = s_long + (t1 - s_long) / 2
    block("ORO — periodo completo", s_long, end, gold_specs)
    block("CARTERA — periodo completo", s_long, end, [x for x in mix_specs if "S&P" not in x[0]])
    block("CARTERA — 1ª mitad", s_long, mid, [x for x in mix_specs if "S&P" not in x[0]])
    block("CARTERA — 2ª mitad", mid, end, [x for x in mix_specs if "S&P" not in x[0]])
    if "SPY" in data:
        s_spy = data["SPY"][0].index[0] + pd.Timedelta(days=90)
        block("S&P 500 y cartera — últimos ~21 meses", s_spy, end, spy_specs + [mix_specs[0], mix_specs[-1]])

    # correlación diaria entre la estrategia en oro y la cartera v5
    if "PAXG" in data:
        _, e_v5 = portfolio(data, v5, s_long, end)
        _, e_g = portfolio(data, {"PAXG": (1.0, RWA_COST, RWA_APR, True)}, s_long, end)
        c = pd.concat([e_v5.resample("1D").last().pct_change(), e_g.resample("1D").last().pct_change()], axis=1).dropna()
        lines.append(f"Correlación diaria estrategia en oro vs cartera v5: {c.corr().iloc[0, 1]:+.2f} "
                     "(cerca de 0 = diversifica; cerca de 1 = se mueven igual)")
        h, _ = data["PAXG"]
        vol_g = h.close.resample("1D").last().pct_change().std() * np.sqrt(365)
        vol_b = data["BTC"][0].close.resample("1D").last().pct_change().std() * np.sqrt(365)
        lines.append(f"Volatilidad anual: oro {vol_g:.0%} vs BTC {vol_b:.0%}")
    txt = "\n".join(lines)
    (RESULTS / "research_2026_11d.txt").write_text(txt)
    pd.DataFrame(rows).to_csv(RESULTS / "research_2026_11d.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
