from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from app.bybit import BybitMarketClient, interval_to_minutes
from app.config import Settings
from app.future_mode import evaluate_future_gate
from app.journal import TradeJournal
from app.models import (
    AccountSnapshot,
    JournalOutcomeSummary,
    LearningReport,
    LiquidationCheckStatus,
    MarketTicker,
    OutcomeNotification,
    ScanStatus,
    SignalSide,
    SignalState,
    TradeSignal,
)
from app.outcome_chart import chart_window_for_outcome, render_outcome_chart
from app.risk import LiquidationRiskResult, evaluate_liquidation_risk
from app.strategy import StrategyAnalyzer
from app.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

BEST_SIGNAL_MIN_SELECTION_SCORE = 90.0


class TradingAnalysisService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = BybitMarketClient(settings)
        self.analyzer = StrategyAnalyzer(
            interval=settings.kline_interval,
            max_tp3_eta_minutes=settings.telegram_max_tp3_eta_minutes,
            max_stop_atr_multiple=settings.max_stop_atr_multiple,
            max_stop_distance_pct=settings.max_stop_distance_pct,
            max_market_spread_pct=settings.max_market_spread_pct,
        )
        self.notifier = TelegramNotifier(settings)
        self.journal = TradeJournal(settings.learning_db_path, settings) if settings.learning_enabled else None
        self.learning_report = LearningReport(learning_enabled=settings.learning_enabled, bybit_env=settings.bybit_env)
        if self.journal is not None:
            try:
                self.learning_report = self.journal.build_report()
            except Exception as exc:
                logger.warning("Initial learning journal report failed: %s", exc)
        self.signals: list[TradeSignal] = []
        self.status = ScanStatus(
            running=False,
            next_scan_hint_seconds=settings.scan_interval_seconds,
            top_markets=settings.top_markets,
            initial_scan_markets=max(settings.top_markets, settings.min_scan_markets),
            max_scan_markets=settings.max_scan_markets,
            scan_market_limit=max(settings.top_markets, settings.min_scan_markets),
            telegram_configured=settings.telegram_configured,
            bybit_keys_configured=settings.bybit_keys_configured,
            learning_enabled=settings.learning_enabled,
            bybit_env=settings.bybit_env,
            telegram_status="configured" if settings.telegram_configured else "not_configured",
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._alerted_keys: set[str] = set()
        self._last_scan_market_limit = max(settings.top_markets, settings.min_scan_markets)
        self._last_scan_expanded = False
        self._last_scan_expansion_reason = "Adaptive scan has not run yet."

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._scan_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.client.close()

    async def scan_once(self) -> list[TradeSignal]:
        self.status.running = True
        self.status.error = None
        try:
            tickers = await self.client.get_top_linear_usdt_tickers(self.settings.max_scan_markets)
            semaphore = asyncio.Semaphore(self.settings.max_concurrent_requests)
            signals: list[TradeSignal] = []
            analyzed_count = 0
            limits = _adaptive_scan_limits(self.settings, len(tickers))
            self._last_scan_expanded = False
            self._last_scan_expansion_reason = "Initial scan completed."
            for scan_limit in limits:
                batch = tickers[analyzed_count:scan_limit]
                if not batch:
                    continue
                results = await asyncio.gather(
                    *(self._analyze_ticker(ticker, semaphore) for ticker in batch),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, TradeSignal):
                        signals.append(result)
                    elif isinstance(result, Exception):
                        logger.warning("Ticker analysis failed: %s", result)
                analyzed_count = scan_limit
                signals.sort(key=_scan_rank)
                if _has_enough_scan_candidates(signals, self.settings, self.learning_report):
                    self._last_scan_expansion_reason = (
                        f"Stopped at top {analyzed_count}: Telegram-viable candidate threshold satisfied."
                    )
                    break
                if scan_limit < limits[-1]:
                    self._last_scan_expanded = True
                    self._last_scan_expansion_reason = (
                        f"Expanded beyond top {scan_limit}: not enough ready/manual candidates."
                    )
            self._last_scan_market_limit = analyzed_count
            await self._apply_liquidation_checks(signals)
            await self._sync_learning_outcomes()
            self._apply_signal_statistics(signals)
            self._apply_best_signal_selection(signals)
            signals.sort(key=lambda item: _display_rank(item, self.learning_report))
            self.signals = signals
            await self._send_alerts(signals)
            self._refresh_status(signals)
            return signals
        except Exception as exc:
            logger.exception("Scan failed")
            self.status.error = str(exc)
            return self.signals
        finally:
            self.status.running = False

    async def _scan_loop(self) -> None:
        while not self._stop.is_set():
            await self.scan_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.scan_interval_seconds)
            except TimeoutError:
                continue

    async def _analyze_ticker(self, ticker: MarketTicker, semaphore: asyncio.Semaphore) -> TradeSignal:
        async with semaphore:
            candles, htf = await asyncio.gather(
                self.client.get_closed_klines(ticker.symbol, self.settings.kline_interval, self.settings.kline_limit),
                self.client.get_closed_klines(
                    ticker.symbol,
                    self.settings.higher_timeframe_interval,
                    min(self.settings.kline_limit, 120),
                ),
            )
        return self.analyzer.analyze(ticker, candles, htf)

    async def _apply_liquidation_checks(self, signals: list[TradeSignal]) -> None:
        if not self.settings.bybit_keys_configured:
            for signal in signals:
                _apply_liquidation_result(
                    signal,
                    LiquidationRiskResult(
                        status=LiquidationCheckStatus.NOT_VALIDATED,
                        liquidation_price=None,
                        safety_buffer=None,
                        account_risk_validated=False,
                        reason="Account liquidation not validated because Bybit API keys are not configured.",
                    ),
                )
            return

        trade_ready = [signal for signal in signals if signal.state == SignalState.TRADE_READY]
        if not trade_ready:
            return

        checks = await asyncio.gather(
            *(self._liquidation_check_for_signal(signal) for signal in trade_ready),
            return_exceptions=True,
        )
        for signal, result in zip(trade_ready, checks, strict=False):
            if isinstance(result, LiquidationRiskResult):
                _apply_liquidation_result(signal, result)
            elif isinstance(result, Exception):
                logger.warning("Liquidation check failed for %s: %s", signal.symbol, result)
                _apply_liquidation_result(
                    signal,
                    LiquidationRiskResult(
                        status=LiquidationCheckStatus.NOT_VALIDATED,
                        liquidation_price=None,
                        safety_buffer=None,
                        account_risk_validated=False,
                        reason=f"Account liquidation not validated because Bybit position lookup failed: {result}",
                    ),
                )

    async def _liquidation_check_for_signal(self, signal: TradeSignal) -> LiquidationRiskResult:
        position = await self.client.get_linear_position(signal.symbol, signal.side)
        return evaluate_liquidation_risk(
            signal,
            position,
            min_buffer_r=self.settings.min_liquidation_sl_buffer_r,
        )

    async def _sync_learning_outcomes(self) -> None:
        if self.journal is None:
            self.learning_report = LearningReport(learning_enabled=False, bybit_env=self.settings.bybit_env)
            return

        if self.settings.bybit_keys_configured:
            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=max(24, self.settings.outcome_match_window_hours + 6))
            try:
                closed_pnl = await self.client.get_closed_pnl(start, end)
                transactions = await self._safe_transaction_logs(start, end)
                executions = await self._safe_executions(start, end)
                synced = self.journal.sync_closed_outcomes(closed_pnl, transactions, executions)
                if synced:
                    logger.info("Synced %s read-only closed outcome(s) into learning journal", synced)
            except Exception as exc:
                logger.warning("Read-only learning outcome sync failed: %s", exc)

        await self._send_tpsl_notifications()
        self.learning_report = self.journal.build_report()

    def _apply_signal_statistics(self, signals: list[TradeSignal]) -> None:
        if self.journal is None:
            return
        for signal in signals:
            self.journal.apply_signal_statistics(signal)

    async def _safe_transaction_logs(self, start: datetime, end: datetime):
        try:
            return await self.client.get_transaction_logs(start, end)
        except Exception as exc:
            logger.warning("Read-only transaction log lookup failed; continuing with closed PnL only: %s", exc)
            return []

    async def _safe_executions(self, start: datetime, end: datetime):
        try:
            return await self.client.get_executions(start, end)
        except Exception as exc:
            logger.warning("Read-only execution lookup failed; continuing with closed PnL only: %s", exc)
            return []

    async def account_snapshot(self) -> AccountSnapshot:
        if not self.settings.bybit_keys_configured:
            return AccountSnapshot(
                bybit_env=self.settings.bybit_env,
                keys_configured=False,
                error="Bybit API keys are not configured; account balance cannot be displayed.",
            )
        try:
            return await self.client.get_wallet_balance("USDT")
        except Exception as exc:
            logger.warning("Read-only account snapshot lookup failed: %s", exc)
            return AccountSnapshot(
                bybit_env=self.settings.bybit_env,
                keys_configured=True,
                error=str(exc),
            )

    def journal_outcome_summary(self) -> JournalOutcomeSummary:
        if self.journal is None:
            return JournalOutcomeSummary()
        return self.journal.build_outcome_summary()

    async def _send_tpsl_notifications(self) -> None:
        if self.journal is None:
            return
        if not self.settings.telegram_configured or self.settings.telegram_tpsl_thread_id is None:
            return
        for outcome in self.journal.pending_tpsl_notifications():
            status = await self.notifier.send_outcome(outcome)
            if status == "sent":
                self.journal.mark_tpsl_notification_sent(outcome.outcome_id)
                logger.info("Sent TP/SL outcome notification for %s %s", outcome.symbol, outcome.outcome_type)
                continue
            self.journal.mark_tpsl_notification_failed(outcome.outcome_id, status)
            logger.warning("TP/SL outcome notification failed for %s: %s", outcome.symbol, status)
            break

    async def _outcome_chart(self, outcome: OutcomeNotification) -> bytes | None:
        try:
            interval_minutes = interval_to_minutes(self.settings.kline_interval)
            window = chart_window_for_outcome(outcome, interval_minutes)
            if window is None:
                candles = await self.client.get_closed_klines(
                    outcome.symbol,
                    self.settings.kline_interval,
                    min(self.settings.kline_limit, 220),
                )
            else:
                start, end = window
                candles = await self.client.get_closed_klines_window(
                    outcome.symbol,
                    self.settings.kline_interval,
                    start,
                    end,
                    min(self.settings.kline_limit, 220),
                )
            return render_outcome_chart(outcome, candles)
        except Exception as exc:
            logger.warning("TP/SL chart render failed for %s; sending text fallback: %s", outcome.symbol, exc)
            return None

    def _record_telegram_sent_signal(self, signal: TradeSignal) -> None:
        if self.journal is None:
            return
        try:
            self.journal.record_signal(signal, telegram_status="sent", telegram_sent_at=datetime.now(timezone.utc))
        except Exception as exc:
            logger.warning("Telegram signal journal write failed for %s: %s", signal.symbol, exc)
            return
        self.learning_report = self.journal.build_report()

    async def _send_alerts(self, signals: list[TradeSignal]) -> None:
        if not self.settings.telegram_configured:
            self.status.telegram_status = "not_configured"
            return
        selected = sorted(
            (signal for signal in signals if signal.alert_eligible),
            key=_alert_rank,
        )
        if not selected:
            self.status.telegram_status = "no_best_signal"
            return
        sent: list[str] = []
        skipped_duplicates: list[str] = []
        for signal in selected[: self.settings.max_trade_alerts_per_scan]:
            first_target = signal.targets[0].price if signal.targets else ""
            alert_key = f"{signal.symbol}:{signal.side}:{signal.state}:{signal.stop_loss}:{first_target}"
            if alert_key in self._alerted_keys:
                skipped_duplicates.append(signal.symbol)
                continue
            recent_reason = self._recent_sent_signal_reason(signal)
            if recent_reason is not None:
                skipped_duplicates.append(signal.symbol)
                signal.alert_eligible = False
                signal.alert_reason = f"Not sent to Telegram: {recent_reason}."
                continue
            status = await self.notifier.send_signal(signal)
            if status != "sent":
                self.status.telegram_status = status
                return
            self._alerted_keys.add(alert_key)
            self._record_telegram_sent_signal(signal)
            sent.append(signal.symbol)
        if sent:
            self.status.telegram_status = f"sent_best:{','.join(sent)}"
            return
        self.status.telegram_status = f"best_already_sent:{','.join(skipped_duplicates)}"

    def _recent_sent_signal_reason(self, signal: TradeSignal) -> str | None:
        if self.journal is None:
            return None
        try:
            return self.journal.recent_sent_signal_reason(signal.symbol, signal.side)
        except Exception as exc:
            logger.warning("Recent Telegram signal cooldown lookup failed for %s: %s", signal.symbol, exc)
            return None

    def _refresh_status(self, signals: list[TradeSignal]) -> None:
        self.status.last_scan_at = datetime.now(timezone.utc)
        self.status.top_markets = self._last_scan_market_limit
        self.status.initial_scan_markets = max(self.settings.top_markets, self.settings.min_scan_markets)
        self.status.max_scan_markets = self.settings.max_scan_markets
        self.status.scan_market_limit = self._last_scan_market_limit
        self.status.scan_expanded = self._last_scan_expanded
        self.status.scan_expansion_reason = self._last_scan_expansion_reason
        self.status.symbols_scanned = len(signals)
        self.status.trade_ready_count = sum(1 for signal in signals if signal.state == SignalState.TRADE_READY)
        self.status.watch_count = sum(1 for signal in signals if signal.state == SignalState.WATCH)
        self.status.wait_count = sum(1 for signal in signals if signal.state == SignalState.WAIT)
        self.status.avoid_count = sum(1 for signal in signals if signal.state == SignalState.AVOID)
        self.status.learning_enabled = self.learning_report.learning_enabled
        self.status.bybit_env = self.learning_report.bybit_env
        self.status.journal_signal_count = self.learning_report.journal_signal_count
        self.status.matched_trade_count = self.learning_report.matched_trade_count
        self.status.valid_bot_assisted_sample_count = self.learning_report.valid_bot_assisted_sample_count
        self.status.all_time_expectancy_r = self.learning_report.all_time_expectancy_r
        self.status.all_time_profit_factor = self.learning_report.all_time_profit_factor
        self.status.today_net_pnl = self.learning_report.today_net_pnl
        self.status.today_net_r = self.learning_report.today_net_r
        self.status.today_win_rate = self.learning_report.today_win_rate
        self.status.sample_warning = self.learning_report.sample_warning
        self.status.active_symbol_cooldowns = self.learning_report.active_symbol_cooldowns
        self.status.active_fresh_loss_cooldowns = self.learning_report.active_fresh_loss_cooldowns
        self.status.active_recent_signal_cooldowns = self.learning_report.active_recent_signal_cooldowns
        self.status.adaptive_penalty_summary = self.learning_report.adaptive_penalty_summary
        self.status.ml_status = self.learning_report.ml_status

    def _apply_best_signal_selection(self, signals: list[TradeSignal]) -> None:
        for signal in signals:
            if signal.state == SignalState.TRADE_READY:
                signal.alert_eligible = False
                signal.alert_reason = "Not selected for Telegram: another TRADE_READY candidate has stronger composite quality."

        selected = select_trade_signals(
            signals,
            self.settings,
            self.learning_report,
            limit=self.settings.max_trade_alerts_per_scan,
        )
        if not selected:
            self.status.best_signal_symbol = None
            self.status.best_signal_side = None
            self.status.best_signal_score = None
            blocked_reason = best_signal_blocked_reason(signals, self.settings, self.learning_report)
            self.status.best_signal_selection_reason = blocked_reason
            self.status.telegram_best_signal_blocked_reason = blocked_reason
            return

        for rank, (signal, _, reason) in enumerate(selected, start=1):
            signal.alert_eligible = True
            signal.alert_reason = f"BEST_TRADE_READY #{rank} selected for Telegram: {reason}"
        signal, selection_score, reason = selected[0]
        self.status.best_signal_symbol = signal.symbol
        self.status.best_signal_side = signal.side
        self.status.best_signal_score = round(selection_score, 2)
        self.status.best_signal_selection_reason = reason
        self.status.telegram_best_signal_blocked_reason = None


