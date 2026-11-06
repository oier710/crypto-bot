# 2026-11-05 — Acumulación desde bien pronto

Oier: "que desde bien pronto podamos acumular".
- Antes: la compra de BTC/ETH para el bolsillo de acumulación exigía 3 $ por moneda (MIN_TRADE_USD) → con el 30 %
  de cada ganancia nueva, la primera compra llegaba con +20 $ realizados por encima de 0 (más de un año).
- Ahora: `ACCUM_MIN_USD = 1.0` por moneda → la primera compra llega con ~+6.7 $ por encima de 0 y después se
  acumula poco a poco con cada ganancia nueva. Coste de una compra de 1 $ en Arbitrum: ~0.0005 $ + céntimos de gas.
- Además, con cortos abiertos (que ahora son frecuentes) ya no se espera a cerrarlos: se compra solo con el USDC libre
  de la wallet, sin tocar nunca la garantía de Dolomite/Aave. Si no hay USDC libre suficiente, espera.
- Sin cambios: solo cuentan las ganancias NUEVAS por encima del máximo realizado anterior (hay que recuperar primero
  los −6.34 $ actuales); lo acumulado nunca se vende y no cuenta para el trading.
- Tests: 28 bloques OK (19: corto abierto + USDC libre; 19b: primera acumulación con +6.90 $).
