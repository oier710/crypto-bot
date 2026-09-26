# crypto-bot

Experimento de trading sistemático con capital inicial de 100 $ en Hyperliquid.
Objetivo: crecimiento lento y consistente con riesgo controlado. No es un esquema
de enriquecimiento rápido.

## Principios

1. **Nada toca dinero real sin backtest y paper trading previos.**
2. **Fees, slippage y funding se modelan siempre.** Con 100 $ deciden el resultado.
3. **Sin apalancamiento por defecto.**
4. **El bot solo puede operar, nunca retirar.** Usa una *agent wallet* de Hyperliquid.
5. **La clave principal de MetaMask no se escribe en ningún sitio.** Ni en el chat, ni en la carpeta.
6. **Límites de pérdida codificados.** Si se superan, el bot cierra posiciones y se apaga.
7. **Todo queda registrado** en `logs/` y en `journal/`.

## Estructura

```
crypto-bot/
├── bot/
│   ├── config.py        # parámetros globales (fees, riesgo, activos)
│   ├── data.py          # descarga de velas (Binance para histórico, Hyperliquid para live)
│   ├── indicators.py    # indicadores técnicos (puro pandas/numpy)
│   ├── strategies/      # una estrategia por archivo, reglas explícitas
│   ├── backtest.py      # motor de simulación con fees
│   └── report.py        # métricas y tablas
├── scripts/
│   ├── fetch_data.py    # EJECUTAR EN TU TERMINAL: descarga datos a data/
│   └── run_backtest.py  # backtest de todas las estrategias sobre data/
├── data/                # CSVs de velas (ignorados por git)
├── results/             # salidas de backtests
├── logs/                # logs de ejecución
├── journal/             # diario de decisiones y revisiones semanales
├── .env.example         # plantilla; copia a .env y rellena (nunca se sube a git)
└── requirements.txt
```

## Uso (desde la Terminal del Mac)

```bash
cd ~/Desktop/crypto-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Descargar datos históricos (BTC, ETH, SOL; 1h y 4h; ~2 años)
python scripts/fetch_data.py

# 2. Backtest de todas las estrategias
python scripts/run_backtest.py
```

## Fases

- [x] Fase 0 — Infraestructura (este repo)
- [ ] Fase 1 — Datos históricos descargados
- [ ] Fase 2 — Backtesting de estrategias candidatas; selección
- [ ] Fase 3 — Paper trading en Hyperliquid testnet (2-4 semanas)
- [ ] Fase 4 — Dinero real: 100 USDC, revisión semanal
