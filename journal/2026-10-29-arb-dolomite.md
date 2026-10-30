# 2026-10-29 — v5: ARB como tercera moneda (largo en Uniswap, corto vía Dolomite)

## Por qué
- Oier (28-oct): "¿no tenemos corto para otras monedas?". Aave en Arbitrum/Optimism/Base solo presta WBTC/WETH.
- Escaneo de protocolos de préstamo (`results/research_lending_scan.txt`): **Dolomite** (Arbitrum) presta ARB
  (~0.28 M$ disponibles, ~7 %/año), UNI, LINK, PENDLE.
- Backtest (`results/research_2026_10g.txt`, 3 años): ARB al **15 %** del capital, largo y corto, sube la rentabilidad
  de +61 % a +79 % con la misma caída máxima (−17 %), 11 meses en rojo y mejor peor mes (−7.4 %). A partir del 20 %
  la caída supera −20 %. Oier (29-oct): "vamos a probar y si no está siendo rentable lo cambiamos de vuelta".

## Qué cambia
- `SYMBOLS = ("BTC", "ETH", "ARB")`, `WEIGHTS = {BTC 0.425, ETH 0.425, ARB 0.15}` (tamaño = equity × peso × 75 %).
- `SHORT_VENUE`: BTC/ETH → Aave (como siempre); ARB → **Dolomite**, cuenta aislada nº 1:
  - abrir: depositar USDC de garantía en la cuenta 1 → `withdrawWei(1, ARB, cantidad, None)` (= préstamo) → vender ARB
    por USDC en Uniswap (ARB→WETH→USDC, 0.05 %) → depositar lo vendido también en la cuenta 1.
  - cerrar: comprar ARB → `depositWei(1, ARB, cantidad)` (= devolver) → retirar la garantía. Por tramos si hace falta.
  - salud = garantía / (deuda × k), con k = (1 + margen global) × primas de los mercados USDC y ARB (leídas en cadena);
    al abrir, garantía ≥ deuda × k × 1.30 antes de depositar lo vendido (queda ~1.9-2.0 después); HF_MIN 1.25 cierra.
  - si Dolomite no responde: sin posición de ARB → el bot sigue sin cortos de ARB y sin evaluar límites de riesgo esa
    ejecución; con posición abierta → la ejecución falla (la siguiente, 15 min después, reintenta). Nunca se da por
    cerrada una posición que no se ha podido leer.
  - si la apertura falla a medias (préstamo hecho, venta no) → se devuelve el préstamo y se retira la garantía.
- `MIN_TRADE_USD` 5 → 3 (la parcial de 1/3 de una posición de ARB de ~12 $ son ~4 $). Las parciales solo se apuntan
  en `pnl.csv` si de verdad se ejecutaron.
- Para abrir una posición no se retira garantía de Aave/Dolomite por debajo de HF 1.5 (antes 1.01, que habría
  provocado cierres forzosos del otro corto en la siguiente ejecución).
- `equity.csv`: 2 columnas nuevas al final (`dusdc`, `hf_dolo`). `report.py` las lee.
- Pruebas: `tests/test_onchain_v3.py` 24 bloques (17, 21-24 son de Dolomite) → TODO OK. Revisión independiente:
  9 hallazgos, los 5 importantes corregidos (fallo a medias, garantía de Aave, cuenta 0/1, primas de margen,
  verificación en cadena).

## Antes de activar en real
1. `results/research_dolomite_check.txt`: direcciones correctas, proxy operador global, mercado ARB abierto,
   margen y primas, préstamo mínimo.
2. Prueba con dinero real pequeño: Actions → Run workflow → `test-short` con `ARB 5` (abre un corto de 5 $ en
   Dolomite y lo cierra; coste ~0.05 $).
3. Primer ciclo diario con ARB: vigilar `CORTO ARB abierto en Dolomite` / `salud` en `onchain.log`.

## Recuperación manual (si el bot muere con un corto de ARB abierto)
La cuenta nº 1 **no aparece en la web de Dolomite** (solo lista posiciones con número ≥ 100). Para cerrarla a mano,
desde la wallet del bot, en arbiscan → contrato `DepositWithdrawalProxy` (0xAdB9D68c613df4AA363B42161E1282117C7B9594)
→ Write Contract:
1. Comprar en Uniswap la cantidad de ARB que se debe (ver `debt_ARB` en el último log) y aprobar ARB para
   `DolomiteMargin` (0x6Bd780E7fDf01D77e4d475c821f1e7AE05409072).
2. `depositWei(toAccountNumber=1, marketId=<id de ARB>, amountWei=<cantidad en wei>)` → salda la deuda.
3. `withdrawWei(fromAccountNumber=1, marketId=<id de USDC>, amountWei=115792089237316195423570985008687907853269984665640564039457584007913129639935, balanceCheckFlag=1)` → retira toda la garantía.
Los ids de mercado están en `results/research_dolomite_check.txt`.

## Criterio de reversión
Si tras ≥ 2-3 meses ARB resta de forma clara (ver `pnl.csv` por moneda en las revisiones semanales), quitar "ARB" de
`SYMBOLS` y de `WEIGHTS` (volver a BTC+ETH a partes iguales). El resto del código no se toca.
