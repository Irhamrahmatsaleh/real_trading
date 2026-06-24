from __future__ import annotations

import re

from app.future_mode.models import FutureFeatures, ThermodynamicState
from app.models import TradeSignal


def extract_future_features(signal: TradeSignal) -> FutureFeatures:
    tp3 = signal.targets[-1]
    return FutureFeatures(
        score=signal.score,
        side=signal.side.value,
        risk_reward=float(signal.risk_reward or 0.0),
        tp3_distance_pct=max(0.0, float(tp3.distance_pct)),
        tp3_eta_minutes=max(1, int(tp3.estimated_minutes)),
        volume_percentile=_volume_percentile(signal),
        fvg_age=_evidence_age(signal, "IFVG / FVG"),
        displacement_age=_evidence_age(signal, "Displacement"),
        sweep_passed=_evidence_passed(signal, "Manipulation sweep"),
        displacement_passed=_evidence_passed(signal, "Displacement"),
        ifvg_passed=_evidence_passed(signal, "IFVG / FVG"),
        structure_passed=_evidence_passed(signal, "HTF/LTF structure"),
        htf_alignment_passed=_evidence_passed(signal, "Higher timeframe alignment"),
        execution_quality_status=signal.execution_quality_status,
        post_signal_revalidation=signal.post_signal_revalidation,
        market_regime=signal.market_regime,
        market_spread_pct=signal.market_spread_pct,
        liquidation_status=signal.liquidation_check_status.value,
    )


def thermodynamic_state(features: FutureFeatures) -> ThermodynamicState:
    energy = _energy(features)
    entropy = _entropy(features)
    dissipation = _dissipation(features)
    exhaustion = _exhaustion(features)
    phase_risk = _phase_risk(features.market_regime)
    return ThermodynamicState(
        energy=energy,
        entropy=entropy,
        dissipation=dissipation,
        exhaustion=exhaustion,
        phase_risk=phase_risk,
    )


def _energy(features: FutureFeatures) -> float:
    momentum_per_hour = min((features.tp3_distance_pct / features.tp3_eta_minutes) * 60.0, 12.0) / 12.0
    value = 0.0
    value += 0.25 * (features.score / 100.0)
    value += 0.18 * (min(features.volume_percentile, 100.0) / 100.0)
    value += 0.12 if features.sweep_passed else 0.0
    value += 0.10 if features.displacement_passed else 0.0
    value += 0.10 if features.ifvg_passed else 0.0
    value += 0.10 if features.structure_passed else 0.0
    value += 0.10 if features.htf_alignment_passed else 0.0
    value += 0.10 * momentum_per_hour
    value += 0.05 * min(features.risk_reward / 2.5, 1.0)
    return _clamp(value)


def _entropy(features: FutureFeatures) -> float:
    value = 0.12
    if not features.sweep_passed:
        value += 0.13
    if features.volume_percentile < 60.0:
        value += 0.10
    if features.fvg_age is not None and features.fvg_age <= 2:
        value += 0.12
    if features.fvg_age is not None and features.fvg_age > 36:
        value += 0.14
    if features.tp3_eta_minutes <= 30:
        value += 0.14
    if features.displacement_age is not None and features.displacement_age > 5:
        value += 0.08
    if features.execution_quality_status not in {"PASSED", "NOT_VALIDATED"}:
        value += 0.18
    if features.post_signal_revalidation not in {"PASSED", "NOT_VALIDATED"}:
        value += 0.18
    value += _phase_risk(features.market_regime)
    return _clamp(value)


def _dissipation(features: FutureFeatures) -> float:
    spread = max(0.0, features.market_spread_pct or 0.0)
    value = 0.03 + min(spread / 0.25, 1.5) * 0.09
    if features.liquidation_status == "NOT_VALIDATED":
        value += 0.04
    if features.liquidation_status == "FAILED":
        value += 0.30
    if features.tp3_eta_minutes > 180:
        value += min((features.tp3_eta_minutes - 180) / 180.0, 1.0) * 0.08
    return _clamp(value)


def _exhaustion(features: FutureFeatures) -> float:
    value = 0.0
    if features.tp3_eta_minutes <= 30:
        value += 0.32
    if features.fvg_age is not None and features.fvg_age <= 2:
        value += 0.24
    if features.displacement_age is not None and features.displacement_age <= 2:
        value += 0.08
    if not features.sweep_passed:
        value += 0.12
    if features.volume_percentile >= 80.0:
        value += 0.08
    if features.score < 80:
        value += 0.14
    if features.score >= 85:
        value -= 0.12
    if features.sweep_passed:
        value -= 0.12
    return _clamp(value)


def _phase_risk(regime: str) -> float:
    mapping = {
        "quiet_chop": 0.24,
        "wide_atr_risk": 0.22,
        "high_volatility_expansion": 0.10,
        "compression_near_value": 0.08,
        "balanced": 0.06,
        "bullish_trend": 0.02,
        "bearish_trend": 0.02,
    }
    return mapping.get(regime, 0.06)


def _evidence_passed(signal: TradeSignal, name: str) -> bool:
    item = _evidence(signal, name)
    return item is not None and item.status == "passed"


def _evidence_age(signal: TradeSignal, name: str) -> int | None:
    item = _evidence(signal, name)
    if item is None:
        return None
    match = re.search(r"(?:age|swept|displacement)\s+([0-9]+)", item.detail, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _volume_percentile(signal: TradeSignal) -> float:
    item = _evidence(signal, "Volume participation")
    if item is None:
        return 0.0
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)%", item.detail)
    return float(match.group(1)) if match else 0.0


def _evidence(signal: TradeSignal, name: str):
    for item in signal.evidence:
        if item.name == name:
            return item
    return None


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))
