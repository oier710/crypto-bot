# 2026-11-17 — Stop que persigue (3 ATR desde la parcial 1)

**Por qué**: crítica de Oier — tras las parciales, el resto de la posición solo salía por el stop en la entrada o por la señal diaria; si el precio volvía a la entrada se devolvía toda la ganancia no cobrada. Prioridad expresada: "que gane más" y acumular más BTC/ETH.

**Qué cambia** (`bot/onchain.py`, `manage_positions`, bloque 1b): desde la 1ª parcial, en cada ejecución el stop sigue al extremo (máximo en largos, mínimo en cortos) a `TRAIL_ATR = 3` × ATR de la posición. Solo se aprieta. Tras una reentrada (pt_done vuelve a 0) deja de perseguir hasta la siguiente parcial 1. `TRAIL_ATR = 0` lo desactiva.

**Datos** (`research_2026_11g`, variante S4; `research_2026_11h` para alternativas): +93.5 % vs +78.2 % en 33 meses; caída máx. −21.0 % vs −17.0 %; peor mes −12.8 % vs −7.3 %; gana en las dos mitades y 2 de 3 tercios, pierde en el último (+15.7 % vs +21.5 %). **No pasa el criterio habitual**: se despliega por decisión expresa de Oier. Escalón y salida rápida (11h) salieron peor.

**Efecto al desplegar** (estado del 17-nov): stop ETH 2,232 → ≈ 2,015; stop BTC 75,113 → ≈ 69,300. No salta de inmediato (precio 1,800 / 64,300).

**Pruebas**: `tests/test_onchain_v3.py` bloque 32 (largo y corto, no afloja, ciclo diario, reentrada, desactivado). Revisión independiente: sin bloqueantes; avisos: muestreo cada 15 min (más salidas que el backtest horario) y más comisiones (~+14 %).

**Criterio de reversión**: si tras 2-3 meses las salidas por stop que persigue restan frente al stop fijo (comparar en pnl.csv), `TRAIL_ATR = 0`.
