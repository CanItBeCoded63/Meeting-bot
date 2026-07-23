"""Memory subsystem for joinly conversational agents.

Provides persistent cross-meeting memory via Azure Table Storage with an
LRU-cached wrapper and a ``build_memory_block()`` helper that assembles
scoped context for system-prompt injection.

Exports
-------
MemoryEntry, MemoryStore, AzureTableMemoryStore, CachedMemoryStore,
build_memory_block, write_memory, recall_memory, REMEMBER_TOOL_DEFINITION,
RECALL_MEMORY_TOOL_DEFINITION
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, runtime_checkable

import cachetools
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables.aio import TableClient, TableServiceClient
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_ai.tools import ToolDefinition

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TABLE_NAME = "joinlymemory"

_SCOPE_VALUES: frozenset[str] = frozenset(
    {"agent", "project", "client", "meeting", "participant", "global"}
)
_TYPE_VALUES: frozenset[str] = frozenset(
    {
        "fact",
        "preference",
        "decision",
        "action_item",
        "participant_profile",
        "relationship",
    }
)
_PUBLIC_TYPE_VALUES: frozenset[str] = _TYPE_VALUES
_RECALLABLE_SCOPE_VALUES: frozenset[str] = frozenset(
    {"agent", "project", "client", "meeting", "participant", "global"}
)
_TTL_DEFAULTS_DAYS: dict[str, int | None] = {
    "fact": None,
    "preference": None,
    "decision": None,
    "action_item": 30,
    "participant_profile": 365,
    "relationship": None,
}

# ---------------------------------------------------------------------------
# MemoryEntry
# ---------------------------------------------------------------------------

_ERR_PROJECT_ID = "project_id required for scope='project'"
_ERR_CLIENT_ID = "client_id required for scope='client'"
_ERR_MEETING_ID = "meeting_id required for scope='meeting'"
_ERR_PARTICIPANT_ID = "participant_id required for scope='participant'"


class MemoryEntry(BaseModel):
    """A single piece of persisted agent memory."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    row_key: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # Identity dimensions
    agent_id: str
    scope: Literal["agent", "project", "client", "meeting", "participant", "global"]
    memory_type: Literal[
        "fact",
        "preference",
        "decision",
        "action_item",
        "participant_profile",
        "relationship",
    ]

    # Scoping dimensions
    project_id: str | None = None
    client_id: str | None = None
    meeting_id: str | None = None
    participant_id: str | None = None
    participant_id_type: (
        Literal["email", "gaia_id", "aad_id", "anonymous_hash"] | None
    ) = None

    # Content
    content: str = Field(min_length=1, max_length=10_000)
    source: Literal["explicit_tool", "auto_extracted"] = "explicit_tool"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    # Lifecycle
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    # Schema version for future migrations
    version: str = "1"

    @computed_field  # type: ignore[misc]
    @property
    def partition_key(self) -> str:
        """Composite partition key: ``<agent_id>:<scope>``."""
        return f"{self.agent_id}:{self.scope}"

    @model_validator(mode="after")
    def validate_scope_dimensions(self) -> MemoryEntry:
        """Ensure required IDs are present for each scope."""
        if self.scope == "project" and not self.project_id:
            raise ValueError(_ERR_PROJECT_ID)
        if self.scope == "client" and not self.client_id:
            raise ValueError(_ERR_CLIENT_ID)
        if self.scope == "meeting" and not self.meeting_id:
            raise ValueError(_ERR_MEETING_ID)
        if self.scope == "participant" and not self.participant_id:
            raise ValueError(_ERR_PARTICIPANT_ID)
        return self

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def ensure_utc(cls, v: datetime | str | None) -> datetime | str | None:
        """Attach UTC timezone to naive datetimes."""
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    def is_expired(self) -> bool:
        """Return True if this entry has passed its expiry time."""
        return self.expires_at is not None and datetime.now(UTC) >= self.expires_at


