from __future__ import annotations

import re

from app.future_mode.features import extract_future_features, thermodynamic_state
from app.future_mode.models import FutureEvaluation
from app.future_mode.scenarios import project_scenarios
from app.models import LearningReport, TradeSignal

MIN_FUTURE_SCORE = 58.0
MIN_EXPECTED_R = 0.05
MAX_ENTROPY = 0.72
MAX_EXHAUSTION = 0.76


def evaluate_future_gate(signal: TradeSignal, learning_report: LearningReport | None = None) -> FutureEvaluation:
    features = extract_future_features(signal)
    state = thermodynamic_state(features)
    projection = project_scenarios(features, state)
    future_score = _future_score(signal, state, projection, learning_report)
    reasons = _blocking_reasons(signal, future_score, state, projection)
    allowed = not reasons
    if allowed:
        reasons = (
            f"scenario win {projection.win_probability * 100:.1f}%",
            f"loss {projection.loss_probability * 100:.1f}%",
        )
    return FutureEvaluation(
        allowed=allowed,
        score=future_score,
        state=state,
        projection=projection,
        reasons=reasons,
    )


def _future_score(
    signal: TradeSignal,
    state,
    projection,
    learning_report: LearningReport | None,
) -> float:
    value = 50.0
    value += state.energy * 32.0
    value -= state.entropy * 18.0
    value -= state.dissipation * 10.0
    value -= state.exhaustion * 14.0
    value += max(-0.25, min(projection.expected_r, 0.45)) * 45.0
    if signal.score >= 80:
        value += 4.0
    if learning_report is not None and learning_report.all_time_expectancy_r is not None:
        value += max(-3.0, min(float(learning_report.all_time_expectancy_r) * 3.0, 3.0))
    return max(0.0, min(100.0, value))


def _blocking_reasons(signal: TradeSignal, future_score: float, state, projection) -> tuple[str, ...]:
    reasons: list[str] = []
    tp3_eta = signal.targets[-1].estimated_minutes if signal.targets else 0
    fvg_age = _fvg_age(signal)
    sweep_passed = _evidence_passed(signal, "Manipulation sweep")
    if signal.score < 80 and fvg_age is not None and fvg_age <= 2 and tp3_eta <= 30 and not sweep_passed:
        reasons.append("fast young-FVG setup without sweep confirmation")
    if projection.expected_r < MIN_EXPECTED_R:
        reasons.append(f"expected R {projection.expected_r:+.2f} below minimum {MIN_EXPECTED_R:+.2f}")
    if future_score < MIN_FUTURE_SCORE:
        reasons.append(f"future score {future_score:.1f} below minimum {MIN_FUTURE_SCORE:.1f}")
    if state.entropy > MAX_ENTROPY:
        reasons.append(f"entropy {state.entropy:.2f} above maximum {MAX_ENTROPY:.2f}")
    if state.exhaustion > MAX_EXHAUSTION:
        reasons.append(f"exhaustion {state.exhaustion:.2f} above maximum {MAX_EXHAUSTION:.2f}")
    return tuple(reasons)


def _fvg_age(signal: TradeSignal) -> int | None:
    item = next((evidence for evidence in signal.evidence if evidence.name == "IFVG / FVG"), None)
    if item is None:
        return None
    match = re.search(r"age\s+([0-9]+)", item.detail, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _evidence_passed(signal: TradeSignal, name: str) -> bool:
    return any(evidence.name == name and evidence.status == "passed" for evidence in signal.evidence)
