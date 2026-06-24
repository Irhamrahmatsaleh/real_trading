from __future__ import annotations

from statistics import mean

from app.models import Candle


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return mean(values[-period:])


def true_ranges(candles: list[Candle]) -> list[float]:
    ranges: list[float] = []
    for index, candle in enumerate(candles):
        if index == 0:
            ranges.append(candle.high - candle.low)
            continue
        previous_close = candles[index - 1].close
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    return ranges


def atr(candles: list[Candle], period: int = 14) -> float | None:
    ranges = true_ranges(candles)
    if len(ranges) < period:
        return None
    return mean(ranges[-period:])


def average_abs_close_move(candles: list[Candle], period: int = 30) -> float | None:
    if len(candles) < period + 1:
        return None
    moves = [
        abs(candles[index].close - candles[index - 1].close)
        for index in range(len(candles) - period, len(candles))
    ]
    return mean(moves)


def recent_high(candles: list[Candle], lookback: int = 40) -> float | None:
    if len(candles) < lookback:
        return None
    return max(candle.high for candle in candles[-lookback:-1])


def recent_low(candles: list[Candle], lookback: int = 40) -> float | None:
    if len(candles) < lookback:
        return None
    return min(candle.low for candle in candles[-lookback:-1])


def percentile_rank(values: list[float], current: float) -> float:
    if not values:
        return 0.0
    below_or_equal = sum(1 for value in values if value <= current)
    return below_or_equal / len(values)
