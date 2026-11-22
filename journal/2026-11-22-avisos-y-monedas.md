# 2026-11-22 — Avisos al móvil (ntfy) y estudio de más monedas

**Avisos** (`bot/notify.py`, petición de Oier): durante cada ejecución se apuntan los hechos importantes y al terminar se manda UN aviso a ntfy.sh (canal = secreto `NTFY_TOPIC`, que crea Oier; sin él no hace nada).
- Avisa: operaciones, parciales, stops, reentradas, compras de acumulación, alertas de riesgo y errores internos; resumen diario en el ciclo de ~19:05 Miami.
- No avisa: ejecuciones tranquilas, ajustes del stop que persigue, un fallo suelto (solo a partir de 2 ejecuciones fallidas seguidas), parciales/reentradas anunciadas pero no ejecutadas.
- Una misma alerta (p. ej. BOT PARADO, que se registra en cada ejecución) no se repite antes de 6 h. Memoria en `logs/notify_state.json`.
- Seguridad: nunca rompe ni cambia el trading (todo en try/except; `flush()` en el `finally` no altera la excepción ni el código de salida). El mensaje solo lleva monedas, importes y precios.
- Pruebas: `tests/test_notify.py` (23 comprobaciones) + `tests/test_onchain_v3.py` TODO OK. Revisión independiente: sin bloqueantes; sus 3 recomendaciones (parciales no ejecutadas, errores internos sin aviso, repetición de alertas) aplicadas.
- Workflow: `bot.yml` pasa `NTFY_TOPIC` y guarda `logs/notify_state.json`.

**Estudio de monedas** (`scripts/research_2026_11i.py`): escaneo en vivo de Dolomite (qué se puede poner en corto) + backtest de cada candidata añadida al 10 % y 15 % con el sistema de producción (incluido el stop que persigue), costes 0.35 %/lado y estrés 0.60 %.
