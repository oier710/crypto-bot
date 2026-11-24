"""Investigación nov-2026 (11ª parte): TAMAÑO SEGÚN LA CLARIDAD DE LA TENDENCIA, con y sin apalancamiento.

Idea de Oier (2026-11-24): "si la tendencia es muy clara, apalancarse para aprovecharla; cuando es difusa o lateral,
menos riesgo". Hoy cada posición usa siempre el 75 % de su parte. Aquí el tamaño (fracción f de su parte) depende de
la claridad de la tendencia, medida con datos del cierre anterior (sin mirar el futuro). f > 1 = apalancado: en
largos se pide prestado USDC (interés 8 %/año); en cortos se pide prestado más token (interés ya incluido).

Medidas de claridad (fijadas ANTES de ver resultados, propuestas a Oier el 24-nov):
  ADX   por escalones: ADX < 25 -> 0.50 · 25-35 -> 0.75 · >= 35 -> 1.25
  SEP   separación de medias |EMA20 - EMA60| / ATR: < 1 -> 0.50 · 1-2 -> 0.75 · >= 2 -> 1.25
  ALI   medias alineadas a favor (largo: cierre > EMA20 > EMA60; corto al revés): sí -> 1.25 · no -> 0.50
  COMP  compuesta: puntos = (ADX >= 30) + (SEP >= 1.5) + (ALI): 0 -> 0.50 · 1 -> 0.75 · 2 -> 1.00 · 3 -> 1.25
  COMP+ compuesta agresiva: igual pero 3 puntos -> 1.50
Cada una en tres formas:
  * "apalancada" (máximo 1.25 o 1.50), tamaño fijado al ENTRAR (como hoy).
  * "tope 1x": los 1.25 pasan a 1.00 (sin apalancamiento; separa si ayuda reducir en lateral o aumentar en tendencia).
  * "ajuste diario": el tamaño se reajusta cada día (ciclo diario) si cambia el escalón, comprando o vendiendo la
    diferencia (con costes), manteniendo la estructura de parciales, stops y stop que persigue.
REFERENCIAS de tamaño fijo: f = 0.50, 0.625, 0.75 (= actual), 0.875, 1.00, 1.125, 1.25, 1.50. Comparación justa: cada
variante se compara con el tamaño fijo de la MISMA exposición media real, hora a hora (interpolado), porque apalancarse siempre da más
retorno y más caída; la claridad de la tendencia solo aporta si gana a esa referencia.

Criterio (fijado antes):
  1) Gana a la referencia de igual exposición media en retorno en ambas mitades y en >= 2 de 3 tercios, y en
     retorno/caída máxima en el periodo completo.
  2) Caída máxima no peor que -30 % (con apalancamiento el bot se para en -25 % desde el máximo).
  3) No más meses en rojo que la actual.
Se informa también frente a la actual (0.75 fijo).
Base = producción v6 (cartera BTC 42.5 / ETH 42.5 / ARB 15 %, stop 2 ATR, parciales 1.5/3 ATR, reentrada, stop que
persigue 3 ATR desde la parcial 1), costes reales. Se comprueba que con f = 0.75 fijo da lo mismo que producción.
Limitaciones: no se modela la parada a -25 % ni la liquidación (con f <= 1.5 y stop a 2 ATR la garantía sobra).
Salida: results/research_2026_11k.txt y .csv
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import research_2026_10 as base  # noqa: E402
import research_2026_11c as m11c  # noqa: E402
import research_2026_11g as m11g  # noqa: E402
from bot.indicators import adx, atr, ema  # noqa: E402
from bot.strategies import trend_ema  # noqa: E402

COST, APR, WEIGHTS = m11c.COST, m11c.APR, m11c.WEIGHTS
STOP_ATR, MIN_TRADE, PT, PB_ATR, RESTOP_K = m11c.STOP_ATR, m11c.MIN_TRADE, m11c.PT, m11c.PB_ATR, m11c.RESTOP_K
FAST, SLOW, N, ADX_MIN = m11c.FAST, m11c.SLOW, m11c.N, m11c.ADX_MIN
TRAIL_K, TRAIL_AFTER = 3.0, 1
USDC_APR = 0.08                         # interés por pedir USDC prestado (largos apalancados)
BASE_F = base.POS_FRACTION              # 0.75


@dataclass(frozen=True)
class Var:
    name: str
    kind: str | None = None   # None = tamaño fijo | "adx" | "sep" | "ali" | "comp"
    fixed: float = BASE_F     # tamaño fijo (si kind es None)
    top: float = 1.25         # tamaño del escalón más alto
    daily: bool = False       # reajustar el tamaño cada día


REFS = [Var(f"F tamaño fijo {f:.3g}", fixed=f) for f in (0.5, 0.625, 0.75, 0.875, 1.0, 1.125, 1.25, 1.5)]
DYN = [
    Var("ADX apalancada", "adx"), Var("SEP apalancada", "sep"), Var("ALI apalancada", "ali"),
    Var("COMP apalancada", "comp"), Var("COMP+ agresiva 1.5", "comp", top=1.5),
    Var("ADX tope 1x", "adx", top=1.0), Var("SEP tope 1x", "sep", top=1.0), Var("ALI tope 1x", "ali", top=1.0),
    Var("COMP tope 1x", "comp", top=1.0),
    Var("ADX ajuste diario", "adx", daily=True), Var("SEP ajuste diario", "sep", daily=True),
    Var("ALI ajuste diario", "ali", daily=True), Var("COMP ajuste diario", "comp", daily=True),
    Var("COMP+ agresiva ajuste diario", "comp", top=1.5, daily=True),
]
VARIANTS = [REFS[2]] + DYN + [r for i, r in enumerate(REFS) if i != 2]


def clarity(d: pd.DataFrame) -> pd.DataFrame:
    """Medidas de claridad por día (ya desplazadas: valor del día t = cierre de t-1)."""
    c = d.close
    e20, e60 = ema(c, FAST), ema(c, SLOW)
    a = atr(d, N)
    out = pd.DataFrame({
        "adx": adx(d, N),
        "sep": (e20 - e60).abs() / a,
        "up": ((c > e20) & (e20 > e60)).astype(int),
        "dn": ((c < e20) & (e20 < e60)).astype(int),
    }, index=d.index)
    return out.shift(1)


def frac(v: Var, row, side: int) -> float:
    if v.kind is None:
        return v.fixed
    if row is None or any(pd.isna(row[k]) for k in ("adx", "sep", "up", "dn")):
        return BASE_F
    hi = v.top
    ali = bool(row["up"]) if side == 1 else bool(row["dn"])
    if v.kind == "adx":
        return 0.5 if row["adx"] < 25 else (0.75 if row["adx"] < 35 else hi)
    if v.kind == "sep":
        return 0.5 if row["sep"] < 1 else (0.75 if row["sep"] < 2 else hi)
    if v.kind == "ali":
        return hi if ali else 0.5
    if v.kind == "comp":
        pts = int(row["adx"] >= 30) + int(row["sep"] >= 1.5) + int(ali)
        return [0.5, 0.75, min(1.0, hi), hi][pts]
    raise ValueError(v.kind)


# ---- simulador: el de producción (11c + stop que persigue) con tamaño variable --------------------------------
def simulate(h, d, v: Var, capital, cost_pct, apr):
    sig_d = trend_ema.signals(d, fast=FAST, slow=SLOW, adx_n=N, adx_min=ADX_MIN, allow_short=True).shift(1)
    day_atr = atr(d, N).shift(1)
    clar = clarity(d) if v.kind else None
    s = base.Sleeve(cash=capital)
    s.ref, s.pb = 0.0, 0.0
    st = {"f": BASE_F, "fs": [], "adj": 0}
    eq = np.empty(len(h))
    notional = np.zeros(len(h))
    closes, opens = h["close"].values, h["open"].values
    last_day, cur_sig = None, 0
    hourly_rate = apr / (365 * 24)
    usdc_rate = USDC_APR / (365 * 24)

    def swap_cost(usd, short):
        return abs(usd) * (cost_pct + (base.SHORT_EXTRA_PCT if short else 0.0)) + base.GAS_USD

    def mtm(px):
        return s.cash + s.side * s.qty * px if s.side else s.cash

    def close_part(px, frac_, kind, t):
        q = s.qty * frac_
        c = swap_cost(q * px, s.side == -1)
        s.cash += (q * px - c) if s.side == 1 else (-(q * px) - c)
        s.realized_trade += s.side * q * (px - s.entry) - c
        s.qty -= q
        s.fees += c
        if s.qty <= 1e-12 or frac_ >= 0.999:
            s.trades.append((t, kind, s.realized_trade))
            s.side, s.qty, s.full_qty, s.pt_done, s.realized_trade = 0, 0.0, 0.0, 0, 0.0

    def open_pos(px, side, a, t, day):
        f = frac(v, clar.loc[day] if clar is not None and day in clar.index else None, side)
        usd = mtm(px) * f
        if usd < MIN_TRADE or np.isnan(a):
            return
        c = swap_cost(usd, side == -1)
        q = usd / px
        s.cash += (-usd if side == 1 else usd) - c
        s.side, s.qty, s.full_qty, s.entry = side, q, q, px
        s.atr_e, s.extreme, s.pt_done, s.realized_trade = a, px, 0, -c
        s.stop = px - side * STOP_ATR * a
        s.ref, s.pb = px, 0.0
        s.fees += c
        st["f"] = f
        st["fs"].append(f)

    def resize(px, f_new, t):
        """Reajusta la posición abierta al tamaño f_new (proporcional: mantiene parciales y stops)."""
        r = f_new / st["f"]
        dq = s.qty * (r - 1)
        if abs(dq) * px < MIN_TRADE:
            return
        c = swap_cost(abs(dq) * px, s.side == -1)
        if dq > 0:
            s.cash += (-(dq * px) if s.side == 1 else dq * px) - c
            s.entry = (s.entry * s.qty + px * dq) / (s.qty + dq)
            s.realized_trade -= c
        else:
            q = -dq
            s.cash += (q * px - c) if s.side == 1 else (-(q * px) - c)
            s.realized_trade += s.side * q * (px - s.entry) - c
        s.qty *= r
        s.full_qty *= r
        s.fees += c
        st["f"] = f_new
        st["adj"] += 1

    def rebuy(px, a):
        q = s.full_qty - s.qty
        if q * px < MIN_TRADE:
            return
        c = swap_cost(q * px, s.side == -1)
        s.cash += (-(q * px) if s.side == 1 else q * px) - c
        s.entry = (s.entry * s.qty + px * q) / (s.qty + q)
        s.qty += q
        s.fees += c
        s.realized_trade -= c
        s.pt_done, s.ref, s.pb = 0, px, 0.0
        if not np.isnan(a):
            s.atr_e = a
            ns = px - s.side * RESTOP_K * a
            s.stop = max(s.stop, ns) if s.side == 1 else min(s.stop, ns)

    for i, t in enumerate(h.index):
        day = t.normalize()
        px = closes[i]
        a_day = day_atr.get(day, np.nan)
        if day != last_day:
            last_day = day
            sig = sig_d.get(day, np.nan)
            if not np.isnan(sig):
                cur_sig = int(sig)
                o = opens[i]
                if s.side and cur_sig != s.side:
                    close_part(o, 1.0, "señal", t)
                if cur_sig != 0 and s.side == 0 and s.stopped_day != day:
                    open_pos(o, cur_sig, a_day, t, day)
                elif v.daily and s.side and clar is not None and day in clar.index:
                    fn = frac(v, clar.loc[day], s.side)
                    if fn != st["f"]:
                        resize(o, fn, t)
        if s.side == -1:
            c = s.qty * px * hourly_rate
            s.cash -= c
            s.fees += c
        if s.cash < 0:                                   # USDC prestado (largo apalancado)
            c = -s.cash * usdc_rate
            s.cash -= c
            s.fees += c
        if s.side:
            side = s.side
            new_high = side * (px - s.extreme) > 0
            partial_out = s.qty < s.full_qty * 0.99
            if partial_out and not new_high and s.atr_e:
                s.pb = max(s.pb, side * (s.extreme - px) / s.atr_e)
            s.extreme = max(s.extreme, px) if side == 1 else min(s.extreme, px)
            fav = side * (px - s.ref) / s.atr_e if s.atr_e else 0.0
            be_px = s.entry * (1 + side * 2 * cost_pct)
            if s.pt_done < len(PT) and fav >= PT[s.pt_done]:
                close_part(px, (1 / 3) / (s.qty / s.full_qty), f"parcial{s.pt_done + 1}", t)
                if s.side:
                    s.pt_done += 1
                    s.pb = 0.0
                    s.stop = max(s.stop, be_px) if side == 1 else min(s.stop, be_px)
            if s.side and s.pt_done >= TRAIL_AFTER and s.atr_e:
                tr = s.extreme - side * TRAIL_K * s.atr_e
                s.stop = max(s.stop, tr) if side == 1 else min(s.stop, tr)
            if s.side and ((side == 1 and px <= s.stop) or (side == -1 and px >= s.stop)):
                close_part(px, 1.0, "stop", t)
                s.stopped_day = day
            elif s.side and partial_out and new_high and s.pb >= PB_ATR and cur_sig == side:
                rebuy(px, a_day)
        eq[i] = mtm(px)
        notional[i] = s.qty * px if s.side else 0.0
    s.fs, s.adj = st["fs"], st["adj"]
    s.notional = pd.Series(notional, index=h.index)
    return pd.Series(eq, index=h.index), s


def portfolio(data, v, start, end):
    eqs, trades, fees, fs, adj, nots = [], [], 0.0, [], 0, []
    for sym, w in WEIGHTS.items():
        h, d = data[sym]
        hh = h[(h.index >= start) & (h.index < end)]
        e, s = simulate(hh, d, v, base.CAPITAL * w, COST[sym], APR[sym])
        eqs.append(e)
        trades += s.trades
        fees += s.fees
        fs += s.fs
        nots.append(s.notional)
        adj += s.adj
    eq = pd.concat(eqs, axis=1).ffill().bfill().sum(axis=1)
    r = base.metrics(eq, trades, fees)
    r["f_media"] = float(np.mean(fs)) if fs else BASE_F
    r["ajustes"] = adj
    nt = pd.concat(nots, axis=1).fillna(0).sum(axis=1)
    r["expo"] = float((nt / eq.reindex(nt.index)).mean())
    return r


def interp_ref(refs: pd.DataFrame, f: float, col: str) -> float:
    x = refs["expo"].to_numpy()
    o = np.argsort(x)
    return float(np.interp(f, x[o], refs[col].to_numpy()[o]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    data = {}
    for sym in WEIGHTS:
        h = base.load_hourly(sym if not a.synthetic else ("BTC" if sym == "BTC" else "ETH"), a.synthetic)
        if a.synthetic and sym == "ARB":
            rng = np.random.default_rng(7)
            h = h * (1 + rng.normal(0, 0.0005, len(h))[:, None]).clip(0.99, 1.01)
        data[sym] = (h, base.daily_from_hourly(h))
    t0 = max(data[s][0].index[0] for s in data)
    t1 = min(data[s][0].index[-1] for s in data)
    start = t0 + pd.Timedelta(days=90)
    end = t1 + pd.Timedelta(hours=1)
    mid = start + (t1 - start) / 2
    th = [start + (t1 - start) * k / 3 for k in range(4)]
    th[-1] = end
    periods = [("PERIODO COMPLETO", start, end), ("1ª MITAD", start, mid), ("2ª MITAD", mid, end),
               ("TERCIO 1", th[0], th[1]), ("TERCIO 2 (lateral)", th[1], th[2]), ("TERCIO 3", th[2], th[3])]

    lines = ["# Investigación nov-2026 (11ª parte) — tamaño según la claridad de la tendencia, con y sin apalancamiento (idea de Oier)",
             f"Datos: Coinbase 1h {'SINTÉTICOS (prueba)' if a.synthetic else 'reales'}, evaluación {start.date()} -> {t1.date()}. "
             f"Base = producción v6, costes reales; USDC prestado al {USDC_APR:.0%}/año."]
    h0, d0 = data["ETH"]
    hh = h0[(h0.index >= start) & (h0.index < end)]
    e1, _ = m11g.sim(hh, d0, m11g.Var("prod", k=TRAIL_K, after=TRAIL_AFTER), 50.0, 0.001, 0.04)
    e2, _ = simulate(hh, d0, REFS[2], 50.0, 0.001, 0.04)
    lines.append(f"Comprobación del simulador: diferencia máxima con producción (tamaño fijo 0.75) {float((e1 - e2).abs().max()):.6f} $ (debe ser 0).")
    lines.append("Columnas extra: f_med = tamaño medio al entrar (fracción de su parte; 0.75 = hoy); expo = exposición media real (posición / capital, hora a hora, incluido estar fuera); ajus = reajustes diarios. La referencia de igual exposición se interpola por expo.")
    lines.append("")
    rows = []
    for lab, s0, s1 in periods:
        lines += [f"## {lab} ({s0.date()} -> {min(s1, t1).date()})", base.HEADER + f" {'f_med':>6} {'expo':>5} {'ajus':>5}"]
        for v in VARIANTS:
            r = portfolio(data, v, s0, s1)
            r["variant"] = v.name
            lines.append(base.fmt(r) + f" {r['f_media']:>6.2f} {r['expo']:>5.2f} {r['ajustes']:>5}")
            rows.append(r | {"period": lab, "ref": v.kind is None})
        lines.append("")
    df = pd.DataFrame(rows)
    df["ret_dd"] = df.ret / df.max_dd.abs().replace(0, np.nan)

    # comparación con la referencia de igual exposición media, por periodo
    comp = []
    for lab, _, _ in periods:
        p = df[df.period == lab]
        refs = p[p.ref]
        for _, r in p[~p.ref].iterrows():
            comp.append({"variant": r.variant, "period": lab,
                         "ret": r.ret, "ref_ret": interp_ref(refs, r.expo, "ret"),
                         "ret_dd": r.ret_dd, "ref_ret_dd": interp_ref(refs, r.expo, "ret_dd")})
    cp = pd.DataFrame(comp)
    cp["gana"] = cp.ret > cp.ref_ret
    full = df[df.period == "PERIODO COMPLETO"].set_index("variant")
    b = full.loc[REFS[2].name]
    summ = []
    for v in DYN:
        c = cp[cp.variant == v.name].set_index("period")
        r = full.loc[v.name]
        summ.append({
            "variant": v.name, "ret": r.ret, "max_dd": r.max_dd, "neg_months": r.neg_months, "sharpe": r.sharpe,
            "f_media": r.f_media, "expo": r.expo, "ret_ref_igual": c.loc["PERIODO COMPLETO", "ref_ret"],
            "ret_dd": r.ret_dd, "ret_dd_ref": c.loc["PERIODO COMPLETO", "ref_ret_dd"],
            "mitades_vs_ref": int(c.loc[["1ª MITAD", "2ª MITAD"], "gana"].sum()),
            "tercios_vs_ref": int(c.loc[[p for p in c.index if p.startswith("TERCIO")], "gana"].sum()),
        })
    sm = pd.DataFrame(summ).set_index("variant")
    sm["pasa"] = ((sm.mitades_vs_ref == 2) & (sm.tercios_vs_ref >= 2) & (sm.ret_dd > sm.ret_dd_ref)
                  & (sm.max_dd >= -0.30) & (sm.neg_months <= b.neg_months))
    lines += ["## REFERENCIAS de tamaño fijo (periodo completo)",
              full[full.ref][["f_media", "expo", "ret", "max_dd", "neg_months", "worst_month", "sharpe", "ret_dd"]]
              .sort_values("f_media").round(3).to_string(), "",
              "## RESUMEN: cada variante frente a la referencia fija de su MISMA exposición media",
              sm.round(3).to_string(), ""]
    ok = sm[sm.pasa]
    if len(ok):
        lines.append("PASAN el criterio: " + "; ".join(
            f"{n}: {r.ret:+.1%} (ref igual exposición {r.ret_ref_igual:+.1%}), caída {r.max_dd:+.1%}, ret/caída {r.ret_dd:.2f} vs {r.ret_dd_ref:.2f}"
            for n, r in ok.iterrows()) + f"  (actual 0.75 fijo: {b.ret:+.1%}, {b.max_dd:+.1%}, ret/caída {b.ret_dd:.2f})")
        lines.append("Solo vale si la versión hermana (tope 1x / ajuste diario) y las otras medidas apuntan en la misma dirección.")
    else:
        lines.append("Ninguna variante pasa: la claridad de la tendencia no aporta sobre el tamaño fijo con la misma exposición.")
    txt = "\n".join(lines)
    suffix = "_synthetic" if a.synthetic else ""
    (base.RESULTS / f"research_2026_11k{suffix}.txt").write_text(txt)
    df.to_csv(base.RESULTS / f"research_2026_11k{suffix}.csv", index=False)
    print(txt)


if __name__ == "__main__":
    main()
