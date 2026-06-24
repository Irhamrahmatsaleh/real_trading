from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FutureFeatures:
    score: int
    side: str
    risk_reward: float
    tp3_distance_pct: float
    tp3_eta_minutes: int
    volume_percentile: float
    fvg_age: int | None
    displacement_age: int | None
    sweep_passed: bool
    displacement_passed: bool
    ifvg_passed: bool
    structure_passed: bool
    htf_alignment_passed: bool
    execution_quality_status: str
    post_signal_revalidation: str
    market_regime: str
    market_spread_pct: float | None
    liquidation_status: str


@dataclass(frozen=True)
class ThermodynamicState:
    energy: float
    entropy: float
    dissipation: float
    exhaustion: float
    phase_risk: float


@dataclass(frozen=True)
class ScenarioProjection:
    win_probability: float
    loss_probability: float
    chop_probability: float
    expected_win_r: float
    expected_loss_r: float
    cost_r: float
    expected_r: float


@dataclass(frozen=True)
class FutureEvaluation:
    allowed: bool
    score: float
    state: ThermodynamicState
    projection: ScenarioProjection
    reasons: tuple[str, ...]

    @property
    def summary(self) -> str:
        verdict = "passed" if self.allowed else "blocked"
        reason = "; ".join(self.reasons)
        return (
            f"FUTURE_MODE {verdict}: future_score {self.score:.1f}/100; "
            f"expected {self.projection.expected_r:+.2f}R; "
            f"energy {self.state.energy:.2f}; entropy {self.state.entropy:.2f}; "
            f"dissipation {self.state.dissipation:.2f}; exhaustion {self.state.exhaustion:.2f}"
            + (f"; {reason}" if reason else "")
        )
