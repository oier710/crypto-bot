"""Investigación 2026-11-21: segundo sistema (acciones/ETF vía Alpaca) — rotación apalancada con la media de 200 días.

Encargo de Oier + crítica del chat del crypto-bot (21-nov):
  1. Historia larga: mercado de EE. UU. diario desde 1926 (Ken French, Mkt-RF + RF), que incluye 1929-32, los 70 y
     el crash de 1987, además de 1988-2026 con el S&P 500 y el Nasdaq-100 de Yahoo.
  2. Coste real del apalancamiento: el ETF 2x sintético paga cada día la letra REAL de ese día más un margen sobre la
     parte prestada, más los gastos del ETF. El margen se calibra contra el ETF real (SSO, QLD, UPRO, TQQQ, UGL, UBT)
     y se valida AÑO A AÑO (también frente al "1 %/año fijo" de Gayed).
  3. Impuestos (solo escenario): corto plazo 24 %, largo plazo 15 %, Florida 0 %. Lotes FIFO, compensación de
     pérdidas y arrastre; interés de las letras a 24 %; dividendos de los ETF 1x a 15 % (rendimiento supuesto 1.5 %).
     Se informa antes de impuestos (= cuenta IRA/Roth) y después (cuenta normal, liquidando todo al final).
  4. Estrategias: mantener (1x y 2x sin filtro), LRS 1x/2x/3x (media de 200), objetivo de volatilidad
     (Moreira-Muir 2017: exposición = vol objetivo / vol de 20 días, tope 2x, solo sobre la media de 200),
     cesta con tendencia (S&P, Nasdaq, oro, bonos largos) 1x, 2x y con objetivo de volatilidad, QLD frente a SSO.
  5. Validación: completo, dos mitades y tres tercios; robustez con medias de 150-250; ejecución el mismo día
     (lag 1, ~15:45) y al cierre del día siguiente (lag 2, conservador, el que decide).
  6. Métricas: además de rentabilidad y caída, peor tramo de 3 años (absoluto y frente al mercado), % de ventanas de
     5 años por delante del mercado, % de años por delante, tiempo máximo bajo el agua.
  7. Estrés: peores días, episodios históricos y huecos de un día inyectados (−20 % el 9-oct-2007 y el 3-ene-2022).

No usa claves ni toca fondos. Datos públicos: Ken French (Dartmouth) y Yahoo Finance.
Salida: results/research_stocks_lrs.txt (+ .csv con la tabla resumen).
Prueba local sin red: RESEARCH_FAKE=1 python scripts/research_stocks_lrs.py
"""
from __future__ import annotations

import io
import math
import os
import time
import traceback
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
RESULTS.mkdir(exist_ok=True)
FAKE = os.environ.get("RESEARCH_FAKE") == "1"
OUT_DIR = Path("/tmp") if FAKE else RESULTS          # con datos falsos no se toca results/
OUT_TXT = OUT_DIR / "research_stocks_lrs.txt"
OUT_CSV = OUT_DIR / "research_stocks_lrs.csv"

# ---------------------------------------------------------------- supuestos (todos a la vista)
TAX_ST, TAX_LT = 0.24, 0.15          # escenario fiscal federal; Florida 0 %
TAX_COLL = 0.28                      # GLD (oro físico): ganancia a largo plazo tributa como coleccionable
TAX_6040 = 0.6 * 0.15 + 0.4 * 0.24   # UGL (fondo de futuros/swaps, K-1): regla 60/40 sin importar el plazo
ABS_DD_LIMIT = -0.60                 # «apto para Oier»: nunca peor que −60 % en ningún tramo
COST_MULT_PRE1990 = 5.0              # variante de sesgo: costes ×5 antes de 1990
DIV_YIELD = 0.015                    # dividendo supuesto de los ETF 1x de acciones (tributa cada año al 15 %)
BOND_YIELD = 0.035                   # cupón supuesto de TLT (interés: tributa cada año al 24 %)
COST = {"1x": 0.0002, "lev": 0.0005, "lev_thin": 0.0015, "cash": 0.0002}   # horquilla + deslizamiento por lado
ER = {"SPY": 0.000945, "QQQ": 0.0020, "MKT": 0.000945,                     # gastos anuales (aprox.)
      "SSO": 0.0089, "QLD": 0.0095, "UPRO": 0.0091, "TQQQ": 0.0084, "UGL": 0.0095, "UBT": 0.0095}
SMA_MAIN, SMA_GRID = 200, [150, 175, 200, 225, 250]
TV_MAIN, TV_GRID, TV_BASKET_GRID = 0.20, [0.15, 0.20, 0.25], [0.10, 0.15, 0.20]
CAP = 2.0
BAND = {"lrs": 0.02, "vt": 0.10, "basket": 0.05}   # solo se rebalancea si un peso se aleja más que esto
WARMUP = 260
LAG_MAIN = 2          # señal al cierre de t, operación al cierre de t+1
UA = {"User-Agent": "Mozilla/5.0 (crypto-bot-research)"}

out_lines: list[str] = []


def say(s: str = "") -> None:
    print(s)
    out_lines.append(s)


# ================================================================ 1. DATOS
def _get(url, params=None, tries=4):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=90)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (i + 1))
    raise last


def load_french() -> pd.DataFrame:
    url = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_daily_CSV.zip"
    z = zipfile.ZipFile(io.BytesIO(_get(url).content))
    txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
    rows = []
    for line in txt:
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 5 and len(p[0]) == 8 and p[0].isdigit():
            rows.append((p[0], float(p[1]), float(p[4])))
    df = pd.DataFrame(rows, columns=["d", "mkt_rf", "rf"])
    idx = pd.to_datetime(df["d"], format="%Y%m%d")
    return pd.DataFrame({"mkt": ((df["mkt_rf"] + df["rf"]) / 100).values, "rf": (df["rf"] / 100).values}, index=idx)


def load_yahoo(ticker: str) -> pd.Series:
    last = None
    for host in ("query1", "query2"):
        try:
            r = _get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(ticker)}",
                     params={"interval": "1d", "period1": 0, "period2": int(time.time()), "events": "div,split",
                             "includeAdjustedClose": "true"}, tries=2)
            res = r.json()["chart"]["result"][0]
            ind = res["indicators"]
            adj = (ind.get("adjclose") or [{}])[0].get("adjclose") or ind["quote"][0]["close"]
            s = pd.Series(adj, index=pd.to_datetime(res["timestamp"], unit="s").normalize(), dtype=float).dropna()
            s = s[(s > 0) & ~s.index.duplicated(keep="last")]
            if len(s) > 100:
                return s
            last = ValueError(f"{ticker}: solo {len(s)} datos")
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


STOOQ = {"SPY": "spy.us", "QQQ": "qqq.us", "GLD": "gld.us", "TLT": "tlt.us", "SSO": "sso.us", "UPRO": "upro.us",
         "QLD": "qld.us", "TQQQ": "tqqq.us", "UGL": "ugl.us", "UBT": "ubt.us", "^NDX": "^ndx"}


def load_stooq(ticker: str) -> pd.Series:
    sym = STOOQ[ticker]
    r = _get("https://stooq.com/q/d/l/", params={"s": sym, "i": "d"}, tries=2)
    df = pd.read_csv(io.StringIO(r.text))
    if "Close" not in df.columns or len(df) < 100:
        raise ValueError(f"Stooq {sym}: respuesta sin datos ({r.text[:80]!r})")
    s = pd.Series(df["Close"].values, index=pd.to_datetime(df["Date"]), dtype=float).dropna()
    return s[(s > 0) & ~s.index.duplicated(keep="last")]


def fake_data():
    """Datos aleatorios con la misma forma que los reales, solo para probar el código sin red."""
    rng = np.random.default_rng(7)
    d_all = pd.bdate_range("1926-07-01", "2026-10-30")
    mkt = pd.Series(rng.normal(0.0004, 0.011, len(d_all)), index=d_all)
    mkt.loc["1987-10-19"] = -0.17
    rf = pd.Series(0.00012, index=d_all)
    fr = pd.DataFrame({"mkt": mkt, "rf": rf})

    def px(start, base, lev=1.0, vol=0.011, drift=0.0004, er=0.0):
        d = d_all[d_all >= pd.Timestamp(start)]
        if base is not None:
            r = lev * base.reindex(d).fillna(0) - (lev - 1) * 0.00015 - er / 252 + rng.normal(0, 0.0005, len(d))
        else:
            r = pd.Series(rng.normal(drift, vol, len(d)), index=d)
        return 100 * (1 + r).cumprod()

    spx = px("1988-01-04", mkt)
    ndx_r = mkt * 1.3 + rng.normal(0, 0.006, len(d_all))
    ndx_r = pd.Series(ndx_r.values, index=d_all)
    gld_r = pd.Series(rng.normal(0.0003, 0.010, len(d_all)), index=d_all)
    tlt_r = pd.Series(rng.normal(0.0002, 0.009, len(d_all)), index=d_all)
    y = {
        "^SP500TR": spx, "SPY": px("1993-01-29", mkt),
        "^NDX": px("1985-10-01", ndx_r), "QQQ": px("1999-03-10", ndx_r),
        "GLD": px("2004-11-18", gld_r), "TLT": px("2002-07-30", tlt_r),
        "SSO": px("2006-06-21", mkt, 2, er=0.0089), "UPRO": px("2009-06-25", mkt, 3, er=0.0091),
        "QLD": px("2006-06-21", ndx_r, 2, er=0.0095), "TQQQ": px("2010-02-11", ndx_r, 3, er=0.0084),
        "UGL": px("2008-12-02", gld_r, 2, er=0.0095), "UBT": px("2010-01-19", tlt_r, 2, er=0.0095),
        "^IRX": pd.Series(3.0, index=d_all),
    }
    return fr, y


