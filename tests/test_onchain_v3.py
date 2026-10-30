"""Prueba de la v3 (parciales + reentrada en máximo nuevo) con una cadena FALSA.

No toca la red ni firma nada: sustituye Chain, precios y señales por versiones simuladas y recorre
escenarios completos (adopción de las posiciones abiertas de la v2, parciales, stop a entrada,
reentrada, stop, cambio de señal, cortos con recorte parcial).

    python tests/test_onchain_v3.py
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from bot import onchain as oc  # noqa: E402
import logging  # noqa: E402

for h in list(logging.getLogger().handlers):  # no escribir en logs/onchain.log real
    if isinstance(h, logging.FileHandler):
        logging.getLogger().removeHandler(h)

P = {"ETH": 2495.0, "BTC": 82774.0, "ARB": 0.30}
SIG = {"ETH": 1, "BTC": 1, "ARB": 0}
ATR = {"ETH": 90.0, "BTC": 2264.0, "ARB": 0.02}
COST = 0.001
PRICE = {oc.USDC: lambda: 1.0, oc.WETH: lambda: P["ETH"], oc.WBTC: lambda: P["BTC"], oc.ARB: lambda: P["ARB"]}
DEBTKEY = {oc.WETH: "debt_ETH", oc.WBTC: "debt_BTC", oc.ARB: "debt_ARB"}


class FakeChain:
    address = "0xFAKE"
    dolo_available = True      # simula Dolomite caído

    def __init__(self, dry_run=False):
        self.dry_run = False
        self.b = FakeChain.state
        self.b.setdefault("dUSDC", 0.0)
        self.dolo_ok = FakeChain.dolo_available
        self.dolo_margin_ratio = 0.15
        self.dolo_min_borrow_usd = 0.0

    def balances(self):
        return dict(self.b)

    def bal(self, token, who=None):
        return self.b[token]

    def health_factor(self):
        debt = self.b["debt_ETH"] * P["ETH"] + self.b["debt_BTC"] * P["BTC"]
        return 1e9 if debt <= 0 else self.b["aUSDC"] * oc.LIQ_THRESHOLD / debt

    # ---- Dolomite simulado (cuenta aislada: dUSDC garantía, debt_ARB deuda) ----
    def dolo_health(self):
        debt = self.b["debt_ARB"] * P["ARB"]
        return 1e9 if debt <= 0 else self.b["dUSDC"] / (debt * (1 + self.dolo_margin_ratio))

    def dolo_deposit_usdc(self, usd):
        assert self.dolo_ok, "Dolomite caído"
        assert usd <= self.b[oc.USDC] + 1e-9, f"Dolomite revierte: depósito {usd:.2f} > wallet {self.b[oc.USDC]:.2f}"
        self.b[oc.USDC] -= usd
        self.b["dUSDC"] += usd

    def dolo_withdraw_usdc(self, usd):
        assert self.dolo_ok, "Dolomite caído"
        usd = self.b["dUSDC"] if usd is None else min(usd, self.b["dUSDC"])
        debt = self.b["debt_ARB"] * P["ARB"]
        if debt > 0 and (self.b["dUSDC"] - usd) < debt * (1 + self.dolo_margin_ratio):
            raise RuntimeError(f"Dolomite revierte: retiro {usd:.2f} dejaría la cuenta liquidable")
        self.b["dUSDC"] -= usd
        self.b[oc.USDC] += usd

    def dolo_borrow(self, token, amount):
        assert self.dolo_ok and token == oc.ARB
        q = amount / 10 ** oc.DECIMALS[token]
        if self.b["dUSDC"] < (self.b["debt_ARB"] + q) * P["ARB"] * (1 + self.dolo_margin_ratio):
            raise RuntimeError("Dolomite revierte: garantía insuficiente para el préstamo")
        self.b["debt_ARB"] += q
        self.b[token] += q

    def dolo_repay(self, token, amount):
        q = min(amount / 10 ** oc.DECIMALS[token], self.b["debt_ARB"], self.b[token])
        self.b["debt_ARB"] -= q
        self.b[token] -= q

    def dolo_repay_all(self, token):
        q = min(self.b["debt_ARB"], self.b[token])
        self.b["debt_ARB"] -= q
        self.b[token] -= q

    def _move(self, tin, tout, amt_in_tokens):
        assert self.b[tin] >= amt_in_tokens - 1e-12, f"saldo insuficiente {tin[:6]} {self.b[tin]} < {amt_in_tokens}"
        usd = amt_in_tokens * PRICE[tin]()
        out = usd * (1 - COST) / PRICE[tout]()
        self.b[tin] -= amt_in_tokens
        self.b[tout] += out
        return out

    def swap_exact_in(self, hops, amount_in):
        tin, tout = hops[0][0], hops[-1][2]
        out = self._move(tin, tout, amount_in / 10 ** oc.DECIMALS[tin])
        return int(out * 10 ** oc.DECIMALS[tout])

    def swap_exact_out(self, hops, amount_out):
        tin, tout = hops[0][0], hops[-1][2]
        want = amount_out / 10 ** oc.DECIMALS[tout]
        need = want * PRICE[tout]() / (1 - COST) / PRICE[tin]()
        self._move(tin, tout, need)
        return int(need * 10 ** oc.DECIMALS[tin])

    def aave_supply_usdc(self, usd):
        usd = min(usd, self.b[oc.USDC])
        self.b[oc.USDC] -= usd
        self.b["aUSDC"] += usd

    def aave_withdraw_usdc(self, usd):
        usd = self.b["aUSDC"] if usd is None else min(usd, self.b["aUSDC"])
        debt = self.b["debt_ETH"] * P["ETH"] + self.b["debt_BTC"] * P["BTC"]
        if debt > 0 and (self.b["aUSDC"] - usd) * oc.LIQ_THRESHOLD / debt < 1.0:
            raise RuntimeError(f"Aave revierte: retiro {usd:.2f} dejaría HF < 1")
        self.b["aUSDC"] -= usd
        self.b[oc.USDC] += usd

    def aave_borrow(self, token, amount):
        k = DEBTKEY[token]
        q = amount / 10 ** oc.DECIMALS[token]
        self.b[k] += q
        self.b[token] += q

    def aave_repay(self, token, amount):
        k = DEBTKEY[token]
        q = min(amount / 10 ** oc.DECIMALS[token], self.b[k], self.b[token])
        self.b[k] -= q
        self.b[token] -= q

    def aave_repay_all(self, token):
        k = DEBTKEY[token]
        q = min(self.b[k], self.b[token])
        self.b[k] -= q
        self.b[token] -= q


def fake_daily(c):
    return pd.DataFrame({"close": [P[c]] * 5})


oc.Chain = FakeChain
oc.SYMBOLS = ("BTC", "ETH")       # los bloques 1-16 son de la v3 con dos monedas; ARB se prueba en 17 y 21-23
oc.WEIGHTS = {}                   # partes iguales (como antes); los pesos reales se prueban en el bloque 21
oc.spot = lambda s: P[s]
oc.coinbase_daily = fake_daily
oc.trend_ema = types.SimpleNamespace(signals=lambda df, **k: pd.Series([SIG[c] for c in SIG if P[c] == df.close.iloc[-1]][:1] * len(df)))
oc.indicators = types.SimpleNamespace(atr=lambda df, n: pd.Series([[ATR[c] for c in ATR if P[c] == df.close.iloc[-1]][0]] * len(df)))

tmp = Path(tempfile.mkdtemp())
oc.STATE_FILE, oc.TRADES_FILE, oc.EQUITY_FILE = tmp / "state.json", tmp / "trades.csv", tmp / "equity.csv"
oc.ACCUM_FILE = tmp / "accum.csv"
oc.PNL_FILE = tmp / "pnl.csv"


def pnl_rows():
    if not oc.PNL_FILE.exists():
        return []
    import csv
    return list(csv.DictReader(oc.PNL_FILE.open()))
oc.TRADES_FILE.write_text("time,coin,side,usd,price,reason\n"
                          "2026-09-26T21:47:46+00:00,BTC,BUY,43.39,84136.07,señal +1\n"
                          "2026-10-08T15:26:05+00:00,ETH,SELL,39.72,2461.76,stop\n"
                          "2026-10-09T00:06:33+00:00,ETH,BUY,41.47,2477.55,señal +1\n")
today = pd.Timestamp.now(tz="UTC").date().isoformat()
oc.save_state({"peak_equity": 117.9, "day": today, "day_start_equity": 111.2, "halted": False, "halt_reason": "",
               "stops": {"BTC": 79109.64303059799, "ETH": 2290.9282014921605}, "last_signal_day": today,
               "gaps": 4, "stopped_today": {}})
FakeChain.state = {"ETH_native": 0.0015, oc.USDC: 27.07, oc.WETH: 0.016754, oc.WBTC: 0.000515, oc.ARB: 0.0,
                   "aUSDC": 0.0, "debt_ETH": 0.0, "debt_BTC": 0.0, "debt_ARB": 0.0, "dUSDC": 0.0}

fails = []


def check(cond, msg):
    print(("  OK  " if cond else "  FALLO ") + msg)
    if not cond:
        fails.append(msg)


def run(eth=None, btc=None, daily=False):
    if eth is not None:
        P["ETH"] = eth
    if btc is not None:
        P["BTC"] = btc
    oc.run_cycle(dry_run=False, signals=daily)
    return oc.load_state()


def eth_qty():
    return FakeChain.state[oc.WETH]


print("\n== 1. Adopción de las posiciones abiertas (v2 -> v3)")
st = run()
pe = st["pos"]["ETH"]
check(abs(pe["entry"] - 2477.55) < 0.01, f"ETH entrada adoptada 2477.55 (es {pe['entry']:.2f})")
check(abs(pe["atr"] - (2477.55 - 2290.928) / 2) < 0.01, f"ETH ATR reconstruido {pe['atr']:.2f}")
check(abs(st["pos"]["BTC"]["entry"] - 84136.07) < 0.01, "BTC entrada adoptada 84136.07")
q0 = eth_qty()
lvl1 = pe["ref"] + 1.5 * pe["atr"]
lvl2 = pe["ref"] + 3.0 * pe["atr"]
print(f"  niveles ETH: parcial1 {lvl1:.2f}  parcial2 {lvl2:.2f}")

print("\n== 2. Precio sube sin llegar al nivel: nada")
st = run(eth=lvl1 - 5)
check(abs(eth_qty() - q0) < 1e-9, "sin operaciones")

print("\n== 3. Parcial 1 + stop a entrada")
st = run(eth=lvl1 + 1)
check(abs(eth_qty() - q0 * 2 / 3) / q0 < 0.01, f"queda 2/3 de ETH ({eth_qty():.6f} de {q0:.6f})")
be = 2477.55 * 1.002
check(abs(st["stops"]["ETH"] - be) < 0.01, f"stop ETH sube a entrada {be:.2f} (es {st['stops']['ETH']:.2f})")
check(st["pos"]["ETH"]["pt_done"] == 1, "pt_done = 1")
r = pnl_rows()
exp = q0 / 3 * (lvl1 + 1 - 2477.55)
check(len(r) == 1 and r[0]["evento"] == "parcial 1" and abs(float(r[0]["bruto_usd"]) - exp) < 0.02,
      f"P&L apuntado: parcial 1 bruto {r[0]['bruto_usd'] if r else '-'} (esperado {exp:.2f})")
check(float(r[0]["neto_usd"]) < float(r[0]["bruto_usd"]), "neto descuenta costes")

print("\n== 4. Ciclo diario con señal +1: NO recompra lo vendido (no redimensiona)")
st = run(daily=True)
check(abs(eth_qty() - q0 * 2 / 3) / q0 < 0.01, "sigue en 2/3 tras el ciclo diario")
check(st["pos"]["ETH"]["atr_today"] == ATR["ETH"], "ATR del día guardado para la reentrada")

print("\n== 5. Parcial 2")
st = run(eth=lvl2 + 1)
check(abs(eth_qty() - q0 / 3) / q0 < 0.01, f"queda 1/3 ({eth_qty():.6f})")
peak = lvl2 + 1

print("\n== 6. Corrección < 1 ATR y máximo nuevo: NO recompra")
run(eth=peak - 0.5 * pe["atr"])
st = run(eth=peak + 5)
check(abs(eth_qty() - q0 / 3) / q0 < 0.01, "sin recompra (corrección insuficiente)")
peak = peak + 5

print("\n== 7. Corrección >= 1 ATR y máximo nuevo: RECOMPRA y stop sube")
run(eth=peak - 1.1 * pe["atr"])
check(abs(eth_qty() - q0 / 3) / q0 < 0.01, "en la corrección no hace nada")
px = peak + 2
st = run(eth=px)
check(abs(eth_qty() - q0) / q0 < 0.01, f"posición completa otra vez ({eth_qty():.6f})")
exp_stop = px - 1.5 * ATR["ETH"]
check(abs(st["stops"]["ETH"] - exp_stop) < 0.01, f"stop = precio - 1.5 ATR = {exp_stop:.2f} (es {st['stops']['ETH']:.2f})")
check(st["pos"]["ETH"]["ref"] == px and st["pos"]["ETH"]["pt_done"] == 0, "parciales recalculados desde la recompra")

print("\n== 8. Stop: cierra todo, no reentra el mismo día, sí al día siguiente")
st = run(eth=exp_stop - 1)
check(eth_qty() * P["ETH"] < 1, "ETH vendido")
check("ETH" not in st["stops"] and "ETH" not in st.get("pos", {}), "stop y posición borrados")
st = run(daily=True)
check(eth_qty() * P["ETH"] < 1, "mismo día: no reentra")
s = oc.load_state()
s["stopped_today"] = {}
s["last_signal_day"] = None
oc.save_state(s)
st = run(daily=True)
check(eth_qty() * P["ETH"] > 30, f"día siguiente: reentra ({eth_qty() * P['ETH']:.2f}$)")
check(st["pos"]["ETH"]["entry"] == P["ETH"] and st["pos"]["ETH"]["pt_done"] == 0, "nueva operación con datos v3")

r = pnl_rows()
check(r[-1]["evento"] == "stop" and abs(float(r[-1]["total_usd"]) - sum(float(x["neto_usd"]) for x in r)) < 0.02,
      f"stop apuntado y total acumulado cuadra ({r[-1]['total_usd']})")
check(abs(float(r[-1]["operacion_usd"]) - sum(float(x["neto_usd"]) for x in r)) < 0.02,
      "resultado de la operación completa = parciales + recompra + stop")

print("\n== 9. Cambio de señal a -1 en ETH: cierra el largo y abre corto")
SIG["ETH"] = -1
st = run(daily=True)
check(FakeChain.state["debt_ETH"] > 0, f"corto abierto (deuda {FakeChain.state['debt_ETH']:.5f} ETH)")
ps = st["pos"]["ETH"]
check(ps["side"] == -1, "posición v3 corta")
check(st["stops"]["ETH"] > P["ETH"], "stop del corto por encima")
d0 = FakeChain.state["debt_ETH"]

print("\n== 10. Corto: parcial al bajar 1.5 ATR, stop a entrada, y cierre en el stop")
st = run(eth=ps["ref"] - 1.5 * ps["atr"] - 1)
check(abs(FakeChain.state["debt_ETH"] - d0 * 2 / 3) / d0 < 0.02, f"deuda baja a 2/3 ({FakeChain.state['debt_ETH']:.5f})")
check(abs(st["stops"]["ETH"] - ps["entry"] * 0.998) < 0.01, "stop del corto baja a entrada")
r = pnl_rows()
check(r[-1]["lado"] == "corto" and r[-1]["evento"] == "parcial 1" and float(r[-1]["bruto_usd"]) > 0,
      f"parcial del corto apuntada con ganancia bruta {r[-1]['bruto_usd']}")
st = run(eth=ps["entry"] * 0.998 + 1)
check(FakeChain.state["debt_ETH"] < 1e-9, "corto cerrado en el stop a entrada")
check("ETH" not in st.get("pos", {}), "posición borrada")

print("\n== 11. BTC sigue intacto durante todo")
check(abs(FakeChain.state[oc.WBTC] - 0.000515) < 1e-9, "BTC sin tocar")

print("\n== 11b. Corto de tamaño máximo (HF 1.5): el cierre por stop funciona (por tramos)")
SIG["ETH"] = -1
P["ETH"] = 2500.0
FakeChain.state.update({oc.USDC: 27.0, oc.WETH: 0.0, "aUSDC": 0.0, "debt_ETH": 0.0})
eq0, _ = oc.portfolio(FakeChain(), P)
s = oc.load_state()
s.update(stopped_today={}, last_signal_day=None, peak_equity=eq0, day_start_equity=eq0, halted=False)
s["stops"].pop("ETH", None); s.get("pos", {}).pop("ETH", None)
oc.save_state(s)
st = run(daily=True)
hf0 = FakeChain().health_factor()
d0 = FakeChain.state["debt_ETH"]
print(f"  corto abierto {d0 * P['ETH']:.2f}$ con HF {hf0:.2f}")
check(d0 > 0 and hf0 < 1.6, "corto al máximo que permite la garantía")
st = run(eth=st["stops"]["ETH"] + 1)
check(FakeChain.state["debt_ETH"] < 1e-9, f"deuda saldada (queda {FakeChain.state['debt_ETH']:.8f})")
check(FakeChain.state["aUSDC"] < 0.01 and FakeChain.state[oc.USDC] > 10, f"USDC recuperado {FakeChain.state[oc.USDC]:.2f}")
SIG["ETH"] = 1

print("\n== 12. Si la gestión v3 falla, los stops de siempre siguen funcionando")
eq0, _ = oc.portfolio(FakeChain(), P)
s = oc.load_state(); s.update(peak_equity=eq0, day_start_equity=eq0, halted=False); oc.save_state(s)
real = oc.manage_positions
oc.manage_positions = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fallo simulado"))
st = run(btc=st["stops"]["BTC"] - 10)
check(FakeChain.state[oc.WBTC] * P["BTC"] < 1, "BTC vendido por el stop de respaldo")
check("BTC" not in st["stops"], "stop BTC borrado")
oc.manage_positions = real

def fresh(state_chain, pos=None, stops=None):
    FakeChain.state = {"ETH_native": 0.0015, oc.USDC: 0.0, oc.WETH: 0.0, oc.WBTC: 0.0, oc.ARB: 0.0,
                       "aUSDC": 0.0, "debt_ETH": 0.0, "debt_BTC": 0.0, "debt_ARB": 0.0, "dUSDC": 0.0} | state_chain
    e, _ = oc.portfolio(FakeChain(), P)
    oc.save_state({"peak_equity": e, "day": today, "day_start_equity": e, "halted": False, "halt_reason": "",
                   "stops": stops or {}, "pos": pos or {}, "last_signal_day": today, "stopped_today": {}})


print("\n== 13. Parcial idempotente: estado viejo (pt_done=0) con la posición ya recortada -> no vende otra vez")
P.update(ETH=2700.0, BTC=80000.0); SIG.update(ETH=1, BTC=1)
pos = oc.new_pos(1, 2500.0, 100.0, 0.018, 1)
fresh({oc.WETH: 0.012, oc.USDC: 20.0}, {"ETH": pos}, {"ETH": 2300.0})
st = run(eth=2660.0)
check(abs(FakeChain.state[oc.WETH] - 0.012) < 1e-9, "no vende de más")
check(st["pos"]["ETH"]["pt_done"] == 1 and st["stops"]["ETH"] > 2500, "marca la parcial 1 y sube el stop a entrada")

print("\n== 14. Reentrada sin USDC -> no toca el estado")
pos = oc.new_pos(1, 2500.0, 100.0, 0.018, 1)
pos.update(pt_done=1, extreme=2700.0, pb=1.2)
fresh({oc.WETH: 0.012, oc.USDC: 0.0}, {"ETH": pos}, {"ETH": 2505.0})
st = run(eth=2710.0)
pe = st["pos"]["ETH"]
check(abs(FakeChain.state[oc.WETH] - 0.012) < 1e-9, "no compra (no hay USDC)")
check(pe["pt_done"] == 1 and pe["ref"] == 2500.0 and st["stops"]["ETH"] == 2505.0, "estado intacto")

print("\n== 15. Reentrada no se hace en la primera ejecución del día (manda la señal nueva)")
pos = oc.new_pos(1, 2500.0, 100.0, 0.018, 1)
pos.update(pt_done=1, extreme=2700.0, pb=1.2)
fresh({oc.WETH: 0.012, oc.USDC: 20.0}, {"ETH": pos}, {"ETH": 2505.0})
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
SIG["ETH"] = 0
st = run(eth=2710.0, daily=True)
check(FakeChain.state[oc.WETH] * P["ETH"] < 1, "señal 0: vende el resto sin haber recomprado antes")
SIG["ETH"] = 1

print("\n== 16. Si el corto no se puede cerrar (HF al límite), mantiene stop y posición para reintentar")
P["ETH"] = 2500.0
pos = oc.new_pos(-1, 2400.0, 50.0, 0.04, -1)
fresh({"aUSDC": 0.04 * 2500 / oc.LIQ_THRESHOLD * 1.005, "debt_ETH": 0.04}, {"ETH": pos}, {"ETH": 2499.0})
st = run(eth=2500.0)
check(FakeChain.state["debt_ETH"] > 0.039, "deuda sigue (Aave no deja retirar)")
check("ETH" in st["stops"] and "ETH" in st["pos"], "stop y posición se mantienen")

print("\n== 17. ARB con partes iguales: abre largo 1/3 y luego corto en Dolomite (garantía aislada)")
oc.SYMBOLS = ("BTC", "ETH", "ARB")
P.update(ETH=2500.0, BTC=80000.0, ARB=0.30); SIG.update(ETH=0, BTC=0, ARB=1)
fresh({oc.USDC: 90.0})
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
st = run(daily=True)
arb_usd = FakeChain.state[oc.ARB] * P["ARB"]
check(abs(arb_usd - 90 * 0.75 / 3) / 22.5 < 0.03, f"ARB largo ≈ 22.5$ (es {arb_usd:.2f}$)")
check("ARB" in st["stops"] and "ARB" in st["pos"], "stop y posición v3 de ARB")
SIG["ARB"] = -1
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
st = run(daily=True)
check(FakeChain.state[oc.ARB] * P["ARB"] < 1 and FakeChain.state["debt_ARB"] > 0, f"ARB corto abierto (deuda {FakeChain.state['debt_ARB']:.2f} ARB)")
check(FakeChain.state["dUSDC"] > 20 and FakeChain.state["aUSDC"] < 0.01, f"garantía en Dolomite ({FakeChain.state['dUSDC']:.2f}$), nada en Aave")
check(FakeChain().dolo_health() >= oc.HF_TARGET_MIN * 0.98, f"salud Dolomite {FakeChain().dolo_health():.2f} >= {oc.HF_TARGET_MIN}")
SIG["ARB"] = 0
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
st = run(daily=True)
check(FakeChain.state["debt_ARB"] < 1e-9, "corto ARB cerrado por señal 0")
check(FakeChain.state["dUSDC"] < 0.01, "garantía retirada de Dolomite al no quedar deuda")
oc.SYMBOLS = ("BTC", "ETH")

print("\n== 18. Acumulación: 30 % de la ganancia realizada nueva compra BTC y ETH y se aparta")
SIG.update(ETH=0, BTC=0, ARB=0)
fresh({oc.USDC: 150.0})
import csv as _csv
oc.PNL_FILE.write_text("time,coin,lado,evento,cantidad,entrada,salida,bruto_usd,costes_usd,neto_usd,neto_pct,operacion_usd,total_usd\n"
                       "2026-11-01T00:00:00+00:00,ETH,largo,señal +0,0.01,2000,2500,5.00,0.10,4.90,24.5,4.90,4.90\n")
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
st = run(daily=True)
acc = st["accum"]
check(abs(acc["pending_usd"] - 4.90 * 0.30) < 0.01 and not acc["qty"], f"ganancia 4.90$ -> 1.47$ pendientes, aún sin comprar (<5$ por moneda)")
check(abs(acc["hwm"] - 4.90) < 0.01, "máximo realizado = 4.90")
oc.PNL_FILE.write_text(oc.PNL_FILE.read_text() + "2026-11-02T00:00:00+00:00,BTC,largo,señal +0,0.001,80000,120000,40.00,0.20,39.80,49.7,39.80,44.70\n")
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
usdc0 = FakeChain.state[oc.USDC]
st = run(daily=True)
acc = st["accum"]
exp_pend = 44.70 * 0.30
check(acc["qty"].get("BTC", 0) > 0 and acc["qty"].get("ETH", 0) > 0, f"compra BTC y ETH ({acc['qty']})")
check(abs(acc["cost_usd"] - 2 * (exp_pend / 2)) < 0.05 and acc["pending_usd"] < 0.05, f"gastados {acc['cost_usd']:.2f}$ (esperado {exp_pend:.2f}$), pendiente {acc['pending_usd']:.2f}$")
check(abs(usdc0 - FakeChain.state[oc.USDC] - acc["cost_usd"]) < 0.05, "el USDC bajó justo lo gastado")
eq_tr, pf = oc.portfolio(FakeChain(), P)
check(abs(pf["accum_usd"] - acc["cost_usd"] * (1 - COST)) < 0.1 and abs(pf["val"]["BTC"]) < 0.01 and abs(pf["val"]["ETH"]) < 0.01,
      f"las monedas acumuladas no cuentan como posición (val BTC {pf['val']['BTC']:.2f}, ETH {pf['val']['ETH']:.2f}; acumulado {pf['accum_usd']:.2f}$)")
check(abs(eq_tr + pf["accum_usd"] - 150 * (1 - 0) + acc["cost_usd"] * COST) < 0.2, f"equity trading {eq_tr:.2f} + acumulado = total")
# pérdida posterior: no se vende lo acumulado y no se acumula más hasta superar el máximo
oc.PNL_FILE.write_text(oc.PNL_FILE.read_text() + "2026-11-03T00:00:00+00:00,ETH,largo,stop,0.01,2500,2000,-5.00,0.10,-5.10,-20,-5.10,39.60\n")
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
q_before = dict(acc["qty"])
st = run(daily=True)
check(st["accum"]["qty"] == q_before and abs(st["accum"]["hwm"] - 44.70) < 0.01, "tras una pérdida: nada nuevo, máximo se mantiene en 44.70")
# señal +1 en ETH con monedas acumuladas: la posición se abre aparte y un stop no toca lo acumulado
SIG["ETH"] = 1
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
st = run(daily=True)
tr_eth = st["pos"]["ETH"]["qty_now"]
check(abs(FakeChain.state[oc.WETH] - (tr_eth + st["accum"]["qty"]["ETH"])) < 1e-6, "WETH en wallet = posición de trading + acumulado")
st = run(eth=st["stops"]["ETH"] - 1)
check(abs(FakeChain.state[oc.WETH] - st["accum"]["qty"]["ETH"]) < 1e-6, "el stop vende solo la parte de trading; lo acumulado se queda")

print("\n== 19. Con un corto abierto no se acumula (no se retira garantía de Aave)")
SIG.update(ETH=-1, BTC=0, ARB=0); P.update(ETH=2500.0)
fresh({oc.USDC: 60.0})
oc.PNL_FILE.write_text("time,coin,lado,evento,cantidad,entrada,salida,bruto_usd,costes_usd,neto_usd,neto_pct,operacion_usd,total_usd\n"
                       "2026-11-01T00:00:00+00:00,ETH,largo,señal +0,0.01,2000,2500,40.00,0.10,39.90,24.5,39.90,39.90\n")
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
st = run(daily=True)
check(FakeChain.state["debt_ETH"] > 0, "corto ETH abierto")
check(not st["accum"]["qty"] and st["accum"]["pending_usd"] > 10, f"nada comprado; {st['accum']['pending_usd']:.2f}$ pendientes")
SIG["ETH"] = 0
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
st = run(daily=True)
check(FakeChain.state["debt_ETH"] < 1e-9 and st["accum"]["qty"], f"corto cerrado y entonces sí compra ({st['accum']['qty']})")

print("\n== 20. Acumular no altera los límites de riesgo (% desde máximo y % del día)")
SIG.update(ETH=0, BTC=0)
fresh({oc.USDC: 100.0})
s = oc.load_state(); s.update(peak_equity=110.0, day_start_equity=104.0, last_signal_day=None); oc.save_state(s)
oc.PNL_FILE.write_text(oc.PNL_FILE.read_text().splitlines()[0] + "\n2026-11-05T00:00:00+00:00,BTC,largo,señal +0,0.001,1,2,50.00,0.00,50.00,1,50.00,50.00\n")
dd0, day0 = 100 / 110 - 1, 100 / 104 - 1
st = run(daily=True)
eq_tr, _ = oc.portfolio(FakeChain(), P)
dd1, day1 = eq_tr / st["peak_equity"] - 1, eq_tr / st["day_start_equity"] - 1
check(abs(dd1 - dd0) < 0.002 and abs(day1 - day0) < 0.002, f"caída {dd0:+.3%} -> {dd1:+.3%}; día {day0:+.3%} -> {day1:+.3%}")
check(not st["halted"], "no se para")

print("\n== 21. Pesos reales (BTC 42.5 / ETH 42.5 / ARB 15): tamaño de cada posición")
oc.SYMBOLS = ("BTC", "ETH", "ARB"); oc.WEIGHTS = {"BTC": 0.425, "ETH": 0.425, "ARB": 0.15}
P.update(ETH=2500.0, BTC=80000.0, ARB=0.30); SIG.update(ETH=1, BTC=1, ARB=-1)
fresh({oc.USDC: 100.0})
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
st = run(daily=True)
b = FakeChain.state
check(abs(b[oc.WETH] * P["ETH"] - 100 * 0.425 * 0.75) < 1.5, f"ETH largo ≈ {100*0.425*0.75:.2f}$ (es {b[oc.WETH]*P['ETH']:.2f}$)")
check(abs(b[oc.WBTC] * P["BTC"] - 100 * 0.425 * 0.75) < 1.5, f"BTC largo ≈ {100*0.425*0.75:.2f}$ (es {b[oc.WBTC]*P['BTC']:.2f}$)")
check(abs(b["debt_ARB"] * P["ARB"] - 100 * 0.15 * 0.75) < 1.5, f"ARB corto ≈ {100*0.15*0.75:.2f}$ (es {b['debt_ARB']*P['ARB']:.2f}$)")
check(b["dUSDC"] >= b["debt_ARB"] * P["ARB"] * 1.15 * oc.HF_TARGET_MIN * 0.98, f"garantía Dolomite {b['dUSDC']:.2f}$ cubre la deuda con salud >= 1.5")

print("\n== 22. Corto de ARB con parciales y stop (Dolomite), cortos de ETH en Aave intactos")
pos = oc.new_pos(-1, 0.30, 0.02, 37.5, -1)
fresh({oc.USDC: 20.0, "dUSDC": 30.0, "debt_ARB": 37.5, "aUSDC": 40.0, "debt_ETH": 0.008}, {"ARB": pos}, {"ARB": 0.34, "ETH": 2600.0})
SIG.update(ETH=-1, BTC=0, ARB=-1)
st = run()                                   # sin movimiento: nada
check(abs(FakeChain.state["debt_ARB"] - 37.5) < 1e-9, "sin cambios con el precio quieto")
P["ARB"] = 0.30 - 1.5 * 0.02 - 0.001         # -1.5 ATR -> parcial 1 del corto
st = run()
check(abs(FakeChain.state["debt_ARB"] - 25.0) / 25 < 0.02, f"parcial 1: queda 2/3 de la deuda ({FakeChain.state['debt_ARB']:.2f} ARB)")
check(st["stops"]["ARB"] < 0.30, f"stop del corto baja a entrada ({st['stops']['ARB']:.4f})")
check(abs(FakeChain.state["debt_ETH"] - 0.008) < 1e-12 and FakeChain.state["aUSDC"] > 39, "corto ETH en Aave sin tocar")
P["ARB"] = st["stops"]["ARB"] + 0.001       # stop
st = run()
check(FakeChain.state["debt_ARB"] < 1e-9 and "ARB" not in st["pos"], "stop: corto ARB cerrado del todo")
check(FakeChain.state["dUSDC"] < 0.01, "garantía Dolomite retirada")
rows = [r for r in pnl_rows() if r["coin"] == "ARB"]
check(len(rows) == 2 and rows[-1]["lado"] == "corto", f"pnl.csv apunta parcial y stop de ARB ({len(rows)} filas)")

print("\n== 23. Dolomite caído: ARB no abre cortos (largos sí); salud baja en Dolomite cierra solo ARB")
FakeChain.dolo_available = False
SIG.update(ETH=0, BTC=0, ARB=-1); P.update(ARB=0.30)
fresh({oc.USDC: 100.0})
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
st = run(daily=True)
check(FakeChain.state["debt_ARB"] < 1e-9 and "ARB" not in st["pos"], "sin Dolomite no se abre el corto de ARB")
SIG["ARB"] = 1
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
st = run(daily=True)
check(FakeChain.state[oc.ARB] * P["ARB"] > 5, "el largo de ARB sí se abre")
FakeChain.dolo_available = True
pos = oc.new_pos(-1, 0.30, 0.02, 37.5, -1)
fresh({oc.USDC: 30.0, "dUSDC": 13.5, "debt_ARB": 37.5, "aUSDC": 40.0, "debt_ETH": 0.008}, {"ARB": pos}, {"ARB": 0.34, "ETH": 2600.0})
st = run()                                   # salud Dolomite = 13.5/(11.25*1.15) = 1.04 < 1.25 -> cierra ARB
check(FakeChain.state["debt_ARB"] < 1e-9, "salud Dolomite baja: corto ARB cerrado")
check(abs(FakeChain.state["debt_ETH"] - 0.008) < 1e-12, "corto ETH (Aave, sano) se mantiene")

print("\n== 24. Apertura de ARB que falla a medias (préstamo hecho, venta falla): se deshace y el ciclo sigue")
SIG.update(ETH=1, BTC=0, ARB=-1); P.update(ARB=0.30, ETH=2500.0)
fresh({oc.USDC: 100.0})
s = oc.load_state(); s["last_signal_day"] = None; oc.save_state(s)
_swap = FakeChain.swap_exact_in
def _swap_fail(self, hops, amount_in):
    if hops[0][0] == oc.ARB:
        raise RuntimeError("swap revertido (simulado)")
    return _swap(self, hops, amount_in)
FakeChain.swap_exact_in = _swap_fail
st = run(daily=True)
FakeChain.swap_exact_in = _swap
b = FakeChain.state
check(b["debt_ARB"] < 1e-9 and b["dUSDC"] < 0.01 and b[oc.ARB] < 1e-9, f"sin deuda, sin garantía atrapada ni ARB suelto (deuda {b['debt_ARB']:.4f}, dUSDC {b['dUSDC']:.2f}, ARB {b[oc.ARB]:.4f})")
check(b[oc.WETH] * P["ETH"] > 25 and "ETH" in st["stops"], "el largo de ETH se abrió y tiene stop pese al fallo de ARB")
_, pf24 = oc.portfolio(FakeChain(), P)
check(abs(pf24["equity_total"] - 100) < 1.0, f"capital intacto (total {pf24['equity_total']:.2f}$)")
oc.SYMBOLS = ("BTC", "ETH"); oc.WEIGHTS = {}

eq, _ = oc.portfolio(FakeChain(), P)
print(f"\nEquity final simulada: {eq:.2f} $")
print(f"\n{'TODO OK' if not fails else f'{len(fails)} FALLOS'}")
sys.exit(1 if fails else 0)
