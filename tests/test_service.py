import asyncio
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.models import (
    ClosedPnlRecord,
    LearningReport,
    LiquidationCheckStatus,
    SignalSide,
    SignalState,
    StrategyEvidence,
    TargetPlan,
    TradeSignal,
)
from app.service import TradingAnalysisService, _adaptive_scan_limits, _has_enough_scan_candidates, select_best_trade_signal


class FakeNotifier:
    def __init__(self, status: str = "sent"):
        self.status = status
        self.symbols: list[str] = []
        self.outcomes: list[str] = []
        self.outcome_charts: list[bytes | None] = []

    async def send_signal(self, signal):
        self.symbols.append(signal.symbol)
        return self.status

    async def send_outcome(self, outcome, chart_png=None):
        self.outcomes.append(outcome.outcome_id)
        self.outcome_charts.append(chart_png)
        return self.status


def test_best_signal_selector_chooses_composite_quality_not_all_trade_ready():
    dash = _trade_ready(
        "DASHUSDT",
        score=82,
        tp3_pct=12.17,
        tp3_eta=247,
        volume=79.7,
        fvg_age=0,
        sweep=False,
        displacement_age=2,
        structure=True,
        htf=True,
    )
    kernel = _trade_ready(
        "KERNELUSDT",
        score=78,
        tp3_pct=7.08,
        tp3_eta=153,
        volume=89.8,
        fvg_age=12,
        sweep=True,
        displacement_age=1,
        structure=False,
        htf=False,
    )
    grass = _trade_ready(
        "GRASSUSDT",
        score=74,
        tp3_pct=7.90,
        tp3_eta=147,
        volume=8.5,
        fvg_age=29,
        sweep=False,
        displacement_age=5,
        structure=True,
        htf=True,
    )

    selected = select_best_trade_signal([grass, kernel, dash])

    assert selected is not None
    signal, selection_score, reason = selected
    assert signal.symbol == "DASHUSDT"
    assert selection_score > 90
    assert "TP3 12.17%" in reason


def test_telegram_sends_configured_number_of_best_signals_per_scan():
    async def run():
        settings = Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_CHAT_ID="123",
            MAX_TRADE_ALERTS_PER_SCAN=3,
            LEARNING_ENABLED=False,
        )
        service = TradingAnalysisService(settings)
        fake = FakeNotifier()
        service.notifier = fake
        signals = [
            _trade_ready("GRASSUSDT", score=74, tp3_pct=7.90, tp3_eta=147, volume=8.5, fvg_age=29),
            _trade_ready(
                "KERNELUSDT",
                score=78,
                tp3_pct=7.08,
                tp3_eta=153,
                volume=89.8,
                fvg_age=12,
                sweep=True,
                structure=False,
                htf=False,
            ),
            _trade_ready("DASHUSDT", score=82, tp3_pct=12.17, tp3_eta=247, volume=79.7, fvg_age=0),
        ]
        try:
            service._apply_best_signal_selection(signals)
            assert [signal.symbol for signal in signals if signal.alert_eligible] == ["KERNELUSDT", "DASHUSDT"]
            assert service.status.best_signal_symbol == "DASHUSDT"

            await service._send_alerts(signals)

            assert fake.symbols == ["DASHUSDT", "KERNELUSDT"]
            assert service.status.telegram_status == "sent_best:DASHUSDT,KERNELUSDT"
        finally:
            await service.client.close()

    asyncio.run(run())


def test_journal_records_only_after_telegram_sent(tmp_path):
    async def run():
        settings = Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_CHAT_ID="123",
            LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"),
            MAX_TRADE_ALERTS_PER_SCAN=2,
        )
        service = TradingAnalysisService(settings)
        fake = FakeNotifier()
        service.notifier = fake
        signals = [
            _trade_ready("KERNELUSDT", score=78, tp3_pct=7.08, tp3_eta=153, volume=89.8, fvg_age=12, sweep=True),
            _trade_ready("DASHUSDT", score=82, tp3_pct=12.17, tp3_eta=247, volume=79.7, fvg_age=0),
        ]
        try:
            service._apply_best_signal_selection(signals)
            assert service.journal is not None
            assert service.journal.count_signals() == 0

            await service._send_alerts(signals)

            assert set(fake.symbols) == {"DASHUSDT", "KERNELUSDT"}
            assert service.journal.count_signals() == 2
        finally:
            await service.client.close()

    asyncio.run(run())