def load_all():
    """Ken French + Yahoo (query1/query2) con Stooq de plan B. Nada de esto debe tumbar el script."""
    if FAKE:
        fr, y = fake_data()
        return fr, y, {k: "falso" for k in y}
    fr = None
    try:
        fr = load_french()
    except Exception as e:  # noqa: BLE001
        say(f"AVISO: no se pudo descargar Ken French: {e}")
    y, src = {}, {}
    for t in ["^SP500TR", "SPY", "^NDX", "QQQ", "GLD", "TLT", "SSO", "UPRO", "QLD", "TQQQ", "UGL", "UBT", "^IRX"]:
        try:
            y[t], src[t] = load_yahoo(t), "yahoo"
        except Exception as e:  # noqa: BLE001
            if t in STOOQ:
                try:
                    y[t], src[t] = load_stooq(t), "stooq"
                    say(f"AVISO: {t} desde Stooq (Yahoo falló: {e})")
                    continue
                except Exception as e2:  # noqa: BLE001
                    e = f"{e} | Stooq: {e2}"
            say(f"AVISO: no se pudo descargar {t}: {e}")
        time.sleep(1)
    return fr, y, src


def rets(s: pd.Series) -> pd.Series:
    r = s.pct_change().dropna()
    bad = r.abs() > 0.45
    if bad.any():
        say(f"AVISO: {bad.sum()} rentabilidades diarias > 45 % descartadas en {s.name} ({list(r.index[bad].date)[:5]})")
        r = r[~bad]
    return r


def build_rf(fr: pd.DataFrame, y: dict, idx: pd.DatetimeIndex) -> pd.Series:
    """Letra diaria REAL: RF de Ken French (1 mes) y, tras su último dato, ^IRX (13 semanas) de Yahoo."""
    rf = fr["rf"].reindex(idx) if fr is not None else pd.Series(np.nan, index=idx)
    if "^IRX" in y:
        irx = (y["^IRX"] / 100 / 252).reindex(idx).ffill()
        rf = rf.fillna(irx)
    return rf.ffill().fillna(0.0)


# ================================================================ 2. APALANCADOS SINTÉTICOS
def lev(r: pd.Series, rf: pd.Series, L: float, er: float, spread: float) -> pd.Series:
    """ETF apalancado diario: L × subyacente − (L−1) × (letra del día + margen) − gastos."""
    return (L * r - (L - 1) * (rf + spread / 252) - er / 252).clip(lower=-0.9999)   # un ETF no baja de 0


def yearly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.year).prod() - 1


def calibrate(name, real_px, under_r, rf, L, er):
    real = rets(real_px.rename(name))
    idx = real.index.intersection(under_r.index)
    real, u, f = real.loc[idx], under_r.loc[idx], rf.loc[idx]
    cnt = real.groupby(real.index.year).size()
    full = cnt[cnt >= 240].index
    if len(full) < 3:
        return None
    ry = yearly(real).loc[full]
    best = None
    for s in np.arange(-0.01, 0.0301, 0.0005):
        sy = yearly(lev(u, f, L, er, s)).loc[full]
        e = float(np.sqrt(((sy - ry) ** 2).mean()))
        if best is None or e < best[1]:
            best = (round(float(s), 4), e)
    s = best[0]
    tab = pd.DataFrame({
        "real": ry,
        "sintético": yearly(lev(u, f, L, er, s)).loc[full],
        "sint_margen0": yearly(lev(u, f, L, er, 0.0)).loc[full],
        "gayed_1pct": yearly(L * u - 0.01 / 252).loc[full],
        "coste_apal_%/año": ((L - 1) * (f + s / 252) * 252 + er).groupby(f.index.year).mean().loc[full] * 100,
    })
    tab["dif_sint"] = tab["sintético"] - tab["real"]
    tab["dif_gayed"] = tab["gayed_1pct"] - tab["real"]
    rmse = {c: float(np.sqrt(((tab[c] - tab["real"]) ** 2).mean())) for c in ["sintético", "sint_margen0", "gayed_1pct"]}
    # seguimiento diario (para ver que el sintético no solo cuadra en total)
    dd = lev(u, f, L, er, s) - real
    return {"name": name, "spread": s, "tab": tab, "rmse": rmse, "te_daily": float(dd.std() * math.sqrt(252)),
            "start": idx[0].date(), "end": idx[-1].date()}


# ================================================================ 3. MOTOR (con lotes e impuestos)
def tax_netting(st, lt, cf):
    st -= cf
    cf = 0.0
    if st < 0 <= lt:
        lt += st
        st = 0.0
    elif lt < 0 <= st:
        st += lt
        lt = 0.0
    if st < 0 or lt < 0:
        cf = -(min(st, 0) + min(lt, 0))
        st, lt = max(st, 0.0), max(lt, 0.0)
    return st, lt, cf


def run(R: np.ndarray, rf: np.ndarray, W: np.ndarray, dates: pd.DatetimeIndex, costs: np.ndarray,
        divflag: np.ndarray, band: float, taxes: bool, inc_y: np.ndarray | None = None, inc_rate: np.ndarray | None = None,
        gmode: np.ndarray | None = None, cost_mult: np.ndarray | None = None):
    """Simula día a día. R[t] = rentabilidad de cada instrumento el día t; W[t] = pesos objetivo al cierre de t
    (ya desplazados por el retraso de ejecución). Cash = 1 − suma de pesos, rinde la letra del día.
    Con taxes=True paga impuestos cada 31-dic (proporcionalmente, sin realizar) y liquida todo al final.
    Rentas de cada instrumento: inc_y (rendimiento anual) al tipo inc_rate; por defecto divflag × DIV_YIELD al 15 %.
    gmode: 0 = ganancia normal (corto/largo según plazo), 1 = coleccionable (largo plazo al 28 %), 2 = 60/40.
    cost_mult: multiplicador de costes por día (variante de sesgo)."""
    T, K = R.shape
    if gmode is None:
        gmode = np.zeros(K, dtype=int)
    if cost_mult is None:
        cost_mult = np.ones(T)
    if inc_y is None:
        inc_y = divflag * DIV_YIELD
    if inc_rate is None:
        inc_rate = np.full(K, TAX_LT)
    units = np.zeros(K)
    p = np.ones(K)
    cash = 1.0
    lots: list[list[list[float]]] = [[] for _ in range(K)]
    st = lt = ltc = interest = 0.0
    divk = np.zeros(K)
    cf = 0.0
    eqs = np.empty(T)
    ntr = 0
    tax_total = 0.0
    dint = (dates.values.astype("datetime64[D]").astype(np.int64))
    years = dates.year.values
    final = None
    for t in range(T):
        if t > 0:
            p *= 1 + R[t]
            inc = cash * rf[t]
            cash += inc
            interest += inc
        v = units * p
        eq = v.sum() + cash
        if taxes:
            divk += v * inc_y / 252
        tw = W[t]
        w = v / eq if eq > 0 else v * 0
        if t == 0 or np.max(np.abs(tw - w)) > band:
            delta = tw * eq - v
            if np.any(np.abs(delta) > 1e-12):
                cm = cost_mult[t]
                c0 = cm * float((np.abs(delta) * costs).sum() + abs(delta.sum()) * COST["cash"])
                delta = tw * (eq - c0) - v           # los costes salen del total: sin efectivo negativo
                c = cm * float((np.abs(delta) * costs).sum() + abs(delta.sum()) * COST["cash"])
                for k in range(K):
                    dk = delta[k]
                    if dk > 1e-12:
                        u = dk / p[k]
                        units[k] += u
                        lots[k].append([dint[t], u, dk])
                    elif dk < -1e-12:
                        us = min(-dk / p[k], units[k])
                        units[k] -= us
                        while us > 1e-15 and lots[k]:
                            lot = lots[k][0]
                            take = min(us, lot[1])
                            basis = lot[2] * take / lot[1]
                            gain = take * p[k] - basis
                            if gmode[k] == 2:
                                lt += 0.6 * gain
                                st += 0.4 * gain
                            elif dint[t] - lot[0] > 365:
                                if gmode[k] == 1:
                                    ltc += gain
                                else:
                                    lt += gain
                            else:
                                st += gain
                            lot[1] -= take
                            lot[2] -= basis
                            us -= take
                            if lot[1] <= 1e-15:
                                lots[k].pop(0)
                cash -= float(delta.sum()) + c
                if t > 0:
                    ntr += 1
        last = t == T - 1
        if taxes and (last or years[t + 1] != years[t]):
            for k in range(K):                      # dividendo ya tributado → sube la base del último lote
                if divk[k] > 0 and lots[k]:
                    lots[k][-1][2] += divk[k]
            div_tax = float((divk * inc_rate).sum())
            divk[:] = 0
            if last:                                # liquidación final
                for k in range(K):
                    for lot in lots[k]:
                        gain = lot[1] * p[k] - lot[2]
                        if gmode[k] == 2:
                            lt += 0.6 * gain
                            st += 0.4 * gain
                        elif dint[t] - lot[0] > 365:
                            if gmode[k] == 1:
                                ltc += gain
                            else:
                                lt += gain
                        else:
                            st += gain
            s_, l_, cf = tax_netting(st, lt + ltc, cf)
            coll = min(max(ltc, 0.0), l_)           # la parte coleccionable del largo plazo neto va al 28 %
            tax = TAX_ST * s_ + TAX_LT * (l_ - coll) + TAX_COLL * coll + TAX_ST * max(interest, 0.0) + div_tax
            st = lt = ltc = interest = 0.0
            eq_now = float(units @ p + cash)
            tax_total += tax
            if last:
                final = eq_now - tax
            elif tax > 0 and eq_now > 0:
                f = max(1 - tax / eq_now, 0.0)
                units *= f
                cash *= f
                for k in range(K):
                    for lot in lots[k]:
                        lot[1] *= f
                        lot[2] *= f
        eqs[t] = units @ p + cash
    if final is None:
        final = eqs[-1]
    return eqs, float(final), ntr, tax_total


