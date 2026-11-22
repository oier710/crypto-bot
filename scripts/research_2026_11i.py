"""Investigación nov-2026 (9ª parte): MÁS MONEDAS para el bot (petición de Oier, 2026-11-22).

Oier: "que haya cuantas más opciones de ver oportunidades en el rango de movimiento al alza o a la baja".
Para que una moneda nueva sirva al bot hacen falta tres cosas:
  1. Que se pueda poner en CORTO: alguien la presta en Arbitrum (Dolomite, que ya usamos) con liquidez suficiente.
  2. Que tenga historia de precios en Coinbase (señal y backtest).
  3. Que AÑADIRLA mejore la cartera actual con el sistema de producción, no solo que la moneda gane sola.

Paso 1 — escaneo en vivo de préstamos (DefiLlama, sin claves): mercados de Dolomite en Arbitrum, no estables, no
BTC/ETH, prestables, con ≥ 100.000 $ disponibles. Se añaden los candidatos de siempre (PENDLE, LINK, UNI, GMX, AAVE,
CRV, LDO) aunque el escaneo falle, para no depender de él.
Paso 2 — backtest con el sistema de PRODUCCIÓN (señal diaria EMA 20/60 + ADX, parciales 1.5/3 ATR, stop que persigue
3 ATR desde la parcial 1, reentrada; cartera v5 BTC 42.5 / ETH 42.5 / ARB 15 %, 75 % por posición).
Cada candidata se añade al 10 % y al 15 % del capital (lo demás se reduce en proporción) y se compara con la cartera
actual EN SU MISMO PERIODO (cada moneda tiene su historia en Coinbase).
Costes de las monedas nuevas: 0.35 %/lado (pool de Uniswap 0.30 % + deslizamiento), y prueba de estrés al 0.60 %.
Interés del préstamo en corto: el que da el escaneo (mín. 5 %/año), o 10 %/año si no hay dato.
Criterio (fijado antes de ver resultados, igual que en todos los estudios): con 10 % Y con 15 % gana a la actual en el
periodo completo, en las dos mitades y en ≥ 2 de 3 tercios, sin más caída máxima ni más meses en rojo; y sigue
ganando con costes de estrés. Al final se prueba la cesta de todas las que pasen juntas.
Salida: results/research_2026_11i.txt y .csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import requests

import research_2026_10 as base  # noqa: E402
import research_2026_11c as m11c  # noqa: E402
import research_2026_11g as m11g  # noqa: E402  (simulador con el stop que persigue)

PROD = m11g.Var("producción (persigue 3 ATR desde parcial 1)", k=3.0, after=1)
CUR_W = {"BTC": 0.425, "ETH": 0.425, "ARB": 0.15}
COST = dict(m11c.COST)                 # BTC/ETH/ARB 0.10 %/lado
APR = dict(m11c.APR)
NEW_COST, STRESS_COST = 0.0035, 0.0060
FIXED_CANDIDATES = ["PENDLE", "LINK", "UNI", "GMX", "AAVE", "CRV", "LDO"]
MIN_AVAIL = 100_000
MAJORS = {"WBTC", "CBBTC", "TBTC", "LBTC", "BTC", "WETH", "ETH", "WSTETH", "WEETH", "RETH", "CBETH", "EZETH", "RSETH",
          "WRSETH", "OSETH", "SFRXETH", "FRXETH", "WOETH", "STETH", "ARB"}
SHARES = (0.10, 0.15)

lines: list[str] = []


def say(s=""):
    print(s)
    lines.append(s)


# ------------------------------------------------------------------ paso 1: escaneo de Dolomite
def scan_dolomite() -> pd.DataFrame:
    pools = requests.get("https://yields.llama.fi/pools", timeout=60).json()["data"]
    lb = requests.get("https://yields.llama.fi/lendBorrow", timeout=60).json()
    lbm = {x["pool"]: x for x in lb}
    rows = []
    for p in pools:
        if p.get("chain") != "Arbitrum" or p.get("project") != "dolomite" or p.get("stablecoin"):
            continue
        x = lbm.get(p["pool"])
        if not x:
            continue
        sym = str(p.get("symbol", "")).upper()
        sup = float(x.get("totalSupplyUsd") or 0)
        bor = float(x.get("totalBorrowUsd") or 0)
        cap = x.get("debtCeilingUsd")
        avail = sup - bor
        if cap:
            avail = min(avail, float(cap) - bor)
        apr = (x.get("apyBaseBorrow") or 0) - (x.get("apyRewardBorrow") or 0)
        rows.append({"sym": sym, "suministrado": sup, "prestado": bor, "disponible": avail,
                     "interes_pct": apr, "prestable": bool(x.get("borrowable", True))})
    return pd.DataFrame(rows).sort_values("disponible", ascending=False) if rows else pd.DataFrame()


def coinbase_has(sym: str) -> bool:
    try:
        r = requests.get(f"https://api.exchange.coinbase.com/products/{sym}-USD", timeout=20,
                         headers={"User-Agent": "crypto-bot/0.1"})
        return r.status_code == 200 and r.json().get("status") == "online"
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ paso 2: backtest
def sim(data, sym, start, end, capital, cost=None):
    h, d = data[sym]
    hh = h[(h.index >= start) & (h.index < end)]
    return m11g.sim(hh, d, PROD, capital, COST[sym] if cost is None else cost, APR[sym])


def portfolio(data, weights, start, end, cost_new=None):
    eqs, trades, fees = [], [], 0.0
    for sym, w in weights.items():
        if w <= 0:
            continue
        c = cost_new if (cost_new is not None and sym not in CUR_W) else None
        e, s = sim(data, sym, start, end, base.CAPITAL * w, c)
        eqs.append(e)
        trades += s.trades
        fees += s.fees
    eq = pd.concat(eqs, axis=1).ffill().bfill().sum(axis=1)
    return base.metrics(eq, trades, fees)


def with_coin(sym, share):
    w = {k: v * (1 - share) for k, v in CUR_W.items()}
    w[sym] = share
    return w


def periods(t0, t1):
    start = t0 + pd.Timedelta(days=90)
    end = t1 + pd.Timedelta(hours=1)
    mid = start + (t1 - start) / 2
    th = [start + (t1 - start) * k / 3 for k in range(4)]
    th[-1] = end
    return [("completo", start, end), ("mitad 1", start, mid), ("mitad 2", mid, end),
            ("tercio 1", th[0], th[1]), ("tercio 2", th[1], th[2]), ("tercio 3", th[2], th[3])]


def verdict(data, weights_new, t0, t1, cost_new=None):
    """Compara cartera actual vs nueva en el mismo periodo. Devuelve (pasa, detalle, filas)."""
    res = {}
    for lab, s0, s1 in periods(t0, t1):
        res[lab] = (portfolio(data, CUR_W, s0, s1), portfolio(data, weights_new, s0, s1, cost_new))
    a, b = res["completo"]
    full = b["ret"] > a["ret"]
    halves = all(res[h][1]["ret"] > res[h][0]["ret"] for h in ("mitad 1", "mitad 2"))
    thirds = sum(res[h][1]["ret"] > res[h][0]["ret"] for h in ("tercio 1", "tercio 2", "tercio 3"))
    dd_ok = b["max_dd"] >= a["max_dd"]
    neg_ok = b["neg_months"] <= a["neg_months"]
    ok = full and halves and thirds >= 2 and dd_ok and neg_ok
    det = (f"completo {'sí' if full else 'no'}, mitades {'sí' if halves else 'no'}, tercios {thirds}/3, "
           f"caída {'ok' if dd_ok else 'PEOR'}, meses rojo {'ok' if neg_ok else 'MÁS'}")
    return ok, det, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    args = ap.parse_args()
    say("# Investigación nov-2026 (9ª parte) — más monedas para el bot (sistema de producción)")

    # ---- paso 1
    say("\n## 1. Escaneo de préstamos en Dolomite (Arbitrum) — ¿qué se puede poner en corto?")
    cands = list(FIXED_CANDIDATES)
    scan = pd.DataFrame()
    if not args.synthetic:
        try:
            scan = scan_dolomite()
        except Exception as e:  # noqa: BLE001
            say(f"AVISO: escaneo de DefiLlama fallido ({e}); se usan solo los candidatos fijos.")
    if len(scan):
        show = scan[~scan.sym.isin(MAJORS)].copy()
        say(f"{'moneda':10s} {'suministrado':>14s} {'prestado':>12s} {'disponible':>12s} {'interés':>8s}")
        for _, r in show.head(25).iterrows():
            say(f"{r.sym:10s} {r.suministrado:14,.0f} {r.prestado:12,.0f} {r.disponible:12,.0f} {r.interes_pct:7.2f}%")
        ok = show[(show.disponible >= MIN_AVAIL) & show.prestable]
        for s in ok.sym:
            if s not in cands:
                cands.append(s)
        for _, r in ok.iterrows():
            APR[r.sym] = max(0.05, float(r.interes_pct) / 100)
        not_lendable = [c for c in FIXED_CANDIDATES if c not in set(ok.sym)]
        if not_lendable:
            say(f"Candidatos fijos SIN préstamo suficiente hoy en Dolomite (no se podrían poner en corto): "
                f"{', '.join(not_lendable)} — se prueban igual, pero solo cuentan si Dolomite los presta.")
    for c in cands:
        APR.setdefault(c, 0.10)
        COST[c] = NEW_COST

    # ---- datos
    say("\n## 2. Datos (Coinbase 1h)")
    data = {}
    for sym in ["BTC", "ETH", "ARB"] + cands:
        if args.synthetic:
            src = "BTC" if sym == "BTC" else "ETH"
            h = base.load_hourly(src, True)
            if sym not in ("BTC", "ETH"):
                rng = np.random.default_rng(abs(hash(sym)) % 2**32)
                h = h * (1 + rng.normal(0, 0.002, len(h))[:, None]).clip(0.98, 1.02)
        else:
            if sym not in ("BTC", "ETH", "ARB") and not coinbase_has(sym):
                say(f"  {sym}: no cotiza en Coinbase → fuera")
                continue
            try:
                h = base.load_hourly(sym, False)
            except Exception as e:  # noqa: BLE001
                say(f"  {sym}: no se pudo descargar ({e}) → fuera")
                continue
        if len(h) < 24 * 400:
            say(f"  {sym}: solo {len(h) // 24} días de historia → fuera (mínimo 400)")
            continue
        data[sym] = (h, base.daily_from_hourly(h))
        say(f"  {sym}: {h.index[0].date()} → {h.index[-1].date()}")
    cands = [c for c in cands if c in data]

    # ---- paso 2: cada candidata
    say("\n## 3. Añadir cada moneda a la cartera actual (mismo periodo para las dos)")
    say("Formato: retorno / caída máx. / meses en rojo. «estrés» = costes de la moneda nueva al 0.60 %/lado.")
    rows, passing = [], []
    for c in cands:
        t0 = max(data[s][0].index[0] for s in ["BTC", "ETH", "ARB", c])
        t1 = min(data[s][0].index[-1] for s in ["BTC", "ETH", "ARB", c])
        say(f"\n### {c} ({(t0 + pd.Timedelta(days=90)).date()} → {t1.date()}, préstamo {APR[c] * 100:.1f} %/año)")
        solo = portfolio(data, {c: 1.0}, t0 + pd.Timedelta(days=90), t1 + pd.Timedelta(hours=1))
        say(f"  {c} sola (100 %): {solo['ret']:+.1%} / {solo['max_dd']:+.1%} / {solo['neg_months']} meses rojo, "
            f"{solo['trades']} operaciones, acierto {solo['win_rate']:.0%}")
        all_ok = True
        for sh in SHARES:
            ok, det, res = verdict(data, with_coin(c, sh), t0, t1)
            a, b = res["completo"]
            okS, detS, resS = verdict(data, with_coin(c, sh), t0, t1, cost_new=STRESS_COST)
            bs = resS["completo"][1]
            say(f"  +{c} al {sh:.0%}: {b['ret']:+.1%} / {b['max_dd']:+.1%} / {b['neg_months']}  vs actual "
                f"{a['ret']:+.1%} / {a['max_dd']:+.1%} / {a['neg_months']}  → {'PASA' if ok else 'no pasa'} ({det}); "
                f"estrés {bs['ret']:+.1%} → {'PASA' if okS else 'no'}")
            for lab in ("mitad 1", "mitad 2", "tercio 1", "tercio 2", "tercio 3"):
                x, y = res[lab]
                say(f"      {lab:9s} actual {x['ret']:+.1%} ({x['max_dd']:+.1%})  con {c} {y['ret']:+.1%} ({y['max_dd']:+.1%})")
            all_ok &= ok and okS
            rows.append({"moneda": c, "peso": sh, "pasa": ok, "pasa_estres": okS, "ret_nueva": b["ret"],
                         "ret_actual": a["ret"], "dd_nueva": b["max_dd"], "dd_actual": a["max_dd"],
                         "meses_rojo_nueva": b["neg_months"], "meses_rojo_actual": a["neg_months"],
                         "desde": str(t0.date()), "hasta": str(t1.date())})
        if all_ok:
            passing.append(c)

    # ---- cesta de las que pasan
    say("\n## 4. Cesta con todas las que pasan")
    if passing:
        share = min(0.10, 0.30 / len(passing))
        w = {k: v * (1 - share * len(passing)) for k, v in CUR_W.items()}
        for c in passing:
            w[c] = share
        t0 = max(data[s][0].index[0] for s in w)
        t1 = min(data[s][0].index[-1] for s in w)
        ok, det, res = verdict(data, w, t0, t1)
        a, b = res["completo"]
        say(f"  {', '.join(passing)} al {share:.0%} cada una: {b['ret']:+.1%} / {b['max_dd']:+.1%} / {b['neg_months']} vs actual "
            f"{a['ret']:+.1%} / {a['max_dd']:+.1%} / {a['neg_months']} → {'PASA' if ok else 'no pasa'} ({det})")
    else:
        say("  Ninguna moneda pasa con el 10 % y el 15 % y con costes de estrés.")

    say("\n## RESUMEN")
    lendable = set(scan[(scan.disponible >= MIN_AVAIL) & scan.prestable].sym) if len(scan) else set()
    for c in cands:
        tag = "PASA" if c in passing else "no pasa"
        lend = "Dolomite la presta" if (not len(scan) or c in lendable) else "Dolomite NO la presta (solo largos)"
        say(f"  {c:8s} {tag:8s} · {lend}")
    say("Una moneda solo se añade si PASA y Dolomite la presta (para poder ir en corto).")
    (base.RESULTS / f"research_2026_11i{'_synthetic' if args.synthetic else ''}.txt").write_text("\n".join(lines))
    pd.DataFrame(rows).to_csv(base.RESULTS / f"research_2026_11i{'_synthetic' if args.synthetic else ''}.csv", index=False)


if __name__ == "__main__":
    main()
