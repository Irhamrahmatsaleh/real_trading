from __future__ import annotations

from app.future_mode.models import FutureFeatures, ScenarioProjection, ThermodynamicState


def project_scenarios(features: FutureFeatures, state: ThermodynamicState) -> ScenarioProjection:
    win_probability = _clamp(
        0.50
        + 0.35 * state.energy
        - 0.22 * state.entropy
        - 0.12 * state.dissipation
        - 0.16 * state.exhaustion
        - 0.08 * state.phase_risk
    )
    chop_probability = _clamp(0.10 + 0.28 * state.entropy + 0.12 * state.dissipation, upper=0.35)
    loss_probability = _clamp(1.0 - win_probability - chop_probability, lower=0.05)
    total = win_probability + loss_probability + chop_probability
    win_probability /= total
    loss_probability /= total
    chop_probability /= total

    expected_win_r = _expected_win_r(features, state)
    expected_loss_r = _expected_loss_r(state)
    cost_r = 0.02 + 0.12 * state.dissipation
    expected_r = (win_probability * expected_win_r) - (loss_probability * expected_loss_r) - cost_r

    return ScenarioProjection(
        win_probability=win_probability,
        loss_probability=loss_probability,
        chop_probability=chop_probability,
        expected_win_r=expected_win_r,
        expected_loss_r=expected_loss_r,
        cost_r=cost_r,
        expected_r=expected_r,
    )


def _expected_win_r(features: FutureFeatures, state: ThermodynamicState) -> float:
    rr_ceiling = min(max(features.risk_reward, 0.0), 3.0)
    tp_capture = 0.42 + 0.28 * state.energy - 0.12 * state.entropy
    if features.tp3_eta_minutes <= 30:
        tp_capture -= 0.08
    if features.sweep_passed:
        tp_capture += 0.05
    return max(0.35, min(rr_ceiling * tp_capture, 1.15))


def _expected_loss_r(state: ThermodynamicState) -> float:
    return 0.95 + 0.32 * state.entropy + 0.18 * state.exhaustion + 0.15 * state.dissipation


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))
