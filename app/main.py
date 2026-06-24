from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.models import SignalState
from app.service import TradingAnalysisService
from app.telegram import format_signal_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

settings = get_settings()
service = TradingAnalysisService(settings)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#0b1117"/><path d="M16 42V20h14c7 0 11 4 11 10 0 4-2 7-6 9l7 11H31l-6-10h-1v10H16Zm8-17v9h5c3 0 5-2 5-5s-2-4-5-4h-5Z" fill="#21d07a"/><path d="M43 20h6v22h-6z" fill="#f5f7fb"/></svg>"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    await service.start()
    try:
        yield
    finally:
        await service.stop()


app = FastAPI(
    title="Real Trading Manual Signal Bot",
    description="Manual real-trading decision support using real Bybit market data. No automatic order execution.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    trade_ready = [signal for signal in service.signals if signal.state == SignalState.TRADE_READY][:3]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "status": service.status,
            "signals": trade_ready,
            "max_alerts": settings.max_trade_alerts_per_scan,
            "bybit_env": settings.bybit_env,
        },
    )


@app.get("/account-analysis", response_class=HTMLResponse)
async def account_analysis(request: Request) -> HTMLResponse:
    account = await service.account_snapshot()
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "status": service.status,
            "learning": service.learning_report,
            "account": account,
            "outcomes": service.journal_outcome_summary(),
        },
    )


@app.get("/api/status")
async def api_status():
    return service.status


@app.get("/api/signals")
async def api_signals(limit: int = 50):
    return service.signals[: max(1, min(limit, 200))]


@app.get("/api/learning")
async def api_learning():
    return service.learning_report


@app.get("/api/account")
async def api_account():
    return {
        "account": await service.account_snapshot(),
        "learning": service.learning_report,
        "outcomes": service.journal_outcome_summary(),
    }


@app.post("/api/scan")
async def api_scan():
    signals = await service.scan_once()
    return {"status": service.status, "signals": signals[:50]}


@app.get("/api/telegram/dry-run")
async def api_telegram_dry_run():
    signal = next((item for item in service.signals if item.alert_eligible), None)
    if signal is None:
        signals = await service.scan_once()
        signal = next((item for item in signals if item.alert_eligible), None)
    if signal is None:
        return {"message": "No Telegram-selected real market signal is available yet."}
    return {"telegram_configured": settings.telegram_configured, "message": format_signal_message(signal)}
