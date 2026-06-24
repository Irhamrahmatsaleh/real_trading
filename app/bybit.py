from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.models import (
    AccountSnapshot,
    Candle,
    CoinBalance,
    ClosedPnlRecord,
    ExecutionRecord,
    MarketTicker,
    PositionSnapshot,
    SignalSide,
    TransactionLogRecord,
)


def interval_to_minutes(interval: str) -> int:
    mapping = {"D": 1440, "W": 10080, "M": 43200}
    if interval in mapping:
        return mapping[interval]
    return int(interval)


class BybitMarketClient:
    """Read-only Bybit V5 market-data client."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.bybit_base_url,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": "real-trading-manual-signal-bot/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
        return payload.get("result", {})

    async def _signed_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.bybit_keys_configured:
            raise RuntimeError("Bybit API keys are not configured")
        clean_params = {key: value for key, value in params.items() if value is not None}
        query_string = urlencode(clean_params)
        timestamp = str(int(time.time() * 1000))
        recv_window = str(self.settings.bybit_recv_window_ms)
        raw_signature = f"{timestamp}{self.settings.bybit_api_key}{recv_window}{query_string}"
        signature = hmac.new(
            self.settings.bybit_api_secret.encode(),
            raw_signature.encode(),
            hashlib.sha256,
        ).hexdigest()
        response = await self._client.get(
            f"{path}?{query_string}",
            headers={
                "X-BAPI-API-KEY": self.settings.bybit_api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "X-BAPI-SIGN": signature,
                "X-BAPI-SIGN-TYPE": "2",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')}")
        return payload.get("result", {})

    async def get_top_linear_usdt_tickers(self, limit: int) -> list[MarketTicker]:
        result = await self._get("/v5/market/tickers", {"category": "linear"})
        tickers: list[MarketTicker] = []
        for raw in result.get("list", []):
            symbol = str(raw.get("symbol", ""))
            if not symbol.endswith("USDT"):
                continue
            last_price = _safe_float(raw.get("lastPrice"))
            turnover = _safe_float(raw.get("turnover24h"))
            volume = _safe_float(raw.get("volume24h"))
            pct = _safe_float(raw.get("price24hPcnt")) * 100.0
            bid = _optional_float(raw.get("bid1Price"))
            ask = _optional_float(raw.get("ask1Price"))
            spread_pct = _spread_pct(bid, ask)
            if last_price <= 0 or turnover <= 0:
                continue
            tickers.append(
                MarketTicker(
                    symbol=symbol,
                    last_price=last_price,
                    turnover24h=turnover,
                    volume24h=volume,
                    price24h_pct=pct,
                    bid1_price=bid,
                    ask1_price=ask,
                    spread_pct=spread_pct,
                )
            )
        tickers.sort(key=lambda item: item.turnover24h, reverse=True)
        return tickers[:limit]

    async def get_closed_klines(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        result = await self._get(
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
        )
        candles = _parse_kline_rows(result.get("list", []))
        candles.sort(key=lambda item: item.open_time)
        return _drop_unclosed_candle(candles, interval)

    async def get_closed_klines_window(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int = 200,
    ) -> list[Candle]:
        result = await self._get(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "start": _to_millis(start),
                "end": _to_millis(end),
                "limit": limit,
            },
        )
        candles = _parse_kline_rows(result.get("list", []))
        candles.sort(key=lambda item: item.open_time)
        return _drop_unclosed_candle(candles, interval)

    async def get_linear_position(self, symbol: str, side: SignalSide) -> PositionSnapshot | None:
        bybit_side = "Buy" if side == SignalSide.LONG else "Sell" if side == SignalSide.SHORT else ""
        if not bybit_side:
            return None
        result = await self._signed_get("/v5/position/list", {"category": "linear", "symbol": symbol})
        for raw in result.get("list", []):
            if str(raw.get("symbol", "")) != symbol or str(raw.get("side", "")) != bybit_side:
                continue
            size = _safe_float(raw.get("size"))
            if size <= 0:
                continue
            return PositionSnapshot(
                symbol=symbol,
                side=side,
                size=size,
                avg_price=_optional_float(raw.get("avgPrice")),
                mark_price=_optional_float(raw.get("markPrice")),
                liquidation_price=_optional_float(raw.get("liqPrice")),
            )
        return None

    async def get_wallet_balance(self, coin: str = "USDT") -> AccountSnapshot:
        result = await self._signed_get(
            "/v5/account/wallet-balance",
            {
                "accountType": "UNIFIED",
                "coin": coin,
            },
        )
        accounts = result.get("list", [])
        raw_account = accounts[0] if accounts else {}
        coins: list[CoinBalance] = []
        for raw in raw_account.get("coin", []):
            coin_name = str(raw.get("coin") or "")
            if not coin_name:
                continue
            coins.append(
                CoinBalance(
                    coin=coin_name,
                    equity=_nullable_float(raw.get("equity")),
                    usd_value=_nullable_float(raw.get("usdValue")),
                    wallet_balance=_nullable_float(raw.get("walletBalance")),
                    available_to_withdraw=_nullable_float(raw.get("availableToWithdraw")),
                    unrealised_pnl=_nullable_float(raw.get("unrealisedPnl")),
                    cum_realised_pnl=_nullable_float(raw.get("cumRealisedPnl")),
                )
            )
        return AccountSnapshot(
            bybit_env=self.settings.bybit_env,
            account_type=str(raw_account.get("accountType") or "UNIFIED"),
            fetched_at=datetime.now(timezone.utc),
            keys_configured=True,
            total_equity=_nullable_float(raw_account.get("totalEquity")),
            total_wallet_balance=_nullable_float(raw_account.get("totalWalletBalance")),
            total_margin_balance=_nullable_float(raw_account.get("totalMarginBalance")),
            total_available_balance=_nullable_float(raw_account.get("totalAvailableBalance")),
            total_perp_upl=_nullable_float(raw_account.get("totalPerpUPL")),
            total_initial_margin=_nullable_float(raw_account.get("totalInitialMargin")),
            total_maintenance_margin=_nullable_float(raw_account.get("totalMaintenanceMargin")),
            coins=coins,
        )

    async def get_closed_pnl(
        self,
        start: datetime,
        end: datetime,
        limit: int = 100,
    ) -> list[ClosedPnlRecord]:
        result = await self._signed_get(
            "/v5/position/closed-pnl",
            {
                "category": "linear",
                "startTime": _to_millis(start),
                "endTime": _to_millis(end),
                "limit": limit,
            },
        )
        records: list[ClosedPnlRecord] = []
        for raw in result.get("list", []):
            symbol = str(raw.get("symbol", ""))
            if not symbol.endswith("USDT"):
                continue
            updated_at = _millis_to_datetime(raw.get("updatedTime") or raw.get("createdTime"))
            created_at = _millis_to_datetime(raw.get("createdTime") or raw.get("updatedTime"))
            record_id = (
                str(raw.get("orderId") or raw.get("closedPnlId") or "")
                or f"{symbol}:{raw.get('updatedTime')}:{raw.get('avgEntryPrice')}:{raw.get('avgExitPrice')}"
            )
            records.append(
                ClosedPnlRecord(
                    record_id=record_id,
                    symbol=symbol,
                    side=_position_side_from_closed_pnl(raw),
                    qty=_safe_float(raw.get("qty") or raw.get("closedSize")),
                    avg_entry_price=_optional_float(raw.get("avgEntryPrice")),
                    avg_exit_price=_optional_float(raw.get("avgExitPrice")),
                    closed_pnl=_safe_float(raw.get("closedPnl")),
                    open_fee=abs(_safe_float(raw.get("openFee"))),
                    close_fee=abs(_safe_float(raw.get("closeFee"))),
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        return records

    async def get_transaction_logs(
        self,
        start: datetime,
        end: datetime,
        limit: int = 100,
    ) -> list[TransactionLogRecord]:
        result = await self._signed_get(
            "/v5/account/transaction-log",
            {
                "accountType": "UNIFIED",
                "category": "linear",
                "currency": "USDT",
                "startTime": _to_millis(start),
                "endTime": _to_millis(end),
                "limit": limit,
            },
        )
        records: list[TransactionLogRecord] = []
        for raw in result.get("list", []):
            created_at = _millis_to_datetime(raw.get("transactionTime") or raw.get("createdTime"))
            record_id = str(raw.get("id") or raw.get("transactionId") or f"{raw.get('symbol')}:{created_at.isoformat()}")
            transaction_type = str(raw.get("type") or raw.get("transactionType") or "")
            funding = _safe_float(raw.get("funding"))
            if transaction_type.upper() == "SETTLEMENT":
                funding = funding or _safe_float(raw.get("change"))
            records.append(
                TransactionLogRecord(
                    record_id=record_id,
                    symbol=str(raw.get("symbol") or "") or None,
                    transaction_type=transaction_type,
                    cash_flow=_safe_float(raw.get("cashFlow")),
                    funding=funding,
                    fee=abs(_safe_float(raw.get("fee"))),
                    change=_safe_float(raw.get("change")),
                    created_at=created_at,
                )
            )
        return records

    async def get_executions(
        self,
        start: datetime,
        end: datetime,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        result = await self._signed_get(
            "/v5/execution/list",
            {
                "category": "linear",
                "startTime": _to_millis(start),
                "endTime": _to_millis(end),
                "limit": limit,
            },
        )
        records: list[ExecutionRecord] = []
        for raw in result.get("list", []):
            symbol = str(raw.get("symbol", ""))
            if not symbol.endswith("USDT"):
                continue
            created_at = _millis_to_datetime(raw.get("execTime"))
            record_id = str(raw.get("execId") or f"{raw.get('orderId')}:{created_at.isoformat()}")
            records.append(
                ExecutionRecord(
                    record_id=record_id,
                    symbol=symbol,
                    side=_side_from_bybit(raw.get("side")),
                    exec_price=_optional_float(raw.get("execPrice")),
                    exec_qty=_safe_float(raw.get("execQty")),
                    exec_fee=abs(_safe_float(raw.get("execFee"))),
                    exec_type=str(raw.get("execType") or ""),
                    order_id=str(raw.get("orderId") or "") or None,
                    created_at=created_at,
                )
            )
        return records


def _parse_kline_rows(rows: list[Any]) -> list[Candle]:
    candles: list[Candle] = []
    for row in rows:
        if len(row) < 7:
            continue
        open_time = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
        candles.append(
            Candle(
                open_time=open_time,
                open=_safe_float(row[1]),
                high=_safe_float(row[2]),
                low=_safe_float(row[3]),
                close=_safe_float(row[4]),
                volume=_safe_float(row[5]),
                turnover=_safe_float(row[6]),
            )
        )
    return candles


def _drop_unclosed_candle(candles: list[Candle], interval: str) -> list[Candle]:
    if not candles:
        return candles
    interval_delta = timedelta(minutes=interval_to_minutes(interval))
    now = datetime.now(timezone.utc)
    if candles[-1].open_time + interval_delta > now:
        return candles[:-1]
    return candles


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nullable_float(value: Any) -> float | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _spread_pct(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    midpoint = (bid + ask) / 2
    if midpoint <= 0:
        return None
    return (ask - bid) / midpoint * 100.0


def _to_millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _millis_to_datetime(value: Any) -> datetime:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        millis = int(time.time() * 1000)
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)


def _side_from_bybit(value: Any) -> SignalSide | None:
    side = str(value or "").lower()
    if side == "buy":
        return SignalSide.LONG
    if side == "sell":
        return SignalSide.SHORT
    return None


def _position_side_from_closed_pnl(raw: dict[str, Any]) -> SignalSide | None:
    entry = _optional_float(raw.get("avgEntryPrice"))
    exit_price = _optional_float(raw.get("avgExitPrice"))
    pnl = _safe_float(raw.get("closedPnl"))
    if entry is not None and exit_price is not None and pnl != 0:
        price_delta = exit_price - entry
        if price_delta * pnl > 0:
            return SignalSide.LONG
        if price_delta * pnl < 0:
            return SignalSide.SHORT
    return _side_from_bybit(raw.get("side"))
