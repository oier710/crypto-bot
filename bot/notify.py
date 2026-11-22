"""Avisos al móvil con ntfy (2026-11-22, petición de Oier).

Durante cada ejecución se apuntan los hechos importantes (operaciones, parciales, stops, reentradas, acumulación,
alertas de riesgo y fallos) y al terminar se manda UN solo aviso con todo. En la primera ejecución del día UTC
(ciclo diario, ~19:05 Miami) se manda además un resumen aunque no haya operaciones.

Seguridad:
  * Solo actúa si existe la variable NTFY_TOPIC (secreto de GitHub que crea Oier). Sin ella no hace nada.
  * Nunca rompe el bot: cualquier error al avisar se ignora (queda una línea en el log y sigue).
  * El mensaje no lleva claves ni direcciones: solo monedas, importes y precios.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import requests

SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TIMEOUT = 8
CAPITAL_REF = 115.75                    # capital de referencia del arranque (2026-09-26)
STATE_FILE = Path(__file__).resolve().parent.parent / "logs" / "notify_state.json"
REPEAT_HOURS = 6                        # una misma alerta no se repite antes de 6 h (p. ej. BOT PARADO en cada ejecución)
FAIL_STREAK = 2                         # un fallo suelto no avisa; dos ejecuciones fallidas seguidas, sí

_EVENT = re.compile(r"^(PARCIAL \d+ \S+ \S+: .*-> cierro|STOP |REENTRADA \S+ en .*recompro|RESULTADO |"
                    r"ACUMULACIÓN: comprados|CORTO \S+ abierto|CORTO \S+ cerrado:)")
_ALERT = re.compile(r"^(BOT PARADO|CERRANDO TODO|LÍMITE DE RIESGO|PRECIO DUDOSO persistente|CORTO \S+ NO cerrado|"
                    r"HF .*cerrando|Salud Dolomite .*cerrando|ERROR |"
                    r"CORTO \S+: (fallo tras|no se pudo deshacer|no se puede recomprar))")
_CANCEL_PT = re.compile(r"^PARCIAL \d+ (\S+): .*no se vende")
_CANCEL_RE = re.compile(r"^REENTRADA (\S+) no ejecutada")
_FAIL = re.compile(r"^EJECUCIÓN FALLIDA")
_EQ = re.compile(r"equity trading ([\d.]+) USD \(total ([\d.]+)\)")
_SIG = re.compile(r"^(\w+) cierre=([\d.]+) señal=([+-]?\d+)")
_REAL = re.compile(r"realizado acumulado ([+-][\d.]+)\$")

_log = logging.getLogger("onchain")


class _State:
    def __init__(self):
        self.reset()

    def reset(self):
        self.trades: list[str] = []
        self.events: list[str] = []
        self.alerts: list[str] = []
        self.fail: str | None = None
        self.signals: list[tuple[str, str, int]] = []
        self.equity: float | None = None
        self.realized: float | None = None


S = _State()


def topic() -> str:
    return os.environ.get("NTFY_TOPIC", "").strip()


class Collector(logging.Handler):
    """Lee los mensajes del log del bot y apunta lo que merece aviso."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            msg = record.getMessage()
            m = _EQ.search(msg)
            if m:
                S.equity = float(m.group(2))
            m = _REAL.search(msg)
            if m:
                S.realized = float(m.group(1))
            m = _SIG.match(msg)
            if m:
                S.signals.append((m.group(1), m.group(2), int(m.group(3))))
            m = _CANCEL_PT.match(msg) or _CANCEL_RE.match(msg)
            if m:       # lo anunciado no se ejecutó: se retira el aviso correspondiente
                kind = "PARCIAL" if msg.startswith("PARCIAL") else "REENTRADA"
                for i in range(len(S.events) - 1, -1, -1):
                    if S.events[i].startswith(kind) and f" {m.group(1)}" in S.events[i]:
                        del S.events[i]
                        break
                return
            if _FAIL.match(msg):
                S.fail = msg.split("\n")[0][:300]
            elif _ALERT.match(msg):
                S.alerts.append(msg[:300])
            elif _EVENT.match(msg):
                S.events.append(msg[:300])
        except Exception:  # noqa: BLE001
            pass


_handler: Collector | None = None


def install(logger: logging.Logger | None = None) -> None:
    """Engancha el recolector al log del bot (idempotente)."""
    global _handler
    S.reset()
    lg = logger or _log
    if _handler is None:
        _handler = Collector(level=logging.INFO)
    if _handler not in lg.handlers:
        lg.addHandler(_handler)


def add_trade(coin: str, side: str, usd: float, price: float, reason: str) -> None:
    """Llamado por onchain.record() en cada operación real."""
    try:
        S.trades.append(f"{side} {coin} {usd:.2f}$ a {price:,.4g} ({reason})" if price < 10
                        else f"{side} {coin} {usd:.2f}$ a {price:,.2f} ({reason})")
    except Exception:  # noqa: BLE001
        pass


