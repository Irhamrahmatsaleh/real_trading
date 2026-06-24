from datetime import datetime, timezone

from app.future_mode import evaluate_future_gate
from app.models import (
    LiquidationCheckStatus,
    SignalSide,
    SignalState,
    StrategyEvidence,
    TargetPlan,
    TradeSignal,
)


def test_future_mode_blocks_fast_young_fvg_without_sweep():
    signal = _signal(score=76, tp3_eta=24, fvg_age=1, volume=92, sweep=False)

    evaluation = evaluate_future_gate(signal)

    assert evaluation.allowed is False
    assert "fast young-FVG setup without sweep confirmation" in evaluation.reasons


def test_future_mode_allows_high_energy_confirmed_setup():
    signal = _signal(score=92, tp3_eta=55, fvg_age=5, volume=86, sweep=True)

    evaluation = evaluate_future_gate(signal)

    assert evaluation.allowed is True
    assert evaluation.projection.expected_r > 0
    assert evaluation.state.energy > evaluation.state.entropy


def _signal(*, score: int, tp3_eta: int, fvg_age: int, volume: float, sweep: bool) -> TradeSignal:
    return TradeSignal(
        symbol="FUTUREUSDT",
        side=SignalSide.SHORT,
        state=SignalState.TRADE_READY,
        score=score,
        generated_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
        entry_low=100,
        entry_high=101,
        stop_loss=103,
        targets=[
            TargetPlan(label="TP1", price=98, distance_pct=2.0, estimated_minutes=18, timing_basis="ATR"),
            TargetPlan(label="TP2", price=96, distance_pct=4.0, estimated_minutes=34, timing_basis="ATR"),
            TargetPlan(label="TP3", price=94, distance_pct=6.0, estimated_minutes=tp3_eta, timing_basis="ATR"),
        ],
        risk_reward=2.0,
        invalidation_condition="Invalid above SL.",
        confidence_explanation="Rule-based confluence score, not a guaranteed win rate.",
        evidence=[
            _evidence("HTF/LTF structure", True, "Trend regime is bearish."),
            _evidence("Manipulation sweep", sweep, "Buy-side liquidity swept 3 candle(s) ago; rejected 103."),
            _evidence("Displacement", True, "Bearish displacement 2 candle(s) ago with body 0.29."),
            _evidence("IFVG / FVG", True, f"Bearish FVG resistance age {fvg_age}: 101 - 102."),
            _evidence("Volume participation", True, f"Volume percentile: {volume:.1f}%."),
            _evidence("Higher timeframe alignment", True, "Higher timeframe close is aligned."),
        ],
        alert_eligible=False,
        alert_reason="TRADE_READY: test.",
        execution_quality_status="PASSED",
        market_regime="bearish_trend",
        market_spread_pct=0.04,
        post_signal_revalidation="PASSED",
        liquidation_check_status=LiquidationCheckStatus.PASSED,
        account_risk_validated=True,
    )


def _evidence(name: str, passed: bool, detail: str) -> StrategyEvidence:
    return StrategyEvidence(
        name=name,
        status="passed" if passed else "not_confirmed",
        detail=detail,
        score=10 if passed else 0,
    )
