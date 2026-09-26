"""Parámetros globales del experimento.

Todo lo que afecta a riesgo o costes vive aquí, no repartido por el código.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fees:
    """Costes de Hyperliquid para cuenta pequeña (tier base, sin descuentos).

    Fuente: docs de Hyperliquid, septiembre 2026. Revisar periódicamente.
    Se modelan de forma conservadora: siempre asumimos taker.
    """
    taker: float = 0.00045      # 0.045 % por lado
    maker: float = 0.00015      # 0.015 % por lado
    slippage: float = 0.0005    # 0.05 % estimado por lado en BTC/ETH/SOL con órdenes pequeñas
    funding_annual_est: float = 0.0  # se calcula desde datos reales cuando los tengamos

    @property
    def round_trip(self) -> float:
        """Coste total de entrar y salir a mercado (sin funding)."""
        return 2 * (self.taker + self.slippage)


@dataclass(frozen=True)
class Risk:
    initial_capital: float = 100.0
    position_fraction: float = 0.95   # fracción del capital por operación (sin apalancamiento)
    max_leverage: float = 1.0
    max_daily_loss_pct: float = 0.05  # -5 % en un día -> parar hasta el día siguiente
    max_total_drawdown_pct: float = 0.25  # -25 % desde máximo -> parar el bot y revisar
    one_position_at_a_time: bool = True


@dataclass(frozen=True)
class Universe:
    symbols: tuple = ("BTC", "ETH", "SOL")
    timeframes: tuple = ("1h", "4h")
    history_days: int = 730


FEES = Fees()
RISK = Risk()
UNIVERSE = Universe()

# Umbrales para que una estrategia pase de backtest a paper trading
ACCEPTANCE = dict(
    min_trades=60,              # menos que esto es ruido estadístico
    min_profit_factor=1.3,      # ganancias brutas / pérdidas brutas
    max_drawdown_pct=0.25,
    min_net_return_pct=0.10,    # neto de fees, sobre el periodo completo
    min_sharpe=0.8,             # anualizado sobre retornos diarios
)
