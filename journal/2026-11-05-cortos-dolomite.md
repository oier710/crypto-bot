# 2026-11-05 — v6: todos los cortos en Dolomite (Aave puso el LTV del USDC a 0)

## Qué pasó
- 2026-11-05 00:06 UTC, ciclo diario: ETH con señal −1 → el bot depositó 85.75 USDC en Aave y el `borrow` de WETH
  revirtió con `LtvValidationFailed()` (0x5b263df7). Retiró el USDC; coste solo gas. Sin pérdidas.
- Diagnóstico en cadena (`results/research_aave_usdc_check.txt`): **Aave V3 Arbitrum tiene el USDC con LTV 0 %**
  (umbral de liquidación 78 % sigue, pero ya no sirve para pedir prestado). Cambio de gobernanza de Aave; hasta el
  27-oct funcionaba.

## Cambio
- `SHORT_VENUE = {BTC: dolomite, ETH: dolomite, ARB: dolomite}`. Dolomite: WETH 1.6 M$ disponibles al 3.3 %/año,
  WBTC 3.1 M$ al 1.6 %/año, sin primas de margen. Una sola cuenta aislada (nº 1) para todos los cortos.
- El camino de Aave se conserva (respaldo, probado en los bloques 1-24 de los tests).
- **Corrección de un fallo antiguo**: al saltar el límite de riesgo (−5 % día / −25 % desde máximo), si un corto no
  se podía cerrar del todo, el bot borraba igualmente stop/posición y se paraba (`halted`), dejando la deuda sin
  gestionar. Ahora mantiene la posición, NO se para y reintenta en la siguiente ejecución; solo se para cuando todo
  está cerrado.
- Tests: 27 bloques OK (25: ETH+BTC+ARB en corto a la vez en Dolomite, parcial, stop y cierre por señal;
  26: cierre por tramos; 27: límite de riesgo con corto sin poder cerrar).

## Antes de confiar en él
Prueba real pequeña: Actions → Run workflow → `test-short` con `ETH 5` (abre y cierra un corto de 5 $ de ETH en
Dolomite). La de ARB ya se hizo el 30-oct.
