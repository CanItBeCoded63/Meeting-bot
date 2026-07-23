from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import ModelRequest, ToolCallPart, UserPromptPart
from joinly_client.main import (
    _MeetingExclusion,
    _compose_action_instructions,
    _compose_memory_instructions,
    _delete_matching_memories_for_exclusion,
    _derive_meeting_id,
    _derive_meeting_occurrence_id,
    _derive_meeting_series_id,
    _derive_single_agent_id,
    _filter_memory_text_by_exclusions,
    _save_meeting_summary_memory,
    _scrub_agent_messages_for_exclusions,
)
from joinly_client.memory import (
    RECALL_MEMORY_TOOL_DEFINITION,
    MemoryEntry,
    build_memory_block,
    recall_memory,
)

_MEETING_ID_LENGTH = 16
_SUMMARY_MEMORY_ENTRY_COUNT = 2
_CURRENT_MEETING_EXCLUSION_DELETE_COUNT = 3
_PROJECT_CONTEXT_MEMORY = (
    "<project_context>\n"
    "- Previous meeting summary: budget approved\n"
    "</project_context>"
)


class _FakeMemoryStore:
    """Minimal in-memory store for single-agent memory tests."""

    def __init__(self, entries: list[MemoryEntry] | None = None) -> None:
        self.entries = entries or []
        self.saved: list[MemoryEntry] = []
        self.deleted: list[tuple[str, str]] = []
        self.queries: list[dict[str, str | int | None]] = []

    async def save(self, entry: MemoryEntry) -> None:
        """Record saved entries."""
        for index, existing in enumerate([*self.entries, *self.saved]):
            if (
                existing.partition_key == entry.partition_key
                and existing.row_key == entry.row_key
            ):
                if index < len(self.entries):
                    self.entries[index] = entry
                else:
                    self.saved[index - len(self.entries)] = entry
                return
        self.saved.append(entry)

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
        """Return matching in-memory entries."""
        self.queries.append(
            {
                "agent_id": agent_id,
                "scope": scope,
                "project_id": project_id,
                "client_id": client_id,
                "meeting_id": meeting_id,
                "participant_id": participant_id,
                "memory_type": memory_type,
                "limit": limit,
            }
        )
        matches = []
        for entry in [*self.entries, *self.saved]:
            if entry.agent_id != agent_id or entry.scope != scope:
                continue
            if project_id is not None and entry.project_id != project_id:
                continue
            if client_id is not None and entry.client_id != client_id:
                continue
            if meeting_id is not None and entry.meeting_id != meeting_id:
                continue
            if participant_id is not None and entry.participant_id != participant_id:
                continue
            if memory_type is not None and entry.memory_type != memory_type:
                continue
            matches.append(entry)
        return matches[:limit]

    async def delete(self, partition_key: str, row_key: str) -> None:
        """Delete matching in-memory entries."""
        self.deleted.append((partition_key, row_key))
        self.entries = [
            entry
            for entry in self.entries
            if (entry.partition_key, entry.row_key) != (partition_key, row_key)
        ]
        self.saved = [
            entry
            for entry in self.saved
            if (entry.partition_key, entry.row_key) != (partition_key, row_key)
        ]

    async def expire_old(self, before: datetime) -> int:
        """No-op expiry."""
        _ = before
        return 0


def test_memory_agent_id_is_stable_and_cli_safe() -> None:
    """Display names should map to stable memory namespaces."""
    assert _derive_single_agent_id("Alex Bot") == "alex-bot"
    assert _derive_single_agent_id("  ") == "joinly"


def test_meeting_id_is_url_and_date_scoped() -> None:
    """Meeting memory ids should be stable for one UTC day."""
    meeting_url = "https://teams.microsoft.com/l/meetup-join/abc"
    meeting_date = datetime(2026, 7, 3, tzinfo=UTC)

    first = _derive_meeting_id(meeting_url, meeting_date)
    second = _derive_meeting_id(meeting_url, meeting_date)
    next_day = _derive_meeting_id(
        meeting_url,
        datetime(2026, 7, 4, tzinfo=UTC),
    )

    assert first == second
    assert first != next_day
    assert len(first) == _MEETING_ID_LENGTH