# ---------------------------------------------------------------------------
# MemoryStore Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryStore(Protocol):
    """Minimal async interface for memory backends."""

    async def save(self, entry: MemoryEntry) -> None:
        """Persist or update a memory entry."""
        ...

    async def query(  # noqa: PLR0913
        self,
        agent_id: str,
        scope: str,
        *,
        project_id: str | None = None,
        client_id: str | None = None,
        meeting_id: str | None = None,
        participant_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Return up to *limit* non-expired entries matching the given filters."""
        ...

    async def delete(self, partition_key: str, row_key: str) -> None:
        """Remove a specific entry by its composite key."""
        ...

    async def expire_old(self, before: datetime) -> int:
        """Delete all entries whose ``expires_at`` is before *before*. Returns count."""
        ...


# ---------------------------------------------------------------------------
# OData helpers
# ---------------------------------------------------------------------------


def _sanitize_odata_value(v: str) -> str:
    """Escape single quotes for use in an OData filter string."""
    return v.replace("'", "''")


# ---------------------------------------------------------------------------
# Azure Table Storage deserialization
# ---------------------------------------------------------------------------


def _entity_to_memory(entity: dict[str, Any]) -> MemoryEntry:
    """Convert an Azure Table Storage entity dict to a :class:`MemoryEntry`."""
    skip = {"PartitionKey", "RowKey", "Timestamp", "etag"}
    raw: dict[str, Any] = {
        k: v for k, v in entity.items() if not k.startswith("odata.") and k not in skip
    }
    raw["row_key"] = entity["RowKey"]
    # Parse datetime strings back to UTC-aware datetime objects
    for dt_field in ("created_at", "expires_at"):
        if dt_field in raw and isinstance(raw[dt_field], str):
            raw[dt_field] = datetime.fromisoformat(raw[dt_field]).replace(tzinfo=UTC)
    return MemoryEntry(**raw)


# ---------------------------------------------------------------------------
# AzureTableMemoryStore
# ---------------------------------------------------------------------------


class AzureTableMemoryStore:
    """Azure Table Storage-backed :class:`MemoryStore` implementation."""

    def __init__(self, connection_string: str) -> None:
        """Initialise the store with an Azure Storage connection string."""
        self._connection_string = connection_string
        self._service: TableServiceClient = TableServiceClient.from_connection_string(
            connection_string
        )
        self._table: TableClient | None = None
        self._init_lock = asyncio.Lock()

    async def _ensure_table(self) -> TableClient:
        """Lazily create or retrieve the Azure Table client (thread-safe)."""
        if self._table is not None:
            return self._table
        async with self._init_lock:
            # Double-checked locking
            if self._table is not None:
                return self._table
            try:
                self._table = await self._service.create_table(TABLE_NAME)
            except ResourceExistsError:
                self._table = self._service.get_table_client(TABLE_NAME)
        return self._table  # type: ignore[return-value]

    async def save(self, entry: MemoryEntry) -> None:
        """Upsert a :class:`MemoryEntry` into the table."""
        table = await self._ensure_table()
        raw = entry.model_dump(mode="json")
        entity: dict[str, Any] = {
            k: v for k, v in raw.items() if v is not None and k != "partition_key"
        }
        entity["PartitionKey"] = entry.partition_key
        entity["RowKey"] = entry.row_key
        await table.upsert_entity(entity)

    async def query(  # noqa: PLR0913
        self,
        agent_id: str,
        scope: str,
        *,
        project_id: str | None = None,
        client_id: str | None = None,
        meeting_id: str | None = None,
        participant_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Return up to *limit* non-expired matching entries."""
        table = await self._ensure_table()
        pk = _sanitize_odata_value(f"{agent_id}:{scope}")
        filters = [f"PartitionKey eq '{pk}'"]
        if project_id:
            filters.append(f"project_id eq '{_sanitize_odata_value(project_id)}'")
        if client_id:
            filters.append(f"client_id eq '{_sanitize_odata_value(client_id)}'")
        if meeting_id:
            filters.append(f"meeting_id eq '{_sanitize_odata_value(meeting_id)}'")
        if participant_id:
            filters.append(
                f"participant_id eq '{_sanitize_odata_value(participant_id)}'"
            )
        if memory_type:
            filters.append(f"memory_type eq '{_sanitize_odata_value(memory_type)}'")

        entries: list[MemoryEntry] = []
        async for entity in table.query_entities(
            query_filter=" and ".join(filters),
            results_per_page=min(limit, 100),
        ):
            try:
                entry = _entity_to_memory(entity)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Skipping malformed memory entity: %s", entity.get("RowKey")
                )
                continue
            if entry.is_expired():
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries[:limit]

    async def delete(self, partition_key: str, row_key: str) -> None:
        """Delete a single entry; silently ignores missing entries."""
        table = await self._ensure_table()
        with contextlib.suppress(ResourceNotFoundError):
            await table.delete_entity(partition_key=partition_key, row_key=row_key)

    async def close(self) -> None:
        """Close underlying Azure async clients."""
        if self._table is not None:
            await self._table.close()
        await self._service.close()

    async def expire_old(self, before: datetime) -> int:
        """Bulk-delete entries whose ``expires_at`` is before *before*.

        Uses ``submit_transaction`` in batches of 100, grouped by partition key
        (Azure Table Storage requires all operations in a batch share the same
        partition key).
        """
        table = await self._ensure_table()
        before_iso = _sanitize_odata_value(before.astimezone(UTC).isoformat())
        filter_str = f"expires_at le '{before_iso}'"

        to_delete: list[dict[str, str]] = [
            {"PartitionKey": entity["PartitionKey"], "RowKey": entity["RowKey"]}
            async for entity in table.query_entities(query_filter=filter_str)
        ]

        deleted = 0
        batch_size = 100
        for i in range(0, len(to_delete), batch_size):
            chunk = to_delete[i : i + batch_size]
            # Group by PartitionKey — Table Storage batches require same partition
            by_partition: dict[
                str, list[tuple[str, dict[str, str], dict[str, Any]]]
            ] = defaultdict(list)
            for e in chunk:
                by_partition[e["PartitionKey"]].append(("delete", e, {}))
            for ops in by_partition.values():
                try:
                    await table.submit_transaction(ops)
                    deleted += len(ops)
                except Exception:
                    logger.exception(
                        "Batch delete failed for partition %s",
                        ops[0][1]["PartitionKey"] if ops else "<unknown>",
                    )
        return deleted


