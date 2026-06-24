import asyncio
from datetime import datetime, timezone

from app.bybit import BybitMarketClient
from app.bybit import _position_side_from_closed_pnl
from app.config import Settings
from app.models import SignalSide


def test_closed_pnl_infers_short_when_close_buy_is_profitable_below_entry():
    side = _position_side_from_closed_pnl(
        {
            "side": "Buy",
            "avgEntryPrice": "0.15884",
            "avgExitPrice": "0.1566",
            "closedPnl": "1.3888",
        }
    )

    assert side == SignalSide.SHORT


def test_closed_pnl_infers_long_when_close_sell_is_profitable_above_entry():
    side = _position_side_from_closed_pnl(
        {
            "side": "Sell",
            "avgEntryPrice": "100",
            "avgExitPrice": "104",
            "closedPnl": "4",
        }
    )

    assert side == SignalSide.LONG


def test_wallet_balance_parser_keeps_demo_environment_and_usdt_totals():
    async def run():
        settings = Settings(
            _env_file=None,
            BYBIT_ENV="demo",
            BYBIT_API_KEY="key",
            BYBIT_API_SECRET="secret",
        )
        client = BybitMarketClient(settings)

        async def fake_signed_get(path, params):
            assert path == "/v5/account/wallet-balance"
            assert params == {"accountType": "UNIFIED", "coin": "USDT"}
            return {
                "list": [
                    {
                        "accountType": "UNIFIED",
                        "totalEquity": "155.0887",
                        "totalWalletBalance": "154.9500",
                        "totalMarginBalance": "155.0100",
                        "totalAvailableBalance": "140.0000",
                        "totalPerpUPL": "0.0600",
                        "totalInitialMargin": "12.0000",
                        "totalMaintenanceMargin": "1.5000",
                        "coin": [
                            {
                                "coin": "USDT",
                                "equity": "155.0887",
                                "usdValue": "155.0887",
                                "walletBalance": "154.9500",
                                "availableToWithdraw": "140.0000",
                                "unrealisedPnl": "0.0600",
                                "cumRealisedPnl": "1.2500",
                            }
                        ],
                    }
                ]
            }

        client._signed_get = fake_signed_get
        try:
            snapshot = await client.get_wallet_balance("USDT")
        finally:
            await client.close()

        assert snapshot.bybit_env == "demo"
        assert snapshot.total_equity == 155.0887
        assert snapshot.total_available_balance == 140.0
        assert snapshot.total_perp_upl == 0.06
        assert snapshot.coins[0].coin == "USDT"
        assert snapshot.coins[0].cum_realised_pnl == 1.25

    asyncio.run(run())


def test_signed_request_uses_configured_recv_window(monkeypatch):
    async def run():
        settings = Settings(
            _env_file=None,
            BYBIT_API_KEY="key",
            BYBIT_API_SECRET="secret",
            BYBIT_RECV_WINDOW_MS=10000,
        )
        client = BybitMarketClient(settings)
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"retCode": 0, "result": {"ok": True}}

        async def fake_get(url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

        monkeypatch.setattr(client._client, "get", fake_get)
        try:
            result = await client._signed_get("/v5/private/test", {"symbol": "BTCUSDT"})
        finally:
            await client.close()

        assert result == {"ok": True}
        assert captured["url"] == "/v5/private/test?symbol=BTCUSDT"
        assert captured["headers"]["X-BAPI-RECV-WINDOW"] == "10000"

    asyncio.run(run())


def test_kline_window_uses_start_and_end_parameters():
    async def run():
        settings = Settings(_env_file=None)
        client = BybitMarketClient(settings)
        start = datetime(2026, 6, 22, 10, tzinfo=timezone.utc)
        end = datetime(2026, 6, 22, 12, tzinfo=timezone.utc)

        async def fake_get(path, params):
            assert path == "/v5/market/kline"
            assert params["category"] == "linear"
            assert params["symbol"] == "BSBUSDT"
            assert params["interval"] == "15"
            assert params["start"] == int(start.timestamp() * 1000)
            assert params["end"] == int(end.timestamp() * 1000)
            assert params["limit"] == 50
            return {
                "list": [
                    [
                        str(int(start.timestamp() * 1000)),
                        "1",
                        "2",
                        "0.5",
                        "1.5",
                        "100",
                        "150",
                    ]
                ]
            }

        client._get = fake_get
        try:
            candles = await client.get_closed_klines_window("BSBUSDT", "15", start, end, limit=50)
        finally:
            await client.close()

        assert candles[0].open_time == start
        assert candles[0].open == 1
        assert candles[0].close == 1.5

    asyncio.run(run())