def test_journal_does_not_record_when_telegram_is_not_configured(tmp_path):
    async def run():
        settings = Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="",
            TELEGRAM_CHAT_ID="",
            LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"),
        )
        service = TradingAnalysisService(settings)
        service.notifier = FakeNotifier()
        signal = _trade_ready("DASHUSDT", score=82, tp3_pct=12.17, tp3_eta=247, volume=79.7, fvg_age=0)
        try:
            service._apply_best_signal_selection([signal])
            await service._send_alerts([signal])

            assert service.journal is not None
            assert service.status.telegram_status == "not_configured"
            assert service.journal.count_signals() == 0
        finally:
            await service.client.close()

    asyncio.run(run())


def test_telegram_selector_does_not_send_failed_liquidation_signal():
    async def run():
        settings = Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_CHAT_ID="123",
            LEARNING_ENABLED=False,
        )
        service = TradingAnalysisService(settings)
        fake = FakeNotifier()
        service.notifier = fake
        failed = _trade_ready("FAILEDUSDT", score=95, tp3_pct=12, tp3_eta=120, volume=90, fvg_age=0)
        failed.liquidation_check_status = LiquidationCheckStatus.FAILED
        failed.liquidation_check_reason = "Liquidation check failed: LONG liquidation is between entry and SL."
        safe = _trade_ready("SAFEUSDT", score=82, tp3_pct=8, tp3_eta=120, volume=80, fvg_age=2)
        safe.liquidation_check_status = LiquidationCheckStatus.PASSED
        try:
            service._apply_best_signal_selection([failed, safe])
            await service._send_alerts([failed, safe])

            assert fake.symbols == ["SAFEUSDT"]
            assert failed.alert_eligible is False
        finally:
            await service.client.close()

    asyncio.run(run())


def test_weak_best_candidate_sends_no_telegram_alert():
    async def run():
        settings = Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_CHAT_ID="123",
            LEARNING_ENABLED=False,
            TELEGRAM_MIN_BEST_SCORE=120,
        )
        service = TradingAnalysisService(settings)
        fake = FakeNotifier()
        service.notifier = fake
        weak = _trade_ready("WEAKUSDT", score=74, tp3_pct=4, tp3_eta=280, volume=51, fvg_age=20)
        try:
            service._apply_best_signal_selection([weak])
            await service._send_alerts([weak])

            assert weak.alert_eligible is False
            assert fake.symbols == []
            assert service.status.telegram_status == "no_best_signal"
            assert "below Telegram minimum" in service.status.telegram_best_signal_blocked_reason
        finally:
            await service.client.close()

    asyncio.run(run())


def test_symbol_cooldown_blocks_telegram_selection():
    settings = Settings(_env_file=None, LEARNING_ENABLED=False)
    service = TradingAnalysisService(settings)
    cooled = _trade_ready("COOLDOWNUSDT", score=95, tp3_pct=10, tp3_eta=120, volume=90, fvg_age=1, sweep=True)
    service.learning_report.active_symbol_cooldowns = ["COOLDOWNUSDT (2 losses/12h)"]
    try:
        service._apply_best_signal_selection([cooled])

        assert cooled.alert_eligible is False
        assert "cooldown" in service.status.telegram_best_signal_blocked_reason
    finally:
        asyncio.run(service.client.close())


def test_fresh_loss_symbol_side_cooldown_blocks_telegram_selection():
    settings = Settings(_env_file=None, LEARNING_ENABLED=False)
    service = TradingAnalysisService(settings)
    cooled = _trade_ready("CLOUSDT", score=95, tp3_pct=10, tp3_eta=120, volume=90, fvg_age=1, sweep=True)
    service.learning_report.active_fresh_loss_cooldowns = [
        "CLOUSDT LONG (1 loss/4h, latest 2026-06-23T03:24:32+00:00)"
    ]
    try:
        service._apply_best_signal_selection([cooled])

        assert cooled.alert_eligible is False
        assert "immediate loss cooldown" in service.status.telegram_best_signal_blocked_reason
    finally:
        asyncio.run(service.client.close())


