from __future__ import annotations

import binascii
import struct
import zlib
from datetime import datetime, timedelta

from app.models import Candle, OutcomeNotification


class OutcomeChartError(ValueError):
    pass


def render_outcome_chart(outcome: OutcomeNotification, candles: list[Candle]) -> bytes:
    selected = _select_candles(outcome, candles)
    if len(selected) < 3:
        raise OutcomeChartError("not enough candles to render outcome chart")

    width = 1280
    height = 720
    canvas = _Canvas(width, height, (248, 250, 252))
    left = 72
    right = 52
    top = 54
    bottom = 76
    plot_x1 = left
    plot_y1 = top
    plot_x2 = width - right
    plot_y2 = height - bottom
    plot_width = plot_x2 - plot_x1
    plot_height = plot_y2 - plot_y1

    prices = [price for candle in selected for price in (candle.high, candle.low)]
    prices.extend(_outcome_prices(outcome))
    low = min(prices)
    high = max(prices)
    if high <= low:
        high = low + max(abs(low) * 0.01, 1e-9)
    padding = max((high - low) * 0.08, high * 0.0005, 1e-9)
    low -= padding
    high += padding

    profitable = outcome.net_pnl >= 0
    accent = (22, 163, 74) if profitable else (220, 38, 38)
    canvas.rect(0, 0, width, 24, accent)
    canvas.rect(plot_x1, plot_y1, plot_width, plot_height, (255, 255, 255))

    def y_of(price: float) -> int:
        ratio = (high - price) / (high - low)
        return plot_y1 + int(max(0.0, min(1.0, ratio)) * plot_height)

    def x_of(index: int) -> int:
        if len(selected) == 1:
            return plot_x1 + plot_width // 2
        return plot_x1 + int(index * plot_width / (len(selected) - 1))

    entry = outcome.actual_entry or _midpoint(outcome.entry_low, outcome.entry_high)
    if entry is not None:
        entry_y = y_of(entry)
        if outcome.side == "SHORT":
            canvas.rect(plot_x1, entry_y, plot_width, plot_y2 - entry_y, (240, 253, 244))
            canvas.rect(plot_x1, plot_y1, plot_width, entry_y - plot_y1, (254, 242, 242))
        else:
            canvas.rect(plot_x1, plot_y1, plot_width, entry_y - plot_y1, (240, 253, 244))
            canvas.rect(plot_x1, entry_y, plot_width, plot_y2 - entry_y, (254, 242, 242))

    for step in range(6):
        y = plot_y1 + int(step * plot_height / 5)
        canvas.hline(plot_x1, plot_x2, y, (226, 232, 240))
    for step in range(8):
        x = plot_x1 + int(step * plot_width / 7)
        canvas.vline(x, plot_y1, plot_y2, (241, 245, 249))

    candle_slot = max(4, int(plot_width / max(len(selected), 1)))
    body_half = max(2, min(8, int(candle_slot * 0.32)))
    for index, candle in enumerate(selected):
        x = x_of(index)
        up = candle.close >= candle.open
        color = (22, 163, 74) if up else (220, 38, 38)
        wick_color = (15, 118, 110) if up else (185, 28, 28)
        canvas.vline(x, y_of(candle.high), y_of(candle.low), wick_color)
        open_y = y_of(candle.open)
        close_y = y_of(candle.close)
        body_y = min(open_y, close_y)
        body_h = max(2, abs(close_y - open_y))
        canvas.rect(x - body_half, body_y, body_half * 2 + 1, body_h, color)

    for value in (outcome.tp1, outcome.tp2, outcome.tp3):
        if value is not None:
            y = y_of(value)
            canvas.dashed_hline(plot_x1, plot_x2, y, (21, 128, 61), dash=14, gap=8)
            canvas.rect(plot_x2 - 10, y - 5, 20, 10, (21, 128, 61))

    if outcome.stop_loss is not None:
        y = y_of(outcome.stop_loss)
        canvas.dashed_hline(plot_x1, plot_x2, y, (190, 18, 60), dash=18, gap=7)
        canvas.rect(plot_x2 - 12, y - 6, 24, 12, (190, 18, 60))

    if entry is not None:
        y = y_of(entry)
        canvas.hline(plot_x1, plot_x2, y, (37, 99, 235))
        canvas.rect(plot_x1 - 12, y - 5, 24, 10, (37, 99, 235))

    if outcome.actual_exit is not None:
        y = y_of(outcome.actual_exit)
        exit_x = _exit_x(outcome, selected, x_of)
        canvas.circle(exit_x, y, 12, (245, 158, 11))
        canvas.circle(exit_x, y, 6, (15, 23, 42))
        canvas.dashed_hline(plot_x1, plot_x2, y, (245, 158, 11), dash=6, gap=8)

    canvas.rect(plot_x1, plot_y1, plot_width, 2, (148, 163, 184))
    canvas.rect(plot_x1, plot_y2 - 2, plot_width, 2, (148, 163, 184))
    canvas.rect(plot_x1, plot_y1, 2, plot_height, (148, 163, 184))
    canvas.rect(plot_x2 - 2, plot_y1, 2, plot_height, (148, 163, 184))
    return canvas.to_png()


