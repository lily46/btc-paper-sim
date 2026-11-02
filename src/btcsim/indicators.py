"""テクニカル指標。すべて確定足のみを入力に受け取る前提。"""
from __future__ import annotations

from decimal import Decimal


def sma(values: list[Decimal], window: int) -> Decimal | None:
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:], Decimal(0)) / Decimal(window)


def sma_series(values: list[Decimal], window: int) -> list[Decimal | None]:
    out: list[Decimal | None] = []
    for i in range(len(values)):
        out.append(sma(values[: i + 1], window))
    return out


def rsi(values: list[Decimal], period: int = 14) -> Decimal | None:
    """Wilder 方式の RSI。データ不足なら None。"""
    if period <= 0 or len(values) < period + 1:
        return None
    gains, losses = Decimal(0), Decimal(0)
    for prev, cur in zip(values[:period], values[1:period + 1]):
        diff = cur - prev
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / Decimal(period)
    avg_loss = losses / Decimal(period)
    for prev, cur in zip(values[period:-1], values[period + 1:]):
        diff = cur - prev
        gain = diff if diff > 0 else Decimal(0)
        loss = -diff if diff < 0 else Decimal(0)
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)
    if avg_loss == 0:
        return Decimal(100) if avg_gain > 0 else Decimal(50)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))
