# 2026-10-03 — Infraestructura: el bot deja de depender del reloj de GitHub

**Problema.** Desde el 2026-10-03 10:20 UTC el `schedule` de GitHub Actions dejó de lanzar casi todas las
ejecuciones (tras 7 días perfectos a 24/día). Huecos de 3.3 h y 5.1 h. El workflow seguía activo y
`workflow_dispatch` funcionaba. Es un fallo conocido de GitHub en 2026 (community discussions #206019, #208473).

**Cambios.**
1. Ciclo diario a prueba de huecos: lo hace la primera ejecución de cada día UTC (`last_signal_day != hoy`),
   no solo la de las 00h (commit 98a79d3). Validado el 04-oct (02:45 UTC).
2. Disparador externo gratuito: cron-job.org → `POST .../actions/workflows/bot.yml/dispatches` cada hora a
   los :25 (Miami), con un token fine-grained de Oier (solo crypto-bot, solo Actions; caduca 2027-10-03).
   Prueba: 204 y run #164 OK. El cron interno pasa a `55 * * * *` de respaldo.
3. Revisiones (miércoles salud, sábado semanal) movidas al chat del proyecto, que lee el repo directamente.
4. Limpieza: eliminado el código de Hyperliquid (bot/live.py, scripts/run_live.py), no disponible en EE.UU.

**Sin cambios** en la estrategia ni en `bot/onchain.py`.
