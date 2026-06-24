from datetime import datetime, timezone

from app.models import LiquidationCheckStatus, PositionSnapshot, SignalSide, SignalState, TargetPlan, TradeSignal
from app.risk import evaluate_liquidation_risk


def test_long_liquidation_valid_when_liq_below_sl_with_buffer():
    signal = _signal(SignalSide.LONG, entry=6, stop_loss=4)
    position = PositionSnapshot(symbol="TESTUSDT", side=SignalSide.LONG, size=1, liquidation_price=3)

    result = evaluate_liquidation_risk(signal, position, min_buffer_r=0.25)

    assert result.status == LiquidationCheckStatus.PASSED
    assert result.account_risk_validated is True


def test_long_liquidation_invalid_when_liq_between_sl_and_entry():
    signal = _signal(SignalSide.LONG, entry=6, stop_loss=4)
    position = PositionSnapshot(symbol="TESTUSDT", side=SignalSide.LONG, size=1, liquidation_price=5)

    result = evaluate_liquidation_risk(signal, position, min_buffer_r=0.25)

    assert result.status == LiquidationCheckStatus.FAILED
    assert result.downgrade_state == SignalState.AVOID


def test_short_liquidation_valid_when_liq_above_sl_with_buffer():
    signal = _signal(SignalSide.SHORT, entry=6, stop_loss=7)
    position = PositionSnapshot(symbol="TESTUSDT", side=SignalSide.SHORT, size=1, liquidation_price=8)

    result = evaluate_liquidation_risk(signal, position, min_buffer_r=0.25)

    assert result.status == LiquidationCheckStatus.PASSED
    assert result.account_risk_validated is True


def test_short_liquidation_invalid_when_liq_between_entry_and_sl():
    signal = _signal(SignalSide.SHORT, entry=6, stop_loss=7)
    position = PositionSnapshot(symbol="TESTUSDT", side=SignalSide.SHORT, size=1, liquidation_price=6.5)

    result = evaluate_liquidation_risk(signal, position, min_buffer_r=0.25)

    assert result.status == LiquidationCheckStatus.FAILED
    assert result.downgrade_state == SignalState.AVOID


def test_missing_liquidation_data_does_not_fake_validation():
    signal = _signal(SignalSide.LONG, entry=6, stop_loss=4)

    result = evaluate_liquidation_risk(signal, position=None, min_buffer_r=0.25)

    assert result.status == LiquidationCheckStatus.NOT_VALIDATED
    assert result.account_risk_validated is False
    assert result.liquidation_price is None


def test_liquidation_buffer_failure_downgrades_to_watch():
    signal = _signal(SignalSide.LONG, entry=6, stop_loss=4)
    position = PositionSnapshot(symbol="TESTUSDT", side=SignalSide.LONG, size=1, liquidation_price=3.8)

    result = evaluate_liquidation_risk(signal, position, min_buffer_r=0.25)

    assert result.status == LiquidationCheckStatus.FAILED
    assert result.downgrade_state == SignalState.WATCH


def _signal(side: SignalSide, entry: float, stop_loss: float) -> TradeSignal:
    return TradeSignal(
        symbol="TESTUSDT",
        side=side,
        state=SignalState.TRADE_READY,
        score=90,
        generated_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        entry_low=entry,
        entry_high=entry,
        stop_loss=stop_loss,
        targets=[TargetPlan(label="TP1", price=8, distance_pct=10, estimated_minutes=60, timing_basis="ATR")],
        risk_reward=2,
        invalidation_condition="Invalid beyond SL.",
        confidence_explanation="Test signal.",
        alert_eligible=True,
        alert_reason="TRADE_READY: test.",
    )
