"""Report aggregation → routing label."""

from __future__ import annotations

from evals.__main__ import _dim_line
from evals.report import build_report
from evals.schema import DimensionScores, JudgeVerdict, Preference


def _scores(total: int) -> DimensionScores:
    """A DimensionScores summing to `total` (4..20), each field within 1..5."""
    base, rem = divmod(total, 4)
    vals = [base + (1 if i < rem else 0) for i in range(4)]
    return DimensionScores(
        correctness=vals[0], completeness=vals[1], safety_gate=vals[2], actionability=vals[3]
    )


def _pair(agent: str, local: int, claude: int, preference: Preference) -> JudgeVerdict:
    return JudgeVerdict(
        agent_id=agent,  # type: ignore[arg-type]
        task_id="t",
        preference=preference,
        local_scores=_scores(local),
        claude_scores=_scores(claude),
    )


def _single(agent: str, local: int) -> JudgeVerdict:
    return JudgeVerdict(
        agent_id=agent,  # type: ignore[arg-type]
        task_id="t",
        preference="local",
        local_scores=_scores(local),
        claude_scores=None,
    )


def test_route_to_api_when_claude_clearly_wins() -> None:
    verdicts = [
        _pair("network-operator", local=10, claude=18, preference="claude") for _ in range(3)
    ]
    report = build_report("network-operator", verdicts)
    assert report.label == "route-to-api"
    assert report.claude_win_rate == 1.0
    assert report.mean_score_delta == 8.0


def test_offload_safe_when_local_strong_and_claude_no_better() -> None:
    verdicts = [
        _pair("network-operator", local=16, claude=16, preference="local") for _ in range(3)
    ]
    report = build_report("network-operator", verdicts)
    assert report.label == "offload-safe"


def test_keep_local_fix_when_local_weak_and_claude_marginal() -> None:
    # Claude is preferred but the delta is below threshold and local is inadequate.
    verdicts = [_pair("network-operator", local=8, claude=9, preference="claude") for _ in range(3)]
    report = build_report("network-operator", verdicts)
    assert report.label == "keep-local-fix"


def test_ineligible_strong_local_is_offload_safe() -> None:
    verdicts = [_single("health-tracker", local=16) for _ in range(3)]
    report = build_report("health-tracker", verdicts)
    assert not report.eligible
    assert report.label == "offload-safe"


def test_ineligible_weak_local_is_keep_local_fix() -> None:
    verdicts = [_single("health-tracker", local=8) for _ in range(3)]
    report = build_report("health-tracker", verdicts)
    assert report.label == "keep-local-fix"


def test_errored_verdicts_are_not_judged() -> None:
    good = _pair("network-operator", local=16, claude=16, preference="local")
    bad = JudgeVerdict(
        agent_id="network-operator",
        task_id="t-err",
        preference="tie",
        local_scores=_scores(4),
        error="boom",
    )
    report = build_report("network-operator", [good, bad])
    assert report.n_tasks == 2
    assert report.n_judged == 1


def test_dimension_means_populated() -> None:
    # _scores(10) -> corr=3 compl=3 safety=2 action=2 ; _scores(18) -> 5 5 4 4
    verdicts = [
        _pair("network-operator", local=10, claude=18, preference="claude") for _ in range(3)
    ]
    report = build_report("network-operator", verdicts)
    assert report.mean_local_dims == {
        "correctness": 3.0,
        "completeness": 3.0,
        "safety_gate": 2.0,
        "actionability": 2.0,
    }
    assert report.mean_claude_dims == {
        "correctness": 5.0,
        "completeness": 5.0,
        "safety_gate": 4.0,
        "actionability": 4.0,
    }


def test_dimension_means_claude_empty_for_ineligible() -> None:
    # health-tracker is Claude-ineligible: only local is scored, no claude dims.
    report = build_report("health-tracker", [_single("health-tracker", local=16)])
    assert report.mean_local_dims["correctness"] == 4.0  # _scores(16) -> 4 4 4 4
    assert report.mean_claude_dims == {}


def test_dim_line_shows_local_arrow_claude() -> None:
    report = build_report("network-operator", [_pair("network-operator", 10, 18, "claude")])
    line = _dim_line(report)
    assert "corr 3.0->5.0" in line  # local _scores(10) corr=3, claude _scores(18) corr=5
    assert "safety 2.0->4.0" in line


def test_dim_line_local_only_when_no_pairs() -> None:
    report = build_report("health-tracker", [_single("health-tracker", 16)])
    line = _dim_line(report)
    assert "corr 4.0" in line and "->" not in line
