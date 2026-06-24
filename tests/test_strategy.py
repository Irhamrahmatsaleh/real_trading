from datetime import datetime, timedelta, timezone

from app.models import Candle, MarketTicker, SignalSide, SignalState, TargetPlan
from app.strategy import DirectionCandidate, StrategyAnalyzer


def test_strategy_waits_when_real_data_is_insufficient():
    ticker = MarketTicker(symbol="BTCUSDT", last_price=100, turnover24h=1_000_000, volume24h=1000, price24h_pct=1)
    signal = StrategyAnalyzer().analyze(ticker, [], [])
    assert signal.state == SignalState.WAIT
    assert signal.alert_eligible is False
    assert signal.missing_data


def test_strategy_targets_have_positive_real_data_timing():
    ticker = MarketTicker(symbol="BTCUSDT", last_price=120, turnover24h=1_000_000, volume24h=1000, price24h_pct=1)
    candles = _synthetic_candles(100, 100, drift=0.2)
    htf = _synthetic_candles(50, 95, drift=0.4)
    signal = StrategyAnalyzer().analyze(ticker, candles, htf)
    if signal.targets:
        assert len(signal.targets) == 3
        assert all(target.estimated_minutes > 0 for target in signal.targets)
        assert signal.stop_loss is not None
    assert signal.probability_label == "Probability: Not Calibrated"


def test_stop_plan_caps_unrealistic_structural_sl_distance():
    analyzer = StrategyAnalyzer(max_stop_atr_multiple=2.0, max_stop_distance_pct=2.0)

    plan = analyzer._stop_plan(
        SignalSide.LONG,
        entry_low=99.8,
        entry_high=100.2,
        structural_stop=90.0,
        atr_value=1.0,
    )

    assert plan.capped is True
    assert round(plan.stop_loss, 4) == 98.0
    assert "practical cap" in plan.detail
    assert "manual invalidation" in plan.detail


def test_overlong_tp_timing_cannot_be_trade_ready():
    analyzer = StrategyAnalyzer(max_tp3_eta_minutes=300)
    candidate = DirectionCandidate(
        side=SignalSide.LONG,
        score=95,
        entry_low=99,
        entry_high=100,
        stop_loss=97,
        targets=[TargetPlan(label="TP3", price=104, distance_pct=4, estimated_minutes=600, timing_basis="test")],
        risk_reward=2.0,
        invalidation_condition="Invalid below SL.",
        evidence=[],
        confidence_explanation="Test.",
        critical_count=7,
        core_confirmed=True,
        directional_context_confirmed=True,
        risk_geometry_ok=True,
        timing_ok=False,
    )

    assert analyzer._state_for(candidate) == SignalState.WATCH


def test_ifvg_displacement_without_directional_context_is_not_trade_ready():
    analyzer = StrategyAnalyzer()
    candidate = DirectionCandidate(
        side=SignalSide.LONG,
        score=56,
        entry_low=99,
        entry_high=100,
        stop_loss=97,
        targets=[TargetPlan(label="TP3", price=104, distance_pct=4, estimated_minutes=60, timing_basis="test")],
        risk_reward=2.0,
        invalidation_condition="Invalid below SL.",
        evidence=[],
        confidence_explanation="Test.",
        critical_count=5,
        core_confirmed=True,
        directional_context_confirmed=False,
        risk_geometry_ok=True,
        timing_ok=True,
    )

    assert analyzer._state_for(candidate) == SignalState.WATCH


def test_fresh_core_setup_with_directional_context_can_be_trade_ready():
    analyzer = StrategyAnalyzer()
    candidate = DirectionCandidate(
        side=SignalSide.LONG,
        score=66,
        entry_low=99,
        entry_high=100,
        stop_loss=97,
        targets=[TargetPlan(label="TP3", price=104, distance_pct=4, estimated_minutes=60, timing_basis="test")],
        risk_reward=2.0,
        invalidation_condition="Invalid below SL.",
        evidence=[],
        confidence_explanation="Test.",
        critical_count=6,
        core_confirmed=True,
        directional_context_confirmed=True,
        risk_geometry_ok=True,
        timing_ok=True,
    )

    assert analyzer._state_for(candidate) == SignalState.TRADE_READY


def _synthetic_candles(count: int, start: float, drift: float) -> list[Candle]:
    now = datetime.now(timezone.utc) - timedelta(minutes=15 * count)
    price = start
    candles: list[Candle] = []
    for index in range(count):
        open_price = price
        close = price + drift + ((index % 5) - 2) * 0.05
        high = max(open_price, close) + 0.4
        low = min(open_price, close) - 0.4
        if index == count - 1:
            low -= 1.4
            close = max(close, low + 1.2)
        candles.append(
            Candle(
                open_time=now + timedelta(minutes=15 * index),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=1000 + index * 10,
                turnover=(1000 + index * 10) * close,
            )
        )
        price = close
    return candles