def select_best_trade_signal(
    signals: list[TradeSignal],
    settings: Settings | None = None,
    learning_report: LearningReport | None = None,
) -> tuple[TradeSignal, float, str] | None:
    selected = select_trade_signals(signals, settings, learning_report, limit=1)
    return selected[0] if selected else None


def select_trade_signals(
    signals: list[TradeSignal],
    settings: Settings | None = None,
    learning_report: LearningReport | None = None,
    limit: int | None = None,
) -> list[tuple[TradeSignal, float, str]]:
    candidates = [
        signal
        for signal in signals
        if signal.state == SignalState.TRADE_READY
        and signal.targets
        and signal.risk_reward is not None
        and signal.liquidation_check_status != LiquidationCheckStatus.FAILED
    ]
    ranked = [
        (signal, _selection_score(signal, learning_report), _selection_reason(signal, settings, learning_report))
        for signal in candidates
        if _quality_block_reason(signal, settings, learning_report) is None
    ]
    minimum_score = settings.telegram_min_best_score if settings is not None else BEST_SIGNAL_MIN_SELECTION_SCORE
    ranked = [item for item in ranked if item[1] >= minimum_score]
    if not ranked:
        return []
    ranked.sort(key=lambda item: (-item[1], -item[0].score, item[0].targets[-1].estimated_minutes, item[0].symbol))
    if limit is None:
        limit = 1
    return ranked[: max(1, limit)]


