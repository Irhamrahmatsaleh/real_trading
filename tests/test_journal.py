import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.journal import TradeJournal
from app.models import (
    ClosedPnlRecord,
    ExecutionRecord,
    SignalSide,
    SignalState,
    StrategyEvidence,
    TargetPlan,
    TradeSignal,
    TransactionLogRecord,
)


def test_journal_writes_telegram_eligible_signal(tmp_path):
    settings = Settings(_env_file=None, LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"))
    journal = TradeJournal(settings.learning_db_path, settings)

    journal.record_signal(_signal("BTCUSDT", generated_at=datetime(2026, 6, 20, 10, tzinfo=timezone.utc)))
    report = journal.build_report(now=datetime(2026, 6, 20, 11, tzinfo=timezone.utc))
    with sqlite3.connect(settings.learning_db_path) as conn:
        row = conn.execute("select telegram_status, telegram_sent_at from signals").fetchone()

    assert report.journal_signal_count == 1
    assert report.matched_trade_count == 0
    assert row[0] == "sent"
    assert row[1] is not None
    assert "Probability" not in report.sample_warning
    assert "probability remains not calibrated" in report.sample_warning


def test_outcome_matching_uses_bybit_closed_pnl_as_net_and_stores_fees(tmp_path):
    settings = Settings(_env_file=None, LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"))
    journal = TradeJournal(settings.learning_db_path, settings)
    generated_at = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
    closed_at = generated_at + timedelta(hours=1)
    journal.record_signal(_signal("BTCUSDT", generated_at=generated_at))

    synced = journal.sync_closed_outcomes(
        [
            ClosedPnlRecord(
                record_id="closed-1",
                symbol="BTCUSDT",
                side=SignalSide.LONG,
                qty=10,
                avg_entry_price=100.5,
                avg_exit_price=104,
                closed_pnl=30,
                open_fee=1,
                close_fee=1,
                created_at=generated_at,
                updated_at=closed_at,
            )
        ],
        [
            TransactionLogRecord(
                record_id="funding-1",
                symbol="BTCUSDT",
                transaction_type="Funding Rate Settlement",
                funding=-0.5,
                created_at=generated_at + timedelta(minutes=30),
            )
        ],
    )
    report = journal.build_report(now=closed_at + timedelta(minutes=1))

    assert synced == 1
    assert report.matched_trade_count == 1
    assert report.today_net_pnl == 30
    assert report.today_net_r == 1.2
    assert report.today_win_rate == 100.0
    assert report.profit_factor is None
    assert "1/50" in report.sample_warning
    with sqlite3.connect(settings.learning_db_path) as conn:
        row = conn.execute("select realized_pnl, fee, funding, net_pnl from outcomes").fetchone()
    assert row == (30, 2, -0.5, 30)


def test_uncertain_outcome_does_not_fake_match(tmp_path):
    settings = Settings(_env_file=None, LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"))
    journal = TradeJournal(settings.learning_db_path, settings)
    closed_at = datetime(2026, 6, 20, 11, tzinfo=timezone.utc)

    journal.sync_closed_outcomes(
        [
            ClosedPnlRecord(
                record_id="closed-uncertain",
                symbol="ETHUSDT",
                side=SignalSide.SHORT,
                qty=1,
                avg_entry_price=200,
                avg_exit_price=190,
                closed_pnl=10,
                created_at=closed_at - timedelta(hours=1),
                updated_at=closed_at,
            )
        ],
        [],
    )

    report = journal.build_report(now=closed_at + timedelta(minutes=1))
    with sqlite3.connect(settings.learning_db_path) as conn:
        confidence = conn.execute("select match_confidence from outcomes").fetchone()[0]

    assert report.matched_trade_count == 0
    assert confidence == "uncertain"


def test_outcome_matching_requires_telegram_sent_signal(tmp_path):
    settings = Settings(_env_file=None, LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"))
    journal = TradeJournal(settings.learning_db_path, settings)
    generated_at = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
    closed_at = generated_at + timedelta(hours=1)
    journal.record_signal(
        _signal("BTCUSDT", generated_at=generated_at),
        telegram_status="legacy_alert_eligible",
        telegram_sent_at=None,
    )

    journal.sync_closed_outcomes(
        [
            ClosedPnlRecord(
                record_id="closed-legacy",
                symbol="BTCUSDT",
                side=SignalSide.LONG,
                qty=10,
                avg_entry_price=100.5,
                avg_exit_price=104,
                closed_pnl=30,
                created_at=generated_at,
                updated_at=closed_at,
            )
        ],
        [],
    )

    report = journal.build_report(now=closed_at + timedelta(minutes=1))
    with sqlite3.connect(settings.learning_db_path) as conn:
        row = conn.execute("select match_confidence, signal_id from outcomes").fetchone()
        signal_row = conn.execute("select telegram_status, telegram_sent_at from signals").fetchone()

    assert report.matched_trade_count == 0
    assert row == ("uncertain", None)
    assert signal_row == ("legacy_alert_eligible", None)


def test_manual_adjusted_outcome_matches_telegram_symbol_side_and_time(tmp_path):
    settings = Settings(_env_file=None, LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"))
    journal = TradeJournal(settings.learning_db_path, settings)
    generated_at = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
    closed_at = generated_at + timedelta(hours=1)
    signal_id = journal.record_signal(_signal("BTCUSDT", generated_at=generated_at))

    journal.sync_closed_outcomes(
        [
            ClosedPnlRecord(
                record_id="closed-manual-adjusted",
                symbol="BTCUSDT",
                side=SignalSide.LONG,
                qty=10,
                avg_entry_price=110,
                avg_exit_price=112,
                closed_pnl=20,
                created_at=generated_at,
                updated_at=closed_at,
            )
        ],
        [],
    )

    report = journal.build_report(now=closed_at + timedelta(minutes=1))
    with sqlite3.connect(settings.learning_db_path) as conn:
        row = conn.execute(
            "select match_confidence, signal_id, match_reason from outcomes"
        ).fetchone()

    assert report.matched_trade_count == 1
    assert row[0] == "manual_adjusted"
    assert row[1] == signal_id
    assert "manual entry/TP/SL" in row[2]


def test_outcome_matching_prefers_entry_execution_time_over_later_repeated_signal(tmp_path):
    settings = Settings(_env_file=None, LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"))
    journal = TradeJournal(settings.learning_db_path, settings)
    first_signal_time = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
    entry_time = first_signal_time + timedelta(minutes=2)
    later_signal_time = first_signal_time + timedelta(minutes=10)
    closed_at = first_signal_time + timedelta(minutes=20)
    first_signal_id = journal.record_signal(_signal("BTCUSDT", generated_at=first_signal_time))
    later_signal_id = journal.record_signal(_signal("BTCUSDT", generated_at=later_signal_time))

    journal.sync_closed_outcomes(
        [
            ClosedPnlRecord(
                record_id="closed-after-repeated-signal",
                symbol="BTCUSDT",
                side=SignalSide.LONG,
                qty=10,
                avg_entry_price=100.5,
                avg_exit_price=104,
                closed_pnl=30,
                created_at=closed_at,
                updated_at=closed_at,
            )
        ],
        [],
        [
            ExecutionRecord(
                record_id="entry-exec",
                symbol="BTCUSDT",
                side=SignalSide.LONG,
                exec_price=100.5,
                exec_qty=10,
                created_at=entry_time,
            )
        ],
    )

    with sqlite3.connect(settings.learning_db_path) as conn:
        row = conn.execute(
            "select signal_id, match_confidence, match_reason from outcomes"
        ).fetchone()

    assert row[0] == first_signal_id
    assert row[0] != later_signal_id
    assert row[1] == "matched"
    assert "entry execution time" in row[2]


def test_outcome_summary_counts_valid_learning_samples_and_uncertain_separately(tmp_path):
    settings = Settings(_env_file=None, LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"))
    journal = TradeJournal(settings.learning_db_path, settings)
    generated_at = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
    closed_at = generated_at + timedelta(hours=1)
    journal.record_signal(_signal("BTCUSDT", generated_at=generated_at))
    journal.sync_closed_outcomes(
        [
            ClosedPnlRecord(
                record_id="matched",
                symbol="BTCUSDT",
                side=SignalSide.LONG,
                qty=10,
                avg_entry_price=100.5,
                avg_exit_price=104,
                closed_pnl=30,
                created_at=generated_at,
                updated_at=closed_at,
            ),
            ClosedPnlRecord(
                record_id="manual-adjusted",
                symbol="BTCUSDT",
                side=SignalSide.LONG,
                qty=10,
                avg_entry_price=110,
                avg_exit_price=112,
                closed_pnl=20,
                created_at=generated_at,
                updated_at=closed_at + timedelta(minutes=1),
            ),
            ClosedPnlRecord(
                record_id="uncertain",
                symbol="ETHUSDT",
                side=SignalSide.SHORT,
                qty=1,
                avg_entry_price=200,
                avg_exit_price=190,
                closed_pnl=10,
                created_at=generated_at,
                updated_at=closed_at,
            ),
        ],
        [],
    )

    summary = journal.build_outcome_summary()

    assert summary.total_outcomes == 3
    assert summary.valid_learning_samples == 2
    assert summary.matched_count == 1
    assert summary.manual_adjusted_count == 1
    assert summary.uncertain_count == 1
    assert [bucket.match_confidence for bucket in summary.buckets] == ["matched", "manual_adjusted", "uncertain"]


def test_tpsl_notifications_only_include_valid_pending_outcomes_and_mark_sent(tmp_path):
    settings = Settings(_env_file=None, LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"))
    journal = TradeJournal(settings.learning_db_path, settings)
    generated_at = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
    closed_at = generated_at + timedelta(hours=1)
    journal.record_signal(_signal("BTCUSDT", generated_at=generated_at))
    journal.sync_closed_outcomes(
        [
            ClosedPnlRecord(
                record_id="matched-tpsl",
                symbol="BTCUSDT",
                side=SignalSide.LONG,
                qty=10,
                avg_entry_price=100.5,
                avg_exit_price=104,
                closed_pnl=30,
                created_at=generated_at,
                updated_at=closed_at,
            ),
            ClosedPnlRecord(
                record_id="uncertain-tpsl",
                symbol="ETHUSDT",
                side=SignalSide.SHORT,
                qty=1,
                avg_entry_price=200,
                avg_exit_price=190,
                closed_pnl=10,
                created_at=generated_at,
                updated_at=closed_at,
            ),
        ],
        [],
    )

    pending = journal.pending_tpsl_notifications()

    assert len(pending) == 1
    assert pending[0].symbol == "BTCUSDT"
    assert pending[0].match_confidence == "matched"

    journal.mark_tpsl_notification_sent(pending[0].outcome_id, sent_at=closed_at + timedelta(minutes=1))

    assert journal.pending_tpsl_notifications() == []
    with sqlite3.connect(settings.learning_db_path) as conn:
        rows = conn.execute(
            "select symbol, tpsl_telegram_status, tpsl_telegram_sent_at from outcomes order by symbol"
        ).fetchall()
    assert rows[0][0] == "BTCUSDT"
    assert rows[0][1] == "sent"
    assert rows[0][2] is not None
    assert rows[1][0] == "ETHUSDT"
    assert rows[1][1] == "not_applicable"


def test_existing_legacy_outcomes_are_not_backfilled_as_pending_tpsl_notifications(tmp_path):
    settings = Settings(_env_file=None, LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"))
    journal = TradeJournal(settings.learning_db_path, settings)
    generated_at = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
    closed_at = generated_at + timedelta(hours=1)
    journal.record_signal(_signal("BTCUSDT", generated_at=generated_at))
    journal.sync_closed_outcomes(
        [
            ClosedPnlRecord(
                record_id="legacy-tpsl",
                symbol="BTCUSDT",
                side=SignalSide.LONG,
                qty=10,
                avg_entry_price=100.5,
                avg_exit_price=104,
                closed_pnl=30,
                created_at=generated_at,
                updated_at=closed_at,
            )
        ],
        [],
    )
    with sqlite3.connect(settings.learning_db_path) as conn:
        conn.execute("update outcomes set tpsl_telegram_status = null")

    journal = TradeJournal(settings.learning_db_path, settings)

    assert journal.pending_tpsl_notifications() == []
    with sqlite3.connect(settings.learning_db_path) as conn:
        status = conn.execute("select tpsl_telegram_status from outcomes").fetchone()[0]
    assert status == "legacy_not_sent"


def test_repeated_losing_symbol_triggers_cooldown(tmp_path):
    settings = Settings(
        _env_file=None,
        LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"),
        SYMBOL_COOLDOWN_LOSS_COUNT=2,
        SYMBOL_COOLDOWN_HOURS=12,
    )
    journal = TradeJournal(settings.learning_db_path, settings)
    base = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
    for index in range(2):
        generated_at = base + timedelta(hours=index)
        closed_at = generated_at + timedelta(minutes=30)
        journal.record_signal(_signal("LOSSUSDT", generated_at=generated_at))
        journal.sync_closed_outcomes(
            [
                ClosedPnlRecord(
                    record_id=f"loss-{index}",
                    symbol="LOSSUSDT",
                    side=SignalSide.LONG,
                    qty=10,
                    avg_entry_price=100.5,
                    avg_exit_price=97,
                    closed_pnl=-20,
                    open_fee=0.5,
                    close_fee=0.5,
                    created_at=generated_at,
                    updated_at=closed_at,
                )
            ],
            [],
        )

    report = journal.build_report(now=base + timedelta(hours=3))

    assert report.matched_trade_count == 2
    assert any(item.startswith("LOSSUSDT") for item in report.active_symbol_cooldowns)


def test_single_fresh_loss_triggers_symbol_side_defensive_cooldown(tmp_path):
    settings = Settings(
        _env_file=None,
        LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"),
        SYMBOL_COOLDOWN_LOSS_COUNT=3,
        SYMBOL_COOLDOWN_HOURS=8,
        IMMEDIATE_LOSS_COOLDOWN_HOURS=4,
    )
    journal = TradeJournal(settings.learning_db_path, settings)
    generated_at = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)
    closed_at = generated_at + timedelta(minutes=30)
    journal.record_signal(_signal("FASTLOSSUSDT", generated_at=generated_at))

    journal.sync_closed_outcomes(
        [
            ClosedPnlRecord(
                record_id="fresh-loss",
                symbol="FASTLOSSUSDT",
                side=SignalSide.LONG,
                qty=10,
                avg_entry_price=100.5,
                avg_exit_price=97,
                closed_pnl=-20,
                open_fee=0.5,
                close_fee=0.5,
                created_at=generated_at,
                updated_at=closed_at,
            )
        ],
        [],
    )

    report = journal.build_report(now=closed_at + timedelta(minutes=1))

    assert report.active_symbol_cooldowns == []
    assert any(item.startswith("FASTLOSSUSDT LONG") for item in report.active_fresh_loss_cooldowns)


def test_recent_sent_signal_triggers_persistent_symbol_side_cooldown(tmp_path):
    settings = Settings(
        _env_file=None,
        LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"),
        RECENT_SIGNAL_COOLDOWN_MINUTES=60,
    )
    journal = TradeJournal(settings.learning_db_path, settings)
    sent_at = datetime(2026, 6, 20, 10, tzinfo=timezone.utc)

    journal.record_signal(_signal("RECENTUSDT", generated_at=sent_at), telegram_sent_at=sent_at)
    report = journal.build_report(now=sent_at + timedelta(minutes=30))

    assert any(item.startswith("RECENTUSDT LONG") for item in report.active_recent_signal_cooldowns)
    assert journal.recent_sent_signal_reason("RECENTUSDT", SignalSide.LONG, now=sent_at + timedelta(minutes=30))
    assert journal.recent_sent_signal_reason("RECENTUSDT", SignalSide.LONG, now=sent_at + timedelta(minutes=61)) is None


def test_signal_statistics_show_exact_history_without_lookahead(tmp_path):
    settings = Settings(_env_file=None, LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"))
    journal = TradeJournal(settings.learning_db_path, settings)
    current_time = datetime(2026, 6, 20, 12, tzinfo=timezone.utc)

    _record_bot_outcome(journal, "BICOUSDT", current_time - timedelta(hours=3), current_time - timedelta(hours=2), 12.0, "bico-win")
    _record_bot_outcome(journal, "BICOUSDT", current_time - timedelta(minutes=90), current_time - timedelta(minutes=30), -6.0, "bico-loss")
    _record_bot_outcome(journal, "BICOUSDT", current_time + timedelta(minutes=10), current_time + timedelta(hours=1), 8.0, "bico-future")

    signal = _signal("BICOUSDT", generated_at=current_time)
    journal.apply_signal_statistics(signal)

    assert signal.probability_label == "Probability: Historical Context Only"
    assert signal.sample_size == 2
    assert "Exact BICOUSDT LONG: 2 samples, 1W/1L" in signal.statistics_summary
    assert "calibration requires 50+ relevant samples" in signal.statistics_summary


def test_signal_statistics_calibrate_when_relevant_sample_is_large_enough(tmp_path):
    settings = Settings(
        _env_file=None,
        LEARNING_DB_PATH=str(tmp_path / "journal.sqlite3"),
        LEARNING_MIN_SAMPLE_SIZE=2,
    )
    journal = TradeJournal(settings.learning_db_path, settings)
    current_time = datetime(2026, 6, 20, 12, tzinfo=timezone.utc)

    _record_bot_outcome(journal, "CALUSDT", current_time - timedelta(hours=4), current_time - timedelta(hours=3), 12.0, "cal-win")
    _record_bot_outcome(journal, "CALUSDT", current_time - timedelta(hours=2), current_time - timedelta(hours=1), -6.0, "cal-loss")

    signal = _signal("CALUSDT", generated_at=current_time)
    journal.apply_signal_statistics(signal)

    assert signal.probability_label == "Probability: Calibrated Exact CALUSDT LONG"
    assert signal.sample_size == 2
    assert "Exact CALUSDT LONG: 2 samples, 1W/1L" in signal.statistics_summary
    assert "calibrated with 2+ relevant samples" in signal.statistics_summary


def _record_bot_outcome(
    journal: TradeJournal,
    symbol: str,
    generated_at: datetime,
    closed_at: datetime,
    pnl: float,
    record_id: str,
) -> None:
    journal.record_signal(_signal(symbol, generated_at=generated_at))
    journal.sync_closed_outcomes(
        [
            ClosedPnlRecord(
                record_id=record_id,
                symbol=symbol,
                side=SignalSide.LONG,
                qty=10,
                avg_entry_price=100.5,
                avg_exit_price=104.0 if pnl > 0 else 97.0,
                closed_pnl=pnl,
                open_fee=0,
                close_fee=0,
                created_at=generated_at,
                updated_at=closed_at,
            )
        ],
        [],
        [],
    )


def _signal(symbol: str, generated_at: datetime) -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        side=SignalSide.LONG,
        state=SignalState.TRADE_READY,
        score=88,
        generated_at=generated_at,
        entry_low=100,
        entry_high=101,
        stop_loss=98,
        targets=[
            TargetPlan(label="TP1", price=103, distance_pct=2, estimated_minutes=50, timing_basis="ATR"),
            TargetPlan(label="TP2", price=106, distance_pct=5, estimated_minutes=100, timing_basis="ATR"),
            TargetPlan(label="TP3", price=110, distance_pct=9, estimated_minutes=180, timing_basis="ATR"),
        ],
        risk_reward=2.8,
        invalidation_condition="Invalid below SL.",
        confidence_explanation="Rule-based confluence score, not a guaranteed win rate.",
        evidence=[
            StrategyEvidence(name="Manipulation sweep", status="passed", detail="Sell-side liquidity swept 1 candle(s) ago.", score=18),
            StrategyEvidence(name="Displacement", status="passed", detail="Bullish displacement 1 candle(s) ago.", score=16),
            StrategyEvidence(name="IFVG / FVG", status="passed", detail="Bullish FVG support age 1: 99 - 100.", score=16),
            StrategyEvidence(name="Volume participation", status="passed", detail="Volume percentile: 90.0%.", score=8),
            StrategyEvidence(name="HTF/LTF structure", status="passed", detail="Trend regime is bullish.", score=14),
            StrategyEvidence(name="Higher timeframe alignment", status="passed", detail="Higher timeframe close is aligned.", score=8),
        ],
        alert_eligible=True,
        alert_reason="BEST_TRADE_READY selected for Telegram: test.",
    )