def test_recent_symbol_side_signal_cooldown_selects_another_candidate():
    settings = Settings(_env_file=None, LEARNING_ENABLED=False, MAX_TRADE_ALERTS_PER_SCAN=1)
    service = TradingAnalysisService(settings)
    duplicate = _trade_ready("DUPUSDT", score=98, tp3_pct=12, tp3_eta=120, volume=96, fvg_age=1, sweep=True)
    alternate = _trade_ready("ALTUSDT", score=95, tp3_pct=10, tp3_eta=120, volume=90, fvg_age=1, sweep=True)
    service.learning_report.active_recent_signal_cooldowns = [
        "DUPUSDT LONG (sent within 60m, latest 2026-06-23T03:32:17+00:00)"
    ]
    try:
        service._apply_best_signal_selection([duplicate, alternate])

        assert duplicate.alert_eligible is False
        assert alternate.alert_eligible is True
        assert service.status.best_signal_symbol == "ALTUSDT"
    finally:
        asyncio.run(service.client.close())


def test_low_raw_confluence_score_blocks_telegram_selection():
    settings = Settings(_env_file=None, LEARNING_ENABLED=False, TELEGRAM_MIN_CONFLUENCE_SCORE=75)
    service = TradingAnalysisService(settings)
    low_raw = _trade_ready("LOWRAWUSDT", score=72, tp3_pct=12, tp3_eta=120, volume=96, fvg_age=1, sweep=True)
    try:
        service._apply_best_signal_selection([low_raw])

        assert low_raw.alert_eligible is False
        assert "raw confluence score 72/100" in service.status.telegram_best_signal_blocked_reason
    finally:
        asyncio.run(service.client.close())


def test_execution_quality_blocks_telegram_selection():
    settings = Settings(_env_file=None, LEARNING_ENABLED=False)
    service = TradingAnalysisService(settings)
    late = _trade_ready("LATEUSDT", score=95, tp3_pct=10, tp3_eta=120, volume=95, fvg_age=1, sweep=True)
    late.execution_quality_status = "ENTRY_LATE"
    late.execution_quality_reason = "Latest close is outside entry zone; do not chase."
    try:
        service._apply_best_signal_selection([late])

        assert late.alert_eligible is False
        assert "execution quality" in service.status.telegram_best_signal_blocked_reason
    finally:
        asyncio.run(service.client.close())


def test_future_mode_blocks_fast_young_fvg_and_selects_safer_candidate():
    settings = Settings(_env_file=None, LEARNING_ENABLED=False, FUTURE_MODE="ON", MAX_TRADE_ALERTS_PER_SCAN=1)
    service = TradingAnalysisService(settings)
    chase = _trade_ready("CHASEUSDT", score=76, tp3_pct=6, tp3_eta=24, volume=92, fvg_age=1, sweep=False)
    safer = _trade_ready("SAFERUSDT", score=92, tp3_pct=6, tp3_eta=80, volume=84, fvg_age=5, sweep=True)
    try:
        service._apply_best_signal_selection([chase, safer])

        assert chase.alert_eligible is False
        assert safer.alert_eligible is True
        assert service.status.best_signal_symbol == "SAFERUSDT"
        assert "FUTURE_MODE passed" in safer.alert_reason
    finally:
        asyncio.run(service.client.close())


def test_long_defensive_gate_blocks_unprofitable_fvg_age_after_learning_threshold():
    settings = Settings(_env_file=None, LEARNING_ENABLED=False, TELEGRAM_MIN_CONFLUENCE_SCORE=80)
    service = TradingAnalysisService(settings)
    service.learning_report = LearningReport(
        valid_bot_assisted_sample_count=settings.learning_min_sample_size,
    )
    young_long = _trade_ready("YOUNGLONGUSDT", score=82, tp3_pct=6, tp3_eta=80, volume=84, fvg_age=1)
    healthy_long = _trade_ready("HEALTHYLONGUSDT", score=82, tp3_pct=6, tp3_eta=80, volume=84, fvg_age=5)
    healthy_long.market_regime = "compression_near_value"
    try:
        service._apply_best_signal_selection([young_long, healthy_long])

        assert young_long.alert_eligible is False
        assert healthy_long.alert_eligible is True
        assert service.status.best_signal_symbol == "HEALTHYLONGUSDT"
    finally:
        asyncio.run(service.client.close())


