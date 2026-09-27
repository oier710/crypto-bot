# 2026-09-27 — Estudio sistemático de patrones

10 familias de patrones (tendencia EMA, ruptura Donchian, retroceso a la media, RSI en tendencia,
momentum ROC, medias alineadas, vela envolvente, ruptura+volumen, compresión Bollinger, cruce MACD),
solo largos, BTC+ETH, diario y 4h, fees 0.05 %+0.05 %, 2 años. Script: `scripts/pattern_study.py`.
Tabla completa: `results/pattern_study.txt` / `.csv`.

## Conclusiones
- Diario domina a 4h en todos los patrones. En 4h casi todo pierde en el año bajista.
- Mejores en diario: v1 trend_ema (+26 %, DD −18 %, Sharpe 0.71) y **medias alineadas EMA 10/20/50**
  (+30 %, DD −22 %, Sharpe 0.71, año 2 +7 % con 75 % de variantes positivas). Candidata a probar.
- Momentum ROC: +33 % pero DD −31 % y 135 operaciones. Peor ratio.
- Velas envolventes y cruce MACD: pierden (MACD 0 % variantes positivas). Descartados.
- RSI bajo en tendencia: 1-5 operaciones, sin datos suficientes.
- **Ninguna estrategia solo-largos gana en el año bajista.** Los cortos son el único camino para ese
  régimen → requieren más capital (perps regulados).
