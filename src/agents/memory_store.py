"""LangGraph BaseStore implementation backed by the shared `kg.*` schema.

This adapter lets every agent in the fleet read + write into the same
knowledge-graph substrate that `memory-mcp` exposes to outside agents
(Claude Code, Open WebUI, HolmesGPT) — see [[memory-mcp-phase0-done]].

Transport choice: direct SQL via psycopg3 (NOT through mcp-gateway). The
original plan flagged this as "langgraph-agents keeps direct DB access
as fallback for read-heavy paths." Both processes write the same
schema; MCP would just add a hop for an in-cluster process that already
has the DB URI plumbed.

Namespace mapping:

  BaseStore                                   kg
  -----------------------------------------------------------------
  namespace: ("agent", "alice")               entity.name = "langgraph/agent/alice/<key>"
  key: "conv-7"                               entity.namespace = "langgraph/agent/alice"
  value: {...}                                latest live observation.content (JSON)

  Storing under the `langgraph/*` prefix keeps writes from langgraph
  agents visually separated from human-seeded entries (host/cnpg/etc.)
  when other agents query.

Item identity is (namespace, key). On `aput`, we upsert the entity
row and APPEND a new observation. Latest live observation = current
value. `adelete` soft-deletes all live observations on the entity.
Observation history is preserved (use memory_get_entity from any
agent to inspect).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import httpx
from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    MatchCondition,
    PutOp,
    SearchItem,
    SearchOp,
)
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

ENTITY_TYPE = "langgraph-store-item"
NAMESPACE_ROOT = "langgraph"


def _entity_name(namespace: tuple[str, ...], key: str) -> str:
    ns = "/".join(namespace)
    if ns:
        return f"{NAMESPACE_ROOT}/{ns}/{key}"
    return f"{NAMESPACE_ROOT}/{key}"


def _entity_namespace(namespace: tuple[str, ...]) -> str:
    ns = "/".join(namespace)
    if ns:
        return f"{NAMESPACE_ROOT}/{ns}"
    return NAMESPACE_ROOT


def _ns_from_entity_namespace(entity_namespace: str) -> tuple[str, ...]:
    """Reverse of _entity_namespace: strip the `langgraph/` root and split."""
    if entity_namespace == NAMESPACE_ROOT:
        return ()
    if entity_namespace.startswith(NAMESPACE_ROOT + "/"):
        return tuple(entity_namespace[len(NAMESPACE_ROOT) + 1 :].split("/"))
    # Shouldn't happen for entities we created, but be defensive.
    return tuple(entity_namespace.split("/"))


def _key_from_entity_name(name: str) -> str:
    """Pull the trailing `<key>` segment out of `langgraph/.../<key>`."""
    return name.rsplit("/", 1)[-1]


def _namespace_matches(ns: tuple[str, ...], conds: tuple[MatchCondition, ...]) -> bool:
    """Apply ListNamespacesOp match conditions to a candidate namespace."""
    for cond in conds:
        if cond.match_type == "prefix":
            if ns[: len(cond.path)] != tuple(cond.path):
                return False
        elif cond.match_type == "suffix":
            if cond.path and ns[-len(cond.path) :] != tuple(cond.path):
                return False
        else:
            # Unknown match type — fail closed.
            return False
    return True


class MCPMemoryStore(BaseStore):
    """LangGraph BaseStore backed by the shared `kg.*` schema.

    Constructed by `_build_store()` in main.py. One instance per app
    lifetime; pool is closed via AsyncExitStack.

    Operations:
      - aget / aput / adelete via direct SQL
      - asearch: hybrid (semantic via vchordrq + keyword via ILIKE,
        de-duplicated). Embedding via Ollama best-effort; if Ollama is
        unreachable, semantic returns nothing and keyword carries the
        result set.
      - alist_namespaces: SELECT DISTINCT namespace, then unprefix.

    Sync `batch` raises NotImplementedError — LangGraph 0.2+ uses async
    paths in the runtime, and this store is wired in async land only.
    """

    __slots__ = ("_embed_model", "_ollama_url", "_pool")

    def __init__(
        self,
        pool: AsyncConnectionPool,
        ollama_base_url: str,
        embed_model: str = "nomic-embed-text",
    ) -> None:
        self._pool = pool
        self._ollama_url = ollama_base_url.rstrip("/")
        self._embed_model = embed_model

    # ------------------------------------------------------------------
    # BaseStore interface
    # ------------------------------------------------------------------

    def batch(self, ops: Iterable[Op]) -> list[Any]:
        raise NotImplementedError(
            "MCPMemoryStore is async-only. Use abatch() / aget() / aput() / etc."
        )

    async def abatch(self, ops: Iterable[Op]) -> list[Any]:
        results: list[Any] = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(await self._aget(op))
            elif isinstance(op, PutOp):
                await self._aput(op)
                results.append(None)
            elif isinstance(op, SearchOp):
                results.append(await self._asearch(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(await self._alist_namespaces(op))
            else:
                raise NotImplementedError(f"unsupported op: {type(op).__name__}")
        return results

    # ------------------------------------------------------------------
    # Per-op implementations
    # ------------------------------------------------------------------

    async def _aget(self, op: GetOp) -> Item | None:
        ename = _entity_name(op.namespace, op.key)
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT e.created_at AS entity_created_at,
                           e.updated_at AS entity_updated_at,
                           o.content,
                           o.created_at AS obs_created_at
                    FROM kg.entities e
                    JOIN kg.observations o ON o.entity_id = e.id
                    WHERE e.name = %s AND o.deleted_at IS NULL
                    ORDER BY o.created_at DESC
                    LIMIT 1
                    """,
                    (ename,),
                )
                row = await cur.fetchone()
        if row is None:
            return None
        value = _parse_value(row["content"])
        if value is None:
            return None
        return Item(
            value=value,
            key=op.key,
            namespace=op.namespace,
            created_at=row["entity_created_at"],
            updated_at=row["entity_updated_at"],
        )

    async def _aput(self, op: PutOp) -> None:
        ename = _entity_name(op.namespace, op.key)
        ens = _entity_namespace(op.namespace)

        if op.value is None:
            # Delete: soft-delete all live observations on this entity.
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE kg.observations o
                        SET deleted_at = now()
                        FROM kg.entities e
                        WHERE o.entity_id = e.id
                          AND e.name = %s
                          AND o.deleted_at IS NULL
                        """,
                        (ename,),
                    )
            return

        content = json.dumps(op.value, default=_json_default)
        embedding_text = _embedding_text(op.value, op.index)
        vector = await self._embed(embedding_text) if embedding_text else None
        source = {
            "agent": "langgraph",
            "at": datetime.utcnow().isoformat() + "Z",
            "store": "MCPMemoryStore",
        }
        source_json = json.dumps(source)

        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        INSERT INTO kg.entities (name, type, namespace, source)
                        VALUES (%s, %s, %s, %s::jsonb)
                        ON CONFLICT (name) DO UPDATE
                          SET updated_at = now()
                        RETURNING id
                        """,
                        (ename, ENTITY_TYPE, ens, source_json),
                    )
                    row = await cur.fetchone()
                    if row is None:
                        raise RuntimeError(f"upsert returned no row for entity '{ename}'")
                    entity_id = row["id"]

                    if vector is None:
                        await cur.execute(
                            """
                            INSERT INTO kg.observations
                              (entity_id, content, embedding, source)
                            VALUES (%s, %s, NULL, %s::jsonb)
                            """,
                            (entity_id, content, source_json),
                        )
                    else:
                        await cur.execute(
                            """
                            INSERT INTO kg.observations
                              (entity_id, content, embedding, source)
                            VALUES (%s, %s, %s::vector, %s::jsonb)
                            """,
                            (
                                entity_id,
                                content,
                                _encode_vector(vector),
                                source_json,
                            ),
                        )

    async def _asearch(self, op: SearchOp) -> list[SearchItem]:
        ns_prefix = _entity_namespace(op.namespace_prefix)
        # `prefix` matches the namespace exactly OR any deeper sub-namespace.
        ns_like = ns_prefix + "/%"

        results: list[SearchItem] = []
        seen: set[int] = set()

        # ---- semantic leg ----
        if op.query:
            vector = await self._embed(op.query)
            if vector is not None:
                async with self._pool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            """
                            SELECT e.id AS entity_id,
                                   e.name,
                                   e.namespace,
                                   e.created_at,
                                   e.updated_at,
                                   o.content,
                                   (o.embedding <=> %s::vector) AS distance
                            FROM kg.observations o
                            JOIN kg.entities e ON e.id = o.entity_id
                            WHERE e.type = %s
                              AND (e.namespace = %s OR e.namespace LIKE %s)
                              AND o.deleted_at IS NULL
                              AND o.embedding IS NOT NULL
                            ORDER BY o.embedding <=> %s::vector
                            LIMIT %s
                            """,
                            (
                                _encode_vector(vector),
                                ENTITY_TYPE,
                                ns_prefix,
                                ns_like,
                                _encode_vector(vector),
                                op.limit + op.offset,
                            ),
                        )
                        rows = await cur.fetchall()
                for row in rows[op.offset :]:
                    if row["entity_id"] in seen:
                        continue
                    item = _row_to_search_item(row, score=1.0 - float(row["distance"]))
                    if item is None:
                        continue
                    seen.add(row["entity_id"])
                    results.append(item)
                    if len(results) >= op.limit:
                        return results

        # ---- keyword leg (always run; provides recall when semantic misses) ----
        like_arg = f"%{op.query}%" if op.query else "%"
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT DISTINCT ON (e.id)
                           e.id AS entity_id,
                           e.name,
                           e.namespace,
                           e.created_at,
                           e.updated_at,
                           o.content
                    FROM kg.observations o
                    JOIN kg.entities e ON e.id = o.entity_id
                    WHERE e.type = %s
                      AND (e.namespace = %s OR e.namespace LIKE %s)
                      AND o.deleted_at IS NULL
                      AND o.content ILIKE %s
                    ORDER BY e.id, o.created_at DESC
                    LIMIT %s
                    """,
                    (
                        ENTITY_TYPE,
                        ns_prefix,
                        ns_like,
                        like_arg,
                        op.limit + op.offset,
                    ),
                )
                rows = await cur.fetchall()
        for row in rows[op.offset :]:
            if row["entity_id"] in seen:
                continue
            item = _row_to_search_item(row, score=None)
            if item is None:
                continue
            seen.add(row["entity_id"])
            results.append(item)
            if len(results) >= op.limit:
                break

        return results

    async def _alist_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT DISTINCT namespace
                    FROM kg.entities
                    WHERE type = %s
                      AND (namespace = %s OR namespace LIKE %s)
                    """,
                    (ENTITY_TYPE, NAMESPACE_ROOT, NAMESPACE_ROOT + "/%"),
                )
                rows = await cur.fetchall()

        namespaces: list[tuple[str, ...]] = []
        for row in rows:
            ns = _ns_from_entity_namespace(row["namespace"])
            if op.max_depth is not None and len(ns) > op.max_depth:
                ns = ns[: op.max_depth]
            if _namespace_matches(ns, op.match_conditions or ()):
                namespaces.append(ns)

        unique = sorted(set(namespaces))
        return unique[op.offset : op.offset + op.limit]

    # ------------------------------------------------------------------
    # Embedding (best-effort)
    # ------------------------------------------------------------------

    async def _embed(self, content: str) -> list[float] | None:
        if not content or not content.strip():
            return None
        url = f"{self._ollama_url}/api/embed"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    url,
                    json={"model": self._embed_model, "input": content},
                )
                r.raise_for_status()
                data = r.json()
                rows = data.get("embeddings") or []
                if not rows or not rows[0]:
                    return None
                return list(rows[0])
        except Exception:
            logger.exception("MCPMemoryStore: embed failed; storing NULL")
            return None


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------

