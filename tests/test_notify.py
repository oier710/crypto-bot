"""Pruebas de bot/notify.py (avisos al móvil). Sin red: requests.post se sustituye.
Ejecutar: python tests/test_notify.py
"""
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import notify  # noqa: E402

log = logging.getLogger("onchain")
log.setLevel(logging.INFO)
sent = []


class Resp:
    def __init__(self, code=200):
        self.status_code = code


def fake_post(url, json=None, timeout=None):
    sent.append((url, json))
    return Resp()


def boom(*a, **k):
    raise ConnectionError("sin red")


notify.requests.post = fake_post
import tempfile  # noqa: E402
notify.STATE_FILE = Path(tempfile.mkdtemp()) / "notify_state.json"   # no tocar logs/ reales
ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1
    print("ok ", msg)


# 1) sin NTFY_TOPIC no se manda nada aunque haya operaciones
os.environ.pop("NTFY_TOPIC", None)
notify.install(log)
log.warning("PARCIAL 1 corto BTC: 58228.42 (+1.6 ATR desde 62330.92) -> cierro 1/3 (9.12$)")
check(notify.flush() is False and not sent, "sin canal: no envía")

# 2) ejecución con parcial: un solo aviso con todo
os.environ["NTFY_TOPIC"] = "canal-de-prueba"
notify.install(log)
log.info("BTC: precio 58228.42")
log.warning("PARCIAL 1 corto BTC: 58228.42 (+1.6 ATR desde 62330.92) -> cierro 1/3 (9.12$)")
notify.add_trade("BTC", "COVER", 9.12, 58228.42, "parcial 1")
log.warning("RESULTADO BTC corto (parcial 1): bruto +1.32$ costes 0.08$ -> neto +1.24$ | realizado acumulado -1.54$")
log.info("BTC stop persigue: 66281.98 -> 66130.54 (extremo 58228.42, 3.0 ATR)")
log.info("USDC 178.98 (wallet 0.15 + Aave 0.00 + Dolomite 178.83) | x | deuda y | HF ∞ / Dolomite 2.81 | acumulado 0.00$ -> equity trading 123.87 USD (total 123.87)")
check(notify.flush() is True and len(sent) == 1, "un aviso por ejecución")
p = sent[-1][1]
check(p["topic"] == "canal-de-prueba" and p["title"] == "Crypto-bot: beneficio cobrado" and p["priority"] == 4, "título y prioridad de parcial")
check("PARCIAL 1 corto BTC" in p["message"] and "Resultado BTC corto (parcial 1): +1.24 $" in p["message"], "texto del parcial y su resultado")
check("COVER BTC 9.12$ a 58,228.42 (parcial 1)" in p["message"], "línea de la operación")
check("Equity 123.87 $ (+8.12 $ sobre 115.75)" in p["message"] and "cobrado -1.54 $" in p["message"], "pie con equity y cobrado")
check("persigue" not in p["message"], "los ajustes del stop que persigue no avisan")

# 3) ejecución normal sin nada: no avisa
notify.install(log)
log.info("USDC 1 (wallet ...) -> equity trading 121.00 USD (total 121.00)")
n = len(sent)
check(notify.flush() is False and len(sent) == n, "ejecución tranquila: sin aviso")

# 4) ciclo diario: resumen aunque no haya operaciones
notify.install(log)
log.info("BTC cierre=60086.00 señal=-1 ATR=3028.86 tengo=-18.84$ objetivo=-18.84$")
log.info("ETH cierre=1682.30 señal=-1 ATR=113.45 tengo=-17.48$ objetivo=-17.48$")
log.info("ARB cierre=0.11 señal=+0 ATR=0.01 tengo=+0.00$ objetivo=+0.00$")
log.info("x -> equity trading 123.01 USD (total 123.01)")
check(notify.flush() is True and sent[-1][1]["title"] == "Crypto-bot: resumen diario", "resumen diario")
check("Ciclo diario: BTC bajista, ETH bajista, ARB sin tendencia" in sent[-1][1]["message"], "señales del día en palabras")

