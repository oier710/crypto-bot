# crypto-bot

Bot de trading sistemático con capital pequeño (referencia 115.75 $, 26-sep-2026).
Objetivo: crecimiento lento y consistente con riesgo controlado.

## Cómo funciona

- **Estrategia v2**: tendencia EMA 20/60 + ADX ≥ 20 en velas **diarias** de BTC y ETH, largos y cortos.
  75 % de cada mitad del capital por posición, stop 2×ATR(14). Límites: −5 % en un día o −25 % desde
  máximo → cierra todo y se para (`halted`).
- **Dónde opera**: Uniswap V3 (largos) y Aave V3 (cortos sintéticos) en Arbitrum One, con una cuenta
  de MetaMask dedicada solo a esto.
- **Quién lo lanza**: cron-job.org llama cada hora a `workflow_dispatch` de GitHub Actions
  (`.github/workflows/bot.yml`); el cron interno de GitHub queda de respaldo. Cada ejecución revisa
  stops y salud de Aave; la primera de cada día UTC hace además el ciclo de señales.
- **Registro**: cada ejecución hace commit de `logs/` (estado, operaciones, valor de cartera, log).

## Estructura

```
bot/onchain.py          ejecutor real (Uniswap + Aave)
bot/strategies/         reglas (trend_ema = la que se usa; las otras, para investigación)
bot/backtest.py         motor de backtest con costes
bot/data.py, indicators.py, config.py, report.py
scripts/run_onchain.py  punto de entrada del bot (--auto, --dry-run, --test-short ...)
scripts/report.py       informe de contabilidad (results/report.md + gráfico)
scripts/run_backtest.py, analyze_v1.py, analyze_v2.py, pattern_study.py, fetch_data.py   investigación
journal/                diario de decisiones
results/                resultados de backtests
logs/                   lo escribe el bot (no editar a mano)
```

## Uso manual

- Parar el bot: desactivar el job en cron-job.org **y** el workflow en GitHub → Actions.
- Ejecutar a mano: Actions → crypto-bot → Run workflow (`auto`, `dry-run` o `test-short`).
- Informe: `python scripts/report.py`.

## Reglas

1. Nada toca dinero real sin backtest y prueba previa.
2. Fees, slippage y gas se modelan siempre.
3. Las claves nunca se escriben en el chat ni se suben al repo.
4. Los cambios de código los sube Oier (`git pull && git add ... && git commit && git push`).
