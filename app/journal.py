from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.models import (
    ClosedPnlRecord,
    ExecutionRecord,
    JournalOutcomeSummary,
    LearningReport,
    OutcomeBucket,
    OutcomeNotification,
    RecentOutcome,
    SignalSide,
    TradeSignal,
    TransactionLogRecord,
)


@dataclass(frozen=True)
class SignalMatch:
    row: sqlite3.Row
    confidence: str
    reason: str


@dataclass(frozen=True)
class MatchAnchor:
    matched_at: datetime
    reason: str


@dataclass(frozen=True)
class HistoricalStats:
    label: str
    rows: list[sqlite3.Row]

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def wins(self) -> int:
        return sum(1 for row in self.rows if float(row["net_pnl"] or 0.0) > 0)

    @property
    def losses(self) -> int:
        return sum(1 for row in self.rows if float(row["net_pnl"] or 0.0) < 0)

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.count * 100.0 if self.count else None

    @property
    def result_values(self) -> list[float]:
        return [float(row["result_r"]) for row in self.rows if row["result_r"] is not None]

    @property
    def expectancy_r(self) -> float | None:
        values = self.result_values
        return sum(values) / len(values) if values else None

    @property
    def net_r(self) -> float | None:
        values = self.result_values
        return sum(values) if values else None

    @property
    def profit_factor(self) -> float | None:
        return _profit_factor(self.rows)


@dataclass(frozen=True)
class SignalStatistics:
    probability_label: str
    sample_size: int
    statistics_summary: str
    bucket_label: str | None = None
    win_rate: float | None = None
    expectancy_r: float | None = None
    net_r: float | None = None
    profit_factor: float | None = None
    is_calibrated: bool = False