def test_meeting_series_id_is_link_scoped_across_dates() -> None:
    """Recurring meeting links should keep one stable series memory id."""
    meeting_url = "https://teams.microsoft.com/meet/abc?p=123"
    same_url_different_case = "https://TEAMS.microsoft.com/meet/abc?p=123"
    different_url = "https://teams.microsoft.com/meet/other?p=123"

    assert _derive_meeting_series_id(meeting_url) == _derive_meeting_series_id(
        same_url_different_case
    )
    assert _derive_meeting_series_id(meeting_url) != _derive_meeting_series_id(
        different_url
    )


def test_meeting_occurrence_id_changes_by_date() -> None:
    """Each recurring meeting occurrence should still keep its own history."""
    meeting_url = "https://teams.microsoft.com/meet/abc?p=123"
    this_week = datetime(2026, 7, 6, tzinfo=UTC)
    next_week = datetime(2026, 7, 13, tzinfo=UTC)

    assert _derive_meeting_occurrence_id(
        meeting_url, this_week
    ) != _derive_meeting_occurrence_id(meeting_url, next_week)
    assert _derive_meeting_id(meeting_url, this_week) == _derive_meeting_occurrence_id(
        meeting_url, this_week
    )


def test_memory_instructions_preserve_default_prompt_style() -> None:
    """Memory prompt injection should not drop MPC behavior instructions."""
    instructions = _compose_memory_instructions(
        None,
        "mpc",
        _PROJECT_CONTEXT_MEMORY,
        memory_enabled=True,
    )

    assert instructions is not None
    assert "ONLY" in instructions
    assert "remember" in instructions
    assert "<memory_context>" in instructions
    assert "budget approved" in instructions


def test_memory_instructions_are_unchanged_when_disabled() -> None:
    """Disabled memory should leave custom instructions untouched."""
    assert (
        _compose_memory_instructions(
            "custom instructions",
            "mpc",
            "<project_context>ignored</project_context>",
            memory_enabled=False,
        )
        == "custom instructions"
    )


def test_refreshed_memory_prompt_omits_excluded_topic() -> None:
    """Regenerated agent prompt should not include excluded memory context."""
    memory_block = (
        "<meeting_series_context>\n"
        "- Previous summary: discussed football planning.\n"
        "- Previous summary: approved release plan.\n"
        "</meeting_series_context>"
    )
    exclusions = [_MeetingExclusion(text_or_topic="football")]

    filtered_block = _filter_memory_text_by_exclusions(memory_block, exclusions)
    instructions = _compose_memory_instructions(
        None,
        "mpc",
        filtered_block,
        memory_enabled=True,
    )
    instructions = _compose_action_instructions(instructions, "mpc")

    assert instructions is not None
    assert "football" not in instructions.casefold()
    assert "release plan" in instructions


def test_active_agent_messages_are_scrubbed_after_exclusion() -> None:
    """Excluded text should be removed from buffered LLM conversation history."""
    messages = [
        ModelRequest(
            parts=[
                UserPromptPart(content="Yash: football should be removed."),
                UserPromptPart(content="Yash: release plan stays."),
                ToolCallPart(
                    tool_name="remember",
                    tool_call_id="tool-1",
                    args='{"content": "football should be remembered"}',
                ),
            ]
        )
    ]

    removed = _scrub_agent_messages_for_exclusions(
        messages,
        [_MeetingExclusion(text_or_topic="football")],
    )

    rendered = "\n".join(
        str(getattr(part, "content", "")) + str(getattr(part, "args", ""))
        for message in messages
        for part in message.parts
    )
    assert removed == 2
    assert "football" not in rendered.casefold()
    assert "release plan stays" in rendered


@pytest.mark.asyncio
async def test_meeting_summary_is_saved_to_link_and_occurrence_memory() -> None:
    """End-of-meeting summaries should be separated by meeting link."""
    store = _FakeMemoryStore()

    await _save_meeting_summary_memory(
        store,  # type: ignore[arg-type]
        agent_id="alex",
        project_id="single-agent",
        meeting_series_id="series123",
        meeting_occurrence_id="occurrence123",
        meeting_start_time=datetime(2026, 7, 3, tzinfo=UTC),
        summary_text="OVERVIEW\nThe team agreed to use persistent memory.",
    )

    assert len(store.saved) == _SUMMARY_MEMORY_ENTRY_COUNT
    assert {saved.meeting_id for saved in store.saved} == {
        "series123",
        "occurrence123",
    }
    assert {saved.row_key for saved in store.saved} == {
        "summary-series-occurrence123",
        "summary-current",
    }
    for saved in store.saved:
        assert saved.agent_id == "alex"
        assert saved.scope == "meeting"
        assert saved.memory_type == "fact"
        assert saved.project_id == "single-agent"
        assert saved.source == "auto_extracted"
        assert "persistent memory" in saved.content


