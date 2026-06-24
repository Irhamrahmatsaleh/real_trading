from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class SignalState(StrEnum):
    TRADE_READY = "TRADE_READY"
    WATCH = "WATCH"
    WAIT = "WAIT"
    AVOID = "AVOID"


class SignalSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class LiquidationCheckStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_VALIDATED = "NOT_VALIDATED"


class Candle(BaseModel):
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


class MarketTicker(BaseModel):
    symbol: str
    last_price: float
    turnover24h: float
    volume24h: float
    price24h_pct: float
    bid1_price: float | None = None
    ask1_price: float | None = None
    spread_pct: float | None = None


class PositionSnapshot(BaseModel):
    symbol: str
    side: SignalSide
    size: float
    avg_price: float | None = None
    mark_price: float | None = None
    liquidation_price: float | None = None


class ClosedPnlRecord(BaseModel):
    record_id: str
    symbol: str
    side: SignalSide | None
    qty: float
    avg_entry_price: float | None = None
    avg_exit_price: float | None = None
    closed_pnl: float
    open_fee: float = 0.0
    close_fee: float = 0.0
    created_at: datetime
    updated_at: datetime


class TransactionLogRecord(BaseModel):
    record_id: str
    symbol: str | None = None
    transaction_type: str
    cash_flow: float = 0.0
    funding: float = 0.0
    fee: float = 0.0
    change: float = 0.0
    created_at: datetime


class ExecutionRecord(BaseModel):
    record_id: str
    symbol: str
    side: SignalSide | None
    exec_price: float | None = None
    exec_qty: float = 0.0
    exec_fee: float = 0.0
    exec_type: str = ""
    order_id: str | None = None
    created_at: datetime


class CoinBalance(BaseModel):
    coin: str
    equity: float | None = None
    usd_value: float | None = None
    wallet_balance: float | None = None
    available_to_withdraw: float | None = None
    unrealised_pnl: float | None = None
    cum_realised_pnl: float | None = None


class AccountSnapshot(BaseModel):
    bybit_env: str = "live"
    account_type: str = "UNIFIED"
    fetched_at: datetime | None = None
    keys_configured: bool = False
    total_equity: float | None = None
    total_wallet_balance: float | None = None
    total_margin_balance: float | None = None
    total_available_balance: float | None = None
    total_perp_upl: float | None = None
    total_initial_margin: float | None = None
    total_maintenance_margin: float | None = None
    coins: list[CoinBalance] = Field(default_factory=list)
    error: str | None = None


class OutcomeBucket(BaseModel):
    match_confidence: str
    count: int = 0
    realized_pnl: float = 0.0
    fee: float = 0.0
    funding: float = 0.0
    net_pnl: float = 0.0


class RecentOutcome(BaseModel):
    closed_at: str
    symbol: str
    side: str | None = None
    actual_entry: float | None = None
    actual_exit: float | None = None
    qty: float | None = None
    net_pnl: float = 0.0
    result_r: float | None = None
    outcome_type: str
    match_confidence: str


class OutcomeNotification(BaseModel):
    outcome_id: str
    symbol: str
    side: str | None = None
    closed_at: str
    entry_low: float | None = None
    entry_high: float | None = None
    actual_entry: float | None = None
    actual_exit: float | None = None
    stop_loss: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    tp3: float | None = None
    qty: float | None = None
    net_pnl: float = 0.0
    result_r: float | None = None
    outcome_type: str
    match_confidence: str
    signal_generated_at: str | None = None


class JournalOutcomeSummary(BaseModel):
    total_outcomes: int = 0
    valid_learning_samples: int = 0
    matched_count: int = 0
    manual_adjusted_count: int = 0
    uncertain_count: int = 0
    buckets: list[OutcomeBucket] = Field(default_factory=list)
    recent: list[RecentOutcome] = Field(default_factory=list)


class TargetPlan(BaseModel):
    label: str
    price: float
    distance_pct: float
    estimated_minutes: int
    timing_basis: str


class StrategyEvidence(BaseModel):
    name: str
    status: str
    detail: str
    score: int = Field(ge=0, le=100)