# ================================================================ 4. UNIVERSOS Y ESTRATEGIAS
class Universe:
    """Instrumentos (rentabilidades diarias), tipo de coste, apalancamiento y si reparten dividendo."""

    def __init__(self, name, R: pd.DataFrame, rf: pd.Series, meta: dict, bench: str):
        self.name, self.R, self.rf, self.meta, self.bench = name, R.fillna(0.0), rf.reindex(R.index).fillna(0.0), meta, bench
        self.level = (1 + self.R).cumprod()

    def zeros(self):
        return pd.DataFrame(0.0, index=self.R.index, columns=self.R.columns)


def above_sma(u: Universe, col: str, n: int) -> pd.Series:
    lvl = u.level[col]
    return (lvl > lvl.rolling(n).mean()).astype(float)


def w_hold(u, col):
    W = u.zeros()
    W[col] = 1.0
    return W


def w_lrs15(u, sig, one, two, n=SMA_MAIN):
    """LRS 1.5x (21-nov, último test aprobado por Oier): 50 % 1x + 50 % 2x sobre la media; letras debajo.
    Interpolación entre LRS 1x y 2x, sin parámetro nuevo. Se rebalancea 50/50 si un peso se aleja > 5 puntos."""
    a = above_sma(u, sig, n)
    W = u.zeros()
    W[one] = 0.5 * a
    W[two] = 0.5 * a
    return W


def w_lrs(u, sig, inst, n=SMA_MAIN):
    W = u.zeros()
    W[inst] = above_sma(u, sig, n)
    return W


def split_exposure(x: pd.Series):
    """Exposición x∈[0,2] con un ETF 1x y uno 2x sin margen: x≤1 → x en 1x; x>1 → (2−x) en 1x y (x−1) en 2x."""
    one = np.where(x <= 1, x, 2 - x)
    two = np.where(x > 1, x - 1, 0.0)
    return one, two


def w_vt(u, sig, one, two, n=SMA_MAIN, tv=TV_MAIN, filt=True):
    rv = u.R[sig].rolling(20).std() * math.sqrt(252)
    x = (tv / rv).clip(upper=CAP).fillna(0.0)
    if filt:
        x = x * above_sma(u, sig, n)
    W = u.zeros()
    W[one], W[two] = split_exposure(x)
    return W


def w_basket(u, assets, n=SMA_MAIN, mode="1x", tv=0.15):
    """assets = [(señal, inst_1x, inst_2x)]; cada activo 25 % si está sobre su media, si no ese 25 % a letras."""
    W = u.zeros()
    tr = pd.DataFrame({a[0]: above_sma(u, a[0], n) for a in assets})
    slot = 1.0 / len(assets)
    uw = tr * slot
    if mode == "1x":
        for s, o, _ in assets:
            W[o] = uw[s]
    elif mode == "2x":
        for s, _, t2 in assets:
            W[t2] = uw[s]
    else:
        Rs = u.R[[a[0] for a in assets]].values
        U = uw.values
        k = np.zeros(len(U))
        for t in range(20, len(U)):
            if U[t].sum() <= 0:
                continue
            sd = float(np.std(Rs[t - 19:t + 1] @ U[t])) * math.sqrt(252)
            k[t] = min(CAP, tv / sd) if sd > 0 else CAP
        kk = pd.Series(k, index=u.R.index)
        one_f, two_f = split_exposure(kk)
        for s, o, t2 in assets:
            W[o] = uw[s].values * one_f
            W[t2] = uw[s].values * two_f
    return W


def simulate(u: Universe, W: pd.DataFrame, start, end, lag=LAG_MAIN, taxes=False, band=0.02, costs_pre1990=1.0):
    Wl = W.shift(lag - 1).fillna(0.0) if lag > 1 else W
    m = (u.R.index >= start) & (u.R.index <= end)
    d = u.R.index[m]
    costs = np.array([COST[u.meta[c]["cost"]] for c in u.R.columns])
    divf = np.array([1.0 if u.meta[c].get("div") else 0.0 for c in u.R.columns])
    inc_y = np.array([u.meta[c].get("inc", (DIV_YIELD * divf[i], TAX_LT))[0] for i, c in enumerate(u.R.columns)])
    inc_rate = np.array([u.meta[c].get("inc", (0.0, TAX_LT))[1] for c in u.R.columns])
    gmode = np.array([u.meta[c].get("gm", 0) for c in u.R.columns], dtype=int)
    cm = np.where(d.year < 1990, costs_pre1990, 1.0)
    R = u.R.values[m].copy()
    R[0] = 0.0
    eqs, final, ntr, tax = run(R, u.rf.values[m], Wl.values[m], d, costs, divf, band, taxes, inc_y, inc_rate, gmode, cm)
    return pd.Series(eqs, index=d), final, ntr, tax


def exposure(u: Universe, W: pd.DataFrame) -> pd.Series:
    Ls = np.array([u.meta[c]["L"] for c in W.columns])
    return pd.Series(W.values @ Ls, index=W.index)


# ================================================================ 5. MÉTRICAS
def cagr_from(v0, v1, d0, d1):
    yrs = max((d1 - d0).days / 365.25, 1e-9)
    return (v1 / v0) ** (1 / yrs) - 1 if v1 > 0 else -1.0


def metrics(eq: pd.Series, bench: pd.Series, rf: pd.Series, ntr: int, final_after=None):
    r = eq.pct_change().fillna(0)
    out = {}
    out["CAGR"] = cagr_from(eq.iloc[0], eq.iloc[-1], eq.index[0], eq.index[-1])
    out["CAGR_imp"] = cagr_from(1.0, final_after, eq.index[0], eq.index[-1]) if final_after is not None else np.nan
    vol = r.std() * math.sqrt(252)
    out["vol"] = vol
    out["sharpe"] = (r - rf.reindex(r.index).fillna(0)).mean() * 252 / vol if vol > 0 else np.nan
    dd = eq / eq.cummax() - 1
    out["maxDD"] = dd.min()
    uw = (dd < 0).astype(int)
    grp = (uw.diff() != 0).cumsum()
    runs = uw.groupby(grp).sum()
    out["bajo_agua_años"] = runs.max() / 252 if len(runs) else 0.0
    me = eq.groupby([eq.index.year, eq.index.month]).last()
    out["peor_mes"] = me.pct_change().min()
    ye = eq.groupby(eq.index.year).last()
    yr = ye.pct_change()
    be = bench.groupby(bench.index.year).last().pct_change()
    out["peor_año"] = yr.min()
    common = yr.dropna().index.intersection(be.dropna().index)
    out["%años_>merc"] = float((yr.loc[common] > be.loc[common]).mean()) if len(common) else np.nan
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    out["cambios/año"] = ntr / yrs if yrs > 0 else 0
    n3, n5 = 756, 1260
    if len(eq) > n5 + 1:
        c3 = (eq / eq.shift(n3)) ** (1 / 3) - 1
        b3 = (bench / bench.shift(n3)) ** (1 / 3) - 1
        out["peor_3a"] = c3.min()
        out["peor_3a_vs_merc"] = (c3 - b3).min()
        r5 = eq / eq.shift(n5)
        b5 = bench / bench.shift(n5)
        ok = r5.notna() & b5.notna()
        out["%5a_>merc"] = float((r5[ok] > b5[ok]).mean())
    else:
        out["peor_3a"] = out["peor_3a_vs_merc"] = out["%5a_>merc"] = np.nan
    return out


def pct(x, d=1):
    return "   —  " if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:6.{d}f}%"


COLS = [("CAGR", "Rent."), ("CAGR_imp", "Tras imp."), ("maxDD", "Caída"), ("peor_mes", "Peor mes"),
        ("peor_año", "Peor año"), ("sharpe", "Sharpe"), ("cambios/año", "Camb/a"), ("peor_3a", "Peor 3a"),
        ("peor_3a_vs_merc", "3a vs merc"), ("%5a_>merc", "%5a>merc"), ("%años_>merc", "%años>m"),
        ("bajo_agua_años", "Bajo agua")]


def table(rows: list[tuple[str, dict]]):
    say(f"{'estrategia':34s}" + "".join(f"{h:>11s}" for _, h in COLS))
    for name, m in rows:
        cells = []
        for k, _ in COLS:
            v = m.get(k, np.nan)
            if k == "sharpe":
                cells.append(f"{v:11.2f}")
            elif k == "cambios/año":
                cells.append(f"{v:11.1f}")
            elif k == "bajo_agua_años":
                cells.append(f"{v:10.1f}a")
            else:
                cells.append(f"{pct(v):>11s}")
        say(f"{name:34s}" + "".join(cells))