@pytest.mark.asyncio
async def test_memory_block_loads_agent_wide_meeting_memory() -> None:
    """Prompt memory should include meeting memories across the current agent."""
    store = _FakeMemoryStore(
        [
            MemoryEntry(
                agent_id="alex",
                scope="project",
                memory_type="fact",
                project_id="single-agent",
                content="Leaked project summary from a different meeting link.",
            ),
            MemoryEntry(
                agent_id="alex",
                scope="agent",
                memory_type="fact",
                content="Leaked agent-wide summary from a different meeting link.",
            ),
            MemoryEntry(
                agent_id="alex",
                scope="global",
                memory_type="fact",
                content="Leaked global summary from a different meeting link.",
            ),
            MemoryEntry(
                agent_id="alex",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="series123",
                content="Prior Sales Sync decided to ship the Teams chat fix.",
            ),
            MemoryEntry(
                agent_id="alex",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="other-series",
                content="Unrelated Client Meeting discussed pricing.",
            ),
            MemoryEntry(
                agent_id="sam",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="sam-series",
                content="Sam-only memory should not appear in Alex context.",
            ),
            MemoryEntry(
                agent_id="alex",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="occurrence123",
                content="Today Alex joined the recurring Sales Sync.",
            ),
        ]
    )

    memory_block = await build_memory_block(
        store,  # type: ignore[arg-type]
        "alex",
        project_id="single-agent",
        meeting_id="occurrence123",
        meeting_series_id="series123",
    )

    assert "<agent_meeting_history>" in memory_block
    assert "meeting_id=series123" in memory_block
    assert "meeting_id=other-series" in memory_block
    assert "<meeting_series_context>" in memory_block
    assert "Prior Sales Sync" in memory_block
    assert "<current_meeting_context>" in memory_block
    assert "Today Alex joined" in memory_block
    assert "Unrelated Client Meeting" in memory_block
    assert "Sam-only memory" not in memory_block
    assert "Leaked project summary" not in memory_block
    assert "Leaked agent-wide summary" not in memory_block
    assert "Leaked global summary" not in memory_block
    assert "<project_context>" not in memory_block
    assert "<agent_preferences>" not in memory_block
    assert "<global_context>" not in memory_block
    assert not any(query["scope"] == "project" for query in store.queries)
    assert not any(query["scope"] == "agent" for query in store.queries)
    assert not any(query["scope"] == "global" for query in store.queries)


@pytest.mark.asyncio
async def test_memory_block_can_opt_into_cross_meeting_context() -> None:
    """Broader memory scopes are only loaded when explicitly requested."""
    store = _FakeMemoryStore(
        [
            MemoryEntry(
                agent_id="alex",
                scope="project",
                memory_type="fact",
                project_id="single-agent",
                content="Project-wide preference: use concise summaries.",
            ),
        ]
    )

    isolated_block = await build_memory_block(
        store,  # type: ignore[arg-type]
        "alex",
        project_id="single-agent",
        meeting_id="occurrence123",
        meeting_series_id="series123",
    )
    cross_meeting_block = await build_memory_block(
        store,  # type: ignore[arg-type]
        "alex",
        project_id="single-agent",
        meeting_id="occurrence123",
        meeting_series_id="series123",
        include_cross_meeting_context=True,
    )

    assert "Project-wide preference" not in isolated_block
    assert "Project-wide preference" in cross_meeting_block