class TradeJournal:
    def __init__(self, db_path: str, settings: Settings):
        self.path = Path(db_path)
        self.settings = settings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def record_signal(
        self,
        signal: TradeSignal,
        *,
        telegram_status: str = "sent",
        telegram_sent_at: datetime | None = None,
    ) -> str:
        signal_id = _signal_id(signal)
        features = _feature_snapshot(signal)
        sent_at = telegram_sent_at
        if sent_at is None and telegram_status == "sent":
            sent_at = datetime.now(timezone.utc)
        sent_at_value = sent_at.isoformat() if sent_at is not None else None
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into signals (
                    signal_id, generated_at, symbol, side, state, score,
                    entry_low, entry_high, stop_loss, tp1, tp2, tp3,
                    tp1_eta, tp2_eta, tp3_eta, risk_reward, features_json,
                    liquidation_check_status, liquidation_price,
                    account_risk_validated, selection_reason, telegram_status,
                    telegram_sent_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    signal.generated_at.isoformat(),
                    signal.symbol,
                    signal.side.value,
                    signal.state.value,
                    signal.score,
                    signal.entry_low,
                    signal.entry_high,
                    signal.stop_loss,
                    signal.targets[0].price if len(signal.targets) > 0 else None,
                    signal.targets[1].price if len(signal.targets) > 1 else None,
                    signal.targets[2].price if len(signal.targets) > 2 else None,
                    signal.targets[0].estimated_minutes if len(signal.targets) > 0 else None,
                    signal.targets[1].estimated_minutes if len(signal.targets) > 1 else None,
                    signal.targets[2].estimated_minutes if len(signal.targets) > 2 else None,
                    signal.risk_reward,
                    json.dumps(features, sort_keys=True),
                    signal.liquidation_check_status.value,
                    signal.liquidation_price,
                    int(signal.account_risk_validated),
                    signal.alert_reason,
                    telegram_status,
                    sent_at_value,
                ),
            )
            conn.execute(
                """
                update signals
                set telegram_status = ?, telegram_sent_at = ?, selection_reason = ?
                where signal_id = ?
                """,
                (telegram_status, sent_at_value, signal.alert_reason, signal_id),
            )
        return signal_id

    def apply_signal_statistics(self, signal: TradeSignal) -> None:
        statistics = self.signal_statistics(signal)
        signal.probability_label = statistics.probability_label
        signal.sample_size = statistics.sample_size
        signal.statistics_summary = statistics.statistics_summary
        signal.historical_bucket_label = statistics.bucket_label
        signal.historical_win_rate = statistics.win_rate
        signal.historical_expectancy_r = statistics.expectancy_r
        signal.historical_net_r = statistics.net_r
        signal.historical_profit_factor = statistics.profit_factor
        signal.historical_is_calibrated = statistics.is_calibrated

    def signal_statistics(self, signal: TradeSignal) -> SignalStatistics:
        if signal.side not in {SignalSide.LONG, SignalSide.SHORT}:
            return SignalStatistics(
                probability_label="Probability: Not Calibrated",
                sample_size=0,
                statistics_summary="No directional historical outcome bucket is available for this signal.",
            )

        exact = self._historical_stats(
            signal,
            f"Exact {signal.symbol} {signal.side.value}",
            "and s.symbol = ? and s.side = ?",
            [signal.symbol, signal.side.value],
        )
        score_bucket = self._historical_stats(
            signal,
            f"{signal.side.value} score {signal.score}",
            "and s.side = ? and s.score = ?",
            [signal.side.value, signal.score],
        )
        band_label, band_clause, band_params = _score_band_filter(signal.side, signal.score)
        score_band = self._historical_stats(signal, band_label, band_clause, band_params)
        side_bucket = self._historical_stats(
            signal,
            f"Broad {signal.side.value}",
            "and s.side = ?",
            [signal.side.value],
        )

        minimum = self.settings.learning_min_sample_size
        calibrated = next(
            (bucket for bucket in (exact, score_bucket, score_band) if bucket.count >= minimum),
            None,
        )
        if calibrated is not None:
            return SignalStatistics(
                probability_label=f"Probability: Calibrated {calibrated.label}",
                sample_size=calibrated.count,
                statistics_summary=(
                    f"{_stats_line(calibrated)}; calibrated with {minimum}+ relevant samples; "
                    "historical context only, not a profit guarantee."
                ),
                bucket_label=calibrated.label,
                win_rate=calibrated.win_rate,
                expectancy_r=calibrated.expectancy_r,
                net_r=calibrated.net_r,
                profit_factor=calibrated.profit_factor,
                is_calibrated=True,
            )

        primary = next(
            (bucket for bucket in (exact, score_bucket, score_band, side_bucket) if bucket.count > 0),
            None,
        )
        if primary is None:
            return SignalStatistics(
                probability_label="Probability: Not Calibrated",
                sample_size=0,
                statistics_summary=(
                    "No verified historical outcome bucket is available yet; "
                    "score is confluence-based, not a win-rate claim."
                ),
            )

        fallback = next(
            (
                bucket
                for bucket in (score_bucket, score_band, side_bucket)
                if bucket.count > primary.count
            ),
            None,
        )
        parts = [_stats_line(primary)]
        if fallback is not None and fallback.label != primary.label:
            parts.append(f"Fallback {_stats_line(fallback)}")
        parts.append(f"calibration requires {minimum}+ relevant samples")
        parts.append("historical context only, not a profit guarantee")
        return SignalStatistics(
            probability_label="Probability: Historical Context Only",
            sample_size=primary.count,
            statistics_summary="; ".join(parts) + ".",
            bucket_label=primary.label,
            win_rate=primary.win_rate,
            expectancy_r=primary.expectancy_r,
            net_r=primary.net_r,
            profit_factor=primary.profit_factor,
        )

    def _historical_stats(
        self,
        signal: TradeSignal,
        label: str,
        extra_where: str,
        params: list[object],
    ) -> HistoricalStats:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select o.net_pnl, o.result_r
                from outcomes o
                join signals s on s.signal_id = o.signal_id
                where o.match_confidence in ('matched', 'manual_adjusted')
                  and o.closed_at < ?
                  and o.result_r is not null
                  {extra_where}
                order by o.closed_at desc
                """,
                [signal.generated_at.isoformat(), *params],
            ).fetchall()
        return HistoricalStats(label=label, rows=rows)

    def sync_closed_outcomes(
        self,
        closed_records: list[ClosedPnlRecord],
        transactions: list[TransactionLogRecord],
        executions: list[ExecutionRecord] | None = None,
    ) -> int:
        executions = executions or []
        synced = 0
        for record in closed_records:
            if self._outcome_exists(record):
                continue
            matched = self._match_signal(record, executions)
            matched_row = matched.row if matched is not None else None
            funding = _funding_for_record(record, matched_row, transactions)
            fee = record.open_fee + record.close_fee
            net_pnl = record.closed_pnl
            outcome = _classify_outcome(record, matched_row, net_pnl)
            result_r = _result_r(record, matched_row, net_pnl)
            holding_minutes = None
            if matched is not None:
                holding_minutes = max(0, int((record.updated_at - _parse_dt(matched.row["generated_at"])).total_seconds() // 60))
            confidence = matched.confidence if matched is not None else "uncertain"
            reason = matched.reason if matched is not None else "No prior Telegram-sent signal matched symbol, side, and time window; outcome kept uncertain."
            tpsl_status = "pending" if confidence in {"matched", "manual_adjusted"} else "not_applicable"
            with self._connect() as conn:
                conn.execute(
                    """
                    insert into outcomes (
                        outcome_id, signal_id, symbol, side, closed_at, actual_entry,
                        actual_exit, qty, realized_pnl, fee, funding, net_pnl,
                        result_r, outcome_type, holding_minutes, match_confidence,
                        match_reason, tpsl_telegram_status
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _outcome_id(record),
                        matched.row["signal_id"] if matched is not None else None,
                        record.symbol,
                        record.side.value if record.side else None,
                        record.updated_at.isoformat(),
                        record.avg_entry_price,
                        record.avg_exit_price,
                        record.qty,
                        record.closed_pnl,
                        fee,
                        funding,
                        net_pnl,
                        result_r,
                        outcome,
                        holding_minutes,
                        confidence,
                        reason,
                        tpsl_status,
                    ),
                )
            synced += 1
        return synced

    def build_report(self, now: datetime | None = None) -> LearningReport:
        now = now or datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._connect() as conn:
            signal_count = conn.execute("select count(*) from signals").fetchone()[0]
            rows = conn.execute(
                """
                select o.*, s.features_json
                from outcomes o
                left join signals s on s.signal_id = o.signal_id
                where o.match_confidence in ('matched', 'manual_adjusted') and o.closed_at >= ?
                """,
                (today_start.isoformat(),),
            ).fetchall()
            all_rows = conn.execute(
                """
                select o.*, s.features_json
                from outcomes o
                left join signals s on s.signal_id = o.signal_id
                where o.match_confidence in ('matched', 'manual_adjusted')
                """
            ).fetchall()

        matched_count = len(rows)
        valid_sample_count = len(all_rows)
        net_pnl = sum(float(row["net_pnl"] or 0.0) for row in rows)
        r_values = [float(row["result_r"]) for row in rows if row["result_r"] is not None]
        net_r = sum(r_values)
        wins = [float(row["result_r"] or 0.0) for row in rows if float(row["net_pnl"] or 0.0) > 0]
        losses = [float(row["result_r"] or 0.0) for row in rows if float(row["net_pnl"] or 0.0) < 0]
        today_win_rate = (len(wins) / matched_count * 100.0) if matched_count else None
        all_time_r_values = [float(row["result_r"]) for row in all_rows if row["result_r"] is not None]
        all_time_wins = [row for row in all_rows if float(row["net_pnl"] or 0.0) > 0]
        all_time_losses = [row for row in all_rows if float(row["net_pnl"] or 0.0) < 0]
        all_time_win_rate = (len(all_time_wins) / valid_sample_count * 100.0) if valid_sample_count else None
        all_time_expectancy_r = (
            sum(all_time_r_values) / len(all_time_r_values)
            if all_time_r_values
            else None
        )
        all_time_profit_factor = _profit_factor(all_rows)
        profit_factor = None
        if matched_count >= self.settings.learning_min_sample_size:
            gross_profit = sum(float(row["net_pnl"] or 0.0) for row in rows if float(row["net_pnl"] or 0.0) > 0)
            gross_loss = abs(sum(float(row["net_pnl"] or 0.0) for row in rows if float(row["net_pnl"] or 0.0) < 0))
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

        sample_warning = _sample_warning(valid_sample_count, self.settings.learning_min_sample_size)
        cooldowns = self._active_cooldowns(now)
        fresh_loss_cooldowns = self._active_fresh_loss_cooldowns(now)
        recent_signal_cooldowns = self._active_recent_signal_cooldowns(now)
        penalties, penalty_summary = self._condition_penalties(all_rows)
        return LearningReport(
            learning_enabled=self.settings.learning_enabled,
            bybit_env=self.settings.bybit_env,
            journal_signal_count=signal_count,
            matched_trade_count=matched_count,
            valid_bot_assisted_sample_count=valid_sample_count,
            all_time_net_r=round(sum(all_time_r_values), 4),
            all_time_win_rate=round(all_time_win_rate, 2) if all_time_win_rate is not None else None,
            all_time_expectancy_r=round(all_time_expectancy_r, 4) if all_time_expectancy_r is not None else None,
            all_time_profit_factor=round(all_time_profit_factor, 4) if all_time_profit_factor is not None else None,
            today_net_pnl=round(net_pnl, 4),
            today_net_r=round(net_r, 4),
            today_win_rate=round(today_win_rate, 2) if today_win_rate is not None else None,
            average_win_r=round(sum(wins) / len(wins), 4) if wins else None,
            average_loss_r=round(sum(losses) / len(losses), 4) if losses else None,
            profit_factor=round(profit_factor, 4) if profit_factor is not None else None,
            sample_warning=sample_warning,
            active_symbol_cooldowns=cooldowns,
            active_fresh_loss_cooldowns=fresh_loss_cooldowns,
            active_recent_signal_cooldowns=recent_signal_cooldowns,
            adaptive_penalty_summary=penalty_summary,
            condition_penalties=penalties,
            ml_status=_ml_status(self.settings, valid_sample_count),
            best_symbols=_symbol_rank(rows, reverse=True),
            worst_symbols=_symbol_rank(rows, reverse=False),
            best_evidence_combinations=_combo_rank(rows, reverse=True),
            worst_evidence_combinations=_combo_rank(rows, reverse=False),
            loss_patterns=_loss_patterns(rows),
        )

    def count_signals(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("select count(*) from signals").fetchone()[0])

    def build_outcome_summary(self, limit: int = 20) -> JournalOutcomeSummary:
        with self._connect() as conn:
            bucket_rows = conn.execute(
                """
                select match_confidence,
                    count(*) as count,
                    coalesce(sum(realized_pnl), 0) as realized_pnl,
                    coalesce(sum(fee), 0) as fee,
                    coalesce(sum(funding), 0) as funding,
                    coalesce(sum(net_pnl), 0) as net_pnl
                from outcomes
                group by match_confidence
                order by
                    case match_confidence
                        when 'matched' then 1
                        when 'manual_adjusted' then 2
                        when 'uncertain' then 3
                        else 4
                    end,
                    match_confidence
                """
            ).fetchall()
            recent_rows = conn.execute(
                """
                select closed_at, symbol, side, actual_entry, actual_exit, qty,
                    net_pnl, result_r, outcome_type, match_confidence
                from outcomes
                order by closed_at desc
                limit ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()

        buckets = [
            OutcomeBucket(
                match_confidence=str(row["match_confidence"]),
                count=int(row["count"] or 0),
                realized_pnl=round(float(row["realized_pnl"] or 0.0), 6),
                fee=round(float(row["fee"] or 0.0), 6),
                funding=round(float(row["funding"] or 0.0), 6),
                net_pnl=round(float(row["net_pnl"] or 0.0), 6),
            )
            for row in bucket_rows
        ]
        counts = {bucket.match_confidence: bucket.count for bucket in buckets}
        matched_count = counts.get("matched", 0)
        manual_adjusted_count = counts.get("manual_adjusted", 0)
        uncertain_count = counts.get("uncertain", 0)
        return JournalOutcomeSummary(
            total_outcomes=sum(bucket.count for bucket in buckets),
            valid_learning_samples=matched_count + manual_adjusted_count,
            matched_count=matched_count,
            manual_adjusted_count=manual_adjusted_count,
            uncertain_count=uncertain_count,
            buckets=buckets,
            recent=[
                RecentOutcome(
                    closed_at=str(row["closed_at"]),
                    symbol=str(row["symbol"]),
                    side=str(row["side"]) if row["side"] is not None else None,
                    actual_entry=row["actual_entry"],
                    actual_exit=row["actual_exit"],
                    qty=row["qty"],
                    net_pnl=round(float(row["net_pnl"] or 0.0), 6),
                    result_r=round(float(row["result_r"]), 6) if row["result_r"] is not None else None,
                    outcome_type=str(row["outcome_type"]),
                    match_confidence=str(row["match_confidence"]),
                )
                for row in recent_rows
            ],
        )

    def pending_tpsl_notifications(self, limit: int = 20) -> list[OutcomeNotification]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select o.outcome_id, o.symbol, o.side, o.closed_at, o.actual_entry,
                    o.actual_exit, o.qty, o.net_pnl, o.result_r, o.outcome_type,
                    o.match_confidence, s.generated_at as signal_generated_at,
                    s.entry_low, s.entry_high, s.stop_loss, s.tp1, s.tp2, s.tp3
                from outcomes o
                left join signals s on s.signal_id = o.signal_id
                where o.match_confidence in ('matched', 'manual_adjusted')
                    and coalesce(o.tpsl_telegram_status, 'legacy_not_sent') in ('pending', 'failed')
                order by o.closed_at asc
                limit ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [_outcome_notification_from_row(row) for row in rows]

    def mark_tpsl_notification_sent(self, outcome_id: str, sent_at: datetime | None = None) -> None:
        sent_at = sent_at or datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                """
                update outcomes
                set tpsl_telegram_status = 'sent',
                    tpsl_telegram_sent_at = ?,
                    tpsl_telegram_error = null
                where outcome_id = ?
                """,
                (sent_at.isoformat(), outcome_id),
            )

    def mark_tpsl_notification_failed(self, outcome_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                update outcomes
                set tpsl_telegram_status = 'failed',
                    tpsl_telegram_error = ?
                where outcome_id = ?
                """,
                (error[:500], outcome_id),
            )

    def _match_signal(self, record: ClosedPnlRecord, executions: list[ExecutionRecord]) -> SignalMatch | None:
        if record.side is None:
            return None
        anchor = _match_anchor(record, executions)
        start = anchor.matched_at - timedelta(hours=self.settings.outcome_match_window_hours)
        end = anchor.matched_at + timedelta(minutes=2)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select *
                from signals
                where symbol = ?
                    and side = ?
                    and generated_at <= ?
                    and generated_at >= ?
                    and telegram_status = 'sent'
                order by generated_at desc
                """,
                (record.symbol, record.side.value, end.isoformat(), start.isoformat()),
            ).fetchall()
        for row in rows:
            if record.avg_entry_price is not None and _entry_is_close(row, record.avg_entry_price):
                return SignalMatch(
                    row=row,
                    confidence="matched",
                    reason=(
                        "Matched by symbol, side, Telegram-sent signal time window, "
                        f"{anchor.reason}, and entry proximity."
                    ),
                )
        if rows:
            return SignalMatch(
                row=rows[0],
                confidence="manual_adjusted",
                reason=(
                    "Matched by symbol, side, Telegram-sent signal time window, "
                    f"and {anchor.reason}; manual entry/TP/SL may have been adjusted outside the bot plan."
                ),
            )
        return None

    def _outcome_exists(self, record: ClosedPnlRecord) -> bool:
        with self._connect() as conn:
            row = conn.execute("select 1 from outcomes where outcome_id = ?", (_outcome_id(record),)).fetchone()
        return row is not None

    def _active_cooldowns(self, now: datetime) -> list[str]:
        since = now - timedelta(hours=self.settings.symbol_cooldown_hours)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select symbol, count(*) as losses
                from outcomes
                where match_confidence in ('matched', 'manual_adjusted') and net_pnl < 0 and closed_at >= ?
                group by symbol
                having losses >= ?
                order by losses desc, symbol
                """,
                (since.isoformat(), self.settings.symbol_cooldown_loss_count),
            ).fetchall()
        return [f"{row['symbol']} ({row['losses']} losses/{self.settings.symbol_cooldown_hours}h)" for row in rows]

    def _active_fresh_loss_cooldowns(self, now: datetime) -> list[str]:
        since = now - timedelta(hours=self.settings.immediate_loss_cooldown_hours)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select symbol, side, count(*) as losses, max(closed_at) as latest_loss
                from outcomes
                where match_confidence in ('matched', 'manual_adjusted')
                    and net_pnl < 0
                    and side is not null
                    and closed_at >= ?
                group by symbol, side
                order by latest_loss desc, symbol, side
                """,
                (since.isoformat(),),
            ).fetchall()
        return [
            (
                f"{row['symbol']} {row['side']} "
                f"({row['losses']} loss/{self.settings.immediate_loss_cooldown_hours}h, latest {row['latest_loss']})"
            )
            for row in rows
        ]

    def _active_recent_signal_cooldowns(self, now: datetime) -> list[str]:
        since = now - timedelta(minutes=self.settings.recent_signal_cooldown_minutes)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select symbol, side, max(coalesce(telegram_sent_at, generated_at)) as latest_signal
                from signals
                where telegram_status = 'sent'
                    and coalesce(telegram_sent_at, generated_at) >= ?
                group by symbol, side
                order by latest_signal desc, symbol, side
                """,
                (since.isoformat(),),
            ).fetchall()
        return [
            (
                f"{row['symbol']} {row['side']} "
                f"(sent within {self.settings.recent_signal_cooldown_minutes}m, latest {row['latest_signal']})"
            )
            for row in rows
        ]

    def recent_sent_signal_reason(
        self,
        symbol: str,
        side: SignalSide,
        now: datetime | None = None,
    ) -> str | None:
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(minutes=self.settings.recent_signal_cooldown_minutes)
        with self._connect() as conn:
            row = conn.execute(
                """
                select max(coalesce(telegram_sent_at, generated_at)) as latest_signal
                from signals
                where symbol = ?
                    and side = ?
                    and telegram_status = 'sent'
                    and coalesce(telegram_sent_at, generated_at) >= ?
                """,
                (symbol, side.value, since.isoformat()),
            ).fetchone()
        if row is None or row["latest_signal"] is None:
            return None
        return (
            f"{symbol} {side.value} Telegram alert was already sent at {row['latest_signal']} "
            f"within {self.settings.recent_signal_cooldown_minutes} minutes"
        )

    def _condition_penalties(self, rows: list[sqlite3.Row]) -> tuple[dict[str, float], str]:
        if len(rows) < self.settings.learning_min_sample_size:
            return {}, "No adaptive penalties are active; matched sample is below calibration threshold."
        buckets: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            features = _loads(row["features_json"])
            result_r = row["result_r"]
            if result_r is None:
                continue
            result = float(result_r)
            for condition in _conditions(features):
                buckets[condition].append(result)
        penalties: dict[str, float] = {}
        for condition, values in buckets.items():
            if len(values) < max(5, self.settings.learning_min_sample_size // 3):
                continue
            win_rate = sum(1 for value in values if value > 0) / len(values)
            average_r = sum(values) / len(values)
            loss_values = [value for value in values if value < 0]
            average_loss_r = abs(sum(loss_values) / len(loss_values)) if loss_values else 0.0
            if average_r < 0 or (win_rate < 0.50 and average_loss_r >= 1.0):
                penalties[condition] = round(min(18.0, 5.0 + abs(average_r) * 5.0 + average_loss_r * 2.0), 2)
        if not penalties:
            return {}, "No adaptive penalties are active; no sufficiently sampled losing condition bucket."
        summary = "; ".join(f"{key} -{value}" for key, value in sorted(penalties.items()))
        return penalties, summary

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists signals (
                    signal_id text primary key,
                    generated_at text not null,
                    symbol text not null,
                    side text not null,
                    state text not null,
                    score integer not null,
                    entry_low real,
                    entry_high real,
                    stop_loss real,
                    tp1 real,
                    tp2 real,
                    tp3 real,
                    tp1_eta integer,
                    tp2_eta integer,
                    tp3_eta integer,
                    risk_reward real,
                    features_json text not null,
                    liquidation_check_status text not null,
                    liquidation_price real,
                    account_risk_validated integer not null,
                    selection_reason text not null,
                    telegram_status text,
                    telegram_sent_at text
                )
                """
            )
            _ensure_column(conn, "signals", "telegram_status", "text")
            _ensure_column(conn, "signals", "telegram_sent_at", "text")
            conn.execute(
                """
                update signals
                set telegram_status = 'legacy_alert_eligible'
                where telegram_status is null
                """
            )
            conn.execute(
                """
                create table if not exists outcomes (
                    outcome_id text primary key,
                    signal_id text,
                    symbol text not null,
                    side text,
                    closed_at text not null,
                    actual_entry real,
                    actual_exit real,
                    qty real,
                    realized_pnl real not null,
                    fee real not null,
                    funding real not null,
                    net_pnl real not null,
                    result_r real,
                    outcome_type text not null,
                    holding_minutes integer,
                    match_confidence text not null,
                    match_reason text not null,
                    tpsl_telegram_status text,
                    tpsl_telegram_sent_at text,
                    tpsl_telegram_error text
                )
                """
            )
            _ensure_column(conn, "outcomes", "tpsl_telegram_status", "text")
            _ensure_column(conn, "outcomes", "tpsl_telegram_sent_at", "text")
            _ensure_column(conn, "outcomes", "tpsl_telegram_error", "text")
            conn.execute(
                """
                update outcomes
                set tpsl_telegram_status = 'legacy_not_sent'
                where tpsl_telegram_status is null
                """
            )
            conn.execute("create index if not exists idx_signals_match on signals(symbol, side, generated_at)")
            conn.execute(
                """
                create index if not exists idx_signals_recent_sent
                on signals(symbol, side, telegram_status, telegram_sent_at)
                """
            )
            conn.execute("create index if not exists idx_outcomes_symbol_time on outcomes(symbol, closed_at)")
            self._repair_closed_pnl_accounting(conn)

    def _repair_closed_pnl_accounting(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            select o.outcome_id, o.realized_pnl, o.net_pnl, o.result_r,
                o.outcome_type, o.actual_entry, o.qty, s.stop_loss
            from outcomes o
            left join signals s on s.signal_id = o.signal_id
            where abs(coalesce(o.net_pnl, 0) - coalesce(o.realized_pnl, 0)) > 0.00000001
            """
        ).fetchall()
        for row in rows:
            net_pnl = float(row["realized_pnl"] or 0.0)
            result_r = _result_r_from_values(row["actual_entry"], row["stop_loss"], row["qty"], net_pnl)
            outcome_type = row["outcome_type"]
            if outcome_type in {"MANUAL_PROFIT", "MANUAL_LOSS"}:
                outcome_type = "MANUAL_PROFIT" if net_pnl > 0 else "MANUAL_LOSS"
            conn.execute(
                """
                update outcomes
                set net_pnl = ?, result_r = ?, outcome_type = ?
                where outcome_id = ?
                """,
                (net_pnl, result_r, outcome_type, row["outcome_id"]),
            )