# 5) alerta de riesgo: prioridad máxima
notify.install(log)
log.warning("CERRANDO TODO: caída desde máximo -25.3 %")
check(notify.flush() is True and sent[-1][1]["priority"] == 5 and "ALERTA" in sent[-1][1]["title"], "alerta con prioridad máxima")

# 6) fallos: uno suelto no avisa; dos seguidos sí (prioridad baja); un tercero seguido no repite antes de 6 h
n = len(sent)
notify.install(log)
log.error("EJECUCIÓN FALLIDA: ConnectionError: no conecta al RPC de Arbitrum")
check(notify.flush() is False and len(sent) == n, "un fallo suelto no avisa")
notify.install(log)
log.error("EJECUCIÓN FALLIDA: ConnectionError: no conecta al RPC de Arbitrum")
check(notify.flush() is True and sent[-1][1]["priority"] == 2 and "2 ejecuciones seguidas" in sent[-1][1]["message"], "dos fallos seguidos avisan")
notify.install(log)
log.error("EJECUCIÓN FALLIDA: ConnectionError: no conecta al RPC de Arbitrum")
n = len(sent)
check(notify.flush() is False and len(sent) == n, "el tercer fallo seguido no repite el aviso")
notify.install(log)
log.info("x -> equity trading 121.00 USD (total 121.00)")
notify.flush()
check(notify._load_state().get("fail_streak") == 0, "una ejecución buena reinicia la racha de fallos")

# 6b) una alerta repetida en cada ejecución (BOT PARADO) avisa una vez cada 6 h
notify.install(log)
log.error("BOT PARADO: caída -25.1 % desde máximo. Revisa y borra 'halted' en logs/onchain_state.json")
check(notify.flush() is True and sent[-1][1]["priority"] == 5, "BOT PARADO avisa la primera vez")
n = len(sent)
notify.install(log)
log.error("BOT PARADO: caída -25.1 % desde máximo. Revisa y borra 'halted' en logs/onchain_state.json")
check(notify.flush() is False and len(sent) == n, "BOT PARADO no se repite en la ejecución siguiente")

# 6c) errores internos que el bot captura también avisan
notify.install(log)
log.error("ERROR abriendo ETH (execution reverted); el resto del ciclo sigue")
check(notify.flush() is True and "ERROR abriendo ETH" in sent[-1][1]["message"], "ERROR interno avisa")

# 6d) parcial anunciada pero no ejecutada (por debajo del mínimo): no avisa de beneficio cobrado
n = len(sent)
notify.install(log)
log.warning("PARCIAL 2 corto ARB: 0.0835 (+3.0 ATR desde 0.1221) -> cierro 1/3 (2.10$)")
log.warning("PARCIAL 2 ARB: 2.10$ está por debajo del mínimo operable; no se vende (solo se sube el stop)")
check(notify.flush() is False and len(sent) == n, "parcial no ejecutada: sin aviso")
notify.install(log)
log.warning("REENTRADA ETH en mínimo nuevo 1717.99 (corrección 1.4 ATR): recompro 17.83$")
log.warning("REENTRADA ETH no ejecutada (sin USDC/garantía suficiente); estado sin cambios")
check(notify.flush() is False and len(sent) == n, "reentrada no ejecutada: sin aviso")

# 7) si ntfy falla, el bot no se entera
notify.requests.post = boom
notify.install(log)
log.warning("STOP corto ETH: 1860.00 >= 1858.73")
check(notify.flush() is False, "error de red al avisar: no lanza excepción")
notify.requests.post = lambda *a, **k: Resp(500)
notify.install(log)
log.warning("STOP corto ETH: 1860.00 >= 1858.73")
check(notify.flush() is False, "ntfy responde error: no lanza excepción")

# 8) install es idempotente (un solo recolector aunque se llame dos veces)
notify.install(log)
notify.install(log)
check(sum(isinstance(h, notify.Collector) for h in log.handlers) == 1, "un solo recolector")

print(f"\n{ok} comprobaciones de avisos: TODO OK")