@pytest.mark.asyncio
async def test_current_meeting_exclusion_deletes_only_matching_meeting_memory() -> None:
    """Excluding a topic should delete matching current meeting memory only."""
    store = _FakeMemoryStore(
        [
            MemoryEntry(
                row_key="summary-series-occurrence123",
                agent_id="alex",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="series123",
                content="The meeting included a football discussion.",
            ),
            MemoryEntry(
                row_key="summary-series-old-occurrence",
                agent_id="alex",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="series123",
                content="An older occurrence included a football discussion.",
                created_at=datetime(2024, 1, 1, tzinfo=UTC),
            ),
            MemoryEntry(
                row_key="current-link-remembered-football",
                agent_id="alex",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="series123",
                content="During this meeting, Yash said football is off topic.",
                created_at=datetime(2025, 1, 1, 12, 5, tzinfo=UTC),
            ),
            MemoryEntry(
                row_key="occurrence-football",
                agent_id="alex",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="occurrence123",
                content="Yash said he likes playing football.",
            ),
            MemoryEntry(
                row_key="agent-football",
                agent_id="alex",
                scope="agent",
                memory_type="fact",
                content="Remember that Yash likes football.",
            ),
            MemoryEntry(
                row_key="release-plan",
                agent_id="alex",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="series123",
                content="The team approved the release plan.",
            ),
        ]
    )

    deleted = await _delete_matching_memories_for_exclusion(
        store,  # type: ignore[arg-type]
        agent_id="alex",
        project_id="single-agent",
        meeting_series_id="series123",
        meeting_occurrence_id="occurrence123",
        meeting_start_time=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        exclusion=_MeetingExclusion(text_or_topic="football"),
    )

    assert deleted == _CURRENT_MEETING_EXCLUSION_DELETE_COUNT
    remaining_meeting_content = "\n".join(
        entry.content for entry in store.entries if entry.scope == "meeting"
    )
    remaining_agent_content = "\n".join(
        entry.content for entry in store.entries if entry.scope == "agent"
    )
    current_meeting_content = "\n".join(
        entry.content
        for entry in store.entries
        if entry.meeting_id in {"occurrence123", "series123"}
        and entry.row_key != "summary-series-old-occurrence"
    )
    assert "football" not in current_meeting_content.casefold()
    assert "older occurrence" in remaining_meeting_content.casefold()
    assert "football" in remaining_agent_content.casefold()
    assert "release plan" in remaining_meeting_content


def test_runtime_exclusion_filters_memory_context_text() -> None:
    """Matching current-memory lines should stay hidden after exclusion."""
    memory_text = (
        "<meeting_series_context>\n"
        "- Previous summary: discussed blue canyon rollout.\n"
        "- Previous summary: approved release plan.\n"
        "</meeting_series_context>"
    )

    filtered = _filter_memory_text_by_exclusions(
        memory_text,
        [_MeetingExclusion(text_or_topic="blue canyon")],
    )

    assert "blue canyon" not in filtered
    assert "release plan" in filtered


@pytest.mark.asyncio
async def test_recall_memory_filters_by_agent_and_meeting_id() -> None:
    """The recall tool should support exact meeting-id lookup per agent."""
    store = _FakeMemoryStore(
        [
            MemoryEntry(
                agent_id="alex",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="target-series",
                content="Target meeting memory for Alex.",
            ),
            MemoryEntry(
                agent_id="alex",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="other-series",
                content="Other meeting memory for Alex.",
            ),
            MemoryEntry(
                agent_id="sam",
                scope="meeting",
                memory_type="fact",
                project_id="single-agent",
                meeting_id="target-series",
                content="Sam memory for the same meeting id.",
            ),
        ]
    )

    recalled = await recall_memory(
        store,  # type: ignore[arg-type]
        "alex",
        "single-agent",
        None,
        meeting_id="target-series",
    )

    assert "Target meeting memory for Alex" in recalled
    assert "meeting_id=target-series" in recalled
    assert "Other meeting memory" not in recalled
    assert "Sam memory" not in recalled


def test_recall_memory_tool_supports_current_link_shortcut() -> None:
    """Alex should have a natural shortcut for this meeting link's history."""
    schema = RECALL_MEMORY_TOOL_DEFINITION.parameters_json_schema
    meeting_context = schema["properties"]["meeting_context"]

    assert meeting_context["default"] == "all"
    assert meeting_context["enum"] == [
        "all",
        "current_link",
        "current_occurrence",
    ]
    assert "current meeting link" in meeting_context["description"]