def _adaptive_scan_limits(settings: Settings, available_count: int) -> list[int]:
    initial = min(max(settings.top_markets, settings.min_scan_markets), available_count)
    maximum = min(settings.max_scan_markets, available_count)
    if available_count <= 0:
        return []
    limits = [max(1, initial)]
    next_limit = limits[0]
    while next_limit < maximum:
        next_limit = min(maximum, next_limit + settings.scan_expansion_step)
        limits.append(next_limit)
    return limits


def _has_enough_scan_candidates(
    signals: list[TradeSignal],
    settings: Settings,
    learning_report: LearningReport | None = None,
) -> bool:
    if settings.min_ready_candidates > 0:
        selected = select_trade_signals(
            signals,
            settings,
            learning_report,
            limit=settings.min_ready_candidates,
        )
        return len(selected) >= settings.min_ready_candidates

    manual_count = sum(
        1
        for signal in signals
        if signal.state in {SignalState.TRADE_READY, SignalState.WATCH}
        and signal.targets
        and signal.risk_reward is not None
    )
    return settings.min_manual_candidates == 0 or manual_count >= settings.min_manual_candidates


def _scan_rank(signal: TradeSignal) -> tuple[bool, int, str]:
    return (signal.state != SignalState.TRADE_READY, -signal.score, signal.symbol)


