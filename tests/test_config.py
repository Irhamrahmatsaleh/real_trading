from pathlib import Path

from app.config import Settings


def test_env_example_documents_expected_source_of_truth():
    env_example = Path(".env.example").read_text()
    assert "TELEGRAM_BOT_TOKEN" in env_example
    assert "TELEGRAM_CHAT_ID" in env_example
    assert "TELEGRAM_SIGNAL_THREAD_ID" in env_example
    assert "TELEGRAM_TPSL_THREAD_ID" in env_example
    assert "BYBIT_API_KEY" in env_example
    assert "BYBIT_API_SECRET" in env_example
    assert "BYBIT_ENV=live" in env_example
    assert "TOP_MARKETS=150" in env_example
    assert "MIN_SCAN_MARKETS=150" in env_example
    assert "MAX_SCAN_MARKETS=300" in env_example
    assert "SCAN_EXPANSION_STEP=50" in env_example
    assert "SWITCH_ML=OFF" in env_example
    assert "FUTURE_MODE=OFF" in env_example
    assert "BYBIT_RECV_WINDOW_MS=10000" in env_example
    assert "MAX_TRADE_ALERTS_PER_SCAN=2" in env_example
    assert "MIN_LIQUIDATION_SL_BUFFER_R=0.20" in env_example
    assert "LEARNING_ENABLED=true" in env_example
    assert "LEARNING_MIN_SAMPLE_SIZE=50" in env_example
    assert "IMMEDIATE_LOSS_COOLDOWN_HOURS=4" in env_example
    assert "RECENT_SIGNAL_COOLDOWN_MINUTES=120" in env_example
    assert "TELEGRAM_MIN_CONFLUENCE_SCORE=75" in env_example
    assert "TELEGRAM_MIN_BEST_SCORE=90" in env_example
    assert "MAX_MARKET_SPREAD_PCT=0.25" in env_example
    assert "MAX_STOP_ATR_MULTIPLE=2.2" in env_example
    assert "MAX_STOP_DISTANCE_PCT=3.0" in env_example


def test_settings_default_to_env_example_market_count(monkeypatch):
    monkeypatch.delenv("TOP_MARKETS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.top_markets == 150
    assert settings.min_scan_markets == 150
    assert settings.max_scan_markets == 300
    assert settings.scan_expansion_step == 50
    assert settings.min_ready_candidates == 1
    assert settings.min_manual_candidates == 3
    assert settings.switch_ml == "OFF"
    assert settings.future_mode == "OFF"
    assert settings.bybit_recv_window_ms == 5000
    assert settings.bybit_env == "live"
    assert settings.bybit_base_url == "https://api.bybit.com"
    assert settings.max_trade_alerts_per_scan == 2
    assert settings.min_liquidation_sl_buffer_r == 0.20
    assert settings.learning_enabled is True
    assert settings.immediate_loss_cooldown_hours == 4
    assert settings.recent_signal_cooldown_minutes == 120
    assert settings.telegram_min_confluence_score == 75
    assert settings.max_stop_atr_multiple == 2.2
    assert settings.max_stop_distance_pct == 3.0
    assert settings.max_market_spread_pct == 0.25
    assert settings.telegram_signal_thread_id is None
    assert settings.telegram_tpsl_thread_id is None


def test_bybit_env_demo_uses_demo_base_url():
    settings = Settings(_env_file=None, BYBIT_ENV="demo")

    assert settings.bybit_env == "demo"
    assert settings.bybit_base_url == "https://api-demo.bybit.com"


def test_bybit_recv_window_can_be_configured():
    settings = Settings(_env_file=None, BYBIT_RECV_WINDOW_MS=10000)

    assert settings.bybit_recv_window_ms == 10000


def test_future_mode_can_be_enabled():
    settings = Settings(_env_file=None, FUTURE_MODE="ON")

    assert settings.future_mode == "ON"


def test_telegram_thread_ids_are_optional_and_parsed():
    blank = Settings(_env_file=None, TELEGRAM_SIGNAL_THREAD_ID="", TELEGRAM_TPSL_THREAD_ID="")
    configured = Settings(_env_file=None, TELEGRAM_SIGNAL_THREAD_ID="44", TELEGRAM_TPSL_THREAD_ID="53")

    assert blank.telegram_signal_thread_id is None
    assert blank.telegram_tpsl_thread_id is None
    assert configured.telegram_signal_thread_id == 44
    assert configured.telegram_tpsl_thread_id == 53