def test_negative_exact_history_blocks_telegram_selection():
    settings = Settings(_env_file=None, LEARNING_ENABLED=False, TELEGRAM_MIN_CONFLUENCE_SCORE=80)
    service = TradingAnalysisService(settings)
    weak_history = _trade_ready("WEAKHISTORYUSDT", score=92, tp3_pct=6, tp3_eta=80, volume=90, fvg_age=5)
    weak_history.historical_bucket_label = "Exact WEAKHISTORYUSDT LONG"
    weak_history.sample_size = 2
    weak_history.historical_win_rate = 0.0
    weak_history.historical_expectancy_r = -1.20
    strong = _trade_ready("STRONGUSDT", score=82, tp3_pct=6, tp3_eta=80, volume=84, fvg_age=5)
    strong.market_regime = "compression_near_value"
    try:
        service._apply_best_signal_selection([weak_history, strong])

        assert weak_history.alert_eligible is False
        assert strong.alert_eligible is True
        assert service.status.best_signal_symbol == "STRONGUSDT"
    finally:
        asyncio.run(service.client.close())


def test_database_recent_signal_cooldown_blocks_resend_after_restart(tmp_path):
    async def run():
        settings = Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_CHAT_ID="123",
            LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"),
            RECENT_SIGNAL_COOLDOWN_MINUTES=60,
        )
        service = TradingAnalysisService(settings)
        fake = FakeNotifier()
        service.notifier = fake
        duplicate = _trade_ready("DUPUSDT", score=95, tp3_pct=10, tp3_eta=120, volume=90, fvg_age=1, sweep=True)
        assert service.journal is not None
        service.journal.record_signal(duplicate, telegram_sent_at=datetime.now(timezone.utc))
        duplicate.alert_eligible = True
        duplicate.alert_reason = "BEST_TRADE_READY #1 selected for Telegram: test."
        try:
            await service._send_alerts([duplicate])

            assert fake.symbols == []
            assert service.status.telegram_status == "best_already_sent:DUPUSDT"
        finally:
            await service.client.close()

    asyncio.run(run())


def test_adaptive_scan_limits_expand_from_initial_to_maximum():
    settings = Settings(
        _env_file=None,
        TOP_MARKETS=150,
        MIN_SCAN_MARKETS=150,
        MAX_SCAN_MARKETS=300,
        SCAN_EXPANSION_STEP=50,
    )

    assert _adaptive_scan_limits(settings, available_count=500) == [150, 200, 250, 300]
    assert _adaptive_scan_limits(settings, available_count=220) == [150, 200, 220]


def test_adaptive_scan_candidate_threshold_accepts_ready_or_manual_candidates():
    settings = Settings(_env_file=None, MIN_READY_CANDIDATES=1, MIN_MANUAL_CANDIDATES=3)
    ready = _trade_ready("READYUSDT", score=82, tp3_pct=8, tp3_eta=120, volume=90, fvg_age=1)
    watch = _trade_ready("WATCHUSDT", score=72, tp3_pct=6, tp3_eta=120, volume=80, fvg_age=1)
    watch.state = SignalState.WATCH

    assert _has_enough_scan_candidates([ready], settings) is True
    assert _has_enough_scan_candidates([watch, watch.model_copy(update={"symbol": "W2USDT"})], settings) is False
    assert _has_enough_scan_candidates(
        [
            watch,
            watch.model_copy(update={"symbol": "W2USDT"}),
            watch.model_copy(update={"symbol": "W3USDT"}),
        ],
        settings,
    ) is False

    manual_only_settings = Settings(_env_file=None, MIN_READY_CANDIDATES=0, MIN_MANUAL_CANDIDATES=3)
    assert _has_enough_scan_candidates(
        [
            watch,
            watch.model_copy(update={"symbol": "W2USDT"}),
            watch.model_copy(update={"symbol": "W3USDT"}),
        ],
        manual_only_settings,
    ) is True


def test_adaptive_scan_does_not_stop_on_low_score_trade_ready_only():
    settings = Settings(
        _env_file=None,
        MIN_READY_CANDIDATES=1,
        MIN_MANUAL_CANDIDATES=3,
        TELEGRAM_MIN_CONFLUENCE_SCORE=75,
    )
    low_score_ready = _trade_ready("LOWREADYUSDT", score=72, tp3_pct=8, tp3_eta=120, volume=90, fvg_age=1)
    strong_ready = _trade_ready("STRONGREADYUSDT", score=76, tp3_pct=8, tp3_eta=120, volume=90, fvg_age=1)

    assert _has_enough_scan_candidates([low_score_ready], settings) is False
    assert _has_enough_scan_candidates([low_score_ready, strong_ready], settings) is True