def _display_rank(signal: TradeSignal, learning_report: LearningReport | None = None) -> tuple[bool, bool, float, int, str]:
    selection_score = _selection_score(signal, learning_report) if signal.targets else float(signal.score)
    return (not signal.alert_eligible, signal.state != SignalState.TRADE_READY, -selection_score, -signal.score, signal.symbol)


def best_signal_blocked_reason(
    signals: list[TradeSignal],
    settings: Settings | None = None,
    learning_report: LearningReport | None = None,
) -> str:
    candidates = [
        signal
        for signal in signals
        if signal.state == SignalState.TRADE_READY and signal.targets and signal.risk_reward is not None
    ]
    if not candidates:
        return "No TRADE_READY candidate is available for Telegram."
    scored = sorted(
        ((signal, _selection_score(signal, learning_report)) for signal in candidates),
        key=lambda item: (-item[1], -item[0].score, item[0].symbol),
    )
    signal, score = scored[0]
    quality_reason = _quality_block_reason(signal, settings, learning_report)
    if quality_reason is not None:
        return f"Best candidate {signal.symbol} blocked: {quality_reason}"
    minimum_score = settings.telegram_min_best_score if settings is not None else BEST_SIGNAL_MIN_SELECTION_SCORE
    if score < minimum_score:
        return f"Best candidate {signal.symbol} composite score {score:.2f} is below Telegram minimum {minimum_score:.2f}."
    return "No TRADE_READY candidate passed the best-signal composite threshold."


