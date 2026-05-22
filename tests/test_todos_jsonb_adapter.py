"""Regression test for the dict→jsonb adapter on /admin/todos.

psycopg3 won't auto-adapt a plain dict into a jsonb column — it needs
`psycopg.types.json.Jsonb(...)`. POST and PATCH /admin/todos both
write into the `metadata jsonb` column, so both call sites must wrap.

Without the wrapper, the live server 500s with:

    psycopg.ProgrammingError: cannot adapt type 'dict' using
    placeholder '%s' (format: AUTO)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from agents.api import todos


def _make_app() -> tuple[FastAPI, MagicMock]:
    """FastAPI app + mock pool that records the params of every execute."""
    cur = MagicMock()
    cur.execute = AsyncMock(return_value=None)
    cur.fetchone = AsyncMock(
        return_value=(
            "01TEST",
            "body",
            "open",
            "rob",
            [],
            {},
            datetime.now(UTC),
            datetime.now(UTC),
            None,
        )
    )

    cur_ctx = MagicMock()
    cur_ctx.__aenter__ = AsyncMock(return_value=cur)
    cur_ctx.__aexit__ = AsyncMock(return_value=None)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur_ctx)
    conn_ctx = MagicMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.connection = MagicMock(return_value=conn_ctx)

    app = FastAPI()
    app.include_router(todos.router)
    app.state.queue_pool = pool
    return app, cur


def test_create_todo_wraps_metadata_in_jsonb() -> None:
    app, cur = _make_app()
    with TestClient(app) as client:
        r = client.post(
            "/admin/todos",
            json={"body": "hello", "tags": ["t1"], "metadata": {"k": "v"}},
        )
    assert r.status_code == 201, r.text

    cur.execute.assert_awaited_once()
    _sql, params = cur.execute.await_args.args
    metadata_param: Any = params[3]
    assert isinstance(
        metadata_param, Jsonb
    ), f"metadata must be wrapped in Jsonb(...) for psycopg3, got {type(metadata_param)}"


def test_create_todo_defaults_to_empty_metadata_dict() -> None:
    app, cur = _make_app()
    with TestClient(app) as client:
        r = client.post("/admin/todos", json={"body": "hello"})
    assert r.status_code == 201, r.text
    _sql, params = cur.execute.await_args.args
    assert isinstance(params[3], Jsonb)


def test_patch_todo_wraps_metadata_in_jsonb() -> None:
    app, cur = _make_app()
    with TestClient(app) as client:
        r = client.patch(
            "/admin/todos/01TEST",
            json={"metadata": {"k": "v"}},
        )
    assert r.status_code == 200, r.text

    cur.execute.assert_awaited_once()
    _sql, params = cur.execute.await_args.args
    assert any(
        isinstance(p, Jsonb) for p in params
    ), "PATCH metadata must be wrapped in Jsonb(...) for psycopg3"
