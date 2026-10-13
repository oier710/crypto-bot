# 2026-10-12 — Contabilidad de resultados (P&L por operación)

Petición de Oier: registrar las ganancias y pérdidas a la vez que se hacen las operaciones.

- Nuevo `logs/pnl.csv`: una fila por cada cierre (venta parcial, stop, cambio de señal, límite de riesgo,
  factor de salud). Columnas: cantidad, entrada, salida, bruto, costes, neto, neto %, resultado acumulado de la
  operación completa (parciales + recompras + cierre) y resultado realizado total.
- Costes estimados: 0.10 % por swap + 0.03 $ de gas (cortos +0.10 % de Aave); los de entrada y recompras se
  reparten proporcionalmente entre los cierres de esa operación.
- El log muestra "RESULTADO …" en cada cierre y el ciclo diario imprime el realizado acumulado y la equity vs 115.75 $.
- La contabilidad nunca puede bloquear el bot (errores solo se registran).
- Fila inicial añadida a mano: ETH 26-sep (2684.78) → stop 08-oct (2461.76): bruto −3.60 $, neto −3.67 $.
- La verdad final sigue siendo `logs/equity.csv` (valor real de la cartera); `pnl.csv` explica de dónde viene.

Estado al 2026-10-13 03:00 UTC: equity 111.23 $ (−4.52 $). Realizado −3.67 $; abierto BTC −0.73 $, ETH +0.04 $.