def _signal_id(signal: TradeSignal) -> str:
    raw = "|".join(
        [
            signal.symbol,
            signal.side.value,
            signal.generated_at.isoformat(),
            str(signal.entry_low),
            str(signal.entry_high),
            str(signal.stop_loss),
            str(signal.targets[-1].price if signal.targets else ""),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _outcome_id(record: ClosedPnlRecord) -> str:
    raw = "|".join([record.record_id, record.symbol, record.updated_at.isoformat()])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def _outcome_notification_from_row(row: sqlite3.Row) -> OutcomeNotification:
    return OutcomeNotification(
        outcome_id=str(row["outcome_id"]),
        symbol=str(row["symbol"]),
        side=str(row["side"]) if row["side"] is not None else None,
        closed_at=str(row["closed_at"]),
        entry_low=row["entry_low"],
        entry_high=row["entry_high"],
        actual_entry=row["actual_entry"],
        actual_exit=row["actual_exit"],
        stop_loss=row["stop_loss"],
        tp1=row["tp1"],
        tp2=row["tp2"],
        tp3=row["tp3"],
        qty=row["qty"],
        net_pnl=round(float(row["net_pnl"] or 0.0), 6),
        result_r=round(float(row["result_r"]), 6) if row["result_r"] is not None else None,
        outcome_type=str(row["outcome_type"]),
        match_confidence=str(row["match_confidence"]),
        signal_generated_at=str(row["signal_generated_at"]) if row["signal_generated_at"] is not None else None,
    )


def _feature_snapshot(signal: TradeSignal) -> dict[str, object]:
    return {
        "sweep_passed": _evidence_status(signal, "Manipulation sweep") == "passed",
        "fvg_age": _evidence_age(signal, "IFVG / FVG"),
        "fvg_status": _evidence_status(signal, "IFVG / FVG"),
        "displacement_age": _evidence_age(signal, "Displacement"),
        "displacement_status": _evidence_status(signal, "Displacement"),
        "volume_percentile": _volume_percentile(signal),
        "htf_ltf_status": _evidence_status(signal, "HTF/LTF structure"),
        "htf_alignment_status": _evidence_status(signal, "Higher timeframe alignment"),
        "tp3_distance_pct": signal.targets[-1].distance_pct if signal.targets else None,
        "tp3_eta": signal.targets[-1].estimated_minutes if signal.targets else None,
        "execution_quality_status": signal.execution_quality_status,
        "market_regime": signal.market_regime,
        "market_spread_pct": signal.market_spread_pct,
        "post_signal_revalidation": signal.post_signal_revalidation,
        "liquidation_check_status": signal.liquidation_check_status.value,
        "risk_reward": signal.risk_reward,
    }


def _funding_for_record(
    record: ClosedPnlRecord,
    matched: sqlite3.Row | None,
    transactions: list[TransactionLogRecord],
) -> float:
    start = _parse_dt(matched["generated_at"]) if matched is not None else record.created_at
    total = 0.0
    for item in transactions:
        if item.symbol and item.symbol != record.symbol:
            continue
        if not start <= item.created_at <= record.updated_at:
            continue
        if "fund" in item.transaction_type.lower() or "settlement" in item.transaction_type.lower():
            total += item.funding or item.change
    return total


def _match_anchor(record: ClosedPnlRecord, executions: list[ExecutionRecord]) -> MatchAnchor:
    entry_execution = _entry_execution_for_record(record, executions)
    if entry_execution is not None:
        return MatchAnchor(
            matched_at=entry_execution.created_at,
            reason="entry execution time",
        )
    return MatchAnchor(
        matched_at=record.updated_at,
        reason="closed position time fallback",
    )


def _entry_execution_for_record(
    record: ClosedPnlRecord,
    executions: list[ExecutionRecord],
) -> ExecutionRecord | None:
    if record.side is None:
        return None
    candidates = [
        item
        for item in executions
        if item.symbol == record.symbol
        and item.side == record.side
        and item.exec_qty > 0
        and item.created_at <= record.updated_at
        and _execution_price_matches_entry(record, item)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.created_at, reverse=True)
    if record.qty <= 0:
        return candidates[0]
    selected: list[ExecutionRecord] = []
    total_qty = 0.0
    for item in candidates:
        selected.append(item)
        total_qty += item.exec_qty
        if total_qty >= record.qty * 0.98:
            return min(selected, key=lambda selected_item: selected_item.created_at)
    return candidates[0]


def _execution_price_matches_entry(record: ClosedPnlRecord, execution: ExecutionRecord) -> bool:
    if record.avg_entry_price is None or execution.exec_price is None:
        return True
    entry = float(record.avg_entry_price)
    if entry <= 0:
        return True
    return abs(float(execution.exec_price) - entry) / entry <= 0.03


def _classify_outcome(record: ClosedPnlRecord, matched: sqlite3.Row | None, net_pnl: float) -> str:
    if matched is None or record.avg_exit_price is None:
        return "UNCERTAIN"
    side = matched["side"]
    exit_price = record.avg_exit_price
    stop_loss = matched["stop_loss"]
    tp1 = matched["tp1"]
    tp2 = matched["tp2"]
    tp3 = matched["tp3"]
    if side == SignalSide.LONG.value:
        if tp3 is not None and exit_price >= tp3:
            return "TP3"
        if tp2 is not None and exit_price >= tp2:
            return "TP2"
        if tp1 is not None and exit_price >= tp1:
            return "TP1"
        if stop_loss is not None and exit_price <= stop_loss:
            return "SL"
    if side == SignalSide.SHORT.value:
        if tp3 is not None and exit_price <= tp3:
            return "TP3"
        if tp2 is not None and exit_price <= tp2:
            return "TP2"
        if tp1 is not None and exit_price <= tp1:
            return "TP1"
        if stop_loss is not None and exit_price >= stop_loss:
            return "SL"
    return "MANUAL_PROFIT" if net_pnl > 0 else "MANUAL_LOSS"


def _result_r(record: ClosedPnlRecord, matched: sqlite3.Row | None, net_pnl: float) -> float | None:
    if matched is None or record.avg_entry_price is None or record.qty <= 0:
        return None
    return _result_r_from_values(record.avg_entry_price, matched["stop_loss"], record.qty, net_pnl)


def _result_r_from_values(entry: float | None, stop_loss: float | None, qty: float | None, net_pnl: float) -> float | None:
    if entry is None or stop_loss is None or qty is None:
        return None
    risk_usdt = abs(float(entry) - float(stop_loss)) * float(qty)
    if risk_usdt <= 0:
        return None
    return net_pnl / risk_usdt


def _entry_is_close(row: sqlite3.Row, entry: float) -> bool:
    low = row["entry_low"]
    high = row["entry_high"]
    if low is None or high is None or entry <= 0:
        return False
    if float(low) <= entry <= float(high):
        return True
    midpoint = (float(low) + float(high)) / 2
    return abs(entry - midpoint) / entry <= 0.025


def _sample_warning(matched_count: int, minimum: int) -> str:
    if matched_count < minimum:
        return (
            f"Matched outcomes {matched_count}/{minimum}; probability remains not calibrated "
            "and adaptive penalties are conservative."
        )
    return f"Matched outcomes {matched_count}; enough sample for conservative condition penalties, not profit guarantees."


def _profit_factor(rows: list[sqlite3.Row]) -> float | None:
    gross_profit = sum(float(row["net_pnl"] or 0.0) for row in rows if float(row["net_pnl"] or 0.0) > 0)
    gross_loss = abs(sum(float(row["net_pnl"] or 0.0) for row in rows if float(row["net_pnl"] or 0.0) < 0))
    if gross_loss <= 0:
        return None
    return gross_profit / gross_loss


def _score_band_filter(side: SignalSide, score: int) -> tuple[str, str, list[object]]:
    if score >= 80:
        return f"{side.value} score >=80", "and s.side = ? and s.score >= ?", [side.value, 80]
    if score >= 75:
        return f"{side.value} score 75-79", "and s.side = ? and s.score >= ? and s.score < ?", [side.value, 75, 80]
    return f"{side.value} score <75", "and s.side = ? and s.score < ?", [side.value, 75]


def _stats_line(stats: HistoricalStats) -> str:
    win_rate = f"{stats.win_rate:.1f}%" if stats.win_rate is not None else "n/a"
    expectancy = f"{stats.expectancy_r:+.2f}R" if stats.expectancy_r is not None else "n/a"
    net_r = f"{stats.net_r:+.2f}R" if stats.net_r is not None else "n/a"
    profit_factor = f"{stats.profit_factor:.2f}" if stats.profit_factor is not None else "n/a"
    return (
        f"{stats.label}: {stats.count} samples, {stats.wins}W/{stats.losses}L, "
        f"WR {win_rate}, exp {expectancy}, net {net_r}, PF {profit_factor}"
    )


def _ml_status(settings: Settings, valid_sample_count: int) -> str:
    if settings.switch_ml == "OFF":
        return "SWITCH_ML=OFF; rule-based quant filters active."
    if valid_sample_count < settings.learning_min_sample_size:
        return (
            f"SWITCH_ML=ON requested, but valid samples {valid_sample_count}/"
            f"{settings.learning_min_sample_size}; using rule-based quant fallback."
        )
    return "SWITCH_ML=ON requested; local ML hook ready, rule-based fallback remains active."


def _symbol_rank(rows: list[sqlite3.Row], reverse: bool) -> list[str]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[row["symbol"]] += float(row["net_pnl"] or 0.0)
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=reverse)
    return [f"{symbol} {value:+.2f} USDT" for symbol, value in ranked[:3]]


def _combo_rank(rows: list[sqlite3.Row], reverse: bool) -> list[str]:
    totals: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        features = _loads(row["features_json"])
        combo = _combo_label(features)
        totals[combo].append(float(row["net_pnl"] or 0.0))
    ranked = sorted(
        ((combo, sum(values), len(values)) for combo, values in totals.items()),
        key=lambda item: item[1],
        reverse=reverse,
    )
    return [f"{combo}: {value:+.2f} USDT / {count} trade(s)" for combo, value, count in ranked[:3]]


def _loss_patterns(rows: list[sqlite3.Row]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        if float(row["net_pnl"] or 0.0) >= 0:
            continue
        for condition in _conditions(_loads(row["features_json"])):
            counts[condition] += 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [f"{name}: {count} loss(es)" for name, count in ranked[:8]]


def _conditions(features: dict[str, object]) -> list[str]:
    conditions: list[str] = []
    if not features.get("sweep_passed"):
        conditions.append("no manipulation sweep")
    if float(features.get("volume_percentile") or 0.0) < 60:
        conditions.append("low volume")
    fvg_age = features.get("fvg_age")
    if fvg_age is not None and int(fvg_age) > 20:
        conditions.append("stale FVG/IFVG")
    displacement_age = features.get("displacement_age")
    if displacement_age is not None and int(displacement_age) > 5:
        conditions.append("old displacement")
    if int(features.get("tp3_eta") or 0) > 240:
        conditions.append("TP3 ETA too long")
    if features.get("liquidation_check_status") == "NOT_VALIDATED":
        conditions.append("liquidation not validated")
    if features.get("liquidation_check_status") == "FAILED":
        conditions.append("liquidation failed")
    if features.get("htf_alignment_status") != "passed":
        conditions.append("HTF misalignment")
    if features.get("execution_quality_status") not in {None, "PASSED", "NOT_VALIDATED"}:
        conditions.append("weak execution quality")
    market_regime = features.get("market_regime")
    if market_regime in {"quiet_chop", "wide_atr_risk"}:
        conditions.append(f"market regime {market_regime}")
    market_spread = features.get("market_spread_pct")
    if market_spread is not None and float(market_spread) > 0.25:
        conditions.append("wide market spread")
    return conditions


def _combo_label(features: dict[str, object]) -> str:
    sweep = "sweep" if features.get("sweep_passed") else "no-sweep"
    htf = "htf" if features.get("htf_alignment_status") == "passed" else "no-htf"
    volume = "vol+" if float(features.get("volume_percentile") or 0.0) >= 60 else "vol-"
    return f"{sweep}/{htf}/{volume}"


def _loads(raw: str | None) -> dict[str, object]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _evidence_status(signal: TradeSignal, name: str) -> str | None:
    for item in signal.evidence:
        if item.name == name:
            return item.status
    return None


def _evidence_age(signal: TradeSignal, name: str) -> int | None:
    for item in signal.evidence:
        if item.name != name:
            continue
        match = re.search(r"(?:age|swept|displacement)\s+([0-9]+)", item.detail, flags=re.IGNORECASE)
        return int(match.group(1)) if match else None
    return None


def _volume_percentile(signal: TradeSignal) -> float:
    for item in signal.evidence:
        if item.name != "Volume participation":
            continue
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)%", item.detail)
        return float(match.group(1)) if match else 0.0
    return 0.0