_SIDE = {1: "alcista", -1: "bajista", 0: "sin tendencia"}


def build() -> dict | None:
    """Construye el aviso de esta ejecución, o None si no hay nada que contar."""
    daily = bool(S.signals)
    if not (S.trades or S.events or S.alerts or S.fail or daily):
        return None
    lines: list[str] = []
    if S.alerts:
        lines += ["⚠️ " + a for a in S.alerts]
    for e in S.events:
        if e.startswith("RESULTADO"):
            m = re.search(r"neto ([+-][\d.]+)\$", e)
            head = e.split(":")[0].replace("RESULTADO ", "")
            lines.append(f"Resultado {head}: {m.group(1)} $" if m else e)
        elif e.startswith(("PARCIAL", "STOP", "REENTRADA", "ACUMULACIÓN")):
            lines.append(e.split(" | ")[0])
    if S.trades:
        lines += ["• " + t for t in S.trades]
    if daily:
        lines.append("Ciclo diario: " + ", ".join(f"{c} {_SIDE.get(s, s)}" for c, _, s in S.signals))
    if S.fail:
        lines.append("Fallo (el bot reintenta solo cada 15 min): " + S.fail.replace("EJECUCIÓN FALLIDA: ", ""))
    foot = []
    if S.equity is not None:
        foot.append(f"Equity {S.equity:.2f} $ ({S.equity - CAPITAL_REF:+.2f} $ sobre {CAPITAL_REF:.2f})")
    if S.realized is not None:
        foot.append(f"cobrado {S.realized:+.2f} $")
    if foot:
        lines.append(" · ".join(foot))

    if S.alerts:
        title, prio, tags = "Crypto-bot: ALERTA", 5, ["warning"]
    elif S.events or S.trades:
        kinds = [e.split()[0] for e in S.events]
        if any(k == "PARCIAL" for k in kinds):
            title = "Crypto-bot: beneficio cobrado"
        elif any(k == "STOP" for k in kinds):
            title = "Crypto-bot: stop"
        elif any(k == "REENTRADA" for k in kinds):
            title = "Crypto-bot: reentrada"
        elif any(k == "ACUMULACIÓN:" for k in kinds):
            title = "Crypto-bot: compra BTC/ETH acumulado"
        else:
            title = "Crypto-bot: operación"
        prio, tags = 4, ["chart_with_upwards_trend"]
    elif S.fail:
        title, prio, tags = "Crypto-bot: ejecución fallida", 2, ["hourglass"]
    else:
        title, prio, tags = "Crypto-bot: resumen diario", 2, ["memo"]
    return {"title": title, "message": "\n".join(lines)[:3900], "priority": prio, "tags": tags}


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_state(d: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    except Exception:  # noqa: BLE001
        pass


def _key(text: str) -> str:
    return re.sub(r"[\d.,:+\-]+", "#", text)[:80]


def _dedupe() -> None:
    """Quita de S lo ya avisado hace poco (alertas repetidas) y los fallos sueltos. Guarda la memoria en logs/."""
    st = _load_state()
    now = time.time()
    recent = {k: v for k, v in st.get("alerts", {}).items() if now - v < REPEAT_HOURS * 3600}
    fresh = []
    for a in S.alerts:
        k = _key(a)
        if k not in recent:
            fresh.append(a)
            recent[k] = now
    S.alerts = fresh
    streak = st.get("fail_streak", 0) + 1 if S.fail else 0
    if S.fail and (streak < FAIL_STREAK or now - st.get("fail_at", 0) < REPEAT_HOURS * 3600):
        S.fail = None
    elif S.fail:
        st["fail_at"] = now
        S.fail = f"{streak} ejecuciones seguidas fallan. Última: " + S.fail
    st.update(alerts=recent, fail_streak=streak)
    _save_state(st)


def flush() -> bool:
    """Manda el aviso de esta ejecución (si hay algo y hay canal). Nunca lanza excepción."""
    try:
        t = topic()
        if not t:
            return False
        _dedupe()
        payload = build()
        if payload is None:
            return False
        payload["topic"] = t
        payload["click"] = "https://oier710.github.io/crypto-bot/"
        r = requests.post(SERVER, json=payload, timeout=TIMEOUT)
        if r.status_code >= 300:
            _log.info("aviso al móvil no enviado (ntfy %s)", r.status_code)
            return False
        return True
    except Exception as e:  # noqa: BLE001
        try:
            _log.info("aviso al móvil no enviado (%s)", type(e).__name__)
        except Exception:  # noqa: BLE001
            pass
        return False
    finally:
        S.reset()