class TradeSignal(BaseModel):
    symbol: str
    side: SignalSide
    state: SignalState
    score: int = Field(ge=0, le=100)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data_source: str = "Bybit V5 public market data"
    entry_low: float | None = None
    entry_high: float | None = None
    stop_loss: float | None = None
    targets: list[TargetPlan] = Field(default_factory=list)
    risk_reward: float | None = None
    invalidation_condition: str
    confidence_explanation: str
    probability_label: str = "Probability: Not Calibrated"
    sample_size: int = 0
    statistics_summary: str = "No verified historical outcome bucket is available yet; score is confluence-based, not a win-rate claim."
    historical_bucket_label: str | None = None
    historical_win_rate: float | None = None
    historical_expectancy_r: float | None = None
    historical_net_r: float | None = None
    historical_profit_factor: float | None = None
    historical_is_calibrated: bool = False
    evidence: list[StrategyEvidence] = Field(default_factory=list)
    alert_eligible: bool = False
    alert_reason: str
    execution_quality_status: str = "NOT_VALIDATED"
    execution_quality_reason: str = "Execution quality is not validated yet."
    market_regime: str = "unknown"
    market_spread_pct: float | None = None
    post_signal_revalidation: str = "NOT_VALIDATED"
    missing_data: list[str] = Field(default_factory=list)
    liquidation_check_status: LiquidationCheckStatus = LiquidationCheckStatus.NOT_VALIDATED
    liquidation_price: float | None = None
    liquidation_safety_buffer: float | None = None
    liquidation_check_reason: str = (
        "Account liquidation not validated until position/leverage/size exists. "
        "Use leverage and position size so liquidation remains beyond SL."
    )
    account_risk_validated: bool = False

    @property
    def entry_label(self) -> str:
        if self.entry_low is None or self.entry_high is None:
            return "No manual entry zone"
        return f"{self.entry_low:.6g} - {self.entry_high:.6g}"


class LearningReport(BaseModel):
    learning_enabled: bool = True
    bybit_env: str = "live"
    journal_signal_count: int = 0
    matched_trade_count: int = 0
    valid_bot_assisted_sample_count: int = 0
    all_time_net_r: float = 0.0
    all_time_win_rate: float | None = None
    all_time_expectancy_r: float | None = None
    all_time_profit_factor: float | None = None
    today_net_pnl: float = 0.0
    today_net_r: float = 0.0
    today_win_rate: float | None = None
    average_win_r: float | None = None
    average_loss_r: float | None = None
    profit_factor: float | None = None
    sample_warning: str = "No verified matched outcomes yet; probability remains uncalibrated."
    active_symbol_cooldowns: list[str] = Field(default_factory=list)
    active_fresh_loss_cooldowns: list[str] = Field(default_factory=list)
    active_recent_signal_cooldowns: list[str] = Field(default_factory=list)
    adaptive_penalty_summary: str = "No adaptive penalties are active."
    condition_penalties: dict[str, float] = Field(default_factory=dict)
    ml_status: str = "SWITCH_ML=OFF; rule-based quant filters active."
    best_symbols: list[str] = Field(default_factory=list)
    worst_symbols: list[str] = Field(default_factory=list)
    best_evidence_combinations: list[str] = Field(default_factory=list)
    worst_evidence_combinations: list[str] = Field(default_factory=list)
    loss_patterns: list[str] = Field(default_factory=list)


class ScanStatus(BaseModel):
    running: bool = False
    last_scan_at: datetime | None = None
    next_scan_hint_seconds: int
    top_markets: int
    initial_scan_markets: int = 0
    max_scan_markets: int = 0
    scan_market_limit: int = 0
    scan_expanded: bool = False
    scan_expansion_reason: str = "Adaptive scan has not run yet."
    symbols_scanned: int = 0
    trade_ready_count: int = 0
    watch_count: int = 0
    wait_count: int = 0
    avoid_count: int = 0
    telegram_configured: bool
    bybit_keys_configured: bool
    telegram_status: str = "not_configured"
    best_signal_symbol: str | None = None
    best_signal_side: SignalSide | None = None
    best_signal_score: float | None = None
    best_signal_selection_reason: str | None = None
    learning_enabled: bool = True
    bybit_env: str = "live"
    journal_signal_count: int = 0
    matched_trade_count: int = 0
    valid_bot_assisted_sample_count: int = 0
    all_time_expectancy_r: float | None = None
    all_time_profit_factor: float | None = None
    today_net_pnl: float = 0.0
    today_net_r: float = 0.0
    today_win_rate: float | None = None
    sample_warning: str = "No verified matched outcomes yet; probability remains uncalibrated."
    active_symbol_cooldowns: list[str] = Field(default_factory=list)
    active_fresh_loss_cooldowns: list[str] = Field(default_factory=list)
    active_recent_signal_cooldowns: list[str] = Field(default_factory=list)
    telegram_best_signal_blocked_reason: str | None = None
    adaptive_penalty_summary: str = "No adaptive penalties are active."
    ml_status: str = "SWITCH_ML=OFF; rule-based quant filters active."
    data_source: str = "Bybit V5 public market data"
    error: str | None = None
