from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from statistics import mean

from app.bybit import interval_to_minutes
from app.indicators import atr, average_abs_close_move, percentile_rank, recent_high, recent_low, sma
from app.models import (
    Candle,
    MarketTicker,
    SignalSide,
    SignalState,
    StrategyEvidence,
    TargetPlan,
    TradeSignal,
)


@dataclass(frozen=True)
class DirectionCandidate:
    side: SignalSide
    score: int
    entry_low: float
    entry_high: float
    stop_loss: float
    targets: list[TargetPlan]
    risk_reward: float
    invalidation_condition: str
    evidence: list[StrategyEvidence]
    confidence_explanation: str
    critical_count: int
    core_confirmed: bool
    directional_context_confirmed: bool
    risk_geometry_ok: bool
    timing_ok: bool
    execution_quality_status: str = "NOT_VALIDATED"
    execution_quality_reason: str = "Execution quality is not validated yet."
    market_regime: str = "unknown"
    market_spread_pct: float | None = None
    post_signal_revalidation: str = "NOT_VALIDATED"


@dataclass(frozen=True)
class StopPlan:
    stop_loss: float
    structural_stop: float
    risk: float
    structural_risk: float
    max_risk: float
    capped: bool
    detail: str


class StrategyAnalyzer:
    def __init__(
        self,
        interval: str = "15",
        max_tp3_eta_minutes: int = 300,
        max_stop_atr_multiple: float = 2.2,
        max_stop_distance_pct: float = 3.0,
        max_market_spread_pct: float = 0.25,
    ):
        self.interval = interval
        self.max_tp3_eta_minutes = max_tp3_eta_minutes
        self.max_stop_atr_multiple = max_stop_atr_multiple
        self.max_stop_distance_pct = max_stop_distance_pct
        self.max_market_spread_pct = max_market_spread_pct

    def analyze(self, ticker: MarketTicker, candles: list[Candle], htf_candles: list[Candle]) -> TradeSignal:
        missing = self._missing_data(candles, htf_candles)
        if missing:
            return TradeSignal(
                symbol=ticker.symbol,
                side=SignalSide.NEUTRAL,
                state=SignalState.WAIT,
                score=0,
                invalidation_condition="No manual trade because the analyzer does not have enough closed candles.",
                confidence_explanation="Insufficient real market data for market-structure, volatility, and IFVG validation.",
                evidence=[
                    StrategyEvidence(
                        name="Data sufficiency",
                        status="missing",
                        detail=", ".join(missing),
                        score=0,
                    )
                ],
                alert_eligible=False,
                alert_reason="WAIT: missing required real market data.",
                missing_data=missing,
            )

        current_atr = atr(candles) or 0.0
        close = candles[-1].close
        if current_atr <= 0 or close <= 0:
            return TradeSignal(
                symbol=ticker.symbol,
                side=SignalSide.NEUTRAL,
                state=SignalState.AVOID,
                score=0,
                invalidation_condition="No manual trade because volatility or price data is invalid.",
                confidence_explanation="ATR/price validation failed on real market candles.",
                evidence=[
                    StrategyEvidence(
                        name="Volatility validation",
                        status="invalid",
                        detail="ATR or latest close is not usable.",
                        score=0,
                    )
                ],
                alert_eligible=False,
                alert_reason="AVOID: invalid volatility or price data.",
            )

        context = self._context(candles, htf_candles, current_atr)
        long_candidate = self._candidate(SignalSide.LONG, ticker, candles, htf_candles, context)
        short_candidate = self._candidate(SignalSide.SHORT, ticker, candles, htf_candles, context)
        best = max([long_candidate, short_candidate], key=lambda candidate: candidate.score)
        state = self._state_for(best)

        alert_eligible = state == SignalState.TRADE_READY
        if alert_eligible:
            alert_reason = "TRADE_READY: confluence score, critical evidence, and risk/reward passed manual-signal gates."
        elif state == SignalState.WATCH:
            alert_reason = "WATCH: promising setup, but at least one critical confirmation is not strong enough."
        elif state == SignalState.WAIT:
            alert_reason = "WAIT: market structure is incomplete or timing is not clean enough."
        else:
            alert_reason = "AVOID: current conditions do not justify a manual trade."

        return TradeSignal(
            symbol=ticker.symbol,
            side=best.side if state != SignalState.AVOID else SignalSide.NEUTRAL,
            state=state,
            score=best.score,
            entry_low=best.entry_low if state != SignalState.AVOID else None,
            entry_high=best.entry_high if state != SignalState.AVOID else None,
            stop_loss=best.stop_loss if state != SignalState.AVOID else None,
            targets=best.targets if state != SignalState.AVOID else [],
            risk_reward=best.risk_reward if state != SignalState.AVOID else None,
            invalidation_condition=best.invalidation_condition,
            confidence_explanation=best.confidence_explanation,
            evidence=best.evidence,
            alert_eligible=alert_eligible,
            alert_reason=alert_reason,
            execution_quality_status=best.execution_quality_status,
            execution_quality_reason=best.execution_quality_reason,
            market_regime=best.market_regime,
            market_spread_pct=best.market_spread_pct,
            post_signal_revalidation=best.post_signal_revalidation,
        )

    def _missing_data(self, candles: list[Candle], htf_candles: list[Candle]) -> list[str]:
        missing: list[str] = []
        if len(candles) < 80:
            missing.append("at least 80 closed lower-timeframe candles")
        if len(htf_candles) < 40:
            missing.append("at least 40 closed higher-timeframe candles")
        return missing

    def _context(self, candles: list[Candle], htf_candles: list[Candle], current_atr: float) -> dict[str, object]:
        closes = [candle.close for candle in candles]
        htf_closes = [candle.close for candle in htf_candles]
        volumes = [candle.volume for candle in candles]
        latest = candles[-1]
        prior_high = recent_high(candles, 40) or latest.high
        prior_low = recent_low(candles, 40) or latest.low
        sma20 = sma(closes, 20) or latest.close
        sma50 = sma(closes, 50) or latest.close
        htf_sma20 = sma(htf_closes, 20) or htf_candles[-1].close
        htf_sma40 = sma(htf_closes, 40) or htf_candles[-1].close
        ranges = [candle.high - candle.low for candle in candles[-40:]]
        prior_ranges = ranges[:-10] if len(ranges) > 10 else ranges
        last_ranges = [candle.high - candle.low for candle in candles[-10:]]
        volume_rank = percentile_rank(volumes[-60:-1], latest.volume)
        range_rank = percentile_rank(ranges[:-1], ranges[-1] if ranges else 0)
        accumulation = (
            mean(last_ranges) <= (mean(prior_ranges) * 0.9 if prior_ranges else current_atr)
            and abs(latest.close - sma20) <= current_atr * 1.25
        )
        displacement = self._recent_displacement(candles, current_atr)
        low_sweep = self._recent_liquidity_sweep(candles, "low")
        high_sweep = self._recent_liquidity_sweep(candles, "high")
        fvg = self._ifvg_context(candles, current_atr)
        trend = "balanced"
        if latest.close > sma20 > sma50 and htf_candles[-1].close > htf_sma20 > htf_sma40:
            trend = "bullish"
        elif latest.close < sma20 < sma50 and htf_candles[-1].close < htf_sma20 < htf_sma40:
            trend = "bearish"

        market_regime = self._market_regime(
            trend=trend,
            accumulation=accumulation,
            range_rank=range_rank,
            volume_rank=volume_rank,
            atr_value=current_atr,
            price=latest.close,
        )

        return {
            "atr": current_atr,
            "avg_move": average_abs_close_move(candles) or current_atr,
            "prior_high": prior_high,
            "prior_low": prior_low,
            "sma20": sma20,
            "sma50": sma50,
            "htf_sma20": htf_sma20,
            "htf_sma40": htf_sma40,
            "trend": trend,
            "accumulation": accumulation,
            "bullish_displacement": bool(displacement["bullish"]),
            "bearish_displacement": bool(displacement["bearish"]),
            "displacement_detail": str(displacement["detail"]),
            "swept_low": bool(low_sweep["passed"]),
            "swept_high": bool(high_sweep["passed"]),
            "low_sweep_detail": str(low_sweep["detail"]),
            "high_sweep_detail": str(high_sweep["detail"]),
            "volume_rank": volume_rank,
            "range_rank": range_rank,
            "ifvg": fvg,
            "market_regime": market_regime,
        }

    def _market_regime(
        self,
        *,
        trend: str,
        accumulation: bool,
        range_rank: float,
        volume_rank: float,
        atr_value: float,
        price: float,
    ) -> str:
        atr_pct = (atr_value / price * 100.0) if price > 0 else 0.0
        if range_rank >= 0.90 and volume_rank >= 0.80:
            return "high_volatility_expansion"
        if accumulation:
            return "compression_near_value"
        if trend in {"bullish", "bearish"}:
            return f"{trend}_trend"
        if range_rank <= 0.25 and volume_rank <= 0.35:
            return "quiet_chop"
        if atr_pct >= 4.0:
            return "wide_atr_risk"
        return "balanced"

    def _recent_displacement(self, candles: list[Candle], atr_value: float, lookback: int = 5) -> dict[str, object]:
        bullish = False
        bearish = False
        detail = "No recent displacement candle cleared the ATR/body threshold."
        for offset, candle in enumerate(reversed(candles[-lookback:]), start=1):
            body = abs(candle.close - candle.open)
            candle_range = candle.high - candle.low
            if body >= atr_value * 0.55 and candle_range >= atr_value * 0.75:
                if candle.close > candle.open:
                    bullish = True
                    detail = f"Bullish displacement {offset} candle(s) ago with body {body:.6g}."
                    break
                if candle.close < candle.open:
                    bearish = True
                    detail = f"Bearish displacement {offset} candle(s) ago with body {body:.6g}."
                    break
        return {"bullish": bullish, "bearish": bearish, "detail": detail}

    def _recent_liquidity_sweep(self, candles: list[Candle], direction: str, lookback: int = 10) -> dict[str, object]:
        start_index = max(40, len(candles) - lookback)
        for index in range(len(candles) - 1, start_index - 1, -1):
            previous = candles[max(0, index - 40) : index]
            if len(previous) < 20:
                continue
            candle = candles[index]
            if direction == "low":
                level = min(item.low for item in previous)
                if candle.low < level and candle.close > level:
                    age = len(candles) - 1 - index
                    return {
                        "passed": True,
                        "detail": f"Sell-side liquidity swept {age} candle(s) ago; reclaimed {level:.6g}.",
                    }
            else:
                level = max(item.high for item in previous)
                if candle.high > level and candle.close < level:
                    age = len(candles) - 1 - index
                    return {
                        "passed": True,
                        "detail": f"Buy-side liquidity swept {age} candle(s) ago; rejected {level:.6g}.",
                    }
        label = "sell-side" if direction == "low" else "buy-side"
        return {"passed": False, "detail": f"No recent {label} liquidity sweep with reclaim/rejection."}

    def _ifvg_context(self, candles: list[Candle], atr_value: float) -> dict[str, object]:
        gaps: list[dict[str, object]] = []
        start = max(2, len(candles) - 35)
        for index in range(start, len(candles)):
            left = candles[index - 2]
            current = candles[index]
            if current.low > left.high:
                gaps.append(
                    {
                        "direction": "bullish",
                        "low": left.high,
                        "high": current.low,
                        "mid": (left.high + current.low) / 2,
                        "age": len(candles) - 1 - index,
                    }
                )
            elif current.high < left.low:
                gaps.append(
                    {
                        "direction": "bearish",
                        "low": current.high,
                        "high": left.low,
                        "mid": (current.high + left.low) / 2,
                        "age": len(candles) - 1 - index,
                    }
                )
        if not gaps:
            return {
                "direction": "none",
                "quality": 0,
                "long_ok": False,
                "short_ok": False,
                "long_detail": "No recent bullish IFVG/FVG support.",
                "short_detail": "No recent bearish IFVG/FVG resistance.",
                "detail": "No recent fair value gap or inverted fair value gap.",
            }
        price = candles[-1].close
        long_detail = "No recent bullish IFVG/FVG support."
        short_detail = "No recent bearish IFVG/FVG resistance."
        long_quality = 0
        short_quality = 0
        for gap in reversed(gaps):
            low = float(gap["low"])
            high = float(gap["high"])
            direction = str(gap["direction"])
            age = int(gap["age"])
            near_gap = min(abs(price - low), abs(price - high)) <= atr_value * 1.5
            inside = low <= price <= high
            if direction == "bullish" and (inside or (price > high and near_gap)):
                quality = 80 if inside else 68
                if quality > long_quality:
                    long_quality = quality
                    long_detail = f"Bullish FVG support age {age}: {low:.6g} - {high:.6g}."
            if direction == "bearish" and price > high and near_gap:
                quality = 78
                if quality > long_quality:
                    long_quality = quality
                    long_detail = f"Bearish FVG inverted into bullish support age {age}: {low:.6g} - {high:.6g}."
            if direction == "bearish" and (inside or (price < low and near_gap)):
                quality = 80 if inside else 68
                if quality > short_quality:
                    short_quality = quality
                    short_detail = f"Bearish FVG resistance age {age}: {low:.6g} - {high:.6g}."
            if direction == "bullish" and price < low and near_gap:
                quality = 78
                if quality > short_quality:
                    short_quality = quality
                    short_detail = f"Bullish FVG inverted into bearish resistance age {age}: {low:.6g} - {high:.6g}."
        latest_gap = gaps[-1]
        price = candles[-1].close
        low = float(latest_gap["low"])
        high = float(latest_gap["high"])
        direction = str(latest_gap["direction"])
        inside = low <= price <= high
        inverted = (direction == "bullish" and price < low) or (direction == "bearish" and price > high)
        quality = 80 if inside else 65 if inverted else 45
        label = "IFVG retest" if inside else "inverted FVG" if inverted else "unfilled FVG context"
        return {
            "direction": direction,
            "quality": max(quality, long_quality, short_quality),
            "long_ok": long_quality >= 65,
            "short_ok": short_quality >= 65,
            "long_detail": long_detail,
            "short_detail": short_detail,
            "low": low,
            "high": high,
            "detail": f"{label}: {low:.6g} - {high:.6g}",
        }

    def _candidate(
        self,
        side: SignalSide,
        ticker: MarketTicker,
        candles: list[Candle],
        htf_candles: list[Candle],
        context: dict[str, object],
    ) -> DirectionCandidate:
        latest = candles[-1]
        atr_value = float(context["atr"])
        price = latest.close
        ifvg = context["ifvg"]
        score = 0
        critical_count = 0
        evidence: list[StrategyEvidence] = []

        trend = str(context["trend"])
        if side == SignalSide.LONG:
            trend_ok = trend == "bullish"
            sweep_ok = bool(context["swept_low"])
            displacement_ok = bool(context["bullish_displacement"])
            ifvg_ok = bool(ifvg.get("long_ok"))
            htf_ok = htf_candles[-1].close >= float(context["htf_sma20"])
            entry_low = price - atr_value * 0.18
            entry_high = price + atr_value * 0.08
            structural_sl = min(float(context["prior_low"]), latest.low) - atr_value * 0.25
            stop_plan = self._stop_plan(side, entry_low, entry_high, structural_sl, atr_value)
            stop_loss = stop_plan.stop_loss
            invalidation = "Manual long idea is invalid if price breaks the planned SL level."
        else:
            trend_ok = trend == "bearish"
            sweep_ok = bool(context["swept_high"])
            displacement_ok = bool(context["bearish_displacement"])
            ifvg_ok = bool(ifvg.get("short_ok"))
            htf_ok = htf_candles[-1].close <= float(context["htf_sma20"])
            entry_low = price - atr_value * 0.08
            entry_high = price + atr_value * 0.18
            structural_sl = max(float(context["prior_high"]), latest.high) + atr_value * 0.25
            stop_plan = self._stop_plan(side, entry_low, entry_high, structural_sl, atr_value)
            stop_loss = stop_plan.stop_loss
            invalidation = "Manual short idea is invalid if price breaks the planned SL level."

        score += self._add_evidence(evidence, "HTF/LTF structure", trend_ok, 16, f"Trend regime is {trend}.")
        score += self._add_evidence(evidence, "Accumulation", bool(context["accumulation"]), 10, "Recent range compression near value.")
        sweep_detail = str(context["low_sweep_detail"] if side == SignalSide.LONG else context["high_sweep_detail"])
        ifvg_detail = str(ifvg.get("long_detail" if side == SignalSide.LONG else "short_detail", ifvg.get("detail", "No IFVG detail.")))
        score += self._add_evidence(evidence, "Manipulation sweep", sweep_ok, 20, sweep_detail)
        score += self._add_evidence(evidence, "Displacement", displacement_ok, 18, str(context["displacement_detail"]))
        score += self._add_evidence(evidence, "IFVG / FVG", ifvg_ok, 18, ifvg_detail)
        score += self._add_evidence(evidence, "Volume participation", float(context["volume_rank"]) >= 0.55, 10, f"Volume percentile: {float(context['volume_rank']) * 100:.1f}%.")
        htf_detail = (
            "Higher timeframe close is aligned with the manual direction."
            if htf_ok
            else "Higher timeframe close is not aligned with the manual direction."
        )
        score += self._add_evidence(evidence, "Higher timeframe alignment", htf_ok, 10, htf_detail)

        critical_count += int(sweep_ok) + int(displacement_ok) + int(ifvg_ok) + int(htf_ok)
        risk_reward, targets = self._targets(side, entry_low, entry_high, stop_loss, candles, context, ticker)
        risk_geometry_ok = stop_plan.risk > 0 and stop_plan.risk <= stop_plan.max_risk
        self._add_evidence(evidence, "Risk geometry", risk_geometry_ok, 0, stop_plan.detail)
        if risk_geometry_ok:
            critical_count += 1
        rr_ok = risk_reward >= 2.0
        self._add_evidence(evidence, "Risk/reward", rr_ok, 0, f"TP3 risk/reward is {risk_reward:.2f}R.")
        if rr_ok:
            critical_count += 1
        tp3_eta = targets[-1].estimated_minutes if targets else 0
        timing_ok = tp3_eta <= self.max_tp3_eta_minutes
        timing_detail = (
            f"TP3 ETA is ~{tp3_eta} min; maximum ready-signal ETA is {self.max_tp3_eta_minutes} min."
        )
        self._add_evidence(evidence, "TP timing", timing_ok, 0, timing_detail)
        if timing_ok:
            critical_count += 1

        regime = str(context["market_regime"])
        regime_ok = regime not in {"quiet_chop", "wide_atr_risk"}
        self._add_evidence(evidence, "Market regime", regime_ok, 0, f"Detected regime: {regime}.")
        spread_ok = ticker.spread_pct is None or ticker.spread_pct <= self.max_market_spread_pct
        spread_detail = (
            "Ticker bid/ask spread is unavailable; using turnover and volume filters."
            if ticker.spread_pct is None
            else f"Bid/ask spread is {ticker.spread_pct:.4f}% with max {self.max_market_spread_pct:.4f}%."
        )
        self._add_evidence(evidence, "Liquidity/spread", spread_ok, 0, spread_detail)
        execution_status, execution_reason = self._execution_quality(
            side=side,
            price=price,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            targets=targets,
        )
        self._add_evidence(evidence, "Execution quality", execution_status == "PASSED", 0, execution_reason)
        revalidation = "PASSED" if ifvg_ok and displacement_ok and timing_ok else "WATCH"
        revalidation_detail = (
            "Closed-candle revalidation passed for IFVG/FVG, displacement, and TP timing."
            if revalidation == "PASSED"
            else "Closed-candle revalidation is incomplete; keep this as watch/inspection context."
        )
        self._add_evidence(evidence, "Signal revalidation", revalidation == "PASSED", 0, revalidation_detail)

        score = min(score, 100)
        confidence = (
            f"{side.value} confluence score {score}/100 from real closed candles. "
            "This is a rule-based market-structure score, not a guaranteed win rate."
        )
        return DirectionCandidate(
            side=side,
            score=score,
            entry_low=min(entry_low, entry_high),
            entry_high=max(entry_low, entry_high),
            stop_loss=stop_loss,
            targets=targets,
            risk_reward=risk_reward,
            invalidation_condition=invalidation,
            evidence=evidence,
            confidence_explanation=confidence,
            critical_count=critical_count,
            core_confirmed=bool(ifvg_ok and displacement_ok),
            directional_context_confirmed=bool(sweep_ok or trend_ok or htf_ok),
            risk_geometry_ok=risk_geometry_ok,
            timing_ok=timing_ok,
            execution_quality_status=execution_status,
            execution_quality_reason=execution_reason,
            market_regime=regime,
            market_spread_pct=ticker.spread_pct,
            post_signal_revalidation=revalidation,
        )

    def _execution_quality(
        self,
        *,
        side: SignalSide,
        price: float,
        entry_low: float,
        entry_high: float,
        stop_loss: float,
        targets: list[TargetPlan],
    ) -> tuple[str, str]:
        if not entry_low <= price <= entry_high:
            return (
                "ENTRY_LATE",
                f"Latest close {price:.6g} is outside entry zone {entry_low:.6g} - {entry_high:.6g}; do not chase.",
            )
        entry_mid = (entry_low + entry_high) / 2
        planned_risk = abs(entry_mid - stop_loss)
        actual_risk = abs(price - stop_loss)
        if planned_risk <= 0 or actual_risk <= 0:
            return "FAILED", "Execution quality failed because entry and SL do not define positive risk."
        risk_ratio = actual_risk / planned_risk
        if risk_ratio < 0.75:
            return (
                "ENTRY_TOO_CLOSE_TO_SL",
                f"Entry risk is only {risk_ratio:.2f}x planned midpoint risk; loss can exceed planned R.",
            )
        if targets:
            tp1 = targets[0].price
            if side == SignalSide.LONG and price >= tp1:
                return "ENTRY_LATE", "Latest close is already at or above TP1; trade is late."
            if side == SignalSide.SHORT and price <= tp1:
                return "ENTRY_LATE", "Latest close is already at or below TP1; trade is late."
        return (
            "PASSED",
            f"Latest close is inside entry zone and actual risk is {risk_ratio:.2f}x planned midpoint risk.",
        )

    def _stop_plan(
        self,
        side: SignalSide,
        entry_low: float,
        entry_high: float,
        structural_stop: float,
        atr_value: float,
    ) -> StopPlan:
        entry = (entry_low + entry_high) / 2
        min_risk = atr_value * 0.55
        max_risk_by_atr = atr_value * self.max_stop_atr_multiple
        max_risk_by_pct = entry * (self.max_stop_distance_pct / 100.0)
        max_risk = max(min_risk, min(max_risk_by_atr, max_risk_by_pct))
        structural_risk = abs(entry - structural_stop)
        risk = max(min_risk, min(structural_risk, max_risk))
        capped = structural_risk > max_risk
        stop_loss = entry - risk if side == SignalSide.LONG else entry + risk
        atr_multiple = structural_risk / atr_value if atr_value > 0 else 0.0
        max_atr_multiple = max_risk / atr_value if atr_value > 0 else 0.0

        if capped:
            detail = (
                f"Structural SL distance is {atr_multiple:.2f} ATR, above practical cap "
                f"{max_atr_multiple:.2f} ATR / {self.max_stop_distance_pct:.2f}% price. "
                "Planned SL uses the practical cap; use the planned SL as the manual invalidation level."
            )
        else:
            detail = (
                f"SL distance is {atr_multiple:.2f} ATR and within practical cap "
                f"{max_atr_multiple:.2f} ATR / {self.max_stop_distance_pct:.2f}% price."
            )

        return StopPlan(
            stop_loss=stop_loss,
            structural_stop=structural_stop,
            risk=risk,
            structural_risk=structural_risk,
            max_risk=max_risk,
            capped=capped,
            detail=detail,
        )

    def _add_evidence(
        self,
        evidence: list[StrategyEvidence],
        name: str,
        passed: bool,
        weight: int,
        detail: str,
    ) -> int:
        evidence.append(
            StrategyEvidence(
                name=name,
                status="passed" if passed else "not_confirmed",
                detail=detail,
                score=weight if passed else 0,
            )
        )
        return weight if passed else 0

    def _targets(
        self,
        side: SignalSide,
        entry_low: float,
        entry_high: float,
        stop_loss: float,
        candles: list[Candle],
        context: dict[str, object],
        ticker: MarketTicker,
    ) -> tuple[float, list[TargetPlan]]:
        entry = (entry_low + entry_high) / 2
        risk = abs(entry - stop_loss)
        atr_value = float(context["atr"])
        if risk <= 0:
            risk = atr_value
        multipliers = [0.8, 1.4, 2.0]
        targets: list[TargetPlan] = []
        for index, multiplier in enumerate(multipliers, start=1):
            if side == SignalSide.LONG:
                price = entry + risk * multiplier
                distance = price - entry
            else:
                price = entry - risk * multiplier
                distance = entry - price
            minutes = self._estimated_minutes(distance, candles, context)
            targets.append(
                TargetPlan(
                    label=f"TP{index}",
                    price=price,
                    distance_pct=(distance / entry) * 100 if entry else 0,
                    estimated_minutes=minutes,
                    timing_basis=(
                        f"ATR {atr_value:.6g}, recent candle velocity, "
                        f"and {ticker.symbol} closed-candle movement profile."
                    ),
                )
            )
        risk_reward = round(abs(targets[-1].price - entry) / risk, 6)
        return risk_reward, targets

    def _estimated_minutes(self, distance: float, candles: list[Candle], context: dict[str, object]) -> int:
        interval_minutes = interval_to_minutes(self.interval)
        atr_value = float(context["atr"])
        avg_move = float(context["avg_move"])
        speed_per_minute = max(atr_value / interval_minutes, avg_move / interval_minutes)
        if speed_per_minute <= 0:
            speed_per_minute = max(candles[-1].close * 0.0002, 1e-9)
        estimated = ceil(distance / speed_per_minute)
        return max(interval_minutes, min(estimated, 1440))

    def _state_for(self, candidate: DirectionCandidate) -> SignalState:
        if (
            candidate.score >= 62
            and candidate.critical_count >= 5
            and candidate.core_confirmed
            and candidate.directional_context_confirmed
            and candidate.risk_reward >= 2.0
            and candidate.risk_geometry_ok
            and candidate.timing_ok
        ):
            return SignalState.TRADE_READY
        if candidate.score >= 52:
            return SignalState.WATCH
        if candidate.score >= 35:
            return SignalState.WAIT
        return SignalState.AVOID
