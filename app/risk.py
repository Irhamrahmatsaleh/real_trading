from __future__ import annotations

from dataclasses import dataclass

from app.models import LiquidationCheckStatus, PositionSnapshot, SignalSide, SignalState, TradeSignal


@dataclass(frozen=True)
class LiquidationRiskResult:
    status: LiquidationCheckStatus
    liquidation_price: float | None
    safety_buffer: float | None
    reason: str
    account_risk_validated: bool
    downgrade_state: SignalState | None = None


def evaluate_liquidation_risk(
    signal: TradeSignal,
    position: PositionSnapshot | None,
    min_buffer_r: float,
) -> LiquidationRiskResult:
    if signal.entry_low is None or signal.entry_high is None or signal.stop_loss is None:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.NOT_VALIDATED,
            liquidation_price=None,
            safety_buffer=None,
            account_risk_validated=False,
            reason="Account liquidation not validated because entry or SL is unavailable.",
        )
    if position is None or position.liquidation_price is None:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.NOT_VALIDATED,
            liquidation_price=None,
            safety_buffer=None,
            account_risk_validated=False,
            reason=(
                "Account liquidation not validated until position/leverage/size exists. "
                "Use leverage and position size so liquidation remains beyond SL."
            ),
        )

    entry = (signal.entry_low + signal.entry_high) / 2
    stop_loss = signal.stop_loss
    liquidation = position.liquidation_price
    risk = abs(entry - stop_loss)
    required_buffer = risk * min_buffer_r
    if risk <= 0:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.FAILED,
            liquidation_price=liquidation,
            safety_buffer=0,
            account_risk_validated=True,
            reason="Liquidation check failed because entry and SL do not define positive risk.",
            downgrade_state=SignalState.AVOID,
        )

    if signal.side == SignalSide.LONG:
        return _evaluate_long(entry, stop_loss, liquidation, required_buffer)
    if signal.side == SignalSide.SHORT:
        return _evaluate_short(entry, stop_loss, liquidation, required_buffer)
    return LiquidationRiskResult(
        status=LiquidationCheckStatus.NOT_VALIDATED,
        liquidation_price=liquidation,
        safety_buffer=None,
        account_risk_validated=False,
        reason="Account liquidation not validated for neutral signal side.",
    )


def _evaluate_long(
    entry: float,
    stop_loss: float,
    liquidation: float,
    required_buffer: float,
) -> LiquidationRiskResult:
    if not stop_loss < entry:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.FAILED,
            liquidation_price=liquidation,
            safety_buffer=None,
            account_risk_validated=True,
            reason="Liquidation check failed because LONG SL is not below entry.",
            downgrade_state=SignalState.AVOID,
        )
    if liquidation >= entry:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.FAILED,
            liquidation_price=liquidation,
            safety_buffer=stop_loss - liquidation,
            account_risk_validated=True,
            reason="Liquidation check failed because LONG liquidation is at or above entry.",
            downgrade_state=SignalState.AVOID,
        )
    if stop_loss <= liquidation < entry:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.FAILED,
            liquidation_price=liquidation,
            safety_buffer=stop_loss - liquidation,
            account_risk_validated=True,
            reason="Liquidation check failed: LONG liquidation is between entry and SL.",
            downgrade_state=SignalState.AVOID,
        )
    safety_buffer = stop_loss - liquidation
    if safety_buffer < required_buffer:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.FAILED,
            liquidation_price=liquidation,
            safety_buffer=safety_buffer,
            account_risk_validated=True,
            reason=(
                f"Liquidation check failed: LONG liquidation is beyond SL but buffer {safety_buffer:.6g} "
                f"is below required {required_buffer:.6g}."
            ),
            downgrade_state=SignalState.WATCH,
        )
    return LiquidationRiskResult(
        status=LiquidationCheckStatus.PASSED,
        liquidation_price=liquidation,
        safety_buffer=safety_buffer,
        account_risk_validated=True,
        reason=f"Liquidation Check: PASSED. LONG liquidation is safely below SL by {safety_buffer:.6g}.",
    )


def _evaluate_short(
    entry: float,
    stop_loss: float,
    liquidation: float,
    required_buffer: float,
) -> LiquidationRiskResult:
    if not entry < stop_loss:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.FAILED,
            liquidation_price=liquidation,
            safety_buffer=None,
            account_risk_validated=True,
            reason="Liquidation check failed because SHORT SL is not above entry.",
            downgrade_state=SignalState.AVOID,
        )
    if liquidation <= entry:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.FAILED,
            liquidation_price=liquidation,
            safety_buffer=liquidation - stop_loss,
            account_risk_validated=True,
            reason="Liquidation check failed because SHORT liquidation is at or below entry.",
            downgrade_state=SignalState.AVOID,
        )
    if entry < liquidation <= stop_loss:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.FAILED,
            liquidation_price=liquidation,
            safety_buffer=liquidation - stop_loss,
            account_risk_validated=True,
            reason="Liquidation check failed: SHORT liquidation is between entry and SL.",
            downgrade_state=SignalState.AVOID,
        )
    safety_buffer = liquidation - stop_loss
    if safety_buffer < required_buffer:
        return LiquidationRiskResult(
            status=LiquidationCheckStatus.FAILED,
            liquidation_price=liquidation,
            safety_buffer=safety_buffer,
            account_risk_validated=True,
            reason=(
                f"Liquidation check failed: SHORT liquidation is beyond SL but buffer {safety_buffer:.6g} "
                f"is below required {required_buffer:.6g}."
            ),
            downgrade_state=SignalState.WATCH,
        )
    return LiquidationRiskResult(
        status=LiquidationCheckStatus.PASSED,
        liquidation_price=liquidation,
        safety_buffer=safety_buffer,
        account_risk_validated=True,
        reason=f"Liquidation Check: PASSED. SHORT liquidation is safely above SL by {safety_buffer:.6g}.",
    )