def _selection_score(signal: TradeSignal, learning_report: LearningReport | None = None) -> float:
    tp3 = signal.targets[-1]
    tp3_pct = max(0.0, tp3.distance_pct)
    tp3_eta = max(1, tp3.estimated_minutes)
    volume = _volume_percentile(signal)
    fvg_age = _evidence_age(signal, "IFVG / FVG")
    displacement_age = _evidence_age(signal, "Displacement")
    has_sweep = _evidence_passed(signal, "Manipulation sweep")
    has_displacement = _evidence_passed(signal, "Displacement")
    has_structure = _evidence_passed(signal, "HTF/LTF structure")
    has_htf_alignment = _evidence_passed(signal, "Higher timeframe alignment")
    has_ifvg = _evidence_passed(signal, "IFVG / FVG")
    rr = signal.risk_reward or 0.0

    score = float(signal.score)
    score += min(tp3_pct, 15.0) * 1.2
    score += min((tp3_pct / tp3_eta) * 60.0, 8.0) * 1.8
    score += min(volume, 100.0) * 0.08
    score += min(rr, 3.5) * 2.0
    score += 10.0 if has_sweep else -8.0
    score += 9.0 if has_ifvg else -9.0
    score += 8.0 if has_displacement else -10.0
    score += 8.0 if has_structure else -8.0
    score += 7.0 if has_htf_alignment else -7.0
    score += _freshness_bonus(fvg_age, fresh_bonus=8.0, stale_penalty=-10.0, stale_after=20)
    score += _freshness_bonus(displacement_age, fresh_bonus=5.0, stale_penalty=-8.0, stale_after=6)
    if volume < 20.0:
        score -= 10.0
    if tp3_eta > 240:
        score -= min((tp3_eta - 240) / 30.0, 8.0)
    if signal.liquidation_check_status == LiquidationCheckStatus.NOT_VALIDATED:
        score -= 4.0
    if signal.execution_quality_status not in {"PASSED", "NOT_VALIDATED"}:
        score -= 14.0
    if signal.post_signal_revalidation not in {"PASSED", "NOT_VALIDATED"}:
        score -= 8.0
    if signal.market_regime in {"quiet_chop", "wide_atr_risk"}:
        score -= 8.0
    if signal.market_spread_pct is not None and signal.market_spread_pct > 0.25:
        score -= 10.0
    if learning_report is not None:
        for condition, penalty in learning_report.condition_penalties.items():
            if condition in _signal_conditions(signal):
                score -= penalty
    return score


