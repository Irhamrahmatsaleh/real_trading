from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field("", alias="TELEGRAM_CHAT_ID")
    telegram_signal_thread_id: int | None = Field(None, alias="TELEGRAM_SIGNAL_THREAD_ID")
    telegram_tpsl_thread_id: int | None = Field(None, alias="TELEGRAM_TPSL_THREAD_ID")
    bybit_api_key: str = Field("", alias="BYBIT_API_KEY")
    bybit_api_secret: str = Field("", alias="BYBIT_API_SECRET")
    bybit_env: Literal["live", "demo"] = Field("live", alias="BYBIT_ENV")
    bybit_base_url_override: str = Field("", alias="BYBIT_BASE_URL")
    top_markets: int = Field(150, alias="TOP_MARKETS")
    min_scan_markets: int = Field(150, alias="MIN_SCAN_MARKETS")
    max_scan_markets: int = Field(300, alias="MAX_SCAN_MARKETS")
    scan_expansion_step: int = Field(50, alias="SCAN_EXPANSION_STEP")
    min_ready_candidates: int = Field(1, alias="MIN_READY_CANDIDATES")
    min_manual_candidates: int = Field(3, alias="MIN_MANUAL_CANDIDATES")
    switch_ml: Literal["OFF", "ON"] = Field("OFF", alias="SWITCH_ML")
    future_mode: Literal["OFF", "ON"] = Field("OFF", alias="FUTURE_MODE")

    scan_interval_seconds: int = 300
    kline_interval: str = "15"
    higher_timeframe_interval: str = "60"
    kline_limit: int = 220
    request_timeout_seconds: float = 12.0
    max_concurrent_requests: int = 8
    bybit_recv_window_ms: int = Field(5000, alias="BYBIT_RECV_WINDOW_MS")
    max_trade_alerts_per_scan: int = Field(2, alias="MAX_TRADE_ALERTS_PER_SCAN")
    min_liquidation_sl_buffer_r: float = Field(0.20, alias="MIN_LIQUIDATION_SL_BUFFER_R")
    learning_enabled: bool = Field(True, alias="LEARNING_ENABLED")
    learning_db_path: str = Field("data/trading_journal.sqlite3", alias="LEARNING_DB_PATH")
    learning_min_sample_size: int = Field(50, alias="LEARNING_MIN_SAMPLE_SIZE")
    outcome_match_window_hours: int = Field(24, alias="OUTCOME_MATCH_WINDOW_HOURS")
    symbol_cooldown_loss_count: int = Field(3, alias="SYMBOL_COOLDOWN_LOSS_COUNT")
    symbol_cooldown_hours: int = Field(8, alias="SYMBOL_COOLDOWN_HOURS")
    immediate_loss_cooldown_hours: int = Field(4, alias="IMMEDIATE_LOSS_COOLDOWN_HOURS")
    recent_signal_cooldown_minutes: int = Field(120, alias="RECENT_SIGNAL_COOLDOWN_MINUTES")
    telegram_min_confluence_score: float = Field(75.0, alias="TELEGRAM_MIN_CONFLUENCE_SCORE")
    telegram_min_best_score: float = Field(90.0, alias="TELEGRAM_MIN_BEST_SCORE")
    telegram_min_volume_percentile: float = Field(45.0, alias="TELEGRAM_MIN_VOLUME_PERCENTILE")
    max_market_spread_pct: float = Field(0.25, alias="MAX_MARKET_SPREAD_PCT")
    telegram_max_fvg_age: int = Field(36, alias="TELEGRAM_MAX_FVG_AGE")
    telegram_max_tp3_eta_minutes: int = Field(300, alias="TELEGRAM_MAX_TP3_ETA_MINUTES")
    telegram_require_liquidation_passed: bool = Field(False, alias="TELEGRAM_REQUIRE_LIQUIDATION_PASSED")
    max_stop_atr_multiple: float = Field(2.2, alias="MAX_STOP_ATR_MULTIPLE")
    max_stop_distance_pct: float = Field(3.0, alias="MAX_STOP_DISTANCE_PCT")

    @property
    def bybit_base_url(self) -> str:
        override = self.bybit_base_url_override.strip().rstrip("/")
        if override:
            return override
        if self.bybit_env == "demo":
            return "https://api-demo.bybit.com"
        return "https://api.bybit.com"

    @field_validator("top_markets", "min_scan_markets", "max_scan_markets", "scan_expansion_step")
    @classmethod
    def validate_market_counts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("market scan counts must be at least 1")
        return value

    @field_validator("min_ready_candidates", "min_manual_candidates")
    @classmethod
    def validate_candidate_counts(cls, value: int) -> int:
        if value < 0:
            raise ValueError("candidate counts must be zero or greater")
        return value

    @model_validator(mode="after")
    def validate_scan_range(self) -> "Settings":
        initial = max(self.top_markets, self.min_scan_markets)
        if self.max_scan_markets < initial:
            raise ValueError("MAX_SCAN_MARKETS must be at least TOP_MARKETS/MIN_SCAN_MARKETS")
        return self

    @field_validator("max_trade_alerts_per_scan")
    @classmethod
    def validate_max_trade_alerts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("MAX_TRADE_ALERTS_PER_SCAN must be at least 1")
        return value

    @field_validator("bybit_recv_window_ms")
    @classmethod
    def validate_bybit_recv_window(cls, value: int) -> int:
        if value < 1:
            raise ValueError("BYBIT_RECV_WINDOW_MS must be at least 1")
        return value

    @field_validator("min_liquidation_sl_buffer_r")
    @classmethod
    def validate_min_liquidation_buffer(cls, value: float) -> float:
        if value < 0:
            raise ValueError("MIN_LIQUIDATION_SL_BUFFER_R must be zero or greater")
        return value

    @field_validator(
        "learning_min_sample_size",
        "outcome_match_window_hours",
        "symbol_cooldown_loss_count",
        "symbol_cooldown_hours",
        "immediate_loss_cooldown_hours",
        "recent_signal_cooldown_minutes",
    )
    @classmethod
    def validate_positive_ints(cls, value: int) -> int:
        if value < 1:
            raise ValueError("learning and cooldown numeric settings must be at least 1")
        return value

    @field_validator(
        "telegram_min_confluence_score",
        "telegram_min_best_score",
        "telegram_min_volume_percentile",
        "max_market_spread_pct",
    )
    @classmethod
    def validate_non_negative_float(cls, value: float) -> float:
        if value < 0:
            raise ValueError("Telegram quality thresholds must be zero or greater")
        return value

    @field_validator("max_stop_atr_multiple", "max_stop_distance_pct")
    @classmethod
    def validate_positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("Risk geometry thresholds must be greater than zero")
        return value

    @field_validator("telegram_max_fvg_age", "telegram_max_tp3_eta_minutes")
    @classmethod
    def validate_positive_thresholds(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Telegram maximum thresholds must be at least 1")
        return value

    @field_validator("telegram_signal_thread_id", "telegram_tpsl_thread_id", mode="before")
    @classmethod
    def validate_optional_thread_id(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def telegram_configured(self) -> bool:
        return _looks_configured(self.telegram_bot_token) and _looks_configured(self.telegram_chat_id)

    @property
    def bybit_keys_configured(self) -> bool:
        return _looks_configured(self.bybit_api_key) and _looks_configured(self.bybit_api_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _looks_configured(value: str) -> bool:
    cleaned = value.strip().strip('"').strip("'")
    if not cleaned:
        return False
    lowered = cleaned.lower()
    placeholders = ("isi_", "your_", "changeme", "replace_me", "example")
    return not any(token in lowered for token in placeholders)
