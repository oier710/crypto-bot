"""Comprobación previa para el sistema de acciones/ETF en Alpaca (solo lectura: no envía órdenes).

Qué mira:
  1. Si SSO, SGOV, BIL, SPY, QLD, QQQ, UPRO, GLD, TLT, UGL, UBT se pueden operar y si son FRACCIONABLES
     (con 100 $ el sistema no arranca si SSO o SGOV no lo son).
  2. El reloj del mercado (/v2/clock): next_open / next_close.
  3. Las sesiones de los próximos 12 meses que cierran antes de las 16:00 (medias sesiones, cierre 13:00):
     el bot debe operar según next_close, no a una hora fija.

Uso (lo ejecuta Oier en su Mac, con las claves de la cuenta PAPER; Claude nunca las ve):
    cd ~/Desktop/crypto-bot && python scripts/check_alpaca_assets.py
El script pide la clave y el secreto sin mostrarlos en pantalla (o los lee de ALPACA_API_KEY_ID /
ALPACA_API_SECRET_KEY si ya están en el entorno). No los guarda en ningún sitio. La salida no contiene claves:
se puede copiar y pegar en el chat.
"""
from __future__ import annotations

import datetime as dt
import getpass
import os
import sys

import requests

BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
SYMBOLS = ["SSO", "SGOV", "BIL", "SPY", "QLD", "QQQ", "UPRO", "GLD", "TLT", "UGL", "UBT"]


def main() -> int:
    key = os.environ.get("ALPACA_API_KEY_ID") or getpass.getpass("Clave API (paper): ").strip()
    sec = os.environ.get("ALPACA_API_SECRET_KEY") or getpass.getpass("Secreto API (paper): ").strip()
    h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}
    s = requests.Session()
    s.headers.update(h)

    print(f"Servidor: {BASE}")
    acc = s.get(f"{BASE}/v2/account", timeout=30)
    if acc.status_code != 200:
        print(f"ERROR de autenticación ({acc.status_code}). ¿Son claves de PAPER y del servidor paper?")
        return 1
    a = acc.json()
    print(f"Cuenta: estado {a.get('status')}, equity {a.get('equity')}, multiplicador {a.get('multiplier')}, "
          f"cortos {'sí' if a.get('shorting_enabled') else 'no'}")

    print("\nActivos:")
    print(f"  {'símbolo':8s} {'operable':>9s} {'fraccionable':>13s} {'margen':>7s} {'corto':>6s} {'estado':>8s}")
    bad = []
    for sym in SYMBOLS:
        r = s.get(f"{BASE}/v2/assets/{sym}", timeout=30)
        if r.status_code != 200:
            print(f"  {sym:8s} no encontrado ({r.status_code})")
            bad.append(sym)
            continue
        x = r.json()
        yn = lambda v: "sí" if v else "no"  # noqa: E731
        print(f"  {sym:8s} {yn(x.get('tradable')):>9s} {yn(x.get('fractionable')):>13s} {yn(x.get('marginable')):>7s} "
              f"{yn(x.get('shortable')):>6s} {x.get('status', ''):>8s}")
        if sym in ("SSO", "SGOV") and not (x.get("tradable") and x.get("fractionable")):
            bad.append(sym)

    c = s.get(f"{BASE}/v2/clock", timeout=30).json()
    print(f"\nReloj: ahora {c.get('timestamp')}  abierto {'sí' if c.get('is_open') else 'no'}  "
          f"next_open {c.get('next_open')}  next_close {c.get('next_close')}")

    today = dt.date.today()
    cal = s.get(f"{BASE}/v2/calendar", params={"start": today.isoformat(),
                                               "end": (today + dt.timedelta(days=365)).isoformat()}, timeout=30).json()
    early = [d for d in cal if d.get("close") and d["close"] < "16:00"]
    print(f"\nSesiones con cierre anticipado en los próximos 12 meses ({len(early)}):")
    for d in early:
        print(f"  {d['date']}  {d['open']} → {d['close']}")

    if bad:
        print(f"\nATENCIÓN: {', '.join(bad)} no sirve(n) tal cual con 100 $ (no operable o no fraccionable).")
        return 2
    print("\nOK: SSO y SGOV son operables y fraccionables.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
