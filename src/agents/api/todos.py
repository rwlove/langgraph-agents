"""Admin /admin/todos — durable todo store for the operator.

Lives next to the task queue (same DB, same Postgres pool) but is a
distinct schema with a distinct lifecycle:

- Todos persist until the operator marks them done or dropped.
- No TTL, no idempotency, no worker dequeue, no agent assignment.
- The CLI (`hai todo add/ls/done`) is the primary client; future
  MCP / web surfaces wrap the same endpoints.

Endpoint set:

    POST   /admin/todos               create
    GET    /admin/todos                list (?status=open|all, ?tag=<t>)
    GET    /admin/todos/{id}           show
    PATCH  /admin/todos/{id}           update (body, status, tags, metadata)
    DELETE /admin/todos/{id}           soft-delete (status → 'dropped')

Authentication is provided at the ingress layer (Authelia / oauth2-
proxy in front of the `hai.<domain>` HTTPRoute); the API itself is
single-tenant and trusts the requester.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from ulid import ULID

router = APIRouter(prefix="/admin/todos", tags=["admin", "todos"])


# ---------- Pydantic models ----------


TodoStatus = Literal["open", "done", "dropped"]


class TodoCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TodoUpdate(BaseModel):
    body: str | None = Field(default=None, min_length=1, max_length=10_000)
    status: TodoStatus | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class Todo(BaseModel):
    id: str
    body: str
    status: TodoStatus
    created_by: str
    tags: list[str]
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


# ---------- Helpers ----------


def _row_to_todo(row: tuple) -> Todo:
    """Map a SELECT * row into a Todo model."""
    return Todo(
        id=row[0],
        body=row[1],
        status=row[2],
        created_by=row[3],
        tags=list(row[4] or []),
        metadata=dict(row[5] or {}),
        created_at=row[6],
        updated_at=row[7],
        closed_at=row[8],
    )


def _require_pool(request: Request):
    pool = request.app.state.queue_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="queue pool not initialized")
    return pool


# ---------- Endpoints ----------


@router.post("", response_model=Todo, status_code=201)
async def create_todo(body: TodoCreate, request: Request) -> Todo:
    """Create a todo. Body required; tags + metadata optional."""
    pool = _require_pool(request)
    todo_id = str(ULID())
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO todo (id, body, tags, metadata)
            VALUES (%s, %s, %s, %s)
            RETURNING id, body, status, created_by, tags, metadata,
                      created_at, updated_at, closed_at
            """,
            (todo_id, body.body, body.tags, body.metadata),
        )
        row = await cur.fetchone()
    assert row is not None  # noqa: S101 — INSERT … RETURNING never empty
    return _row_to_todo(row)


@router.get("", response_model=list[Todo])
async def list_todos(
    request: Request,
    status: Literal["open", "all"] = "open",
    tag: str | None = None,
    limit: int = 200,
) -> list[Todo]:
    """List todos. Default = open only; pass status=all to include done/dropped."""
    pool = _require_pool(request)
    clauses: list[str] = []
    params: list[Any] = []
    if status == "open":
        clauses.append("status = 'open'")
    if tag is not None:
        clauses.append("%s = ANY(tags)")
        params.append(tag)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT id, body, status, created_by, tags, metadata,
                   created_at, updated_at, closed_at
            FROM todo
            {where}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = await cur.fetchall()
    return [_row_to_todo(r) for r in rows]


@router.get("/{todo_id}", response_model=Todo)
async def get_todo(todo_id: str, request: Request) -> Todo:
    pool = _require_pool(request)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, body, status, created_by, tags, metadata,
                   created_at, updated_at, closed_at
            FROM todo WHERE id = %s
            """,
            (todo_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="todo not found")
    return _row_to_todo(row)


@router.patch("/{todo_id}", response_model=Todo)
async def update_todo(todo_id: str, body: TodoUpdate, request: Request) -> Todo:
    pool = _require_pool(request)
    sets: list[str] = ["updated_at = now()"]
    params: list[Any] = []
    if body.body is not None:
        sets.append("body = %s")
        params.append(body.body)
    if body.status is not None:
        sets.append("status = %s")
        params.append(body.status)
        # Flip closed_at on/off as status transitions in/out of 'open'.
        if body.status == "open":
            sets.append("closed_at = NULL")
        else:
            sets.append("closed_at = now()")
    if body.tags is not None:
        sets.append("tags = %s")
        params.append(body.tags)
    if body.metadata is not None:
        sets.append("metadata = %s")
        params.append(body.metadata)
    if len(sets) == 1:
        # Only the auto-updated_at set; treat as no-op so the caller
        # gets a clear signal vs. silently bumping the timestamp.
        raise HTTPException(status_code=400, detail="no fields to update")
    params.append(todo_id)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            UPDATE todo SET {", ".join(sets)}
            WHERE id = %s
            RETURNING id, body, status, created_by, tags, metadata,
                      created_at, updated_at, closed_at
            """,
            tuple(params),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="todo not found")
    return _row_to_todo(row)


@router.delete("/{todo_id}", response_model=Todo)
async def delete_todo(todo_id: str, request: Request) -> Todo:
    """Soft-delete — flips status to 'dropped'. Hard delete is not provided."""
    pool = _require_pool(request)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE todo SET status = 'dropped',
                            closed_at = now(),
                            updated_at = now()
            WHERE id = %s AND status != 'dropped'
            RETURNING id, body, status, created_by, tags, metadata,
                      created_at, updated_at, closed_at
            """,
            (todo_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="todo not found or already dropped")
    return _row_to_todo(row)
