"""Per-completion model-group provenance.

Covers the process-local accumulator in ``agents.observability`` and the
recording hook in ``agents.llm`` that stamps the *actually-serving* group
under the bound ``task_id`` contextvar. The read side (``/admin/cost``
``_group_breakdown``) is covered in ``tests/test_admin_endpoints.py``.
"""

from __future__ import annotations

import pytest
import structlog
from langchain_ollama import ChatOllama

from agents.llm import llm
from agents.observability import (
    _task_served_groups,
    clear_task_provenance,
    record_served_group,
    served_groups_for,
)


@pytest.fixture(autouse=True)
def _clear_provenance() -> None:
    """Keep the process-local accumulator independent between tests."""
    _task_served_groups.clear()
    structlog.contextvars.clear_contextvars()


def test_record_and_read_roundtrips() -> None:
    record_served_group("t1", "local-p40")
    assert served_groups_for("t1") == ["local-p40"]


def test_record_dedupes_and_sorts() -> None:
    record_served_group("t1", "local-spark")
    record_served_group("t1", "claude")
    record_served_group("t1", "local-spark")  # dup
    assert served_groups_for("t1") == ["claude", "local-spark"]


def test_distinct_tasks_isolated() -> None:
    record_served_group("t1", "local-p40")
    record_served_group("t2", "claude")
    assert served_groups_for("t1") == ["local-p40"]
    assert served_groups_for("t2") == ["claude"]


def test_none_task_id_is_noop() -> None:
    record_served_group(None, "claude")  # silently dropped
    assert served_groups_for(None) == []
    assert _task_served_groups == {}


def test_clear_removes_task() -> None:
    record_served_group("t1", "local-p40")
    clear_task_provenance("t1")
    assert served_groups_for("t1") == []


def test_clear_unknown_and_none_are_noops() -> None:
    clear_task_provenance("never-seen")  # no KeyError
    clear_task_provenance(None)


def test_unknown_task_reads_empty() -> None:
    assert served_groups_for("nope") == []


def test_llm_records_effective_group_under_bound_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building a local model stamps the serving group on the task's set."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    structlog.contextvars.bind_contextvars(task_id="task-abc")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agents.llm.service_healthy", lambda _url: True)
        model = llm("researcher")  # researcher → local-spark
    assert isinstance(model, ChatOllama)
    assert served_groups_for("task-abc") == ["local-spark"]


def test_llm_records_degraded_group_not_requested_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Spark-down degrade records local-p40 (what served), not local-spark."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    structlog.contextvars.bind_contextvars(task_id="task-degrade")

    def _health(url: str) -> bool:
        return "p40" in url  # spark unhealthy, p40 healthy

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agents.llm.service_healthy", _health)
        model = llm("researcher")  # requests local-spark, degrades to p40
    assert isinstance(model, ChatOllama)
    assert served_groups_for("task-degrade") == ["local-p40"]


def test_llm_unbound_task_id_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No task_id contextvar → no provenance recorded (scheduled/test paths)."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agents.llm.service_healthy", lambda _url: True)
        llm("researcher")
    assert _task_served_groups == {}