def _selection_reason(
    signal: TradeSignal,
    settings: Settings | None = None,
    learning_report: LearningReport | None = None,
) -> str:
    tp3 = signal.targets[-1]
    volume = _volume_percentile(signal)
    fvg_age = _evidence_age(signal, "IFVG / FVG")
    displacement_age = _evidence_age(signal, "Displacement")
    strengths: list[str] = [
        f"confluence {signal.score}/100",
        f"TP3 {tp3.distance_pct:.2f}% in ~{tp3.estimated_minutes} min",
        f"volume percentile {volume:.1f}%",
    ]
    if _evidence_passed(signal, "Manipulation sweep"):
        strengths.append("manipulation sweep confirmed")
    if _evidence_passed(signal, "HTF/LTF structure"):
        strengths.append("HTF/LTF structure confirmed")
    if _evidence_passed(signal, "Higher timeframe alignment"):
        strengths.append("higher timeframe aligned")
    if fvg_age is not None:
        strengths.append(f"IFVG/FVG age {fvg_age}")
    if displacement_age is not None:
        strengths.append(f"displacement age {displacement_age}")
    if signal.execution_quality_status != "NOT_VALIDATED":
        strengths.append(f"execution {signal.execution_quality_status}")
    if signal.market_regime != "unknown":
        strengths.append(f"regime {signal.market_regime}")
    if signal.market_spread_pct is not None:
        strengths.append(f"spread {signal.market_spread_pct:.4f}%")
    if signal.post_signal_revalidation != "NOT_VALIDATED":
        strengths.append(f"revalidation {signal.post_signal_revalidation}")
    strengths.append(f"liquidation check {signal.liquidation_check_status.value}")
    if learning_report is not None and learning_report.condition_penalties:
        applied = [
            f"{condition} -{penalty}"
            for condition, penalty in learning_report.condition_penalties.items()
            if condition in _signal_conditions(signal)
        ]
        if applied:
            strengths.append("adaptive penalties: " + ", ".join(applied))
    if settings is not None and settings.future_mode == "ON":
        strengths.append(evaluate_future_gate(signal, learning_report).summary)
    return "; ".join(strengths)


