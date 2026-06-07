"""Judge blind-ordering + A/B remap (offline, fake model)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from evals.judge import _PairOutput, _remap, judge_pair, swap_for
from evals.schema import DimensionScores, GoldenTask, RunResult

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


def _scores(total: int) -> DimensionScores:
    base, rem = divmod(total, 4)
    vals = [base + (1 if i < rem else 0) for i in range(4)]
    return DimensionScores(
        correctness=vals[0], completeness=vals[1], safety_gate=vals[2], actionability=vals[3]
    )


class _FakeStructured:
    def __init__(self, out: object) -> None:
        self._out = out

    async def ainvoke(self, _messages: object) -> object:
        return self._out


class _FakeModel:
    """Stands in for a ChatAnthropic — returns a canned structured output."""

    def __init__(self, out: object) -> None:
        self._out = out

    def with_structured_output(self, _schema: object, **_kw: object) -> _FakeStructured:
        return _FakeStructured(self._out)


def test_swap_for_is_deterministic() -> None:
    assert swap_for("net-001") == swap_for("net-001")


def test_swap_for_covers_both_orderings() -> None:
    results = {swap_for(f"task-{i}") for i in range(50)}
    assert results == {True, False}


def test_remap_not_swapped() -> None:
    out = _PairOutput(scores_a=_scores(18), scores_b=_scores(10), preferred="A")
    pref, local, claude = _remap(out, swapped=False)
    assert pref == "local"
    assert local.total == 18
    assert claude.total == 10


def test_remap_swapped() -> None:
    out = _PairOutput(scores_a=_scores(18), scores_b=_scores(10), preferred="A")
    pref, local, claude = _remap(out, swapped=True)
    assert pref == "claude"  # A is the Claude output when swapped
    assert local.total == 10
    assert claude.total == 18


async def test_judge_pair_maps_fake_output() -> None:
    task = GoldenTask(task_id="net-xyz", content="audit something")
    local = RunResult(agent_id="network-operator", task_id="net-xyz", group="local", output="L")
    claude = RunResult(agent_id="network-operator", task_id="net-xyz", group="claude", output="C")
    out = _PairOutput(
        scores_a=_scores(18), scores_b=_scores(10), preferred="A", reasoning="because"
    )
    verdict = await judge_pair(
        "network-operator",
        task,
        local,
        claude,
        "rubric",
        model=cast("BaseChatModel", _FakeModel(out)),
    )
    exp_pref, exp_local, exp_claude = _remap(out, swapped=swap_for("net-xyz"))
    assert verdict.error is None
    assert verdict.preference == exp_pref
    assert verdict.local_scores.total == exp_local.total
    assert verdict.claude_scores is not None
    assert verdict.claude_scores.total == exp_claude.total
    assert verdict.reasoning == "because"