def chart_window_for_outcome(outcome: OutcomeNotification, interval_minutes: int) -> tuple[datetime, datetime] | None:
    closed = _parse_datetime(outcome.closed_at)
    generated = _parse_datetime(outcome.signal_generated_at)
    if closed is None:
        return None
    interval = timedelta(minutes=max(1, interval_minutes))
    start = generated or closed - interval * 40
    return start - interval * 8, closed + interval * 8


def _select_candles(outcome: OutcomeNotification, candles: list[Candle]) -> list[Candle]:
    ordered = sorted(candles, key=lambda candle: candle.open_time)
    if not ordered:
        return []
    interval = _candle_interval(ordered)
    closed = _parse_datetime(outcome.closed_at)
    generated = _parse_datetime(outcome.signal_generated_at)
    if closed is not None:
        start = generated or closed - interval * 40
        begin = start - interval * 4
        end = closed + interval * 4
        selected = [candle for candle in ordered if begin <= candle.open_time <= end]
        if len(selected) >= 8:
            return selected[-120:]
        before_close = [candle for candle in ordered if candle.open_time <= end]
        if before_close:
            return before_close[-90:]
    return ordered[-90:]


def _outcome_prices(outcome: OutcomeNotification) -> list[float]:
    values = [
        outcome.entry_low,
        outcome.entry_high,
        outcome.actual_entry,
        outcome.actual_exit,
        outcome.stop_loss,
        outcome.tp1,
        outcome.tp2,
        outcome.tp3,
    ]
    return [float(value) for value in values if value is not None and value > 0]


def _exit_x(outcome: OutcomeNotification, candles: list[Candle], x_of) -> int:
    closed = _parse_datetime(outcome.closed_at)
    if closed is None:
        return x_of(len(candles) - 1)
    closest_index = min(range(len(candles)), key=lambda index: abs((candles[index].open_time - closed).total_seconds()))
    return x_of(closest_index)


def _midpoint(low: float | None, high: float | None) -> float | None:
    if low is None or high is None:
        return None
    return (low + high) / 2


def _candle_interval(candles: list[Candle]) -> timedelta:
    diffs = [
        candles[index].open_time - candles[index - 1].open_time
        for index in range(1, len(candles))
        if candles[index].open_time > candles[index - 1].open_time
    ]
    return min(diffs) if diffs else timedelta(minutes=15)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class _Canvas:
    def __init__(self, width: int, height: int, background: tuple[int, int, int]):
        self.width = width
        self.height = height
        self.pixels = bytearray(bytes(background) * (width * height))

    def rect(self, x: int, y: int, width: int, height: int, color: tuple[int, int, int]) -> None:
        x1 = max(0, x)
        y1 = max(0, y)
        x2 = min(self.width, x + width)
        y2 = min(self.height, y + height)
        if x2 <= x1 or y2 <= y1:
            return
        row = bytes(color) * (x2 - x1)
        for yy in range(y1, y2):
            start = (yy * self.width + x1) * 3
            self.pixels[start : start + len(row)] = row

    def hline(self, x1: int, x2: int, y: int, color: tuple[int, int, int]) -> None:
        self.rect(min(x1, x2), y, abs(x2 - x1) + 1, 2, color)

    def dashed_hline(self, x1: int, x2: int, y: int, color: tuple[int, int, int], *, dash: int, gap: int) -> None:
        x = min(x1, x2)
        end = max(x1, x2)
        while x <= end:
            self.hline(x, min(x + dash, end), y, color)
            x += dash + gap

    def vline(self, x: int, y1: int, y2: int, color: tuple[int, int, int]) -> None:
        self.rect(x, min(y1, y2), 2, abs(y2 - y1) + 1, color)

    def circle(self, cx: int, cy: int, radius: int, color: tuple[int, int, int]) -> None:
        radius_sq = radius * radius
        for y in range(cy - radius, cy + radius + 1):
            for x in range(cx - radius, cx + radius + 1):
                if (x - cx) * (x - cx) + (y - cy) * (y - cy) <= radius_sq:
                    self._pixel(x, y, color)

    def to_png(self) -> bytes:
        raw_rows = []
        for y in range(self.height):
            start = y * self.width * 3
            raw_rows.append(b"\x00" + bytes(self.pixels[start : start + self.width * 3]))
        raw = b"".join(raw_rows)
        png = bytearray(b"\x89PNG\r\n\x1a\n")
        png.extend(_png_chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)))
        png.extend(_png_chunk(b"IDAT", zlib.compress(raw, level=6)))
        png.extend(_png_chunk(b"IEND", b""))
        return bytes(png)

    def _pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if x < 0 or x >= self.width or y < 0 or y >= self.height:
            return
        index = (y * self.width + x) * 3
        self.pixels[index : index + 3] = bytes(color)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(data, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)