# ================================================================ 6. BLOQUES DE PRUEBA
# Registro de resultados para el resumen: REG[(bloque, estrategia)] y ROB[(bloque, familia)]
REG: dict[tuple[str, str], dict] = {}
ROB: dict[tuple[str, str], dict] = {}
summary_rows: list[dict] = []


def segments(d: pd.DatetimeIndex):
    n = len(d)
    segs = [("completo", d[0], d[-1])]
    segs += [("mitad 1", d[0], d[n // 2 - 1]), ("mitad 2", d[n // 2], d[-1])]
    segs += [("tercio 1", d[0], d[n // 3 - 1]), ("tercio 2", d[n // 3], d[2 * n // 3 - 1]), ("tercio 3", d[2 * n // 3], d[-1])]
    return segs


def seg_values(u, W, segs, band, lag=LAG_MAIN, costs_pre1990=1.0):
    """{tramo: (rent. antes, rent. después, caída máx., curva, cambios, final tras impuestos)}"""
    out = {}
    for sname, s0, s1 in segs:
        eq, _, ntr, _ = simulate(u, W, s0, s1, lag=lag, taxes=False, band=band, costs_pre1990=costs_pre1990)
        _, ft, _, _ = simulate(u, W, s0, s1, lag=lag, taxes=True, band=band, costs_pre1990=costs_pre1990)
        out[sname] = (cagr_from(eq.iloc[0], eq.iloc[-1], eq.index[0], eq.index[-1]),
                      cagr_from(1.0, ft, eq.index[0], eq.index[-1]), float((eq / eq.cummax() - 1).min()), eq, ntr, ft)
    return out


def rel_verdict(v, b):
    """Criterio relativo frente a la referencia, para i=0 (antes) e i=1 (después de impuestos)."""
    res = {}
    for i, tag in [(0, "antes"), (1, "después")]:
        full = v["completo"][i] > b["completo"][i]
        halves = all(v[h][i] > b[h][i] for h in ("mitad 1", "mitad 2"))
        thirds = sum(v[h][i] > b[h][i] for h in ("tercio 1", "tercio 2", "tercio 3"))
        ddok = v["completo"][2] >= b["completo"][2]
        res[tag] = {"ok": full and halves and thirds >= 2 and ddok, "full": full, "halves": halves,
                    "thirds": thirds, "ddok": ddok}
    return res


def evaluate(bid: str, u: Universe, strats: dict, start, end, band_of: dict, title: str, lag=LAG_MAIN,
             costs_pre1990=1.0, brief=False):
    """Tabla completa + mitades/tercios + criterio relativo, antes y después de impuestos."""
    d = u.R.index[(u.R.index >= start) & (u.R.index <= end)]
    segs = segments(d)
    vals = {n: seg_values(u, W, segs, band_of.get(n, 0.02), lag, costs_pre1990) for n, W in strats.items()}
    bname = u.bench
    say(f"\n## {title}")
    say(f"Periodo {d[0].date()} → {d[-1].date()} ({(d[-1] - d[0]).days / 365.25:.1f} años). Ejecución lag {lag}. "
        f"Referencia (mercado) = «{bname}»." + (f" Costes ×{costs_pre1990:g} antes de 1990." if costs_pre1990 != 1 else ""))
    if not brief:
        beq = vals[bname]["completo"][3]
        table([(n, metrics(vals[n]["completo"][3], beq, u.rf, vals[n]["completo"][4], vals[n]["completo"][5]))
               for n in strats])
        say("\nPor tramos — Rent. antes de impuestos / después de impuestos / caída máx.:")
        say(f"{'estrategia':34s}" + "".join(f"{s[0]:>24s}" for s in segs[1:]))
        for n in strats:
            say(f"{n:34s}" + "".join(f"{vals[n][sn][0] * 100:6.1f}/{vals[n][sn][1] * 100:5.1f}/{vals[n][sn][2] * 100:6.1f}%"
                                     .rjust(24) for sn, _, _ in segs[1:]))
    say("\nCriterio relativo (frente a la referencia): gana en el completo, en las DOS mitades y en ≥2 de 3 tercios, "
        "y su caída máxima no es peor. «antes» = cuenta IRA/Roth; «después» = cuenta normal con impuestos. "
        f"«apta» = caída máxima mejor que {ABS_DD_LIMIT * 100:.0f} %.")
    for n in strats:
        v = vals[n]
        rv = rel_verdict(v, vals[bname])
        REG[(bid, n)] = {"bloque": title, "rel_antes": rv["antes"]["ok"], "rel_desp": rv["después"]["ok"],
                         "cagr": v["completo"][0], "cagr_t": v["completo"][1], "dd": v["completo"][2],
                         "start": d[0], "end": d[-1]}
        if n == bname:
            continue
        a, b_ = rv["antes"], rv["después"]
        say(f"  {n:32s} antes: {'PASA' if a['ok'] else 'no pasa':8s} (completo {'sí' if a['full'] else 'no'}, "
            f"mitades {'sí' if a['halves'] else 'no'}, tercios {a['thirds']}/3, caída {'ok' if a['ddok'] else 'PEOR'})  |  "
            f"después: {'PASA' if b_['ok'] else 'no pasa':8s} (completo {'sí' if b_['full'] else 'no'}, "
            f"mitades {'sí' if b_['halves'] else 'no'}, tercios {b_['thirds']}/3)  |  "
            f"caída {v['completo'][2] * 100:5.1f}% {'apta' if v['completo'][2] > ABS_DD_LIMIT else 'NO APTA'}")
    return vals


def robustness(bid, u, maker, label, start, end, band):
    say(f"\n### Robustez de la media ({label}) — antes/después de impuestos, caída máx., ¿pasa el criterio (antes/después)?")
    d = u.R.index[(u.R.index >= start) & (u.R.index <= end)]
    segs = segments(d)
    bv = seg_values(u, w_hold(u, u.bench), segs, 0.02)
    allp = {"antes": True, "después": True}
    worst_dd = 0.0
    for n in SMA_GRID:
        v = seg_values(u, maker(n), segs, band)
        rv = rel_verdict(v, bv)
        allp["antes"] &= rv["antes"]["ok"]
        allp["después"] &= rv["después"]["ok"]
        worst_dd = min(worst_dd, v["completo"][2])
        c = v["completo"]
        say(f"  media {n:3d}: {c[0] * 100:6.1f}% / {c[1] * 100:5.1f}%  caída {c[2] * 100:6.1f}%   "
            f"{'PASA' if rv['antes']['ok'] else 'no'} / {'PASA' if rv['después']['ok'] else 'no'}")
    b = bv["completo"]
    say(f"  referencia: {b[0] * 100:6.1f}% / {b[1] * 100:5.1f}%  caída {b[2] * 100:6.1f}%")
    say(f"  → pasan las 5 medias: antes {'SÍ' if allp['antes'] else 'NO'}, después {'SÍ' if allp['después'] else 'NO'}")
    ROB[(bid, label)] = {"antes": allp["antes"], "después": allp["después"], "worst_dd": worst_dd}


def episodes(u: Universe, strats: dict, band_of: dict, eps: list[tuple[str, str, str]]):
    say("\n### Episodios: rentabilidad del tramo / peor caída dentro del tramo (antes de impuestos)")
    say(f"{'episodio':26s}" + "".join(f"{n[:16]:>18s}" for n in strats))
    full = {n: simulate(u, W, u.R.index[WARMUP], u.R.index[-1], taxes=False, band=band_of.get(n, 0.02))[0]
            for n, W in strats.items()}
    for lab, a, b in eps:
        cells = []
        for n in strats:
            e = full[n].loc[a:b]
            if len(e) < 2:
                cells.append(f"{'—':>18s}")
                continue
            prev = full[n][full[n].index < pd.Timestamp(a)]
            base = prev.iloc[-1] if len(prev) else e.iloc[0]
            seg = pd.concat([pd.Series([base]), e.reset_index(drop=True)])
            ret = e.iloc[-1] / base - 1
            dd = (seg / seg.cummax() - 1).min()
            cells.append(f"{ret * 100:7.1f}/{dd * 100:6.1f}%")
        say(f"{lab:26s}" + "".join(f"{c:>18s}" for c in cells))
    return full


def worst_days(strats, full, k=5):
    say("\n### Peores días de cada estrategia (antes de impuestos)")
    for n in strats:
        r = full[n].pct_change().dropna().nsmallest(k)
        say(f"  {n:32s} " + ", ".join(f"{d.date()} {v * 100:.1f}%" for d, v in r.items()))


def guarded(name, fn):
    """Un bloque que falla no tumba a los demás: se anota el error y se sigue."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        say(f"\n!!! ERROR en el bloque «{name}»: {type(e).__name__}: {e}")
        say(traceback.format_exc())
        return None


# ================================================================ 7. FAMILIAS Y RESUMEN
# Una familia = una idea con sus vecinos. Solo es robusta si pasan TODOS los miembros (p. ej. VT 15/20/25 %)
# y las 5 medias (150-250). «analog» = la misma regla sobre el mercado desde 1926 (bloque A), para el tope de caída.
FAMILIES = [
    # nombre, miembros [(bloque, estrategia)], robustez de medias [(bloque, etiqueta)], análogos 1926 [(bloque, estrategia)]
    ("LRS 1x mercado (1926)", [("A", "LRS 1x")], [("A", "LRS 1x")], []),
    ("LRS 2x mercado (1926)", [("A", "LRS 2x")], [("A", "LRS 2x")], []),
    ("LRS 3x mercado (1926)", [("A", "LRS 3x")], [("A", "LRS 3x")], []),
    ("LRS 1.5x mercado (1926)", [("A", "LRS 1.5x")], [("A", "LRS 1.5x")], []),
    ("LRS 1.5x SPY/SSO (1988)", [("B", "LRS 1.5x SPY/SSO")], [("B", "LRS 1.5x SPY/SSO")], [("A", "LRS 1.5x")]),
    ("LRS 1.5x SSO real (2006)", [("C", "LRS 1.5x con SSO REAL")], [("C", "LRS 1.5x con SSO REAL")], [("A", "LRS 1.5x")]),
    ("VT con filtro mercado (1926)", [("A", "VT 15 % con filtro"), ("A", "VT 20 % con filtro"), ("A", "VT 25 % con filtro")],
     [("A", "VT 20 %")], []),
    ("VT sin filtro mercado (1926)", [("A", "VT 15 % sin filtro"), ("A", "VT 20 % sin filtro"), ("A", "VT 25 % sin filtro")], [], []),
    ("mantener 2x mercado (1926)", [("A", "mantener 2x (sin filtro)")], [], []),
    ("LRS 1x SPY", [("B", "LRS 1x SPY")], [("B", "LRS 1x SPY")], [("A", "LRS 1x")]),
    ("LRS 2x SSO", [("B", "LRS 2x SSO")], [("B", "LRS 2x SSO")], [("A", "LRS 2x")]),
    ("LRS 3x UPRO", [("B", "LRS 3x UPRO")], [("B", "LRS 3x UPRO")], [("A", "LRS 3x")]),
    ("VT SPY/SSO", [("B", "VT 15 % SPY/SSO"), ("B", "VT 20 % SPY/SSO"), ("B", "VT 25 % SPY/SSO")],
     [("B", "VT 20 % SPY/SSO")], [("A", "VT 15 % con filtro"), ("A", "VT 20 % con filtro"), ("A", "VT 25 % con filtro")]),
    ("mantener SSO", [("B", "mantener SSO (sin filtro)")], [], [("A", "mantener 2x (sin filtro)")]),
    ("mantener QQQ", [("B", "QQQ")], [], [("A", "MKT")]),
    ("LRS 1x QQQ", [("B", "LRS 1x QQQ")], [("B", "LRS 1x QQQ")], [("A", "LRS 1x")]),
    ("LRS 2x QLD", [("B", "LRS 2x QLD")], [("B", "LRS 2x QLD")], [("A", "LRS 2x")]),
    ("LRS 3x TQQQ", [("B", "LRS 3x TQQQ")], [("B", "LRS 3x TQQQ")], [("A", "LRS 3x")]),
    ("VT QQQ/QLD", [("B", "VT 15 % QQQ/QLD"), ("B", "VT 20 % QQQ/QLD"), ("B", "VT 25 % QQQ/QLD")],
     [("B", "VT 20 % QQQ/QLD")], [("A", "VT 15 % con filtro"), ("A", "VT 20 % con filtro"), ("A", "VT 25 % con filtro")]),
    ("mantener QLD", [("B", "mantener QLD (sin filtro)")], [], [("A", "mantener 2x (sin filtro)")]),
    ("cesta tendencia 1x", [("D", "cesta tendencia 1x")], [("D", "cesta tendencia 1x")], None),
    ("cesta tendencia 2x", [("D", "cesta tendencia 2x")], [("D", "cesta tendencia 2x")], None),
    ("cesta VT", [("D", "cesta VT 10 % (≤2x)"), ("D", "cesta VT 15 % (≤2x)"), ("D", "cesta VT 20 % (≤2x)")],
     [("D", "cesta VT 15 %")], None),
]


def summary():
    say("\n## RESUMEN AUTOMÁTICO")
    say("Familia = una idea con sus vecinos. Robusta = pasan TODOS sus miembros y las 5 medias (150-250); si pasa solo una "
        "parte, se trata como suerte. «Apta para Oier» = caída máxima mejor que "
        f"{ABS_DD_LIMIT * 100:.0f} % en su periodo Y en la misma regla aplicada al mercado desde 1926 (bloque A). "
        "Para el Nasdaq el análogo de 1926 es el mercado entero (más tranquilo) → el tope es optimista para el Nasdaq. "
        "La cesta (desde 2005) no tiene análogo de 1926: queda «sin probar en 1929-32» y no se declara apta.")
    rows = []
    for fam, members, smas, analogs in FAMILIES:
        got = [REG.get(m) for m in members]
        if any(g is None for g in got):
            say(f"  {fam:32s} sin datos (bloque no ejecutado)")
            continue
        r = {"familia": fam}
        for tag, key in [("antes", "rel_antes"), ("después", "rel_desp")]:
            k = sum(g[key] for g in got)
            sma_ok = all(ROB.get(s, {}).get(tag, False) for s in smas) if smas else True
            sma_missing = any(s not in ROB for s in smas)
            r[f"rel_{tag}"] = (k == len(got)) and sma_ok and not sma_missing
            r[f"k_{tag}"] = k
            r["n"] = len(got)
            r[f"det_{tag}"] = (f"{k}/{len(got)} miembros" + (f", medias {'ok' if sma_ok else 'NO'}" if smas else ""))
        dds = [g["dd"] for g in got] + [ROB[s]["worst_dd"] for s in smas if s in ROB]
        if analogs:
            for a in analogs:
                if a in REG:
                    dds.append(REG[a]["dd"])
                if a in ROB:
                    dds.append(ROB[a]["worst_dd"])
        worst = min(dds)
        if analogs is None:
            r["apta"], r["apta_txt"] = False, f"sin probar en 1929-32 (peor caída {worst * 100:.0f} %)"
        else:
            r["apta"] = worst > ABS_DD_LIMIT
            r["apta_txt"] = f"peor caída {worst * 100:.0f} % → {'apta' if r['apta'] else 'NO APTA'}"
        main_m = got[len(got) // 2]
        r.update({"cagr": main_m["cagr"], "cagr_t": main_m["cagr_t"], "dd": main_m["dd"],
                  "periodo": f"{main_m['start'].year}-{main_m['end'].year}"})
        c5 = [REG.get(("A5", m[1])) for m in members if m[0] == "A"]
        if c5 and all(x is not None for x in c5):
            r["c5_antes"] = all(x["rel_antes"] for x in c5)
            r["c5_desp"] = all(x["rel_desp"] for x in c5)
        rows.append(r)
        extra = ""
        if "c5_antes" in r:
            extra = f" | costes ×5 pre-1990: antes {'PASA' if r['c5_antes'] else 'no'}, después {'PASA' if r['c5_desp'] else 'no'}"
            if r["c5_antes"] != r["rel_antes"] or r["c5_desp"] != r["rel_después"]:
                extra += "  ← EL VEREDICTO CAMBIA CON COSTES ×5"
        say(f"  {fam:32s} relativo antes: {'PASA' if r['rel_antes'] else 'no':4s} ({r['det_antes']}) | "
            f"después: {'PASA' if r['rel_después'] else 'no':4s} ({r['det_después']}) | {r['apta_txt']}{extra}")
        summary_rows.append(r)

    def ranking(tag, key_rel, key_ret, title):
        say(f"\n### {title}")
        ok = [r for r in rows if r[key_rel] and r["apta"]]
        if not ok:
            say("  Ninguna familia pasa a la vez el criterio relativo y el tope de caída.")
            return
        for i, r in enumerate(sorted(ok, key=lambda x: -x[key_ret]), 1):
            say(f"  {i}. {r['familia']:32s} {r[key_ret] * 100:5.1f} %/año ({r['periodo']}), caída {r['dd'] * 100:.0f} %")

    ranking("después", "rel_después", "cagr_t", "Candidatas para cuenta NORMAL (pasan relativo después de impuestos + aptas), "
            "ordenadas por rentabilidad DESPUÉS de impuestos")
    ranking("antes", "rel_antes", "cagr", "Candidatas para cuenta IRA/Roth (pasan relativo antes de impuestos + aptas), "
            "ordenadas por rentabilidad antes de impuestos")
    part = [r for r in rows if r["n"] > 1 and (0 < r["k_antes"] < r["n"] or 0 < r["k_después"] < r["n"])]
    if part:
        say("\nPasan solo ALGUNOS de sus vecinos (= suerte, no cuentan como candidatas): " + ", ".join(
            f"{r['familia']} (antes {r['k_antes']}/{r['n']}, después {r['k_después']}/{r['n']})" for r in part))


# ================================================================ 8. PRINCIPAL
def main():
    t0 = time.time()
    try:
        _main()
    except Exception as e:  # noqa: BLE001
        say(f"\n!!! ERROR general: {type(e).__name__}: {e}")
        say(traceback.format_exc())
    finally:
        say(f"\nTiempo de ejecución: {time.time() - t0:.0f} s")
        OUT_TXT.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        pd.DataFrame(summary_rows).to_csv(OUT_CSV, index=False)
        print(f"\nGuardado en {OUT_TXT} y {OUT_CSV}")


def _main():
    say("# Investigación 2026-11-21 — Segundo sistema: rotación apalancada (LRS) y variantes, acciones/ETF")
    say(f"Generado {pd.Timestamp.now('UTC'):%Y-%m-%d %H:%M} UTC" + ("  *** DATOS FALSOS (RESEARCH_FAKE=1) ***" if FAKE else ""))
    say("Supuestos: costes por lado 1x 0.02 %, apalancados 0.05 % (UGL/UBT 0.15 %), letras 0.02 %; letra REAL de cada "
        "día (Ken French RF; después ^IRX); apalancados sintéticos = L×subyacente − (L−1)×(letra+margen calibrado) − "
        "gastos; impuestos 24 % corto / 15 % largo / Florida 0 %; GLD a largo plazo 28 % (coleccionable), UGL 60/40; "
        "dividendo 1.5 % en los ETF 1x de acciones (15 %), cupón de TLT 3.5 % (24 %); sin la deducción de 3.000 $ de "
        "pérdidas (conservador); el impuesto de cada año se paga reduciendo la cartera en proporción, sin realizar más "
        "ganancias (aproximación); sin wash sale (aplazaría pérdidas, no las elimina). Señal sobre el índice de "
        "rentabilidad total. Un apalancado no baja de −100 % en un día. Escenario fiscal, no asesoría.")
    fr, y, src = load_all()

    say("\n## 0. Datos")
    if fr is not None:
        say(f"  Ken French mercado+RF: {fr.index[0].date()} → {fr.index[-1].date()} ({len(fr)} días)")
    else:
        say("  Ken French: NO DISPONIBLE (el bloque A no se puede ejecutar)")
    for t, s in y.items():
        say(f"  {t:9s}: {s.index[0].date()} → {s.index[-1].date()} ({len(s)} días)  fuente {src[t]}")
    if any(v == "stooq" for v in src.values()):
        say("  AVISO: alguna serie viene de Stooq (plan B). Stooq puede no incluir dividendos en el cierre: los 1x "
            "salen ~1-2 %/año peor de lo real con esa fuente (conservador para mantener y para las estrategias 1x).")

    # --- subyacentes
    spx = spx_src = None
    if "^SP500TR" in y and len(y["^SP500TR"]) > 5000:
        spx, spx_src = rets(y["^SP500TR"].rename("^SP500TR")) - ER["SPY"] / 252, "^SP500TR − gastos de SPY"
    elif "SPY" in y:
        spx, spx_src = rets(y["SPY"].rename("SPY")), f"SPY ({src['SPY']})"
    ndx = None
    if "QQQ" in y:
        q = rets(y["QQQ"].rename("QQQ"))
        parts = [q]
        if "^NDX" in y:
            n0 = rets(y["^NDX"].rename("^NDX"))
            parts.insert(0, n0[n0.index < q.index[0]] - ER["QQQ"] / 252)
        ndx = pd.concat(parts).sort_index()
    say(f"  S&P 500 = {spx_src or 'NO DISPONIBLE'}; Nasdaq-100 = "
        f"{'^NDX (solo precio, ≈ −0.7 %/año) hasta QQQ, luego QQQ' if ndx is not None else 'NO DISPONIBLE'}.")
    idx_all = fr.index if fr is not None else pd.DatetimeIndex([])
    if spx is not None:
        idx_all = idx_all.union(spx.index)
    rf_all = build_rf(fr, y, idx_all)

    spreads = guarded("1. calibración de apalancados", lambda: block_calibration(y, spx, ndx, rf_all)) or {}
    s2 = spreads.get("SSO", 0.005)
    sp = {"s2": s2, "s3": spreads.get("UPRO", s2), "q2": spreads.get("QLD", s2), "q3": spreads.get("TQQQ", spreads.get("QLD", s2)),
          "g2": spreads.get("UGL", s2), "t2": spreads.get("UBT", s2)}
    say(f"\nMárgenes usados: S&P 2x {sp['s2'] * 100:.2f} %, 3x {sp['s3'] * 100:.2f} %; Nasdaq 2x {sp['q2'] * 100:.2f} %, "
        f"3x {sp['q3'] * 100:.2f} %; oro 2x {sp['g2'] * 100:.2f} %; bonos 2x {sp['t2'] * 100:.2f} % "
        "(si falta un ETF real: el del S&P 2x, o 0.50 % por defecto).")

    if fr is not None:
        guarded("A. mercado desde 1926", lambda: block_A(fr, sp))
    if spx is not None:
        guarded("B. S&P y Nasdaq desde 1988", lambda: block_B(spx, ndx, rf_all, sp))
        guarded("C. SSO real", lambda: block_C(y, spx, rf_all, sp))
        guarded("D. cesta", lambda: block_D(y, spx, ndx, rf_all, sp))
    guarded("resumen", summary)
    guarded("veredicto final", final_verdict)


EPS = [("1929-09 → 1932-06", "1929-09-01", "1932-06-30"), ("1937-03 → 1938-03", "1937-03-01", "1938-03-31"),
       ("1973-01 → 1974-12", "1973-01-01", "1974-12-31"), ("oct-1987", "1987-10-01", "1987-10-30"),
       ("2000-03 → 2002-10", "2000-03-24", "2002-10-09"), ("2007-10 → 2009-03", "2007-10-09", "2009-03-09"),
       ("2020-02 → 2020-03", "2020-02-19", "2020-03-23"), ("2022", "2022-01-03", "2022-12-30"),
       ("2015-05 → 2016-02 (lateral)", "2015-05-21", "2016-02-11")]


def band_for(name):
    if "1.5x" in name:
        return BAND["basket"]
    return BAND["vt"] if name.startswith("VT") else (BAND["basket"] if name.startswith("cesta") else BAND["lrs"])


def block_calibration(y, spx, ndx, rf_all):
    say("\n## 1. Apalancados sintéticos frente a los reales (año a año)")
    say("Margen = lo que el ETF paga sobre la letra por la parte prestada, calibrado minimizando el error anual. "
        "«gayed_1pct» = L×subyacente − 1 %/año fijo (supuesto del paper de 2016).")
    unders = {"SSO": (spx, 2), "UPRO": (spx, 3), "QLD": (ndx, 2), "TQQQ": (ndx, 3)}
    if "GLD" in y:
        unders["UGL"] = (rets(y["GLD"].rename("GLD")), 2)
    if "TLT" in y:
        unders["UBT"] = (rets(y["TLT"].rename("TLT")), 2)
    spreads = {}
    for nm, (ur, L) in unders.items():
        if ur is None or nm not in y:
            say(f"  {nm}: sin datos, no se calibra")
            continue
        cal = calibrate(nm, y[nm], ur, rf_all.reindex(ur.index).ffill().fillna(0), L, ER[nm])
        if cal is None:
            continue
        spreads[nm] = cal["spread"]
        say(f"\n### {nm} ({L}x) {cal['start']} → {cal['end']}: margen calibrado {cal['spread'] * 100:.2f} %/año; "
            f"error anual (RMSE): sintético {cal['rmse']['sintético'] * 100:.2f} pp, margen 0 {cal['rmse']['sint_margen0'] * 100:.2f} pp, "
            f"Gayed 1 % fijo {cal['rmse']['gayed_1pct'] * 100:.2f} pp; error de seguimiento diario anualizado {cal['te_daily'] * 100:.2f} %")
        say(f"  {'año':>5s} {'real':>8s} {'sint.':>8s} {'dif':>7s} {'Gayed1%':>8s} {'dif':>7s} {'coste apal.':>11s}")
        for yr_, r_ in cal["tab"].iterrows():
            say(f"  {yr_:5d} {r_['real'] * 100:7.1f}% {r_['sintético'] * 100:7.1f}% {r_['dif_sint'] * 100:6.1f}p "
                f"{r_['gayed_1pct'] * 100:7.1f}% {r_['dif_gayed'] * 100:6.1f}p {r_['coste_apal_%/año']:9.2f}%")
    return spreads


def build_M(mkt_r, rfm, sp):
    R = pd.DataFrame({"MKT": mkt_r, "MKT2x": lev(mkt_r, rfm, 2, ER["SSO"], sp["s2"]), "MKT3x": lev(mkt_r, rfm, 3, ER["UPRO"], sp["s3"])})
    meta = {"MKT": {"L": 1, "cost": "1x", "div": True}, "MKT2x": {"L": 2, "cost": "lev"}, "MKT3x": {"L": 3, "cost": "lev"}}
    return Universe("mercado1926", R, rfm, meta, "MKT")


def strats_M(u):
    S = {"MKT": w_hold(u, "MKT"), "mantener 2x (sin filtro)": w_hold(u, "MKT2x"),
         "LRS 1x": w_lrs(u, "MKT", "MKT"), "LRS 1.5x": w_lrs15(u, "MKT", "MKT", "MKT2x"),
         "LRS 2x": w_lrs(u, "MKT", "MKT2x"), "LRS 3x": w_lrs(u, "MKT", "MKT3x")}
    for tv in TV_GRID:
        S[f"VT {int(tv * 100)} % con filtro"] = w_vt(u, "MKT", "MKT", "MKT2x", tv=tv)
    for tv in TV_GRID:
        S[f"VT {int(tv * 100)} % sin filtro"] = w_vt(u, "MKT", "MKT", "MKT2x", tv=tv, filt=False)
    return S


def block_A(fr, sp):
    mk = fr["mkt"] - ER["MKT"] / 252
    rfm = fr["rf"]
    M = build_M(mk, rfm, sp)
    SM = strats_M(M)
    band_of = {n: band_for(n) for n in SM}
    st0, st1 = M.R.index[WARMUP], M.R.index[-1]
    evaluate("A", M, SM, st0, st1, band_of, "A. Mercado de EE. UU. desde 1926 (Ken French, todo el mercado ≈ S&P)")
    robustness("A", M, lambda n: w_lrs(M, "MKT", "MKT", n), "LRS 1x", st0, st1, BAND["lrs"])
    robustness("A", M, lambda n: w_lrs(M, "MKT", "MKT2x", n), "LRS 2x", st0, st1, BAND["lrs"])
    robustness("A", M, lambda n: w_lrs(M, "MKT", "MKT3x", n), "LRS 3x", st0, st1, BAND["lrs"])
    robustness("A", M, lambda n: w_lrs15(M, "MKT", "MKT", "MKT2x", n), "LRS 1.5x", st0, st1, BAND["basket"])
    robustness("A", M, lambda n: w_vt(M, "MKT", "MKT", "MKT2x", n=n, tv=0.20), "VT 20 %", st0, st1, BAND["vt"])
    guarded("A5. costes ×5 antes de 1990", lambda: evaluate(
        "A5", M, SM, st0, st1, band_of, "A5. Sesgo de costes: el mismo bloque A con costes ×5 antes de 1990 (solo veredictos)",
        costs_pre1990=COST_MULT_PRE1990, brief=True))
    say("\n### Ejecución el mismo día (lag 1, ~15:45) frente al cierre del día siguiente (lag 2), completo, antes de impuestos")
    for n in ["LRS 1x", "LRS 2x", "LRS 3x", "VT 20 % con filtro"]:
        e1 = simulate(M, SM[n], st0, st1, lag=1, band=band_of[n])[0]
        e2 = simulate(M, SM[n], st0, st1, lag=2, band=band_of[n])[0]
        say(f"  {n:24s} lag1 {cagr_from(e1.iloc[0], e1.iloc[-1], e1.index[0], e1.index[-1]) * 100:6.1f}% "
            f"caída {(e1 / e1.cummax() - 1).min() * 100:6.1f}%   |  lag2 {cagr_from(e2.iloc[0], e2.iloc[-1], e2.index[0], e2.index[-1]) * 100:6.1f}% "
            f"caída {(e2 / e2.cummax() - 1).min() * 100:6.1f}%")
    show = {k: SM[k] for k in ["MKT", "mantener 2x (sin filtro)", "LRS 1x", "LRS 1.5x", "LRS 2x", "LRS 3x", "VT 20 % con filtro",
                               "VT 20 % sin filtro"]}
    full = episodes(M, show, band_of, EPS)
    worst_days(show, full)
    ex = exposure(M, SM["VT 20 % con filtro"])
    say(f"\nExposición media del VT 20 % cuando está dentro: {ex[ex > 0].mean():.2f}x; % del tiempo a tope 2x: "
        f"{(ex >= 1.999).mean() * 100:.0f} %; % del tiempo fuera: {(ex == 0).mean() * 100:.0f} %. "
        f"LRS dentro del mercado: {SM['LRS 2x']['MKT2x'].mean() * 100:.0f} % del tiempo.")
    guarded("estrés", lambda: block_stress(mk, rfm, sp, M, SM, band_of, st0, st1))


def block_stress(mk, rfm, sp, M, SM, band_of, st0, st1):
    say("\n## Estrés: hueco de un día del mercado con la posición completa dentro")
    say("Pérdida del día = L × hueco (un ETF 2x desaparece con −50 %, uno 3x con −33.3 %; en la realidad el fondo puede "
        "cerrar o reiniciarse antes). La media de 200 no protege de un hueco: actúa después.")
    say(f"  {'hueco':>7s} {'1x':>8s} {'2x':>8s} {'3x':>8s}")
    for g in [-0.10, -0.15, -0.20, -0.25, -0.33, -0.50]:
        say(f"  {g * 100:6.0f}% " + "".join(f"{max(-1.0, L * g) * 100:7.0f}% " for L in (1, 2, 3)))
    say("\nHueco de −20 % inyectado en dos máximos de mercado (en la realidad el S&P estaba sobre su media de 200 esos "
        "días); exposición la víspera, pérdida del día y peor punto en los 12 meses siguientes frente a la víspera:")
    for day in ["2007-10-10", "2022-01-04"]:
        mk2 = mk.copy()
        dts = mk2.index[mk2.index >= pd.Timestamp(day)]
        if len(dts) == 0:
            continue
        gd = dts[0]
        mk2.loc[gd] = -0.20
        Mi = build_M(mk2, rfm, sp)
        Si = strats_M(Mi)
        say(f"  hueco el {gd.date()}:")
        for n in ["MKT", "LRS 1x", "LRS 1.5x", "LRS 2x", "LRS 3x", "VT 20 % con filtro"]:
            e = simulate(Mi, Si[n], st0, st1, band=band_of[n])[0]
            e0 = simulate(M, SM[n], st0, st1, band=band_of[n])[0]
            expo = float(exposure(Mi, Si[n].shift(LAG_MAIN - 1).fillna(0)).loc[:gd].iloc[-2])
            day_r = float(e.loc[gd] / e.loc[:gd].iloc[-2] - 1)
            pk = float(e.loc[:gd].iloc[-2])
            dd12 = float(e.loc[gd:gd + pd.Timedelta(days=365)].min() / pk - 1)
            say(f"    {n:24s} exposición {expo:4.2f}x  día {day_r * 100:6.1f}%  peor punto en 12 meses {dd12 * 100:6.1f}%  "
                f"rent. total {cagr_from(e.iloc[0], e.iloc[-1], e.index[0], e.index[-1]) * 100:5.1f}% "
                f"(sin hueco {cagr_from(e0.iloc[0], e0.iloc[-1], e0.index[0], e0.index[-1]) * 100:5.1f}%)  "
                f"caída máx. {(e / e.cummax() - 1).min() * 100:6.1f}% (sin hueco {(e0 / e0.cummax() - 1).min() * 100:6.1f}%)")


def block_B(spx, ndx, rf_all, sp):
    idx = spx.index.intersection(ndx.index) if ndx is not None else spx.index
    rfb = rf_all.reindex(idx).ffill().fillna(0)
    s1 = spx.reindex(idx).fillna(0)
    R = {"SPY": s1, "SSO": lev(s1, rfb, 2, ER["SSO"], sp["s2"]), "UPRO": lev(s1, rfb, 3, ER["UPRO"], sp["s3"])}
    meta = {"SPY": {"L": 1, "cost": "1x", "div": True}, "SSO": {"L": 2, "cost": "lev"}, "UPRO": {"L": 3, "cost": "lev"}}
    if ndx is not None:
        nq = ndx.reindex(idx).fillna(0)
        R.update({"QQQ": nq, "QLD": lev(nq, rfb, 2, ER["QLD"], sp["q2"]), "TQQQ": lev(nq, rfb, 3, ER["TQQQ"], sp["q3"])})
        meta.update({"QQQ": {"L": 1, "cost": "1x", "div": True}, "QLD": {"L": 2, "cost": "lev"}, "TQQQ": {"L": 3, "cost": "lev"}})
    S = Universe("sp_nasdaq1988", pd.DataFrame(R), rfb, meta, "SPY")
    SS = {"SPY": w_hold(S, "SPY"), "mantener SSO (sin filtro)": w_hold(S, "SSO"), "LRS 1x SPY": w_lrs(S, "SPY", "SPY"),
          "LRS 1.5x SPY/SSO": w_lrs15(S, "SPY", "SPY", "SSO"),
          "LRS 2x SSO": w_lrs(S, "SPY", "SSO"), "LRS 3x UPRO": w_lrs(S, "SPY", "UPRO")}
    for tv in TV_GRID:
        SS[f"VT {int(tv * 100)} % SPY/SSO"] = w_vt(S, "SPY", "SPY", "SSO", tv=tv)
    if ndx is not None:
        SS.update({"QQQ": w_hold(S, "QQQ"), "mantener QLD (sin filtro)": w_hold(S, "QLD"),
                   "LRS 1x QQQ": w_lrs(S, "QQQ", "QQQ"), "LRS 2x QLD": w_lrs(S, "QQQ", "QLD"),
                   "LRS 3x TQQQ": w_lrs(S, "QQQ", "TQQQ")})
        for tv in TV_GRID:
            SS[f"VT {int(tv * 100)} % QQQ/QLD"] = w_vt(S, "QQQ", "QQQ", "QLD", tv=tv)
    band_S = {n: band_for(n) for n in SS}
    b0, b1 = S.R.index[WARMUP], S.R.index[-1]
    evaluate("B", S, SS, b0, b1, band_S, "B. S&P 500 y Nasdaq-100 desde 1988 (sintéticos con letra real y margen calibrado)")
    for lab, sig, inst in [("LRS 1x SPY", "SPY", "SPY"), ("LRS 2x SSO", "SPY", "SSO"), ("LRS 3x UPRO", "SPY", "UPRO")] + (
            [("LRS 1x QQQ", "QQQ", "QQQ"), ("LRS 2x QLD", "QQQ", "QLD"), ("LRS 3x TQQQ", "QQQ", "TQQQ")] if ndx is not None else []):
        robustness("B", S, lambda n, sig=sig, inst=inst: w_lrs(S, sig, inst, n), lab, b0, b1, BAND["lrs"])
    robustness("B", S, lambda n: w_vt(S, "SPY", "SPY", "SSO", n=n, tv=0.20), "VT 20 % SPY/SSO", b0, b1, BAND["vt"])
    robustness("B", S, lambda n: w_lrs15(S, "SPY", "SPY", "SSO", n), "LRS 1.5x SPY/SSO", b0, b1, BAND["basket"])
    if ndx is not None:
        robustness("B", S, lambda n: w_vt(S, "QQQ", "QQQ", "QLD", n=n, tv=0.20), "VT 20 % QQQ/QLD", b0, b1, BAND["vt"])
    episodes(S, {k: SS[k] for k in SS if k in ("SPY", "LRS 1.5x SPY/SSO", "LRS 2x SSO", "LRS 3x UPRO", "VT 20 % SPY/SSO", "LRS 2x QLD",
                                               "VT 20 % QQQ/QLD")}, band_S, EPS[3:])


def block_C(y, spx, rf_all, sp):
    if "SSO" not in y:
        say("\n## C. Sin datos del SSO real")
        return
    r_sso = rets(y["SSO"].rename("SSO"))
    i2 = r_sso.index.intersection(spx.index)
    if len(i2) <= WARMUP + 500:
        return
    rfc = rf_all.reindex(i2).ffill().fillna(0)
    s1 = spx.reindex(i2).fillna(0)
    Rr = pd.DataFrame({"SPY": s1, "SSO_real": r_sso.reindex(i2).fillna(0), "SSO_sint": lev(s1, rfc, 2, ER["SSO"], sp["s2"])})
    mr = {"SPY": {"L": 1, "cost": "1x", "div": True}, "SSO_real": {"L": 2, "cost": "lev"}, "SSO_sint": {"L": 2, "cost": "lev"}}
    C = Universe("sso_real", Rr, rfc, mr, "SPY")
    SC = {"SPY": w_hold(C, "SPY"), "mantener SSO real": w_hold(C, "SSO_real"),
          "LRS 1.5x con SSO REAL": w_lrs15(C, "SPY", "SPY", "SSO_real"),
          "LRS 2x con SSO REAL": w_lrs(C, "SPY", "SSO_real"), "LRS 2x con SSO sintético": w_lrs(C, "SPY", "SSO_sint")}
    c0, c1 = C.R.index[WARMUP], C.R.index[-1]
    evaluate("C", C, SC, c0, c1, {n: band_for(n) for n in SC}, "C. Comprobación: misma regla con el SSO REAL (desde 2006)")
    robustness("C", C, lambda n: w_lrs15(C, "SPY", "SPY", "SSO_real", n), "LRS 1.5x con SSO REAL", c0, c1, BAND["basket"])


def block_D(y, spx, ndx, rf_all, sp):
    if not (all(t in y for t in ("GLD", "TLT")) and ndx is not None):
        say("\n## D. Sin datos para la cesta (GLD/TLT/QQQ)")
        return
    g, tl = rets(y["GLD"].rename("GLD")), rets(y["TLT"].rename("TLT"))
    i3 = spx.index.intersection(ndx.index).intersection(g.index).intersection(tl.index)
    rfd = rf_all.reindex(i3).ffill().fillna(0)
    Rd = pd.DataFrame({"SPY": spx.reindex(i3), "QQQ": ndx.reindex(i3), "GLD": g.reindex(i3), "TLT": tl.reindex(i3)}).fillna(0)
    for one, two, er, s_ in [("SPY", "SSO", ER["SSO"], sp["s2"]), ("QQQ", "QLD", ER["QLD"], sp["q2"]),
                             ("GLD", "UGL", ER["UGL"], sp["g2"]), ("TLT", "UBT", ER["UBT"], sp["t2"])]:
        Rd[two] = lev(Rd[one], rfd, 2, er, s_)
    md = {"SPY": {"L": 1, "cost": "1x", "div": True}, "QQQ": {"L": 1, "cost": "1x", "div": True},
          "GLD": {"L": 1, "cost": "1x", "gm": 1}, "TLT": {"L": 1, "cost": "1x", "inc": (BOND_YIELD, TAX_ST)},
          "SSO": {"L": 2, "cost": "lev"}, "QLD": {"L": 2, "cost": "lev"},
          "UGL": {"L": 2, "cost": "lev_thin", "gm": 2}, "UBT": {"L": 2, "cost": "lev_thin"}}
    D = Universe("cesta2005", Rd, rfd, md, "SPY")
    assets = [("SPY", "SPY", "SSO"), ("QQQ", "QQQ", "QLD"), ("GLD", "GLD", "UGL"), ("TLT", "TLT", "UBT")]
    SD = {"SPY": w_hold(D, "SPY"), "LRS 2x SSO": w_lrs(D, "SPY", "SSO"),
          "cesta tendencia 1x": w_basket(D, assets, mode="1x"), "cesta tendencia 2x": w_basket(D, assets, mode="2x")}
    for tv in TV_BASKET_GRID:
        SD[f"cesta VT {int(tv * 100)} % (≤2x)"] = w_basket(D, assets, mode="vt", tv=tv)
    band_D = {n: band_for(n) for n in SD}
    d0, d1 = D.R.index[WARMUP], D.R.index[-1]
    evaluate("D", D, SD, d0, d1, band_D,
             "D. Cesta con tendencia (S&P, Nasdaq, oro, bonos largos; cada uno 25 % si está sobre su media de 200)")
    robustness("D", D, lambda n: w_basket(D, assets, n=n, mode="1x"), "cesta tendencia 1x", d0, d1, BAND["basket"])
    robustness("D", D, lambda n: w_basket(D, assets, n=n, mode="2x"), "cesta tendencia 2x", d0, d1, BAND["basket"])
    robustness("D", D, lambda n: w_basket(D, assets, n=n, mode="vt", tv=0.15), "cesta VT 15 %", d0, d1, BAND["basket"])
    episodes(D, {k: SD[k] for k in ("SPY", "LRS 2x SSO", "cesta tendencia 1x", "cesta tendencia 2x", "cesta VT 15 % (≤2x)")},
             band_D, EPS[5:])
    say("Notas: UGL y UBT (oro y bonos 2x) son fondos pequeños: coste 0.15 %/lado y riesgo de cierre del fondo. "
        "GLD tributa a largo plazo como coleccionable (28 %), UGL con la regla 60/40 (≈ 18.6 %).")


FAM15 = ["LRS 1.5x mercado (1926)", "LRS 1.5x SPY/SSO (1988)", "LRS 1.5x SSO real (2006)"]


def final_verdict():
    """Regla fijada ANTES de ver los resultados (crítica del 22-nov): la 1.5x solo se monta si pasa en TODO —
    relativo antes y después de impuestos, las 5 medias y apta (−60 %) — en A (1926), B (1988) y C (SSO real).
    Si no, la recomendación final es no montar un bot de acciones."""
    say("\n## VEREDICTO FINAL (regla fijada antes de ver los resultados)")
    say("Referencia explícita del mejor plan sencillo: mantener el índice en una Roth IRA (= «antes de impuestos»: "
        "sin impuestos al retirar si se cumplen las reglas de la Roth) frente a una cuenta normal.")
    say(f"  {'bloque':10s} {'estrategia':28s} {'Roth/IRA':>9s} {'normal':>9s} {'caída':>8s}")
    for bid, names in [("A", ["MKT", "LRS 1x", "LRS 1.5x", "LRS 2x"]),
                       ("B", ["SPY", "LRS 1x SPY", "LRS 1.5x SPY/SSO", "LRS 2x SSO"]),
                       ("C", ["SPY", "LRS 1.5x con SSO REAL", "LRS 2x con SSO REAL"])]:
        for n in names:
            g = REG.get((bid, n))
            if g is None:
                continue
            lab = ("mantener " + n) if n in ("MKT", "SPY") else n
            say(f"  {bid + ' ' + str(g['start'].year) + '-' + str(g['end'].year):10s} {lab:28s} "
                f"{g['cagr'] * 100:8.1f}% {g['cagr_t'] * 100:8.1f}% {g['dd'] * 100:7.1f}%")
    rows = {r["familia"]: r for r in summary_rows if isinstance(r, dict) and "familia" in r}
    missing = [f for f in FAM15 if f not in rows]
    if missing:
        say(f"\n  No se puede decidir: faltan bloques ({', '.join(missing)}).")
        return
    ok_all = all(rows[f]["rel_antes"] and rows[f]["rel_después"] and rows[f]["apta"] for f in FAM15)
    ok_roth = all(rows[f]["rel_antes"] and rows[f]["apta"] for f in FAM15)
    for f in FAM15:
        r = rows[f]
        say(f"  {f:28s} relativo antes {'sí' if r['rel_antes'] else 'NO'}, después {'sí' if r['rel_después'] else 'NO'}, "
            f"{r['apta_txt']}")
    if ok_all:
        say("\n  → LRS 1.5x PASA EN TODO: siguiente paso, paper trading en Alpaca (plan en el doc).")
    elif ok_roth:
        say("\n  → LRS 1.5x pasa solo SIN impuestos (Roth/IRA), no en cuenta normal: no se monta el bot; como mucho, "
            "regla manual dentro de una IRA para quien acepte su caída.")
    else:
        say("\n  → LRS 1.5x NO pasa: recomendación final = no montar un bot de acciones. Índice (S&P/Nasdaq) mantenido "
            "en Roth IRA con aportaciones periódicas; LRS 2x en IRA solo para quien acepte caídas del −70 %.")


if __name__ == "__main__":
    main()