def _quality_block_reason(
    signal: TradeSignal,
    settings: Settings | None,
    learning_report: LearningReport | None,
) -> str | None:
    if signal.liquidation_check_status == LiquidationCheckStatus.FAILED:
        return "liquidation check failed"
    if settings is None:
        return None
    if signal.score < settings.telegram_min_confluence_score:
        return (
            f"raw confluence score {signal.score}/100 is below Telegram minimum "
            f"{settings.telegram_min_confluence_score:.0f}/100"
        )
    if learning_report is not None:
        fresh_loss = _cooldown_item_for_signal(signal, learning_report.active_fresh_loss_cooldowns)
        if fresh_loss is not None:
            return (
                "symbol+side is in immediate loss cooldown after fresh "
                f"matched/manual_adjusted loss: {fresh_loss}"
            )
        recent_alert = _cooldown_item_for_signal(signal, learning_report.active_recent_signal_cooldowns)
        if recent_alert is not None:
            return f"symbol+side was already sent recently: {recent_alert}"
    history_reason = _historical_defensive_block_reason(signal)
    if history_reason is not None:
        return history_reason
    long_reason = _long_direction_defensive_block_reason(signal, settings, learning_report)
    if long_reason is not None:
        return long_reason
    if signal.execution_quality_status not in {"PASSED", "NOT_VALIDATED"}:
        return f"execution quality is {signal.execution_quality_status}: {signal.execution_quality_reason}"
    if signal.post_signal_revalidation not in {"PASSED", "NOT_VALIDATED"}:
        return f"post-signal revalidation is {signal.post_signal_revalidation}"
    if signal.market_regime in {"quiet_chop", "wide_atr_risk"}:
        return f"market regime is {signal.market_regime}"
    if signal.market_spread_pct is not None and signal.market_spread_pct > settings.max_market_spread_pct:
        return f"market spread {signal.market_spread_pct:.4f}% is above maximum {settings.max_market_spread_pct:.4f}%"
    if settings.telegram_require_liquidation_passed and signal.liquidation_check_status != LiquidationCheckStatus.PASSED:
        return "liquidation is not PASSED while TELEGRAM_REQUIRE_LIQUIDATION_PASSED is enabled"
    if learning_report is not None and _symbol_in_cooldown(signal.symbol, learning_report):
        return "symbol is in learning cooldown after repeated matched losses"
    volume = _volume_percentile(signal)
    if volume < settings.telegram_min_volume_percentile:
        return f"volume percentile {volume:.1f}% is below minimum {settings.telegram_min_volume_percentile:.1f}%"
    fvg_age = _evidence_age(signal, "IFVG / FVG")
    strong_compensation = (
        _evidence_passed(signal, "Manipulation sweep")
        and _evidence_passed(signal, "Displacement")
        and _evidence_passed(signal, "HTF/LTF structure")
        and _evidence_passed(signal, "Higher timeframe alignment")
        and volume >= 80.0
        and signal.score >= 85
    )
    if fvg_age is not None and fvg_age > settings.telegram_max_fvg_age and not strong_compensation:
        return f"IFVG/FVG age {fvg_age} is above maximum {settings.telegram_max_fvg_age}"
    tp3_eta = signal.targets[-1].estimated_minutes if signal.targets else 0
    if tp3_eta > settings.telegram_max_tp3_eta_minutes:
        return f"TP3 ETA {tp3_eta} min is above maximum {settings.telegram_max_tp3_eta_minutes} min"
    if (signal.risk_reward or 0.0) < 2.0:
        return "risk/reward is below 2.00R"
    if settings.future_mode == "ON":
        future = evaluate_future_gate(signal, learning_report)
        if not future.allowed:
            return future.summary
    return None


def _historical_defensive_block_reason(signal: TradeSignal) -> str | None:
    label = signal.historical_bucket_label or ""
    expectancy = signal.historical_expectancy_r
    win_rate = signal.historical_win_rate
    net_r = signal.historical_net_r
    if signal.sample_size >= 2 and label.startswith(f"Exact {signal.symbol} {signal.side.value}"):
        weak_win_rate = win_rate is None or win_rate <= 50.0
        win_rate_text = f"{win_rate:.1f}%" if win_rate is not None else "n/a"
        if expectancy is not None and expectancy <= -0.20 and weak_win_rate:
            return (
                f"exact symbol-side historical context is negative: {label}, "
                f"{signal.sample_size} samples, WR {win_rate_text}"
                f", expectancy {expectancy:+.2f}R"
            )
        if net_r is not None and net_r <= -1.50 and weak_win_rate:
            return (
                f"exact symbol-side historical net R is negative: {label}, "
                f"{signal.sample_size} samples, net {net_r:+.2f}R"
            )
    if signal.historical_is_calibrated and expectancy is not None and expectancy < 0:
        return (
            f"calibrated historical bucket is negative: {label}, "
            f"{signal.sample_size} samples, expectancy {expectancy:+.2f}R"
        )
    return None


