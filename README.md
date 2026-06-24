# Real Trading Manual Signal Bot

Manual real-trading decision support for Bybit markets. The application analyzes real public market data and produces professional manual trade signals. It never places, amends, cancels, closes, or auto-executes orders.

## Configuration

Copy `.env.example` to `.env` and fill the values:

```env
TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
BYBIT_ENV=live
BYBIT_API_KEY=isi_api_key_lo_disini
BYBIT_API_SECRET=isi_api_secret_lo_disini
TOP_MARKETS=150
MIN_SCAN_MARKETS=150
MAX_SCAN_MARKETS=300
SCAN_EXPANSION_STEP=50
MIN_READY_CANDIDATES=1
MIN_MANUAL_CANDIDATES=3
SWITCH_ML=OFF
MAX_TRADE_ALERTS_PER_SCAN=1
MIN_LIQUIDATION_SL_BUFFER_R=0.25
LEARNING_ENABLED=true
LEARNING_DB_PATH=data/trading_journal.sqlite3
LEARNING_MIN_SAMPLE_SIZE=50
OUTCOME_MATCH_WINDOW_HOURS=24
SYMBOL_COOLDOWN_LOSS_COUNT=2
SYMBOL_COOLDOWN_HOURS=12
IMMEDIATE_LOSS_COOLDOWN_HOURS=4
RECENT_SIGNAL_COOLDOWN_MINUTES=60
TELEGRAM_MIN_CONFLUENCE_SCORE=75
TELEGRAM_MIN_BEST_SCORE=95
TELEGRAM_MIN_VOLUME_PERCENTILE=50
MAX_MARKET_SPREAD_PCT=0.25
TELEGRAM_MAX_FVG_AGE=24
TELEGRAM_MAX_TP3_ETA_MINUTES=300
TELEGRAM_REQUIRE_LIQUIDATION_PASSED=false
MAX_STOP_ATR_MULTIPLE=2.2
MAX_STOP_DISTANCE_PCT=3.0
```

`BYBIT_ENV=demo` uses Bybit Demo Trading REST data. Use a read-only demo API key for learning. The application only calls read-only market, position, closed PnL, and transaction-log endpoints.
`TOP_MARKETS`/`MIN_SCAN_MARKETS` control the initial high-turnover USDT perpetual market scan.
`MAX_SCAN_MARKETS` caps adaptive expansion; the scanner can expand in `SCAN_EXPANSION_STEP` batches when ready/manual candidates are too sparse.
`SWITCH_ML=OFF` keeps the production path rule-based. `SWITCH_ML=ON` is a future local-ML hook and must fall back to rule-based filters when samples/models are insufficient.
`MAX_MARKET_SPREAD_PCT` blocks Telegram selection when bid/ask spread data is available and too wide.
`MAX_TRADE_ALERTS_PER_SCAN` defaults to `1`; Telegram sends only the single best `TRADE_READY` candidate selected by composite quality.
`MIN_LIQUIDATION_SL_BUFFER_R` requires liquidation to sit beyond SL by at least this fraction of the entry-to-SL risk when an open matching position is available.
`MAX_STOP_ATR_MULTIPLE` and `MAX_STOP_DISTANCE_PCT` cap planned SL distance so stale 40-candle structure cannot create unrealistic TP/SL plans. If TP3 ETA exceeds `TELEGRAM_MAX_TP3_ETA_MINUTES`, the setup is kept out of `TRADE_READY`.
`IMMEDIATE_LOSS_COOLDOWN_HOURS` blocks the same symbol+side from Telegram after any fresh negative `matched` or `manual_adjusted` outcome, without waiting for the statistical sample threshold. `RECENT_SIGNAL_COOLDOWN_MINUTES` prevents repeat Telegram alerts for the same symbol+side across process restarts. `TELEGRAM_MIN_CONFLUENCE_SCORE` is the raw confluence floor before composite ranking is considered.
Learning stores Telegram-eligible signals and matched read-only closed outcomes in SQLite. Probabilities remain uncalibrated until the journal has enough verified outcomes.

## Run

```bash
make serve
```

The service keeps running until terminated with `Ctrl+C`.

## Verify

```bash
make test
```

The tests check environment loading, manual-only safety boundaries, signal state behavior, TP/SL formatting, and Telegram message content.
