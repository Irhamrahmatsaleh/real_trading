from __future__ import annotations

import html

import httpx

from app.config import Settings
from app.models import OutcomeNotification, SignalState, TradeSignal


def format_signal_message(signal: TradeSignal) -> str:
    title_side = signal.side.value if signal.side else "NEUTRAL"
    lines = [
        f"<b>{html.escape(signal.symbol)} {html.escape(title_side)} Manual Trading Signal</b>",
        "Manual trading signal only. No automatic order was placed.",
        f"State: <b>{signal.state.value}</b> | Score: {signal.score}/100",
        f"Generated: {signal.generated_at.isoformat()}",
        f"Data source: {html.escape(signal.data_source)}",
        "",
    ]

    if signal.state == SignalState.TRADE_READY:
        lines.extend(
            [
                f"Entry zone: <b>{signal.entry_label}</b>",
                f"SL: <b>{signal.stop_loss:.6g}</b>" if signal.stop_loss is not None else "SL: unavailable",
                f"Risk/Reward: {signal.risk_reward:.2f}R" if signal.risk_reward is not None else "Risk/Reward: unavailable",
            ]
        )
        for target in signal.targets:
            lines.append(
                f"{target.label}: <b>{target.price:.6g}</b> | ETA: ~{target.estimated_minutes} min | Distance: {target.distance_pct:.2f}%"
            )
        lines.append("")
    else:
        lines.append(f"Reason: {html.escape(signal.alert_reason)}")
        if signal.missing_data:
            lines.append(f"Missing data: {html.escape(', '.join(signal.missing_data))}")
        lines.append("")

    lines.extend(
        [
            f"Liquidation Check: <b>{signal.liquidation_check_status.value.replace('_', ' ')}</b>",
            f"Liquidation Price: {signal.liquidation_price:.6g}" if signal.liquidation_price is not None else "Liquidation Price: not available",
            f"Liquidation Buffer: {signal.liquidation_safety_buffer:.6g}" if signal.liquidation_safety_buffer is not None else "Liquidation Buffer: not available",
            f"Account Risk: {'validated' if signal.account_risk_validated else 'not validated'}",
            f"Liquidation Reason: {html.escape(signal.liquidation_check_reason)}",
            "",
            html.escape(signal.probability_label),
            f"Sample size: {signal.sample_size}",
            f"Statistics: {html.escape(signal.statistics_summary)}",
            "",
            "Evidence:",
        ]
    )
    for item in signal.evidence:
        lines.append(
            f"- {html.escape(item.name)}: {html.escape(item.status)} ({item.score}) - {html.escape(item.detail)}"
        )
    lines.extend(
        [
            "",
            f"Alert eligibility: {html.escape(signal.alert_reason)}",
            f"Invalidation: {html.escape(signal.invalidation_condition)}",
        ]
    )
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def send_signal(self, signal: TradeSignal) -> str:
        if not self.settings.telegram_configured:
            return "not_configured"
        if not signal.alert_eligible:
            return "skipped_not_alert_eligible"
        message = format_signal_message(signal)
        payload = build_send_message_payload(self.settings, message, self.settings.telegram_signal_thread_id)
        return await self._send_payload(payload)

    async def send_outcome(self, outcome: OutcomeNotification, chart_png: bytes | None = None) -> str:
        if not self.settings.telegram_configured or self.settings.telegram_tpsl_thread_id is None:
            return "not_configured"
        message = format_outcome_message(outcome)
        payload = build_send_message_payload(self.settings, message, self.settings.telegram_tpsl_thread_id)
        return await self._send_payload(payload)

    async def _send_payload(self, payload: dict[str, object]) -> str:
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                json=payload,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                return f"telegram_error: {payload}"
        return "sent"

    async def _send_photo(self, chart_png: bytes, caption: str, thread_id: int | None) -> str:
        data = build_send_photo_payload(self.settings, caption, thread_id)
        files = {"photo": ("outcome-chart.png", chart_png, "image/png")}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendPhoto",
                    data=data,
                    files=files,
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    return f"telegram_error: {payload}"
        except httpx.HTTPError as exc:
            return f"telegram_photo_error: {exc}"
        return "sent"


def format_outcome_message(outcome: OutcomeNotification) -> str:
    side = outcome.side or "UNKNOWN"
    title = f"{outcome.symbol} {side} {_outcome_title(outcome)}"
    pnl_label = f"{outcome.net_pnl:+.6g} USDT"
    r_label = f"{outcome.result_r:+.4f}R" if outcome.result_r is not None else "n/a"
    lines = [
        f"<b>{html.escape(title)}</b>",
        "Closed manual trade matched to a bot Telegram signal.",
        f"Match: <b>{html.escape(outcome.match_confidence)}</b>",
        f"Outcome: <b>{html.escape(outcome.outcome_type)}</b>",
        f"Entry: <b>{_format_optional_price(outcome.actual_entry)}</b>",
        f"Exit: <b>{_format_optional_price(outcome.actual_exit)}</b>",
        f"Qty: {_format_optional_qty(outcome.qty)}",
        f"Net PnL: <b>{html.escape(pnl_label)}</b>",
        f"Result R: <b>{html.escape(r_label)}</b>",
        f"Closed: {html.escape(outcome.closed_at)}",
    ]
    if outcome.signal_generated_at:
        lines.append(f"Signal generated: {html.escape(outcome.signal_generated_at)}")
    lines.append("No automatic order was placed by the bot.")
    return "\n".join(lines)


def format_outcome_photo_caption(outcome: OutcomeNotification) -> str:
    side = outcome.side or "UNKNOWN"
    title = f"{outcome.symbol} {side} {_outcome_title(outcome)}"
    pnl_label = f"{outcome.net_pnl:+.6g} USDT"
    r_label = f"{outcome.result_r:+.4f}R" if outcome.result_r is not None else "n/a"
    lines = [
        f"<b>{html.escape(title)}</b>",
        "Real Bybit closed-candle outcome chart.",
        f"Match: <b>{html.escape(outcome.match_confidence)}</b> | Outcome: <b>{html.escape(outcome.outcome_type)}</b>",
        f"Entry: <b>{_format_optional_price(outcome.actual_entry)}</b> | Exit: <b>{_format_optional_price(outcome.actual_exit)}</b>",
        f"Net PnL: <b>{html.escape(pnl_label)}</b> | Result R: <b>{html.escape(r_label)}</b>",
        f"Closed: {html.escape(outcome.closed_at)}",
        "Manual trade only; no automatic order was placed by the bot.",
    ]
    return "\n".join(lines)


def build_send_message_payload(settings: Settings, message: str, thread_id: int | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "chat_id": settings.telegram_chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    return payload


def build_send_photo_payload(settings: Settings, caption: str, thread_id: int | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "chat_id": settings.telegram_chat_id,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if thread_id is not None:
        payload["message_thread_id"] = str(thread_id)
    return payload


def _outcome_title(outcome: OutcomeNotification) -> str:
    if outcome.outcome_type in {"TP1", "TP2", "TP3"}:
        return f"{outcome.outcome_type} HIT"
    if outcome.outcome_type == "SL":
        return "LOSS / SL"
    if outcome.net_pnl > 0:
        return "PROFIT CLOSED"
    if outcome.net_pnl < 0:
        return "LOSS CLOSED"
    return "CLOSED"


def _format_optional_price(value: float | None) -> str:
    return f"{value:.6g}" if value is not None else "n/a"


def _format_optional_qty(value: float | None) -> str:
    return f"{value:.6g}" if value is not None else "n/a"