def _long_direction_defensive_block_reason(
    signal: TradeSignal,
    settings: Settings,
    learning_report: LearningReport | None,
) -> str | None:
    if signal.side != SignalSide.LONG:
        return None
    if learning_report is None:
        return None
    if learning_report.valid_bot_assisted_sample_count < settings.learning_min_sample_size:
        return None
    fvg_age = _evidence_age(signal, "IFVG / FVG")
    if fvg_age is not None and fvg_age <= 2:
        return "LONG defensive gate: IFVG/FVG age <=2 is not currently profitable enough"
    if fvg_age is not None and fvg_age > 10:
        return "LONG defensive gate: IFVG/FVG age >10 is not currently profitable enough"
    if signal.market_regime in {"balanced", "high_volatility_expansion"}:
        return f"LONG defensive gate: market regime {signal.market_regime} is not currently profitable enough"
    return None


def _apply_liquidation_result(signal: TradeSignal, result: LiquidationRiskResult) -> None:
    signal.liquidation_check_status = result.status
    signal.liquidation_price = result.liquidation_price
    signal.liquidation_safety_buffer = result.safety_buffer
    signal.liquidation_check_reason = result.reason
    signal.account_risk_validated = result.account_risk_validated
    if result.status == LiquidationCheckStatus.FAILED and result.downgrade_state is not None:
        signal.state = result.downgrade_state
        signal.alert_eligible = False
        signal.alert_reason = f"{result.downgrade_state.value}: {result.reason}"


def _evidence_passed(signal: TradeSignal, name: str) -> bool:
    item = _evidence(signal, name)
    return item is not None and item.status == "passed"


def _volume_percentile(signal: TradeSignal) -> float:
    item = _evidence(signal, "Volume participation")
    if item is None:
        return 0.0
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)%", item.detail)
    return float(match.group(1)) if match else 0.0


def _evidence_age(signal: TradeSignal, name: str) -> int | None:
    item = _evidence(signal, name)
    if item is None:
        return None
    match = re.search(r"(?:age|swept|displacement)\s+([0-9]+)", item.detail, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _evidence(signal: TradeSignal, name: str):
    for item in signal.evidence:
        if item.name == name:
            return item
    return None


def _freshness_bonus(age: int | None, fresh_bonus: float, stale_penalty: float, stale_after: int) -> float:
    if age is None:
        return 0.0
    if age <= 3:
        return fresh_bonus
    if age >= stale_after:
        return stale_penalty
    return max(0.0, fresh_bonus * (1 - (age - 3) / max(1, stale_after - 3)))


def _signal_conditions(signal: TradeSignal) -> set[str]:
    conditions: set[str] = set()
    if not _evidence_passed(signal, "Manipulation sweep"):
        conditions.add("no manipulation sweep")
    if _volume_percentile(signal) < 60:
        conditions.add("low volume")
    fvg_age = _evidence_age(signal, "IFVG / FVG")
    if fvg_age is not None and fvg_age > 20:
        conditions.add("stale FVG/IFVG")
    displacement_age = _evidence_age(signal, "Displacement")
    if displacement_age is not None and displacement_age > 5:
        conditions.add("old displacement")
    if signal.targets and signal.targets[-1].estimated_minutes > 240:
        conditions.add("TP3 ETA too long")
    if signal.liquidation_check_status == LiquidationCheckStatus.NOT_VALIDATED:
        conditions.add("liquidation not validated")
    if signal.liquidation_check_status == LiquidationCheckStatus.FAILED:
        conditions.add("liquidation failed")
    if not _evidence_passed(signal, "Higher timeframe alignment"):
        conditions.add("HTF misalignment")
    if signal.execution_quality_status not in {"PASSED", "NOT_VALIDATED"}:
        conditions.add("weak execution quality")
    if signal.market_regime in {"quiet_chop", "wide_atr_risk"}:
        conditions.add(f"market regime {signal.market_regime}")
    if signal.market_spread_pct is not None and signal.market_spread_pct > 0.25:
        conditions.add("wide market spread")
    return conditions


def _symbol_in_cooldown(symbol: str, learning_report: LearningReport) -> bool:
    prefix = f"{symbol} "
    return any(item == symbol or item.startswith(prefix) for item in learning_report.active_symbol_cooldowns)


def _cooldown_item_for_signal(signal: TradeSignal, items: list[str]) -> str | None:
    exact = f"{signal.symbol} {signal.side.value}"
    prefix = f"{exact} "
    return next(
        (item for item in items if item == exact or item.startswith(prefix)),
        None,
    )


def _alert_rank(signal: TradeSignal) -> int:
    match = re.search(r"#(\d+)", signal.alert_reason)
    return int(match.group(1)) if match else 999