def test_adaptive_scan_does_not_stop_on_fresh_loss_cooldown_candidate():
    settings = Settings(_env_file=None, MIN_READY_CANDIDATES=1, MIN_MANUAL_CANDIDATES=3)
    report = LearningReport()
    report.active_fresh_loss_cooldowns = [
        "LOSSREADYUSDT LONG (1 loss/4h, latest 2026-06-23T03:24:32+00:00)"
    ]
    cooled = _trade_ready("LOSSREADYUSDT", score=95, tp3_pct=8, tp3_eta=120, volume=90, fvg_age=1)

    assert _has_enough_scan_candidates([cooled], settings, report) is False


def test_tpsl_notifications_are_sent_once_for_matched_outcomes(tmp_path):
    async def run():
        settings = Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_CHAT_ID="-1002265468566",
            TELEGRAM_TPSL_THREAD_ID="53",
            LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"),
        )
        service = TradingAnalysisService(settings)
        fake = FakeNotifier()
        service.notifier = fake
        async def fake_outcome_chart(outcome):
            raise AssertionError("TP/SL notifications should stay text-only")

        service._outcome_chart = fake_outcome_chart
        generated_at = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
        try:
            assert service.journal is not None
            service.journal.record_signal(_trade_ready("DASHUSDT", score=82, tp3_pct=12.17, tp3_eta=247, volume=79.7, fvg_age=0))
            service.journal.sync_closed_outcomes(
                [
                    ClosedPnlRecord(
                        record_id="closed-service-tpsl",
                        symbol="DASHUSDT",
                        side=SignalSide.LONG,
                        qty=10,
                        avg_entry_price=100.5,
                        avg_exit_price=104,
                        closed_pnl=30,
                        created_at=generated_at,
                        updated_at=generated_at + timedelta(hours=1),
                    )
                ],
                [],
            )

            await service._send_tpsl_notifications()
            await service._send_tpsl_notifications()

            assert len(fake.outcomes) == 1
            assert fake.outcome_charts == [None]
            assert service.journal.pending_tpsl_notifications() == []
        finally:
            await service.client.close()

    asyncio.run(run())


def _trade_ready(
    symbol: str,
    *,
    score: int,
    tp3_pct: float,
    tp3_eta: int,
    volume: float,
    fvg_age: int,
    sweep: bool = False,
    displacement_age: int = 2,
    structure: bool = True,
    htf: bool = True,
) -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        side=SignalSide.LONG,
        state=SignalState.TRADE_READY,
        score=score,
        generated_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        entry_low=100,
        entry_high=101,
        stop_loss=98,
        targets=[
            TargetPlan(label="TP1", price=103, distance_pct=2.0, estimated_minutes=40, timing_basis="ATR"),
            TargetPlan(label="TP2", price=106, distance_pct=5.0, estimated_minutes=90, timing_basis="ATR"),
            TargetPlan(label="TP3", price=110, distance_pct=tp3_pct, estimated_minutes=tp3_eta, timing_basis="ATR"),
        ],
        risk_reward=2.8,
        invalidation_condition="Invalid below SL.",
        confidence_explanation="Rule-based confluence score, not a guaranteed win rate.",
        evidence=[
            _evidence("HTF/LTF structure", structure, 14, "Trend regime is bullish."),
            _evidence("Accumulation", True, 12, "Recent range compression near value."),
            _evidence(
                "Manipulation sweep",
                sweep,
                18,
                "Sell-side liquidity swept 7 candle(s) ago; reclaimed 99.",
            ),
            _evidence(
                "Displacement",
                True,
                16,
                f"Bullish displacement {displacement_age} candle(s) ago with body 0.29.",
            ),
            _evidence("IFVG / FVG", True, 16, f"Bullish FVG support age {fvg_age}: 99 - 100."),
            _evidence("Volume participation", volume >= 20, 8, f"Volume percentile: {volume:.1f}%."),
            _evidence("Higher timeframe alignment", htf, 8, "Higher timeframe close is aligned."),
            _evidence("Risk/reward", True, 8, "TP3 risk/reward is 2.80R."),
        ],
        alert_eligible=True,
        alert_reason="TRADE_READY: test.",
    )


def _evidence(name: str, passed: bool, score: int, detail: str) -> StrategyEvidence:
    return StrategyEvidence(
        name=name,
        status="passed" if passed else "not_confirmed",
        detail=detail,
        score=score if passed else 0,
    )
