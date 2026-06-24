from datetime import datetime, timedelta, timezone

from app.models import Candle, OutcomeNotification
from app.outcome_chart import chart_window_for_outcome, render_outcome_chart


def test_render_outcome_chart_returns_png_from_real_candles():
    generated_at = datetime(2026, 6, 22, 12, tzinfo=timezone.utc)
    candles = _candles(generated_at - timedelta(hours=2), 30)
    outcome = OutcomeNotification(
        outcome_id="outcome-chart",
        symbol="BSBUSDT",
        side="SHORT",
        closed_at=(generated_at + timedelta(hours=1)).isoformat(),
        entry_low=0.329,
        entry_high=0.331,
        actual_entry=0.3299,
        actual_exit=0.3225,
        stop_loss=0.337,
        tp1=0.323,
        tp2=0.318,
        tp3=0.312,
        qty=303,
        net_pnl=2.09473,
        result_r=0.7428,
        outcome_type="MANUAL_PROFIT",
        match_confidence="matched",
        signal_generated_at=generated_at.isoformat(),
    )

    png = render_outcome_chart(outcome, candles)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert int.from_bytes(png[16:20], "big") == 1280
    assert int.from_bytes(png[20:24], "big") == 720
    assert len(png) > 5000


def test_chart_window_uses_signal_to_closed_trade_range():
    outcome = OutcomeNotification(
        outcome_id="outcome-window",
        symbol="HMSTRUSDT",
        side="LONG",
        closed_at="2026-06-22T13:01:59+00:00",
        outcome_type="SL",
        match_confidence="matched",
        signal_generated_at="2026-06-22T12:03:12+00:00",
    )

    start, end = chart_window_for_outcome(outcome, 15)

    assert start.isoformat() == "2026-06-22T10:03:12+00:00"
    assert end.isoformat() == "2026-06-22T15:01:59+00:00"


def _candles(start: datetime, count: int) -> list[Candle]:
    candles: list[Candle] = []
    price = 0.331
    for index in range(count):
        open_price = price
        close_price = price - 0.00025 + (0.00008 if index % 4 == 0 else 0)
        high = max(open_price, close_price) + 0.0006
        low = min(open_price, close_price) - 0.0006
        candles.append(
            Candle(
                open_time=start + timedelta(minutes=15 * index),
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                volume=1000 + index,
                turnover=500 + index,
            )
        )
        price = close_price
    return candles
