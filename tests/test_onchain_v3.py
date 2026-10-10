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

P = {"ETH": 2495.0, "BTC": 82774.0}
SIG = {"ETH": 1, "BTC": 1}
ATR = {"ETH": 90.0, "BTC": 2264.0}
COST = 0.001
PRICE = {oc.USDC: lambda: 1.0, oc.WETH: lambda: P["ETH"], oc.WBTC: lambda: P["BTC"]}


class FakeChain:
    address = "0xFAKE"

    def __init__(self, dry_run=False):
        self.dry_run = False
        self.b = FakeChain.state

    def balances(self):
        return dict(self.b)

    def health_factor(self):
        debt = self.b["debt_WETH"] * P["ETH"] + self.b["debt_WBTC"] * P["BTC"]
        return 1e9 if debt <= 0 else self.b["aUSDC"] * oc.LIQ_THRESHOLD / debt

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
        debt = self.b["debt_WETH"] * P["ETH"] + self.b["debt_WBTC"] * P["BTC"]
        if debt > 0 and (self.b["aUSDC"] - usd) * oc.LIQ_THRESHOLD / debt < 1.0:
            raise RuntimeError(f"Aave revierte: retiro {usd:.2f} dejaría HF < 1")
        self.b["aUSDC"] -= usd
        self.b[oc.USDC] += usd

    def aave_borrow(self, token, amount):
        k = "debt_WETH" if token == oc.WETH else "debt_WBTC"
        q = amount / 10 ** oc.DECIMALS[token]
        self.b[k] += q
        self.b[token] += q

    def aave_repay(self, token, amount):
        k = "debt_WETH" if token == oc.WETH else "debt_WBTC"
        q = min(amount / 10 ** oc.DECIMALS[token], self.b[k], self.b[token])
        self.b[k] -= q
        self.b[token] -= q

    def aave_repay_all(self, token):
        k = "debt_WETH" if token == oc.WETH else "debt_WBTC"
        q = min(self.b[k], self.b[token])
        self.b[k] -= q
        self.b[token] -= q


def fake_daily(c):
    return pd.DataFrame({"close": [P[c]] * 5})


oc.Chain = FakeChain
oc.spot = lambda s: P[s]
oc.coinbase_daily = fake_daily
oc.trend_ema = types.SimpleNamespace(signals=lambda df, **k: pd.Series([SIG[c] for c in SIG if P[c] == df.close.iloc[-1]][:1] * len(df)))
oc.indicators = types.SimpleNamespace(atr=lambda df, n: pd.Series([[ATR[c] for c in ATR if P[c] == df.close.iloc[-1]][0]] * len(df)))

tmp = Path(tempfile.mkdtemp())
oc.STATE_FILE, oc.TRADES_FILE, oc.EQUITY_FILE = tmp / "state.json", tmp / "trades.csv", tmp / "equity.csv"
oc.TRADES_FILE.write_text("time,coin,side,usd,price,reason\n"
                          "2026-09-26T21:47:46+00:00,BTC,BUY,43.39,84136.07,señal +1\n"
                          "2026-10-08T15:26:05+00:00,ETH,SELL,39.72,2461.76,stop\n"
                          "2026-10-09T00:06:33+00:00,ETH,BUY,41.47,2477.55,señal +1\n")
today = pd.Timestamp.now(tz="UTC").date().isoformat()
oc.save_state({"peak_equity": 117.9, "day": today, "day_start_equity": 111.2, "halted": False, "halt_reason": "",
               "stops": {"BTC": 79109.64303059799, "ETH": 2290.9282014921605}, "last_signal_day": today,
               "gaps": 4, "stopped_today": {}})
FakeChain.state = {"ETH_native": 0.0015, oc.USDC: 27.07, oc.WETH: 0.016754, oc.WBTC: 0.000515,
                   "aUSDC": 0.0, "debt_WETH": 0.0, "debt_WBTC": 0.0}

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

print("\n== 9. Cambio de señal a -1 en ETH: cierra el largo y abre corto")
SIG["ETH"] = -1
st = run(daily=True)
check(FakeChain.state["debt_WETH"] > 0, f"corto abierto (deuda {FakeChain.state['debt_WETH']:.5f} ETH)")
ps = st["pos"]["ETH"]
check(ps["side"] == -1, "posición v3 corta")
check(st["stops"]["ETH"] > P["ETH"], "stop del corto por encima")
d0 = FakeChain.state["debt_WETH"]

print("\n== 10. Corto: parcial al bajar 1.5 ATR, stop a entrada, y cierre en el stop")
st = run(eth=ps["ref"] - 1.5 * ps["atr"] - 1)
check(abs(FakeChain.state["debt_WETH"] - d0 * 2 / 3) / d0 < 0.02, f"deuda baja a 2/3 ({FakeChain.state['debt_WETH']:.5f})")
check(abs(st["stops"]["ETH"] - ps["entry"] * 0.998) < 0.01, "stop del corto baja a entrada")
st = run(eth=ps["entry"] * 0.998 + 1)
check(FakeChain.state["debt_WETH"] < 1e-9, "corto cerrado en el stop a entrada")
check("ETH" not in st.get("pos", {}), "posición borrada")

print("\n== 11. BTC sigue intacto durante todo")
check(abs(FakeChain.state[oc.WBTC] - 0.000515) < 1e-9, "BTC sin tocar")

print("\n== 11b. Corto de tamaño máximo (HF 1.5): el cierre por stop funciona (por tramos)")
SIG["ETH"] = -1
P["ETH"] = 2500.0
FakeChain.state.update({oc.USDC: 27.0, oc.WETH: 0.0, "aUSDC": 0.0, "debt_WETH": 0.0})
eq0, _ = oc.portfolio(FakeChain(), P)
s = oc.load_state()
s.update(stopped_today={}, last_signal_day=None, peak_equity=eq0, day_start_equity=eq0, halted=False)
s["stops"].pop("ETH", None); s.get("pos", {}).pop("ETH", None)
oc.save_state(s)
st = run(daily=True)
hf0 = FakeChain().health_factor()
d0 = FakeChain.state["debt_WETH"]
print(f"  corto abierto {d0 * P['ETH']:.2f}$ con HF {hf0:.2f}")
check(d0 > 0 and hf0 < 1.6, "corto al máximo que permite la garantía")
st = run(eth=st["stops"]["ETH"] + 1)
check(FakeChain.state["debt_WETH"] < 1e-9, f"deuda saldada (queda {FakeChain.state['debt_WETH']:.8f})")
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
    FakeChain.state = {"ETH_native": 0.0015, oc.USDC: 0.0, oc.WETH: 0.0, oc.WBTC: 0.0,
                       "aUSDC": 0.0, "debt_WETH": 0.0, "debt_WBTC": 0.0} | state_chain
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
fresh({"aUSDC": 0.04 * 2500 / oc.LIQ_THRESHOLD * 1.005, "debt_WETH": 0.04}, {"ETH": pos}, {"ETH": 2499.0})
st = run(eth=2500.0)
check(FakeChain.state["debt_WETH"] > 0.039, "deuda sigue (Aave no deja retirar)")
check("ETH" in st["stops"] and "ETH" in st["pos"], "stop y posición se mantienen")

eq, _ = oc.portfolio(FakeChain(), P)
print(f"\nEquity final simulada: {eq:.2f} $")
print(f"\n{'TODO OK' if not fails else f'{len(fails)} FALLOS'}")
sys.exit(1 if fails else 0)