# ---------------------------------------------------------------------------
# CachedMemoryStore
# ---------------------------------------------------------------------------


class CachedMemoryStore:
    """Write-through LRU/TTL cache wrapping any :class:`MemoryStore` backend.

    Uses :class:`cachetools.TTLCache` to bound memory and automatically evict
    stale results.  A Future-based sentinel prevents duplicate in-flight fetches
    for the same cache key (double-fetch prevention).
    """

    def __init__(
        self,
        backend: MemoryStore,
        ttl_secs: float = 300.0,
        maxsize: int = 256,
    ) -> None:
        """Wrap *backend* with an LRU/TTL cache.

        *maxsize* controls the entry cap; *ttl_secs* is the eviction window.
        """
        self._backend = backend
        self._ttl = ttl_secs
        self._cache: cachetools.TTLCache = cachetools.TTLCache(
            maxsize=maxsize, ttl=ttl_secs
        )
        self._in_flight: dict[str, asyncio.Future[list[MemoryEntry]]] = {}
        self._lock = asyncio.Lock()

    def _cache_key(  # noqa: PLR0913
        self,
        agent_id: str,
        scope: str,
        project_id: str | None,
        client_id: str | None,
        meeting_id: str | None,
        participant_id: str | None,
        memory_type: str | None,
        limit: int,
    ) -> str:
        parts = [agent_id, scope]
        for k, v in sorted(
            {
                "client_id": client_id,
                "limit": str(limit),
                "meeting_id": meeting_id,
                "memory_type": memory_type,
                "participant_id": participant_id,
                "project_id": project_id,
            }.items()
        ):
            if v is not None:
                parts.append(f"{k}={v}")
        return ":".join(parts)

    async def query(  # noqa: PLR0913
        self,
        agent_id: str,
        scope: str,
        *,
        project_id: str | None = None,
        client_id: str | None = None,
        meeting_id: str | None = None,
        participant_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Return cached results, fetching from the backend on a cache miss."""
        key = self._cache_key(
            agent_id,
            scope,
            project_id,
            client_id,
            meeting_id,
            participant_id,
            memory_type,
            limit,
        )

        async with self._lock:
            if key in self._cache:
                return self._cache[key]
            if key in self._in_flight:
                fut = self._in_flight[key]
                fetch = False
            else:
                loop = asyncio.get_event_loop()
                fut: asyncio.Future[list[MemoryEntry]] = loop.create_future()
                self._in_flight[key] = fut
                fetch = True

        if fetch:
            try:
                result = await self._backend.query(
                    agent_id,
                    scope,
                    project_id=project_id,
                    client_id=client_id,
                    meeting_id=meeting_id,
                    participant_id=participant_id,
                    memory_type=memory_type,
                    limit=limit,
                )
            except Exception as exc:
                async with self._lock:
                    self._in_flight.pop(key, None)
                fut.set_exception(exc)
                raise
            else:
                async with self._lock:
                    self._cache[key] = result
                    self._in_flight.pop(key, None)
                fut.set_result(result)
                return result
        return await fut

    async def save(self, entry: MemoryEntry) -> None:
        """Persist an entry and invalidate related cache keys."""
        await self._backend.save(entry)
        prefix = f"{entry.agent_id}:{entry.scope}"
        async with self._lock:
            for k in list(self._cache.keys()):
                if k.startswith(prefix):
                    del self._cache[k]

    async def delete(self, partition_key: str, row_key: str) -> None:
        """Delete an entry and invalidate related cache keys."""
        await self._backend.delete(partition_key, row_key)
        async with self._lock:
            for k in list(self._cache.keys()):
                if k.startswith(partition_key):
                    del self._cache[k]

    async def close(self) -> None:
        """Close the wrapped backend when it exposes a close hook."""
        async with self._lock:
            self._cache.clear()
            self._in_flight.clear()

        close = getattr(self._backend, "close", None)
        if close is not None:
            await close()

    async def expire_old(self, before: datetime) -> int:
        """Expire old entries and clear the entire cache."""
        result = await self._backend.expire_old(before)
        async with self._lock:
            self._cache.clear()
        return result


# ---------------------------------------------------------------------------
# build_memory_block — internal helpers
# ---------------------------------------------------------------------------


def _collect_scope_tasks(  # noqa: PLR0913
    store: MemoryStore,
    agent_id: str,
    project_id: str | None,
    client_id: str | None,
    meeting_id: str | None,
    meeting_series_id: str | None,
    participant_ids: list[str] | None,
    max_per_scope: int,
    *,
    include_agent_meeting_history: bool,
    include_cross_meeting_context: bool,
) -> dict[str, Any]:
    """Build the dict of coroutine tasks for each memory scope."""
    tasks: dict[str, Any] = {}
    if include_cross_meeting_context:
        tasks["agent"] = store.query(agent_id, "agent", limit=5)
        tasks["global"] = store.query(agent_id, "global", limit=5)
        if client_id:
            tasks["client"] = store.query(
                agent_id, "client", client_id=client_id, limit=5
            )
        if project_id:
            tasks["project"] = store.query(
                agent_id, "project", project_id=project_id, limit=max_per_scope
            )
    if include_agent_meeting_history:
        tasks["agent_meeting_history"] = store.query(
            agent_id,
            "meeting",
            project_id=project_id,
            memory_type="fact",
            limit=max_per_scope,
        )
        tasks["agent_decisions"] = store.query(
            agent_id,
            "meeting",
            project_id=project_id,
            memory_type="decision",
            limit=max_per_scope,
        )
        tasks["agent_action_items"] = store.query(
            agent_id,
            "meeting",
            project_id=project_id,
            memory_type="action_item",
            limit=5,
        )
    if meeting_series_id:
        tasks["meeting_series"] = store.query(
            agent_id,
            "meeting",
            project_id=project_id,
            meeting_id=meeting_series_id,
            memory_type="fact",
            limit=max_per_scope,
        )
    if meeting_id:
        tasks["current_meeting"] = store.query(
            agent_id,
            "meeting",
            project_id=project_id,
            meeting_id=meeting_id,
            memory_type="fact",
            limit=max_per_scope,
        )
    decision_meeting_id = meeting_series_id or meeting_id
    if decision_meeting_id:
        tasks["decisions"] = store.query(
            agent_id,
            "meeting",
            project_id=project_id,
            meeting_id=decision_meeting_id,
            memory_type="decision",
            limit=max_per_scope,
        )
        tasks["action_items"] = store.query(
            agent_id,
            "meeting",
            project_id=project_id,
            meeting_id=decision_meeting_id,
            memory_type="action_item",
            limit=5,
        )
    if participant_ids:
        tasks["participants"] = asyncio.gather(
            *[
                store.query(agent_id, "participant", participant_id=pid, limit=3)
                for pid in participant_ids
            ],
            return_exceptions=True,
        )
    return tasks


def _format_entries(
    entries: list[MemoryEntry],
    *,
    include_meeting_id: bool = False,
) -> str:
    """Render a list of entries as Markdown bullet lines."""
    low_confidence_threshold = 0.9
    lines = []
    for e in entries:
        prefix = "[AUTO] " if e.confidence < low_confidence_threshold else ""
        meeting_prefix = ""
        if include_meeting_id and e.meeting_id:
            meeting_prefix = f"[meeting_id={e.meeting_id}] "
        lines.append(f"- {prefix}{meeting_prefix}{e.content}")
    return "\n".join(lines)


def _section_text(
    tag: str,
    entries: list[MemoryEntry],
    *,
    include_meeting_id: bool = False,
) -> str:
    """Render one XML-tagged memory section."""
    return (
        f"<{tag}>\n"
        f"{_format_entries(entries, include_meeting_id=include_meeting_id)}\n"
        f"</{tag}>"
    )


def _assemble_sections(data: dict[str, list[MemoryEntry]]) -> str:
    """Build the final XML-tagged memory block from resolved scope data."""
    section_specs = [
        ("agent", "agent_preferences", False),
        ("global", "global_context", False),
        ("client", "client_context", False),
        ("project", "project_context", False),
        ("agent_meeting_history", "agent_meeting_history", True),
        ("meeting_series", "meeting_series_context", False),
        ("current_meeting", "current_meeting_context", False),
        ("agent_decisions", "agent_decisions", True),
        ("agent_action_items", "agent_action_items", True),
        ("decisions", "recent_decisions", False),
        ("action_items", "open_action_items", False),
        ("participants", "participant_profiles", False),
    ]
    sections = [
        _section_text(tag, entries, include_meeting_id=include_meeting_id)
        for key, tag, include_meeting_id in section_specs
        if (entries := data.get(key))
    ]
    return "\n\n".join(sections)


def _resolve_results(
    keys: list[str],
    raw_results: list[Any],
    max_per_scope: int,
) -> dict[str, list[MemoryEntry]]:
    """Map raw gather results to typed lists, logging individual failures."""
    data: dict[str, list[MemoryEntry]] = {}
    for key, result in zip(keys, raw_results, strict=False):
        if isinstance(result, BaseException):
            logger.warning("Memory query failed for %s: %s", key, result)
            data[key] = []
        elif key == "participants":
            flat: list[MemoryEntry] = []
            for sub in result:
                if isinstance(sub, list):
                    flat.extend(sub)
            data[key] = flat[:max_per_scope]
        else:
            data[key] = result[:max_per_scope]
    return data


# ---------------------------------------------------------------------------
# build_memory_block — public API
# ---------------------------------------------------------------------------


async def build_memory_block(  # noqa: PLR0913
    store: MemoryStore,
    agent_id: str,
    *,
    project_id: str | None = None,
    client_id: str | None = None,
    meeting_id: str | None = None,
    meeting_series_id: str | None = None,
    participant_ids: list[str] | None = None,
    max_per_scope: int = 10,
    include_agent_meeting_history: bool = True,
    include_cross_meeting_context: bool = False,
) -> str:
    """Assemble a structured memory block for system-prompt injection.

    Queries are issued in parallel.  If the store is unavailable or a query
    fails, the affected section is silently omitted so the agent can still
    proceed without memory context.

    By default, meeting memory is agent-wide: recent memories from every meeting
    the current agent participated in are loaded, and current meeting
    series/occurrence memory is also loaded separately. Broader
    agent/project/client/global scopes must still be opted in because they are
    not tied to meeting IDs.

    Returns an empty string on total failure.
    """
    tasks = _collect_scope_tasks(
        store,
        agent_id,
        project_id,
        client_id,
        meeting_id,
        meeting_series_id,
        participant_ids,
        max_per_scope,
        include_agent_meeting_history=include_agent_meeting_history,
        include_cross_meeting_context=include_cross_meeting_context,
    )

    try:
        raw_results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    except Exception:
        logger.exception("Memory store unavailable, proceeding without memory")
        return ""

    data = _resolve_results(list(tasks.keys()), list(raw_results), max_per_scope)
    return _assemble_sections(data)


# ---------------------------------------------------------------------------
# write_memory — tool executor helper
# ---------------------------------------------------------------------------

_ERR_NO_PROJECT = (
    "Cannot remember with scope='project': no project_id in current context."
)
_ERR_NO_CLIENT = "Cannot remember with scope='client': no client_id in current context."
_ERR_NO_MEETING = (
    "Cannot remember with scope='meeting': no meeting_id in current context."
)
_ERR_NO_PARTICIPANT = (
    "Cannot remember with scope='participant': participant_id is required."
)

# Map scope → (required_kwarg_name, error_string) for compact validation
_SCOPE_CONTEXT_CHECKS: tuple[tuple[str, str | None, str], ...] = (
    ("project", None, _ERR_NO_PROJECT),
    ("client", None, _ERR_NO_CLIENT),
    ("meeting", None, _ERR_NO_MEETING),
    ("participant", None, _ERR_NO_PARTICIPANT),
)


def _validate_write_context(  # noqa: PLR0913
    scope: str,
    memory_type: str,
    project_id: str | None,
    client_id: str | None,
    meeting_id: str | None,
    participant_id: str | None,
) -> str | None:
    """Return an error string if context is invalid, else None."""
    if scope not in _SCOPE_VALUES:
        return f"Invalid scope '{scope}'. Valid: {sorted(_SCOPE_VALUES)}"
    if memory_type not in _PUBLIC_TYPE_VALUES:
        return f"Invalid memory_type '{memory_type}'. Valid: {sorted(_PUBLIC_TYPE_VALUES)}"
    checks = [
        ("project", project_id, _ERR_NO_PROJECT),
        ("client", client_id, _ERR_NO_CLIENT),
        ("meeting", meeting_id, _ERR_NO_MEETING),
        ("participant", participant_id, _ERR_NO_PARTICIPANT),
    ]
    for req_scope, ctx_value, err in checks:
        if scope == req_scope and not ctx_value:
            return err
    return None


async def write_memory(  # noqa: PLR0913
    store: MemoryStore,
    agent_id: str,
    project_id: str | None,
    client_id: str | None,
    meeting_id: str | None,
    meeting_participant_ids: set[str],
    *,
    content: str,
    scope: str,
    memory_type: str,
    participant_id: str | None = None,
) -> str:
    """Execute the ``remember`` tool.

    Validates context availability, constructs a :class:`MemoryEntry`, and
    fires a background save so the agent turn is not blocked.

    Returns a human-readable confirmation string.
    """
    err = _validate_write_context(
        scope, memory_type, project_id, client_id, meeting_id, participant_id
    )
    if err:
        return err

    if (
        participant_id
        and meeting_participant_ids
        and participant_id not in meeting_participant_ids
    ):
        logger.warning(
            "participant_id %r not in meeting roster, storing anyway", participant_id
        )

    ttl_days = _TTL_DEFAULTS_DAYS.get(memory_type)
    expires_at = (
        datetime.now(UTC) + timedelta(days=ttl_days) if ttl_days is not None else None
    )

    entry = MemoryEntry(
        agent_id=agent_id,
        scope=scope,  # type: ignore[arg-type]
        memory_type=memory_type,  # type: ignore[arg-type]
        project_id=project_id if scope in ("project", "meeting") else None,
        client_id=client_id if scope == "client" else None,
        meeting_id=meeting_id if scope == "meeting" else None,
        participant_id=participant_id,
        content=content,
        source="explicit_tool",
        expires_at=expires_at,
    )

    # Fire-and-forget — do not block the agent turn
    task = asyncio.create_task(store.save(entry))
    # Suppress "task was destroyed but it is pending" warnings at interpreter exit
    task.add_done_callback(
        lambda t: logger.warning("Memory save failed: %s", t.exception())
        if not t.cancelled() and t.exception()
        else None
    )

    max_preview = 80
    truncated = content[:max_preview] + ("..." if len(content) > max_preview else "")
    return f"Remembered [{scope}/{memory_type}]: {truncated}"


async def recall_memory(  # noqa: PLR0913
    store: MemoryStore,
    agent_id: str,
    project_id: str | None,
    client_id: str | None,
    *,
    scope: str = "meeting",
    meeting_id: str | None = None,
    memory_type: str | None = None,
    participant_id: str | None = None,
    limit: int = 10,
    excluded_meeting_ids: set[str] | None = None,
) -> str:
    """Retrieve persisted memory for the current agent namespace.

    Args:
        excluded_meeting_ids: Optional set of meeting_id values to suppress.
            Rows whose meeting_id is in this set are filtered out before the
            response is formatted.  Pass the set from the
            ``JOINLY_MEMORY_EXCLUDE_MEETING_IDS`` env var (comma-separated).
    """
    if scope not in _RECALLABLE_SCOPE_VALUES:
        return f"Invalid scope '{scope}'. Valid: {sorted(_RECALLABLE_SCOPE_VALUES)}"
    if memory_type and memory_type not in _PUBLIC_TYPE_VALUES:
        return f"Invalid memory_type '{memory_type}'. Valid: {sorted(_PUBLIC_TYPE_VALUES)}"

    safe_limit = max(1, min(limit, 25))
    entries = await store.query(
        agent_id,
        scope,
        project_id=project_id if scope in ("project", "meeting") else None,
        client_id=client_id if scope == "client" else None,
        meeting_id=meeting_id if scope == "meeting" else None,
        participant_id=participant_id if scope == "participant" else None,
        memory_type=memory_type,
        limit=safe_limit,
    )

    # Apply exclusion filter: drop any entry whose meeting_id is in the
    # caller-supplied exclusion set.
    if excluded_meeting_ids and entries:
        before = len(entries)
        entries = [e for e in entries if e.meeting_id not in excluded_meeting_ids]
        if len(entries) < before:
            logger.debug(
                "recall_memory: suppressed %d entries from excluded meetings.",
                before - len(entries),
            )

    if not entries:
        scope_hint = f" scope={scope}"
        meeting_hint = f" meeting_id={meeting_id}" if meeting_id else ""
        type_hint = f" memory_type={memory_type}" if memory_type else ""
        return (
            f"No memory found for agent={agent_id}"
            f"{scope_hint}{meeting_hint}{type_hint}."
        )

    formatted = _format_entries(entries, include_meeting_id=True)
    return (
        f"<recalled_memory agent_id=\"{agent_id}\" scope=\"{scope}\">\n"
        f"{formatted}\n"
        "</recalled_memory>"
    )


# ---------------------------------------------------------------------------
# REMEMBER_TOOL_DEFINITION
# ---------------------------------------------------------------------------

_REMEMBER_DESCRIPTION = (
    "Persist important information for future meetings. Use after learning something "
    "worth keeping. "
    "For meeting discussions, summaries, decisions, and action items, use "
    "scope='meeting'. Meeting memory is namespaced by your agent_id and can be "
    "recalled across all meetings you participated in, while each item still keeps "
    "its meeting_id for specific lookup. Do not store meeting-specific history in "
    "project, client, agent, or global memory. "
    "scope options: "
    "'agent' (your own preferences, e.g. 'User prefers bullet summaries'), "
    "'project' (non-meeting-specific project facts, e.g. "
    "'Project uses Jira for tracking'), "
    "'client' (non-meeting-specific org context, e.g. "
    "'Contoso is a 500-person manufacturing firm'), "
    "'meeting' (durable context for this meeting link/series, e.g. "
    "'Decided to postpone launch to Q3'), "
    "'participant' (person info, requires participant_id, "
    "e.g. 'Alice is CTO, final decision-maker'), "
    "'global' (shared non-meeting-specific facts across all agents). "
    "memory_type options: fact, preference, decision, action_item, "
    "participant_profile, relationship."
)

_RECALL_DESCRIPTION = (
    "Retrieve persisted memory for the current agent. Use this when a participant "
    "asks what you remember, asks about history from other meetings, or gives a "
    "specific meeting_id. Defaults to scope='meeting' across all meetings for this "
    "agent. Provide meeting_id to filter to one meeting link or occurrence. Use "
    "meeting_context='current_link' when asked about this meeting link's history, "
    "or meeting_context='current_occurrence' when asked only about this occurrence."
)

REMEMBER_TOOL_DEFINITION = ToolDefinition(
    name="remember",
    description=_REMEMBER_DESCRIPTION,
    parameters_json_schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "What to remember. 1-3 sentences, specific and factual.",
            },
            "scope": {
                "type": "string",
                "enum": sorted(_SCOPE_VALUES),
            },
            "memory_type": {
                "type": "string",
                "enum": sorted(_PUBLIC_TYPE_VALUES),
            },
            "participant_id": {
                "type": "string",
                "description": (
                    "Email or platform ID of the person this memory is about. "
                    "Required for scope='participant'."
                ),
            },
        },
        "required": ["content", "scope", "memory_type"],
    },
)

RECALL_MEMORY_TOOL_DEFINITION = ToolDefinition(
    name="recall_memory",
    description=_RECALL_DESCRIPTION,
    parameters_json_schema={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": sorted(_RECALLABLE_SCOPE_VALUES),
                "default": "meeting",
            },
            "meeting_id": {
                "type": "string",
                "description": (
                    "Optional meeting series or occurrence id. Only used for "
                    "scope='meeting'."
                ),
            },
            "meeting_context": {
                "type": "string",
                "enum": ["all", "current_link", "current_occurrence"],
                "default": "all",
                "description": (
                    "Meeting filter shortcut for scope='meeting'. Use "
                    "current_link for the current meeting link/series, "
                    "current_occurrence for only today's joined occurrence, or "
                    "all to recall every meeting this agent joined."
                ),
            },
            "memory_type": {
                "type": "string",
                "enum": sorted(_PUBLIC_TYPE_VALUES),
            },
            "participant_id": {
                "type": "string",
                "description": (
                    "Optional participant id. Only used for scope='participant'."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 25,
                "default": 10,
            },
        },
    },
)
