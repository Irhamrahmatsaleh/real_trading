from datetime import datetime, timezone

from app.config import Settings
from app.models import OutcomeNotification, SignalSide, SignalState, StrategyEvidence, TargetPlan, TradeSignal
from app.telegram import build_send_message_payload, build_send_photo_payload, format_outcome_message, format_outcome_photo_caption, format_signal_message


def test_telegram_message_is_manual_professional_and_complete():
    signal = TradeSignal(
        symbol="BTCUSDT",
        side=SignalSide.LONG,
        state=SignalState.TRADE_READY,
        score=88,
        generated_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        entry_low=100.0,
        entry_high=101.0,
        stop_loss=98.5,
        risk_reward=2.8,
        targets=[
            TargetPlan(label="TP1", price=103, distance_pct=2.0, estimated_minutes=45, timing_basis="ATR"),
            TargetPlan(label="TP2", price=105, distance_pct=4.0, estimated_minutes=90, timing_basis="ATR"),
            TargetPlan(label="TP3", price=108, distance_pct=7.0, estimated_minutes=150, timing_basis="ATR"),
        ],
        invalidation_condition="Invalid below sweep low.",
        confidence_explanation="Rule-based confluence score, not a guaranteed win rate.",
        evidence=[StrategyEvidence(name="IFVG / FVG", status="passed", detail="IFVG retest.", score=16)],
        alert_eligible=True,
        alert_reason="TRADE_READY: passed gates.",
    )
    message = format_signal_message(signal)
    assert "Manual trading signal only. No automatic order was placed." in message
    assert "TP1" in message and "TP2" in message and "TP3" in message
    assert "ETA: ~45 min" in message
    assert "Liquidation Check: <b>NOT VALIDATED</b>" in message
    assert "Probability: Not Calibrated" in message
    assert "Probability: Probability:" not in message
    assert "Invalidation" in message


def test_telegram_payload_includes_group_topic_thread_when_configured():
    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="-1002265468566",
        TELEGRAM_SIGNAL_THREAD_ID="44",
    )

    payload = build_send_message_payload(settings, "signal", settings.telegram_signal_thread_id)

    assert payload["chat_id"] == "-1002265468566"
    assert payload["message_thread_id"] == 44
    assert payload["text"] == "signal"
    assert payload["parse_mode"] == "HTML"


def test_telegram_payload_omits_thread_for_regular_chat():
    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="6644936162",
    )

    payload = build_send_message_payload(settings, "signal")

    assert payload["chat_id"] == "6644936162"
    assert "message_thread_id" not in payload


def test_telegram_outcome_message_and_payload_use_tpsl_thread():
    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="-1002265468566",
        TELEGRAM_TPSL_THREAD_ID="53",
    )
    outcome = OutcomeNotification(
        outcome_id="outcome-1",
        symbol="HUSDT",
        side="SHORT",
        closed_at="2026-06-22T09:05:01+00:00",
        actual_entry=0.15884,
        actual_exit=0.1566,
        qty=620,
        net_pnl=1.28123496,
        result_r=0.31,
        outcome_type="TP1",
        match_confidence="manual_adjusted",
        signal_generated_at="2026-06-22T09:01:32+00:00",
    )

    message = format_outcome_message(outcome)
    payload = build_send_message_payload(settings, message, settings.telegram_tpsl_thread_id)

    assert "HUSDT SHORT TP1 HIT" in message
    assert "manual_adjusted" in message
    assert "+1.28123 USDT" in message
    assert payload["message_thread_id"] == 53


def test_telegram_outcome_photo_caption_and_payload_use_tpsl_thread():
    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="-1002265468566",
        TELEGRAM_TPSL_THREAD_ID="53",
    )
    outcome = OutcomeNotification(
        outcome_id="outcome-photo",
        symbol="BSBUSDT",
        side="SHORT",
        closed_at="2026-06-22T13:26:13+00:00",
        actual_entry=0.3299,
        actual_exit=0.3225,
        qty=303,
        net_pnl=2.09473,
        result_r=0.7428,
        outcome_type="MANUAL_PROFIT",
        match_confidence="matched",
    )

    caption = format_outcome_photo_caption(outcome)
    payload = build_send_photo_payload(settings, caption, settings.telegram_tpsl_thread_id)

    assert "BSBUSDT SHORT PROFIT CLOSED" in caption
    assert "Real Bybit closed-candle outcome chart" in caption
    assert payload["chat_id"] == "-1002265468566"
    assert payload["message_thread_id"] == "53"
    assert payload["parse_mode"] == "HTML"