Op = GetOp | PutOp | SearchOp | ListNamespacesOp


def _json_default(value: Any) -> Any:
    """JSON encoder fallback for datetime / tuple."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")


def _parse_value(raw: Any) -> dict[str, Any] | None:
    """Observation content → BaseStore value dict.

    Defensive: skip rows whose content isn't a JSON object (e.g.
    human-seeded entities under `langgraph/*` would not be valid, but
    nothing creates those today).
    """
    if isinstance(raw, dict):
        return raw  # psycopg jsonb codec already deserialized
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _row_to_search_item(row: dict[str, Any], score: float | None) -> SearchItem | None:
    value = _parse_value(row["content"])
    if value is None:
        return None
    ns = _ns_from_entity_namespace(row["namespace"])
    key = _key_from_entity_name(row["name"])
    return SearchItem(
        namespace=ns,
        key=key,
        value=value,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        score=score,
    )


def _embedding_text(value: dict[str, Any], index: Any) -> str:
    """Pick which subset of `value` to send to the embedder.

    - `index=False`: don't embed.
    - `index=list[str]`: concatenate those top-level fields' string repr.
    - `index=None`: embed the whole JSON.
    """
    if index is False:
        return ""
    if isinstance(index, list):
        parts: list[str] = []
        for path in index:
            seg = value.get(path)
            if seg is not None:
                parts.append(str(seg))
        return " ".join(parts)
    return json.dumps(value, default=_json_default)


def _encode_vector(values: list[float]) -> str:
    """Render a Python float vector as the pgvector text input form."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


async def build_pool(database_url: str) -> AsyncConnectionPool:
    """Construct + open the asyncpool used by the store.

    Caller is responsible for `await pool.close()` (use AsyncExitStack).

    The kwargs mirror what `_build_checkpointer` uses for the
    checkpointer pool — same in-cluster network semantics, same
    Cilium-conntrack-can-silently-expire-idle-conns failure mode.
    `tcp_user_timeout` is the load-bearing fix (kernel kills a conn
    after 15s of unacked outgoing data); keepalives are
    defense-in-depth.  See `project_langgraph_reporter_post_node_hang`
    for the full forensics on why both pools need this.
    """
    pool = AsyncConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=5,
        kwargs={
            "row_factory": dict_row,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
            "tcp_user_timeout": 15000,
        },
        check=AsyncConnectionPool.check_connection,
        max_idle=60,
        open=False,
    )
    await pool.open()
    return pool
