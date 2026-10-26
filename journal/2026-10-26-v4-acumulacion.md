# 2026-10-26 — v4: acumulación 70/30 (y ARB preparado pero no activo)

## Decisión de Oier (25-oct)
Dos bolsillos: trading en dólares (BTC+ETH, largos y cortos, estrategia v3) y acumulación de monedas.
Cada ganancia realizada NUEVA se reparte: 70 % se queda en la base de trading (para que las operaciones
crezcan) y 30 % compra BTC y ETH a partes iguales, que se apartan y no se venden nunca.

## Reglas (bot/onchain.py, `accumulate()`, en el ciclo diario tras las aperturas)
- Ganancia nueva = resultado realizado (logs/pnl.csv) por encima del máximo realizado anterior (`accum.hwm`).
  Las pérdidas no restan lo acumulado: primero hay que recuperar el máximo anterior. Hoy realizado = −4.46 $,
  así que la primera compra llegará cuando el realizado supere 0 $.
- El 30 % se apunta como pendiente y se compra cuando hay ≥ 5 $ por moneda (mínimo de operación).
- Las monedas acumuladas (`accum.qty`) se excluyen de la equity de trading, de las posiciones, de los stops
  y de los límites de riesgo; `sell_long` nunca vende por debajo de lo acumulado.
- Al apartar dinero se reescalan `peak_equity` y `day_start_equity` en proporción, para que la compra no cuente
  como pérdida en los límites (−5 % día / −25 % desde máximo).
- No se acumula mientras haya un corto abierto (no se retira garantía de Aave con deuda viva); se reintenta.
- Registro: fila ACUMULAR en onchain_trades.csv y logs/accum.csv (coste, cantidad, totales). equity.csv añade
  6 columnas al final (arb_usd, arb_qty, arb_price, debt_arb_usd, accum_usd, equity_total); `equity_usd` pasa
  a ser la equity DE TRADING. report.py usa equity_total para P&L y caídas.
- Si la acumulación falla, se registra y se reintenta al día siguiente; nunca bloquea stops ni señales.

## ARB
Estudio `research_2026_10d/e`: con largos y cortos duplicaba el retorno, pero Aave V3 Arbitrum NO presta ARB
(`research_arb_check.txt`), y solo en largo no mejora la cartera. El código queda generalizado (ASSETS, rutas,
decimales, deuda por moneda) con ARB definido pero fuera de SYMBOLS. Si Aave activa el préstamo, basta añadirlo.

## Revisión independiente
Tres hallazgos corregidos antes de desplegar: (1) acumular con un corto abierto retiraba garantía y podía bajar
el HF por debajo de 1.25; (2) el ajuste de peak/day_start por resta hacía los límites más estrictos (ahora
proporcional); (3) si el saldo leído tras la compra no la reflejaba, se habría comprado dos veces (ahora se
estima y se descuenta). Pruebas: tests/test_onchain_v3.py, 20 bloques, todo OK.
