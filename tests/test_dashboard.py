import asyncio

from starlette.requests import Request

from app.models import (
    AccountSnapshot,
    JournalOutcomeSummary,
    LearningReport,
    SignalSide,
    SignalState,
    TargetPlan,
    TradeSignal,
)
from app.main import service, templates
from app.main import favicon


def test_dashboard_template_renders_with_current_starlette_signature():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "scheme": "http",
    }
    request = Request(scope)
    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        {"status": service.status, "signals": [], "max_alerts": 1, "bybit_env": "demo"},
    )
    assert response.status_code == 200


def test_favicon_route_returns_icon_response():
    response = asyncio.run(favicon())

    assert response.status_code == 200
    assert response.media_type == "image/svg+xml"
    assert b"<svg" in response.body


def test_dashboard_distinguishes_telegram_sent_from_watchlist():
    request = _request("/")
    status = service.status.model_copy(update={"telegram_status": "sent_best:DASHUSDT"})
    sent = _signal("DASHUSDT", True)
    watchlist = _signal("WAITUSDT", False)

    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        {"status": status, "signals": [sent, watchlist], "max_alerts": 1, "bybit_env": "demo"},
    )
    body = response.body.decode()

    assert "Account Analysis" in body
    assert "Dashboard shows top 3 TRADE_READY candidates" in body
    assert "Telegram Sent" in body
    assert "Watchlist / Not Sent" in body
    assert "Confluence score" in body


def test_account_template_renders_read_only_account_and_journal_sections():
    request = _request("/account-analysis")
    account = AccountSnapshot(bybit_env="demo", keys_configured=False, error="not configured")
    learning = LearningReport(learning_enabled=True, bybit_env="demo")
    outcomes = JournalOutcomeSummary(matched_count=1, manual_adjusted_count=1, uncertain_count=2, valid_learning_samples=2)

    response = templates.TemplateResponse(
        request,
        "account.html",
        {"status": service.status, "account": account, "learning": learning, "outcomes": outcomes},
    )
    body = response.body.decode()

    assert response.status_code == 200
    assert "Account Analysis" in body
    assert "DEMO" in body
    assert "Valid learning samples" in body
    assert "matched + manual_adjusted" in body
    assert "No automatic order execution" in body


def _request(path: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "scheme": "http",
    }
    return Request(scope)


def _signal(symbol: str, alert_eligible: bool) -> TradeSignal:
    return TradeSignal(
        symbol=symbol,
        side=SignalSide.LONG,
        state=SignalState.TRADE_READY,
        score=80,
        entry_low=100,
        entry_high=101,
        stop_loss=98,
        targets=[
            TargetPlan(label="TP1", price=103, distance_pct=2, estimated_minutes=30, timing_basis="ATR"),
            TargetPlan(label="TP2", price=106, distance_pct=5, estimated_minutes=60, timing_basis="ATR"),
            TargetPlan(label="TP3", price=110, distance_pct=9, estimated_minutes=90, timing_basis="ATR"),
        ],
        risk_reward=2.8,
        invalidation_condition="Invalid below SL.",
        confidence_explanation="Rule-based confluence score, not a guaranteed win rate.",
        alert_eligible=alert_eligible,
        alert_reason="BEST_TRADE_READY #1 selected for Telegram: test." if alert_eligible else "Not selected for Telegram.",
    )
