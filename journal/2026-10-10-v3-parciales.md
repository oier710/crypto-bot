# 2026-10-10 — v3: ventas parciales + reentrada en máximo nuevo

## Decisión
Oier elige la variante más consistente aunque gane menos que la v2 ("ya construiremos otro bot más
agresivo si es caso"). Criterio acordado el 07-oct: consistencia por encima de rentabilidad máxima.

## Reglas (largos; cortos en espejo)
- Entrada y salida por señal diaria igual que v2 (EMA 20/60 + ADX ≥ 20). Stop inicial fijo 2×ATR.
- +1.5 ATR desde la referencia → vende 1/3 y el stop sube a la entrada (+0.2 % por costes).
- +3 ATR → vende otro 1/3. El último tercio sigue hasta el stop o el cambio de señal.
- Tras una parcial, si el precio corrige ≥ 1 ATR desde el máximo y después marca un máximo nuevo
  (señal aún a favor) → recompra lo vendido; stop de toda la posición = max(stop, precio − 1.5 ATR);
  la referencia de las parciales pasa a ser el precio de la recompra (con el ATR del día).
- El ciclo diario ya no redimensiona una operación abierta (no recompra lo vendido en parciales).
- Las posiciones abiertas con la v2 se adoptan: entrada = última compra registrada; ATR = (entrada − stop)/2.

## Evidencia (results/research_2026_10.txt y research_2026_10b.txt; 3 años, velas 1h reales, costes reales)
| | v2 | v3 |
|---|---|---|
| Retorno 2024-01 → 2026-10 | +99.7 % | +68.7 % |
| Caída máxima | −24.1 % | −16.2 % |
| Meses en rojo | 13/33 | 11/33 |
| Peor mes | −12.6 % | −9.1 % |
Gana a "solo parciales" en ambas mitades. Punto débil: el año lateral (dic-24 → nov-25) ≈ 0 %.
Riesgo de sobreajuste: es la mejor de 7 combinaciones; la vecina (stop 2 ATR) da +56.6 % / −18.3 %.

## Arreglo incluido (latente en v2)
`close_short` retiraba de golpe todo el USDC necesario para recomprar la deuda. Con un corto del tamaño
máximo (HF 1.5) ese retiro dejaría el HF < 1 y Aave lo rechaza: el stop de un corto grande no se habría
ejecutado. Ahora la recompra se hace por tramos (cada repago libera garantía) sin bajar el HF de 1.01.

## Seguridad (incluye correcciones de una revisión independiente del código)
- Si la gestión v3 lanza un error, el bot lo registra y aplica los stops de siempre (no se queda sin stop).
- El workflow hace checkout de `main` (último estado), no del commit del momento en que se encoló la
  ejecución: una ejecución en cola no puede repetir una parcial con estado viejo.
- Parciales idempotentes: si la posición ya está recortada, solo se actualiza el estado.
- Si un corto no se puede cerrar del todo, se mantienen stop y posición y se reintenta (antes se borraban).
- La reentrada solo actualiza estado si la compra se hizo de verdad; no se recompra en la primera
  ejecución del día (manda la señal diaria nueva).
- `--dry-run` ya no guarda estado.

## Pruebas
`tests/test_onchain_v3.py` (cadena falsa, sin red): adopción v2→v3, parciales, stop a entrada,
no-redimensionado diario, reentrada (y no-reentrada con corrección insuficiente), stop y reentrada al día
siguiente, cambio a corto, parcial y stop de corto, corto de tamaño máximo cerrado por tramos, respaldo
de stops si la v3 falla, parcial idempotente, reentrada sin USDC, sin reentrada en el ciclo diario y corto
que no se puede cerrar. Todo OK.
