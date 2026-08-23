"""Phase 13：CI / Eval 成为真正的 Release Gate。"""

from app.ci.pr_gate import CheckResult, CheckStatus, PrGate, PrGateResult
from app.ci.durable_baseline import (
    ArtifactStore,
    BaselineComparator,
    BaselineReport,
    BaselineSnapshot,
    DurableBaseline,
)
from app.ci.trajectory_eval import (
    Trajectory,
    TrajectoryCheck,
    TrajectoryEval,
    evaluate_trajectory,
    trajectory_outcome_metrics,
)
from app.ci.release_gate import (
    EvalSuite,
    ReleaseGate,
    ReleaseGateResult,
    SuiteStatus,
    make_suite,
)

__all__ = [
    "CheckResult", "CheckStatus", "PrGate", "PrGateResult",
    "ArtifactStore", "BaselineComparator", "BaselineReport", "BaselineSnapshot",
    "DurableBaseline",
    "Trajectory", "TrajectoryCheck", "TrajectoryEval", "evaluate_trajectory",
    "trajectory_outcome_metrics",
    "EvalSuite", "ReleaseGate", "ReleaseGateResult", "SuiteStatus", "make_suite",
]