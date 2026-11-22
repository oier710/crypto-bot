"""Pruebas del motor de scripts/research_stocks_lrs.py (impuestos, lotes, costes, apalancados).
Ejecutar: python tests/test_research_stocks_lrs.py   (no necesita red)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import research_stocks_lrs as m  # noqa: E402

m.COST["cash"] = 0.0
ZERO = np.zeros(1)


def _run(r, w, rf=None, taxes=True, start="2020-01-01", costs=ZERO, div=ZERO, band=0.0):
    d = pd.bdate_range(start, periods=len(r))
    R = np.array(r, dtype=float).reshape(-1, 1)
    R[0] = 0.0
    W = np.array(w, dtype=float).reshape(-1, 1)
    rfa = np.zeros(len(r)) if rf is None else np.array(rf, dtype=float)
    return m.run(R, rfa, W, d, costs, div, band, taxes)


def close(a, b, tol=1e-9, msg=""):
    assert abs(a - b) < tol, f"{msg}: {a} vs {b}"


def test_buy_hold_no_tax():
    rng = np.random.default_rng(1)
    r = rng.normal(0.0005, 0.01, 600)
    eq, fin, ntr, _ = _run(r, np.ones(600), taxes=False)
    r[0] = 0
    close(eq[-1], np.prod(1 + r), msg="mantener")
    assert ntr == 0


def test_long_term_tax():
    m.DIV_YIELD = 0.0
    r = np.full(600, 0.001)                      # > 1 año, sin vender hasta el final
    eq, fin, _, _ = _run(r, np.ones(600))
    g = eq[-1] - 1
    close(fin, 1 + g * (1 - m.TAX_LT), msg="largo plazo 15 %")


def test_short_term_tax():
    m.DIV_YIELD = 0.0
    n = 150                                       # mismo año: compra el día 0, vende el día 99
    r = np.zeros(n)
    r[1:100] = 0.001
    w = np.zeros(n)
    w[:99] = 1.0
    eq, fin, ntr, tax = _run(r, w, start="2021-01-04")
    g = (1.001 ** 99) - 1      # rentabilidades de los días 1..99 (vende al cierre del 99)
    close(tax, m.TAX_ST * g, 1e-9, "impuesto corto plazo")
    close(fin, 1 + g * (1 - m.TAX_ST), 1e-9, "final corto plazo")
    assert ntr == 1


def test_loss_carryforward():
    m.DIV_YIELD = 0.0
    d = pd.bdate_range("2021-06-01", "2022-12-30")
    n = len(d)
    r = np.zeros(n)
    w = np.zeros(n)
    y1 = d.year == 2021
    i1 = np.where(y1)[0]
    i2 = np.where(~y1)[0]
    r[i1[1]:i1[50]] = -0.002                       # pérdida en 2021, se vende en 2021
    w[i1[0]:i1[49]] = 1
    r[i2[1]:i2[50]] = 0.003                        # ganancia en 2022, se vende en 2022
    w[i2[0]:i2[49]] = 1
    eq, fin, _, tax = _run(r, w, start="2021-06-01")
    loss = 1 - 0.998 ** 49
    v1 = 0.998 ** 49
    gain = v1 * (1.003 ** 49 - 1)
    close(tax, m.TAX_ST * max(gain - loss, 0), 1e-9, "arrastre de pérdidas")


def test_interest_tax():
    n = 252
    rf = np.full(n, 0.0002)
    eq, fin, _, tax = _run(np.zeros(n), np.zeros(n), rf=rf, start="2021-01-04")
    interest = eq[-1] - 1
    close(fin, 1 + interest * (1 - m.TAX_ST), 1e-9, "interés de letras")


def test_costs():
    r = np.zeros(10)
    eq, _, _, _ = _run(r, np.ones(10), taxes=False, costs=np.array([0.001]))
    close(eq[-1], 1 - 0.001, 2e-6, "coste de compra")


def test_lev_formula():
    r = pd.Series([0.01, -0.02])
    rf = pd.Series([0.0002, 0.0002])
    out = m.lev(r, rf, 2, 0.0, 0.0)
    close(out.iloc[0], 0.02 - 0.0002, msg="2x")
    out3 = m.lev(r, rf, 3, 0.0, 0.0252)
    close(out3.iloc[1], -0.06 - 2 * (0.0002 + 0.0001), msg="3x con margen")


def test_tax_netting():
    assert m.tax_netting(10, 5, 0) == (10, 5, 0)
    assert m.tax_netting(-4, 10, 0) == (0, 6, 0)
    assert m.tax_netting(10, -15, 0) == (0, 0, 5)
    assert m.tax_netting(3, 2, 4) == (0, 1, 0)


def test_split_exposure():
    one, two = m.split_exposure(pd.Series([0.0, 0.5, 1.0, 1.5, 2.0]))
    expo = one + 2 * two
    assert np.allclose(expo, [0, 0.5, 1, 1.5, 2]) and np.all(one + two <= 1 + 1e-12)


def test_collectible_and_6040():
    m.DIV_YIELD = 0.0
    r = np.full(600, 0.001)                       # > 1 año, ganancia a largo plazo
    eq, fin, _, _ = _run(r, np.ones(600))
    g = eq[-1] - 1
    d = pd.bdate_range("2020-01-01", periods=600)
    R = r.reshape(-1, 1).copy()
    R[0] = 0
    W = np.ones((600, 1))
    _, fc, _, _ = m.run(R, np.zeros(600), W, d, ZERO, ZERO, 0.0, True, gmode=np.array([1]))
    close(fc, 1 + g * (1 - m.TAX_COLL), 1e-9, "oro coleccionable 28 %")
    _, f6, _, _ = m.run(R, np.zeros(600), W, d, ZERO, ZERO, 0.0, True, gmode=np.array([2]))
    close(f6, 1 + g * (1 - m.TAX_6040), 1e-9, "60/40")


def test_cost_multiplier():
    d = pd.bdate_range("1989-12-27", periods=10)
    R = np.zeros((10, 1))
    W = np.zeros((10, 1))
    W[1:] = 1.0                                    # compra en 1989
    cm = np.where(d.year < 1990, 5.0, 1.0)
    eq, _, _, _ = m.run(R, np.zeros(10), W, d, np.array([0.001]), ZERO, 0.0, False, cost_mult=cm)
    close(eq[-1], 1 - 0.005, 1e-4, "costes ×5 antes de 1990")


def test_summary_families():
    m.REG.clear()
    m.ROB.clear()
    m.out_lines.clear()
    t0, t1 = pd.Timestamp("1927-06-30"), pd.Timestamp("2026-10-30")
    base = {"bloque": "A", "rel_antes": False, "rel_desp": False, "cagr": 0.08, "cagr_t": 0.07, "dd": -0.5,
            "start": t0, "end": t1}
    for n in ["LRS 1x", "LRS 2x", "LRS 3x", "MKT", "mantener 2x (sin filtro)"] + \
            [f"VT {v} % {f} filtro" for v in (15, 20, 25) for f in ("con", "sin")]:
        m.REG[("A", n)] = dict(base)
    m.REG[("A", "LRS 1x")].update(rel_antes=True, rel_desp=True, cagr=0.10, cagr_t=0.08, dd=-0.45)
    m.ROB[("A", "LRS 1x")] = {"antes": True, "después": True, "worst_dd": -0.48}
    m.REG[("A", "VT 20 % con filtro")].update(rel_antes=True, rel_desp=True)   # solo 1 de 3 vecinos → suerte
    m.REG[("A", "LRS 2x")].update(rel_antes=True, rel_desp=True, dd=-0.78)   # pasa pero no apta
    m.ROB[("A", "LRS 2x")] = {"antes": True, "después": True, "worst_dd": -0.80}
    m.summary()
    txt = "\n".join(m.out_lines)
    norm = txt.split("Candidatas para cuenta NORMAL")[1].split("Candidatas para cuenta IRA")[0]
    assert "1. LRS 1x mercado (1926)" in norm, norm
    assert "LRS 2x" not in norm, "una no apta no puede ser candidata"
    assert "VT con filtro mercado (1926) (antes 1/3" in txt, "debe marcar la suerte"


if __name__ == "__main__":
    n = 0
    for name, f in list(globals().items()):
        if name.startswith("test_") and callable(f):
            f()
            n += 1
            print(f"ok  {name}")
    print(f"{n} pruebas OK")
