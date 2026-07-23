from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import click
from dotenv import load_dotenv
from fastmcp import Client, FastMCP

from joinly_client.client import JoinlyClient
from joinly_client.prompts import DYADIC_INSTRUCTIONS, MPC_INSTRUCTIONS
from joinly_client.types import (
    McpClientConfig,
    MeetingChatMessage,
    MeetingParticipant,
    SpeakerRole,
    ToolExecutor,
    TranscriptSegment,
)
from joinly_client.utils import get_llm, get_prompt, load_tools

if TYPE_CHECKING:
    from joinly_client.memory import MemoryStore

logger = logging.getLogger(__name__)
_SUMMARY_CHAT_CHUNK_SIZE = 450
_SUMMARY_CHAT_SEND_ATTEMPTS = 3
_SUMMARY_CHAT_RETRY_SECONDS = 2.0
_SUMMARY_BEFORE_LEAVE_TIMEOUT_SECONDS = 90.0
_FALLBACK_TRANSCRIPT_LINES = 20
# Grace period at meeting start: bot waits this long before auto-leaving
# when no other participants have joined yet.
_AUTOLEAVE_GRACE_SECONDS = 300  # 5 minutes
# How long the bot stays alone after the last participant leaves before
# posting the summary.
_AUTOLEAVE_SUMMARY_SECONDS = 60  # 1 minute
# How long the bot stays alone after the last participant leaves before leaving.
_AUTOLEAVE_ALONE_SECONDS = 120  # 2 minutes
_ASSISTIVE_QUIET_GAP_SECONDS = 18.0
_ASSISTIVE_COOLDOWN_SECONDS = 180.0
_ASSISTIVE_MAX_PENDING_ITEMS = 5
_DEFAULT_MEMORY_PROJECT_ID = "single-agent"
_MEMORY_SUMMARY_MAX_CHARS = 9500
_EXCLUSION_MIN_TEXT_CHARS = 3
_EXCLUSION_DELETE_SCAN_LIMIT = 500
_EXCLUSION_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "before",
        "chat",
        "conversation",
        "discussion",
        "forget",
        "forgot",
        "from",
        "ignore",
        "ignored",
        "include",
        "included",
        "memory",
        "meeting",
        "omit",
        "omitted",
        "part",
        "redact",
        "redacted",
        "remove",
        "removed",
        "remember",
        "summary",
        "summaries",
        "that",
        "this",
        "topic",
    }
)
_ABSENT_PARTICIPANT_INFO_RE = re.compile(
    # Teams roster infos that indicate someone is NOT actively in the call.
    r"\b(left|not\s+in\s+(?:the\s+)?meeting|invited|waiting|declined"
    r"|offline)\b",
    re.IGNORECASE,
)
_PRESENCE_ONLY_PARTICIPANT_INFO_RE = re.compile(
    r"\b(available|busy|away|do\s+not\s+disturb)\b",
    re.IGNORECASE,
)
_ACTIVE_MEETING_PARTICIPANT_INFO_RE = re.compile(
    r"\b(muted|unmuted|organizer|presenter)\b",
    re.IGNORECASE,
)
_MEETING_SUMMARY_SYSTEM_PROMPT = (
    "You are a professional meeting summariser. "
    "Given a meeting transcript, you MUST always produce one complete, polished "
    "Teams chat summary using Markdown formatting. Use bold top-level section "
    "headings so the output is easy for meeting participants to scan. You MUST "
    "always include ALL of the following sections, in this exact order. Even if "
    "a section has nothing to report, write 'None' in that section:\n\n"
    "**PROJECTS DISCUSSED**\n"
    "List each project, workstream, client, product area, or initiative discussed. "
    "For each one, include 1-2 bullets covering the topic, current status, risks, "
    "or important context. Bold the project name inline (e.g. '- **Project Alpha**: ...'). "
    "If no explicit project was named, write '- **General / Unassigned**: ...'.\n\n"
    "**OVERVIEW**\n"
    "2-4 sentences describing what the meeting was about and the main outcomes. "
    "If multiple projects were discussed, summarize them project-by-project rather "
    "than blending them together.\n\n"
    "**KEY DECISIONS**\n"
    "Bullet list of concrete decisions made. If multiple projects, group decisions "
    "under visible project subheads such as '- **Project Name**'. Include 'None' "
    "if no decisions.\n\n"
    "**ACTION ITEMS**\n"
    "Bullet list with: action, owner (person responsible), and deadline if mentioned. "
    "If multiple projects, group action items under visible project subheads such "
    "as '- **Project Name**'. Bold the owner's name like 'Owner: **Yash**'. "
    "Include ALL commitments, follow-ups, and 'will look "
    "into it' type statements. If no owner was named, write 'Owner: **unassigned**'. "
    "Include 'None' if no action items.\n\n"
    "**FOLLOW-UPS**\n"
    "Bullet list of open questions, topics deferred, or items needing further "
    "discussion. If multiple projects, group follow-ups by project. Include 'None' "
    "if no follow-ups.\n\n"
    "Return the entire summary as one complete structured message. Do not omit "
    "captured project context, decisions, action items, owners, or follow-ups just "
    "to keep it short. Use concise bullets, but preserve the full substance of the "
    "meeting."
)
_ACTION_PROTOCOL = """
<meeting_action_protocol>
CRITICAL RULE: For every action request your FIRST tool call MUST be the action
tool. Never call joinly_speak_text before an action tool. Zero preamble, zero
acknowledgement, zero "I'll do that now" — just execute the tool immediately.

Action routing:
- Summary (no leave requested): call `joinly-internal_post_meeting_summary` as
  your FIRST and ONLY tool call. Do not write your own summary text. Do not post
  a brief version. Do not speak first.
- Leave requested: call `joinly-internal_request_leave` as your first tool call.
- Exclude/remove/forget part of the current meeting from summary or memory:
    call `joinly-internal_exclude_from_summary` as your first tool call with the
    exact text or topic to omit. Do not call `remember` for excluded content.
- Send a chat message: call the chat tool directly. No announcement beforehand.
- Any other action: call the tool first; speak only if the participant explicitly
  asked for verbal confirmation OR the action failed.

FORBIDDEN — never do this before an action tool:
  joinly_speak_text("I'll post the summary now.")   ← WRONG
  joinly_speak_text("Sure, one moment.")             ← WRONG
  joinly_speak_text("Let me send that for you.")    ← WRONG

CORRECT — action tool first, speech only after if needed:
  joinly-internal_post_meeting_summary()             ← RIGHT
</meeting_action_protocol>
"""


def _with_explicit_speech_mute_control(
    client: JoinlyClient,
    tool_executor: ToolExecutor,
) -> ToolExecutor:
    """Keep speech tool execution from toggling Teams mute state.

    The bot unmutes once after joining and stays unmuted. Toggling mute around
    every speech call is slow and brittle in Teams because the roster can also
    expose a "Mute all" button that matches broad mute selectors.
    """

    async def wrapped_tool_executor(
        tool_name: str,
        args: dict[str, Any],
    ) -> Any:  # noqa: ANN401
        return await tool_executor(tool_name, args)

    return wrapped_tool_executor


async def _speak_action_filler(client: JoinlyClient, text: str) -> bool:
    """Speak a short acknowledgement without blocking the requested action."""
    try:
        await client.speak_text(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to speak action filler %r: %s", text, exc)
        return False
    return True


_MEMORY_PROTOCOL = """
<memory_protocol>
- You have persistent memory through the `remember` tool.
- Use `recall_memory` when a participant asks what you remember, asks about
    other meetings, or gives a specific meeting_id.
- If a participant asks for history for "this meeting link", "this link", or
    "this recurring meeting", call `recall_memory` with
    `meeting_context="current_link"`. If they ask only about today's/current
    occurrence, use `meeting_context="current_occurrence"`.
- Use `remember` when a participant shares durable preferences, decisions,
    action items, project facts, or client context that should help future meetings.
- If a participant says to ignore, remove, exclude, redact, or not remember part
    of the current meeting, use `joinly-internal_exclude_from_summary` instead of
    `remember`. The excluded content must not appear in summaries or memory.
- Store meeting-derived facts, summaries, decisions, and action items in meeting
    memory. Meeting memory is agent-specific: it can be recalled across all
    meetings this agent attended, and each item still keeps a meeting_id for
    specific meeting lookup.
- Do not store one meeting link's discussion, summary, decisions, or action items
    in project, client, agent, or global memory.
- Do not store secrets, credentials, or sensitive personal data.
- Use memory context as historical background. Do not present it as something said
    in the current meeting unless the participant asks about prior context.
</memory_protocol>
""".strip()


class _ChatSender(Protocol):
    """Minimal chat sender interface used by summary posting."""

    async def send_chat_message(self, message: str) -> None:
        """Send a chat message."""
        ...


class _SentChatMatcher(Protocol):
    """Minimal interface for checking messages sent by this bot."""

    def is_sent_chat_message(self, text: str) -> bool:
        """Return whether text was sent by this bot."""
        ...


class _ChatTriggerTracker:
    """Stateful chat trigger guard for single-agent meeting chat polling."""

    def __init__(self, *, agent_name: str, sent_chat_matcher: _SentChatMatcher):
        self._agent_name = agent_name.strip().casefold()
        self._sent_chat_matcher = sent_chat_matcher
        self._seen_messages: dict[tuple[str, str, str], int] = {}
        self._seen_texts: dict[str, int] = {}

    @staticmethod
    def _signature(msg: MeetingChatMessage) -> tuple[str, str, str]:
        sender = (msg.sender or "").strip().casefold()
        timestamp = (msg.timestamp or "").strip()
        text = _ChatTriggerTracker._text_signature(msg)
        return sender, timestamp, text

    @staticmethod
    def _text_signature(msg: MeetingChatMessage) -> str:
        return " ".join((msg.text or "").split()).casefold()

    def _is_own_message(self, msg: MeetingChatMessage) -> bool:
        sender = (msg.sender or "").strip().casefold()
        return bool(sender and sender == self._agent_name) or (
            self._sent_chat_matcher.is_sent_chat_message(msg.text or "")
        )

    def baseline(self, messages: list[MeetingChatMessage]) -> None:
        """Mark existing chat messages as seen before polling starts."""
        for msg in messages:
            sig = self._signature(msg)
            self._seen_messages[sig] = self._seen_messages.get(sig, 0) + 1
            text_sig = self._text_signature(msg)
            if text_sig:
                self._seen_texts[text_sig] = self._seen_texts.get(text_sig, 0) + 1

    def new_participant_messages(
        self,
        messages: list[MeetingChatMessage],
    ) -> list[MeetingChatMessage]:
        """Return new participant chat messages, excluding the bot's own sends."""
        observed_counts: dict[tuple[str, str, str], int] = {}
        observed_text_counts: dict[str, int] = {}
        new_messages: list[MeetingChatMessage] = []
        for msg in messages:
            sig = self._signature(msg)
            observed_counts[sig] = observed_counts.get(sig, 0) + 1
            text_sig = self._text_signature(msg)
            if text_sig:
                observed_text_counts[text_sig] = (
                    observed_text_counts.get(text_sig, 0) + 1
                )
            if observed_counts[sig] <= self._seen_messages.get(sig, 0):
                continue
            self._seen_messages[sig] = self._seen_messages.get(sig, 0) + 1

            if text_sig:
                if observed_text_counts[text_sig] <= self._seen_texts.get(
                    text_sig, 0
                ):
                    continue
                self._seen_texts[text_sig] = observed_text_counts[text_sig]

            if not msg.text or self._is_own_message(msg):
                continue
            new_messages.append(msg)
        return new_messages

    def new_trigger_messages(
        self,
        messages: list[MeetingChatMessage],
    ) -> list[MeetingChatMessage]:
        """Return new participant chat messages that mention the agent by name."""
        return [
            msg
            for msg in self.new_participant_messages(messages)
            if self._agent_name in msg.text.casefold()
        ]


_ASSISTIVE_PENDING_RE = re.compile(
    r"\b(need(?:s)?\s+to|we\s+need\s+to|i(?:'ll|\s+will)"
    r"|we(?:'ll|\s+will)|should|action\s+item|todo|follow\s*up"
    r"|pending|block(?:ed|er)?|risk|deadline|by\s+(?:today|tomorrow|friday|monday|tuesday|wednesday|thursday))\b",
    re.IGNORECASE,
)
_ASSISTIVE_OWNER_RE = re.compile(
    r"\b(owner|assigned\s+to)\s*:?\s*([A-Z][A-Za-z0-9_.-]*)\b"
)
_ASSISTIVE_COMMITMENT_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_.-]*)\s+(?:will|should|needs\s+to)\b"
)
_ASSISTIVE_NON_OWNER_LABELS = {"client", "initiative", "project", "workstream"}
_ASSISTIVE_DEADLINE_RE = re.compile(
    r"\b(?:by\s+)?(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|next\s+week|end\s+of\s+(?:day|week)|eod)\b",
    re.IGNORECASE,
)


@dataclass
class _AssistivePendingItem:
    """A lightweight pending-work item captured from live meeting context."""

    text: str
    speaker: str
    owner: str | None = None
    deadline: str | None = None
    source: str = "transcript"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reminded: bool = False


class _AssistiveModeTracker:
    """Deterministic assistive-mode tracker with quiet-gap/cooldown gating."""

    def __init__(
        self,
        *,
        agent_name: str,
        quiet_gap_seconds: float = _ASSISTIVE_QUIET_GAP_SECONDS,
        cooldown_seconds: float = _ASSISTIVE_COOLDOWN_SECONDS,
        max_pending_items: int = _ASSISTIVE_MAX_PENDING_ITEMS,
    ) -> None:
        self._agent_name = agent_name.strip().casefold()
        self._quiet_gap_seconds = quiet_gap_seconds
        self._cooldown_seconds = cooldown_seconds
        self._max_pending_items = max_pending_items
        self._pending: list[_AssistivePendingItem] = []
        self._seen: set[str] = set()
        self._last_activity_at: datetime | None = None
        self._last_nudge_at: datetime | None = None

    @property
    def pending_items(self) -> list[_AssistivePendingItem]:
        """Return pending items captured so far."""
        return list(self._pending)

    def observe_segments(self, segments: list[TranscriptSegment]) -> None:
        """Capture pending-work hints from participant transcript segments."""
        for segment in segments:
            if segment.role != SpeakerRole.participant:
                continue
            self.observe_text(
                segment.text,
                speaker=segment.speaker or "Participant",
                source="transcript",
            )

    def observe_chat_message(self, message: MeetingChatMessage) -> None:
        """Capture pending-work hints from meeting chat."""
        self.observe_text(
            message.text,
            speaker=message.sender or "Meeting chat",
            source="chat",
        )

    def observe_text(
        self,
        text: str,
        *,
        speaker: str,
        source: str,
        now: datetime | None = None,
    ) -> None:
        """Observe one meeting text line and record pending-work candidates."""
        now = now or datetime.now(UTC)
        normalized_speaker = speaker.strip() or "Participant"
        if normalized_speaker.casefold() == self._agent_name:
            return

        normalized_text = " ".join((text or "").split())
        if not normalized_text:
            return
        self._last_activity_at = now

        if self._agent_name in normalized_text.casefold():
            return
        if not _ASSISTIVE_PENDING_RE.search(normalized_text):
            return

        signature = normalized_text.casefold()
        if signature in self._seen:
            return
        self._seen.add(signature)

        self._pending.append(
            _AssistivePendingItem(
                text=normalized_text,
                speaker=normalized_speaker,
                owner=self._extract_owner(normalized_text, normalized_speaker),
                deadline=self._extract_deadline(normalized_text),
                source=source,
                created_at=now,
            )
        )
        if len(self._pending) > self._max_pending_items:
            self._pending = self._pending[-self._max_pending_items :]

    def maybe_build_nudge(self, *, now: datetime | None = None) -> str | None:
        """Return a short chat nudge when quiet-gap and cooldown allow it."""
        now = now or datetime.now(UTC)
        if self._last_activity_at is None:
            return None
        if (now - self._last_activity_at).total_seconds() < self._quiet_gap_seconds:
            return None
        if (
            self._last_nudge_at is not None
            and (now - self._last_nudge_at).total_seconds() < self._cooldown_seconds
        ):
            return None

        open_items = [item for item in self._pending if not item.reminded]
        if not open_items:
            return None

        selected = open_items[:2]
        for item in selected:
            item.reminded = True
        self._last_nudge_at = now

        lines = ["Assistive note: I heard pending work that may need follow-up:"]
        for item in selected:
            owner = item.owner or "unassigned"
            deadline = item.deadline or "not mentioned"
            lines.append(f"- {item.text} Owner: {owner}. Deadline: {deadline}.")
        lines.append("I will keep tracking this for the summary.")
        return "\n".join(lines)

    @staticmethod
    def _extract_owner(text: str, speaker: str) -> str | None:
        lowered = text.casefold()
        if "i'll" in lowered or "i will" in lowered:
            return speaker
        match = _ASSISTIVE_OWNER_RE.search(text)
        if match:
            return match.group(2)
        for match in _ASSISTIVE_COMMITMENT_RE.finditer(text):
            if _AssistiveModeTracker._has_non_owner_label_prefix(text, match.start()):
                continue
            return match.group(1)
        return None

    @staticmethod
    def _has_non_owner_label_prefix(text: str, start: int) -> bool:
        prefix = text[:start].rstrip()
        previous_word = prefix.rsplit(maxsplit=1)[-1].strip(":-/").casefold()
        return previous_word in _ASSISTIVE_NON_OWNER_LABELS

    @staticmethod
    def _extract_deadline(text: str) -> str | None:
        match = _ASSISTIVE_DEADLINE_RE.search(text)
        return match.group(0) if match else None


def _chat_history_lines_for_summary(
    messages: list[MeetingChatMessage],
    *,
    sent_chat_matcher: _SentChatMatcher,
    max_messages: int = 80,
) -> list[str]:
    """Convert recent meeting chat messages into summary transcript lines."""
    lines: list[str] = []
    seen: set[str] = set()
    for msg in messages[-max_messages:]:
        text = " ".join((msg.text or "").split())
        if not text:
            continue
        text_sig = text.casefold()
        if text_sig in seen:
            continue
        seen.add(text_sig)
        if sent_chat_matcher.is_sent_chat_message(text):
            continue
        if text_sig.startswith("meeting started") or text_sig.startswith(
            "meeting ended"
        ):
            continue
        sender = (msg.sender or "Meeting chat").strip() or "Meeting chat"
        lines.append(f"{sender}: [In Meeting Chat] {text}")
    return lines


def _derive_single_agent_id(name: str | None) -> str:
    """Derive a stable memory agent id from the meeting display name."""
    candidate = (name or "joinly").strip().casefold()
    candidate = re.sub(r"[^a-z0-9_-]+", "-", candidate).strip("-")
    return candidate or "joinly"


def _normalize_meeting_url_for_memory(meeting_url: str) -> str:
    """Normalize a meeting URL before hashing it into a memory namespace."""
    raw_url = meeting_url.strip()
    parsed = urlsplit(raw_url)
    if not parsed.scheme and not parsed.netloc:
        return raw_url

    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    path = parsed.path.rstrip("/") or parsed.path
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            path,
            query,
            "",
        )
    )


def _derive_meeting_series_id(meeting_url: str) -> str:
    """Derive a stable memory id for all occurrences of the same meeting link."""
    normalized_url = _normalize_meeting_url_for_memory(meeting_url)
    return hashlib.sha256(f"series:{normalized_url}".encode()).hexdigest()[:16]


def _derive_meeting_occurrence_id(
    meeting_url: str,
    meeting_date: datetime | None = None,
) -> str:
    """Derive a daily stable memory id for one occurrence of a meeting link."""
    normalized_url = _normalize_meeting_url_for_memory(meeting_url)
    date_str = (meeting_date or datetime.now(UTC)).strftime("%Y%m%d")
    return hashlib.sha256(
        f"occurrence:{normalized_url}:{date_str}".encode()
    ).hexdigest()[:16]


def _derive_meeting_id(meeting_url: str, meeting_date: datetime | None = None) -> str:
    """Derive a daily stable occurrence memory id from meeting URL and UTC date."""
    return _derive_meeting_occurrence_id(meeting_url, meeting_date)


def _resolve_memory_connection_string(connection_string_env: str) -> str | None:
    """Resolve Azure Storage connection string without logging secret material."""
    connection_string = os.getenv(connection_string_env)
    if connection_string:
        return connection_string.strip().strip('"').strip("'")

    account_name = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
    account_key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
    if account_name and account_key:
        return (
            "DefaultEndpointsProtocol=https;"
            f"AccountName={account_name};"
            f"AccountKey={account_key};"
            "EndpointSuffix=core.windows.net"
        )
    return None


def _build_single_agent_memory_store(
    enabled: bool | None,
    connection_string_env: str,
    ttl_secs: float,
    cache_maxsize: int,
) -> MemoryStore | None:
    """Create the persistent memory store for single-agent mode if configured."""
    if enabled is False:
        return None

    backend_type = os.getenv("JOINLY_MEMORY_BACKEND", "azure").lower()

    # ── HelixDB backend ────────────────────────────────────────────────────
    if backend_type == "helix":
        helix_url = os.getenv(
            "JOINLY_HELIX_URL", "http://localhost:6969/v1/query"
        )
        try:
            from joinly_client.memory import CachedMemoryStore
            import joinly_client.memory as memory_module

            helix_store_cls = getattr(memory_module, "HelixMemoryStore", None)
            if helix_store_cls is None:
                logger.warning("HelixDB memory backend is not available.")
                return None

            logger.info("Using HelixDB memory backend at %s", helix_url)
            return CachedMemoryStore(
                helix_store_cls(helix_url),
                ttl_secs=ttl_secs,
                maxsize=cache_maxsize,
            )
        except Exception:
            logger.exception("Failed to initialize HelixDB memory store.")
            return None

    # ── Azure Table Storage backend (default) ──────────────────────────────
    connection_string = _resolve_memory_connection_string(connection_string_env)
    if not connection_string:
        if enabled:
            logger.warning(
                "Memory was enabled, but %s or Azure account/key env vars were "
                "not set. Continuing without persistent memory.",
                connection_string_env,
            )
        return None

    try:
        from joinly_client.memory import (
            AzureTableMemoryStore,
            CachedMemoryStore,
        )

        backend = AzureTableMemoryStore(connection_string)
        return CachedMemoryStore(backend, ttl_secs=ttl_secs, maxsize=cache_maxsize)
    except Exception:
        logger.exception("Failed to initialize persistent memory store.")
        return None


def _compose_memory_instructions(
    instructions: str | None,
    prompt_style: str | None,
    memory_block: str,
    memory_enabled: bool,
) -> str | None:
    """Append memory protocol and loaded context without losing prompt style."""
    if not memory_enabled:
        return instructions

    base_instructions = instructions
    if base_instructions is None:
        base_instructions = (
            DYADIC_INSTRUCTIONS if prompt_style == "dyadic" else MPC_INSTRUCTIONS
        )

    parts = [base_instructions.strip(), _MEMORY_PROTOCOL]
    if memory_block.strip():
        parts.append(f"<memory_context>\n{memory_block.strip()}\n</memory_context>")
    return "\n\n".join(part for part in parts if part)


def _compose_action_instructions(
    instructions: str | None,
    prompt_style: str | None,
) -> str:
    """Append action protocol without depending on memory being enabled."""
    base_instructions = instructions
    if base_instructions is None:
        base_instructions = (
            DYADIC_INSTRUCTIONS if prompt_style == "dyadic" else MPC_INSTRUCTIONS
        )
    return "\n\n".join([base_instructions.strip(), _ACTION_PROTOCOL.strip()])


def _truncate_memory_content(content: str) -> str:
    """Fit auto-saved meeting summaries inside the MemoryEntry content limit."""
    if len(content) <= _MEMORY_SUMMARY_MAX_CHARS:
        return content
    return content[: _MEMORY_SUMMARY_MAX_CHARS - 3].rstrip() + "..."


async def _save_meeting_summary_memory(  # noqa: PLR0913
    store: MemoryStore,
    *,
    agent_id: str,
    project_id: str | None,
    meeting_series_id: str,
    meeting_occurrence_id: str,
    meeting_start_time: datetime,
    summary_text: str,
) -> None:
    """Persist the generated summary for this link and this occurrence."""
    if not summary_text.strip():
        return

    from joinly_client.memory import MemoryEntry

    meeting_date = meeting_start_time.date().isoformat()
    series_content = _truncate_memory_content(
        "Previous occurrence summary for this meeting link "
        f"({meeting_date}, occurrence_id={meeting_occurrence_id}):\n"
        f"{summary_text.strip()}"
    )
    occurrence_content = _truncate_memory_content(
        "Meeting occurrence summary "
        f"({meeting_date}, occurrence_id={meeting_occurrence_id}):\n"
        f"{summary_text.strip()}"
    )
    entries = [
        MemoryEntry(
            row_key=f"summary-series-{meeting_occurrence_id}",
            agent_id=agent_id,
            scope="meeting",
            memory_type="fact",
            project_id=project_id,
            meeting_id=meeting_series_id,
            content=series_content,
            source="auto_extracted",
            confidence=0.95,
        ),
        MemoryEntry(
            row_key="summary-current",
            agent_id=agent_id,
            scope="meeting",
            memory_type="fact",
            project_id=project_id,
            meeting_id=meeting_occurrence_id,
            content=occurrence_content,
            source="auto_extracted",
            confidence=0.95,
        ),
    ]
    try:
        for entry in entries:
            await store.save(entry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to save meeting summary to memory: %s", exc)
    else:
        logger.info("Saved meeting summary to link-scoped persistent memory.")


async def _delete_matching_memories_for_exclusion(  # noqa: PLR0913
    store: MemoryStore,
    *,
    agent_id: str,
    project_id: str | None,
    meeting_series_id: str,
    meeting_occurrence_id: str,
    meeting_start_time: datetime,
    exclusion: _MeetingExclusion,
) -> int:
    """Delete persisted current-meeting memories that match an exclusion."""
    query_specs = [
        ("meeting", {"project_id": project_id, "meeting_id": meeting_series_id}),
        (
            "meeting",
            {"project_id": project_id, "meeting_id": meeting_occurrence_id},
        ),
    ]
    candidates = await asyncio.gather(
        *[
            store.query(
                agent_id,
                scope,
                project_id=params["project_id"],
                meeting_id=params["meeting_id"],
                limit=_EXCLUSION_DELETE_SCAN_LIMIT,
            )
            for scope, params in query_specs
        ],
        return_exceptions=True,
    )
    deleted_keys: set[tuple[str, str]] = set()
    for result in candidates:
        if isinstance(result, BaseException):
            logger.warning("Failed to query memories for exclusion cleanup: %s", result)
            continue
        for entry in result:
            if (
                entry.meeting_id == meeting_series_id
                and entry.row_key != f"summary-series-{meeting_occurrence_id}"
                and entry.created_at < meeting_start_time
            ):
                continue
            if not _matches_meeting_exclusion(entry.content, exclusion):
                continue
            key = (entry.partition_key, entry.row_key)
            if key in deleted_keys:
                continue
            await store.delete(entry.partition_key, entry.row_key)
            deleted_keys.add(key)
    return len(deleted_keys)


def _chunk_chat_message(
    message: str,
    max_chars: int = _SUMMARY_CHAT_CHUNK_SIZE,
) -> list[str]:
    """Split chat text into platform-safe chunks without dropping content."""
    if max_chars <= 0:
        msg = "max_chars must be greater than zero."
        raise ValueError(msg)

    chunks: list[str] = []
    remaining = message.strip()

    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n", 0, max_chars + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, max_chars + 1)
        if split_at <= 0:
            split_at = max_chars

        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)

    return chunks


def _fallback_summary_text(meeting_minutes: int, convo_lines: list[str]) -> str:
    """Build a deterministic summary when model summarization is unavailable."""
    if not convo_lines:
        return (
            "**PROJECTS DISCUSSED**\n"
            "None captured\n\n"
            "**OVERVIEW**\n"
            f"The meeting ran for {meeting_minutes} minutes, but no participant "
            "conversation was captured by the transcription pipeline.\n\n"
            "**KEY DECISIONS**\n"
            "None\n\n"
            "**ACTION ITEMS**\n"
            "None\n\n"
            "**FOLLOW-UPS**\n"
            "None"
        )

    excerpt = "\n".join(
        f"- {line}" for line in convo_lines[-_FALLBACK_TRANSCRIPT_LINES:]
    )
    return (
        "**PROJECTS DISCUSSED**\n"
        "- **General / Unassigned**: Review the transcript excerpt below for "
        "project names, workstreams, status updates, risks, and next steps.\n\n"
        "**OVERVIEW**\n"
        "The model summary was unavailable, so this fallback summary includes "
        "the latest captured transcript lines for review.\n\n"
        "**KEY DECISIONS**\n"
        "None captured automatically\n\n"
        "**ACTION ITEMS**\n"
        "Review the transcript excerpt below for commitments and owners.\n\n"
        "**FOLLOW-UPS**\n"
        "Review any unresolved items from the transcript excerpt.\n\n"
        "**TRANSCRIPT EXCERPT**\n"
        f"{excerpt}"
    )


@dataclass(frozen=True)
class _MeetingExclusion:
    """Runtime-only instruction to omit matching current-meeting content."""

    text_or_topic: str
    reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _stem_exclusion_token(token: str) -> str:
    """Return a tiny stem for matching played/playing/plural variants."""
    for suffix in ("ing", "ed", "es", "s"):
        min_len = len(suffix) + 3
        if len(token) > min_len and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _exclusion_tokens(value: str) -> list[str]:
    """Tokenize exclusion text for fuzzy transcript-line matching."""
    tokens: list[str] = []
    for raw_token in re.findall(r"[a-z0-9]+", value.casefold()):
        if (
            len(raw_token) < _EXCLUSION_MIN_TEXT_CHARS
            or raw_token in _EXCLUSION_STOP_WORDS
        ):
            continue
        token = _stem_exclusion_token(raw_token)
        if token not in _EXCLUSION_STOP_WORDS:
            tokens.append(token)
    return tokens


def _normalize_exclusion_text(value: str) -> str:
    """Normalize text for substring-based exclusion matching."""
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _line_without_speaker(value: str) -> str:
    """Return transcript text without a leading speaker label."""
    match = re.match(r"^\s*[A-Za-z][\w .'-]{0,60}:\s+(.+)$", value)
    if match:
        return match.group(1)
    return value


def _is_exclusion_control_line(content: str) -> bool:
    """Return whether a line is a redaction command or confirmation."""
    normalized = _normalize_exclusion_text(_line_without_speaker(content))
    if not normalized:
        return False
    if re.search(r"\b(?:don t|dont|do not|not) include\b", normalized):
        return True

    has_redaction_action = re.search(
        r"\b(?:exclude|excluded|forget|forgot|ignore|ignored|omit|omitted|"
        r"redact|redacted|redaction|remove|removed)\b",
        normalized,
    )
    has_redaction_target = re.search(
        r"\b(?:discussion|memory|part|private|summar[a-z]*|topic)\b",
        normalized,
    )
    return bool(has_redaction_action and has_redaction_target)


def _matches_meeting_exclusion(content: str, exclusion: _MeetingExclusion) -> bool:
    """Return whether content should be omitted by an exclusion instruction."""
    needle = _normalize_exclusion_text(exclusion.text_or_topic)
    content_text = _line_without_speaker(content)
    haystack = _normalize_exclusion_text(content_text)
    if not needle or not haystack:
        return False
    if needle in haystack:
        return True

    tokens = _exclusion_tokens(exclusion.text_or_topic)
    if not tokens:
        return False

    content_tokens = set(_exclusion_tokens(content_text))
    matched = sum(1 for token in tokens if token in content_tokens)
    if any(len(token) >= 6 and token in content_tokens for token in tokens):
        return True
    if matched >= 2:  # noqa: PLR2004
        return True
    if len(tokens) <= 2:
        return matched == len(tokens)
    return matched >= max(2, round(len(tokens) * 0.6))


def _matches_any_meeting_exclusion(
    content: str,
    exclusions: list[_MeetingExclusion],
) -> bool:
    """Return whether content matches any current-meeting exclusion."""
    return any(_matches_meeting_exclusion(content, item) for item in exclusions)


def _apply_meeting_exclusions(
    convo_lines: list[str],
    exclusions: list[_MeetingExclusion],
) -> tuple[list[str], int]:
    """Remove excluded content and redaction control lines before summarizing."""
    if not exclusions:
        return convo_lines, 0

    filtered = [
        line
        for line in convo_lines
        if not _is_exclusion_control_line(line)
        and not _matches_any_meeting_exclusion(line, exclusions)
    ]
    return filtered, len(convo_lines) - len(filtered)


def _apply_meeting_exclusions_to_text(
    text: str,
    exclusions: list[_MeetingExclusion],
) -> tuple[str, int]:
    """Remove generated summary lines mentioning excluded meeting content."""
    if not exclusions or not text.strip():
        return text, 0

    filtered, removed = _apply_meeting_exclusions(text.splitlines(), exclusions)
    return "\n".join(filtered).strip(), removed


def _filter_memory_text_by_exclusions(
    memory_text: str,
    exclusions: list[_MeetingExclusion],
) -> str:
    """Remove matching persisted memory lines before prompt/recall exposure."""
    if not exclusions or not memory_text.strip():
        return memory_text
    filtered, removed = _apply_meeting_exclusions(memory_text.splitlines(), exclusions)
    if removed:
        logger.info(
            "Suppressed %d persisted memory line(s) matching exclusion rules.",
            removed,
        )
    return "\n".join(filtered).strip()


def _scrub_agent_messages_for_exclusions(
    messages: list[Any],
    exclusions: list[_MeetingExclusion],
) -> int:
    """Remove excluded text from already-buffered LLM conversation history."""
    if not exclusions:
        return 0
    removed = 0
    for message in messages:
        parts = getattr(message, "parts", None)
        if not isinstance(parts, list):
            continue
        for index, part in enumerate(parts):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                filtered, count = _apply_meeting_exclusions(
                    content.splitlines(),
                    exclusions,
                )
                if count:
                    parts[index] = replace(part, content="\n".join(filtered).strip())
                    removed += count
                continue
            args = getattr(part, "args", None)
            if isinstance(args, str) and _matches_any_meeting_exclusion(
                args,
                exclusions,
            ):
                parts[index] = replace(part, args="{}")
                removed += 1
    return removed


def _normalize_participant_name(name: str) -> str:
    """Normalize a participant display name for self/duplicate comparisons."""
    normalized = re.sub(r"\([^)]*\)", " ", name)
    normalized = re.sub(
        r"\b(guest|external|organizer|presenter)\b",
        " ",
        normalized,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _active_other_participants(
    participants: list[MeetingParticipant],
    *,
    self_name: str,
    self_aliases: list[str] | None = None,
) -> list[MeetingParticipant]:
    """Return participants that appear to be present and are not the bot itself."""
    self_names = [self_name, *(self_aliases or [])]
    self_normalized_names = {
        normalized
        for normalized in (_normalize_participant_name(name) for name in self_names)
        if normalized
    }
    active: list[MeetingParticipant] = []
    seen: set[str] = set()

    for participant in participants:
        participant_name = participant.name.strip()
        if not participant_name:
            continue

        normalized_name = _normalize_participant_name(participant_name)
        if not normalized_name or normalized_name in self_normalized_names:
            continue

        info_text = " ".join(participant.infos).casefold()
        if _ABSENT_PARTICIPANT_INFO_RE.search(info_text):
            continue
        if _PRESENCE_ONLY_PARTICIPANT_INFO_RE.search(
            info_text
        ) and not _ACTIVE_MEETING_PARTICIPANT_INFO_RE.search(info_text):
            continue

        if normalized_name in seen:
            continue
        seen.add(normalized_name)
        active.append(participant)

    return active


def _auto_leave_action(
    alone_seconds: float,
    *,
    summary_posted: bool,
) -> str:
    """Return the auto-leave action for the current alone duration."""
    if alone_seconds >= _AUTOLEAVE_ALONE_SECONDS:
        return "leave"
    if not summary_posted and alone_seconds >= _AUTOLEAVE_SUMMARY_SECONDS:
        return "post_summary"
    return "wait"


async def _post_summary_before_leave(
    post_meeting_summary_callback: Any,
    *,
    summary_posted: bool,
    reason: str,
    timeout_seconds: float = _SUMMARY_BEFORE_LEAVE_TIMEOUT_SECONDS,
) -> bool:
    """Post the meeting summary before any leave path exits the meeting."""
    if summary_posted:
        logger.info("Meeting summary already posted before %s.", reason)
        return True
    if post_meeting_summary_callback is None:
        logger.warning("Cannot post meeting summary before %s: callback not ready.", reason)
        return False

    try:
        await asyncio.wait_for(
            post_meeting_summary_callback(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "Summary posting timed out after %.0fs before %s.",
            timeout_seconds,
            reason,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Summary posting failed before %s: %s", reason, exc)
        return False

    return True


def _split_env_list(value: str | None) -> list[str]:
    """Split a comma-separated environment-style list."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _split_excluded_meeting_ids() -> set[str]:
    """Return the set of meeting IDs to suppress from recall_memory results.

    Reads the ``JOINLY_MEMORY_EXCLUDE_MEETING_IDS`` environment variable which
    should be a comma-separated list of meeting_id values (the SHA-256 hashes
    that joinly derives from meeting URLs).

    Example .env entry::

        JOINLY_MEMORY_EXCLUDE_MEETING_IDS=9652cda7a6527a2e,abc123def456

    You can find the meeting_id for a given meeting URL in the bot logs:
    look for the ``meeting_series=<hash>`` segment in the startup output.
    """
    raw = os.environ.get("JOINLY_MEMORY_EXCLUDE_MEETING_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _normalize_mcp_config(
    mcp_config: dict[str, Any] | None,
) -> dict[str, dict[str, dict[str, Any]]] | None:
    """Return an MCP config with disabled servers removed."""
    if not mcp_config:
        return None

    servers = mcp_config.get("mcpServers")
    if not isinstance(servers, dict):
        logger.warning(
            "MCP configuration does not contain 'mcpServers'. "
            "Using the main joinly client only."
        )
        return None

    enabled_servers: dict[str, dict[str, Any]] = {}
    for server_name, server_config in servers.items():
        if not isinstance(server_name, str) or not isinstance(server_config, dict):
            logger.warning("Skipping invalid MCP server config: %r", server_name)
            continue
        if server_config.get("disabled"):
            logger.info("Skipping disabled MCP server: %s", server_name)
            continue

        normalized_name = "_joinly" if server_name == "joinly" else server_name
        enabled_servers[normalized_name] = {
            key: value for key, value in server_config.items() if key != "disabled"
        }

    if not enabled_servers:
        logger.info("No enabled external MCP servers configured.")
        return None

    return {"mcpServers": enabled_servers}


async def _send_chat_message_with_retries(
    client: _ChatSender,
    message: str,
    *,
    attempts: int = _SUMMARY_CHAT_SEND_ATTEMPTS,
    retry_delay: float = _SUMMARY_CHAT_RETRY_SECONDS,
) -> None:
    """Send a summary as one message, falling back to chunks if needed."""
    if attempts <= 0:
        msg = "attempts must be greater than zero."
        raise ValueError(msg)

    message = message.strip()
    if not message:
        return

    try:
        await _send_single_chat_message_with_retries(
            client,
            message,
            attempts=attempts,
            retry_delay=retry_delay,
            label="summary message",
        )
        return
    except Exception as single_message_error:
        chunks = _chunk_chat_message(message)
        if len(chunks) <= 1:
            raise
        logger.warning(
            "Failed to post summary as one chat message after %s attempt(s); "
            "falling back to %s platform-safe chunks: %s",
            attempts,
            len(chunks),
            single_message_error,
        )

    for chunk_index, chunk in enumerate(chunks, start=1):
        await _send_single_chat_message_with_retries(
            client,
            chunk,
            attempts=attempts,
            retry_delay=retry_delay,
            label=f"summary chunk {chunk_index}/{len(chunks)}",
        )


async def _send_single_chat_message_with_retries(
    client: _ChatSender,
    message: str,
    *,
    attempts: int,
    retry_delay: float,
    label: str,
) -> None:
    """Retry one chat send before surfacing a posting failure."""
    chunks = _chunk_chat_message(message)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await client.send_chat_message(message)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "Failed to post %s on attempt %s/%s: %s",
                label,
                attempt,
                attempts,
                exc,
            )
            if attempt < attempts:
                await asyncio.sleep(retry_delay)

    chunk_count = len(chunks)
    suffix = f" ({chunk_count} platform-safe chunk(s))" if chunk_count > 1 else ""
    msg = f"Failed to post {label}{suffix} after {attempts} attempts."
    raise RuntimeError(msg) from last_error


def _parse_kv(
    _ctx: click.Context, _param: click.Parameter, value: tuple[str]
) -> dict[str, object] | None:
    """Convert (--foo-arg key=value) repeated tuples to dict."""
    out: dict[str, object] = {}
    for item in value:
        try:
            k, v = item.split("=", 1)
        except ValueError as exc:
            msg = f"{item!r} is not of the form key=value"
            raise click.BadParameter(msg) from exc

        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out or None


@click.command()
@click.option(
    "--joinly-url",
    type=str,
    help="The URL of the joinly server to connect to.",
    default="http://localhost:8000/mcp/",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_URL",
)
@click.option(
    "-n",
    "--name",
    type=str,
    help="The meeting participant name.",
    default="joinly",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_NAME",
)
@click.option(
    "--llm-provider",
    "--model-provider",
    type=str,
    help="The provider of the LLM model to use in the client.",
    default="openai",
    show_default=True,
    show_envvar=True,
    envvar=["JOINLY_LLM_PROVIDER", "JOINLY_MODEL_PROVIDER"],
)
@click.option(
    "--llm-model",
    "--model-name",
    type=str,
    help="The name of the LLM model to use in the client.",
    default="gpt-4o",
    show_default=True,
    show_envvar=True,
    envvar=["JOINLY_LLM_MODEL", "JOINLY_MODEL_NAME"],
)
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to a .env file to load environment variables from.",
    default=None,
    show_default=True,
    is_eager=True,
    expose_value=False,
    callback=lambda _ctx, _param, value: load_dotenv(value),
)
@click.option(
    "--prompt",
    type=str,
    help="System prompt to use for the model. If not provided, the default "
    "system prompt will be used.",
    default=None,
    envvar="JOINLY_PROMPT",
)
@click.option(
    "--prompt-file",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to a text file containing the system prompt.",
    default=None,
    show_default=True,
    envvar="JOINLY_PROMPT_FILE",
)
@click.option(
    "--prompt-style",
    type=click.Choice(["dyadic", "mpc"], case_sensitive=False),
    help="The type of default prompt to use if no custom prompt is provided."
    "Options are 'dyadic' for one-on-one meetings or 'mpc' for group meetings.",
    default="mpc",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_PROMPT_STYLE",
)
@click.option(
    "--mcp-config",
    type=str,
    help="Path to a JSON configuration file for additional MCP servers. "
    "The file should contain configuration like: "
    '\'{"mcpServers": {"remote": {"url": "https://example.com/mcp"}}}\'. '
    "See https://gofastmcp.com/clients/client for more details.",
    default=None,
)
@click.option(
    "--name-trigger",
    is_flag=True,
    help="Trigger the agent only when the name is mentioned in the transcript.",
)
@click.option(
    "--language",
    "--lang",
    type=str,
    help="The language to use for transcription and text-to-speech.",
    default=None,
    show_envvar=True,
    envvar="JOINLY_LANGUAGE",
)
@click.option(
    "--vad",
    type=str,
    help='Voice Activity Detection service to use. Options are: "silero", "webrtc".',
    default=None,
    show_envvar=True,
    envvar="JOINLY_VAD",
)
@click.option(
    "--stt",
    type=str,
    help='Speech-to-Text service to use. Options are: "whisper" (local), "deepgram".',
    default=None,
    show_envvar=True,
    envvar="JOINLY_STT",
)
@click.option(
    "--tts",
    type=str,
    help='Text-to-Speech service to use. Options are: "kokoro" (local), '
    '"elevenlabs", "deepgram".',
    default=None,
    show_envvar=True,
    envvar="JOINLY_TTS",
)
@click.option(
    "--vad-arg",
    "vad_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="Arguments for the VAD service in the form of key=value. "
    "Can be specified multiple times.",
)
@click.option(
    "--stt-arg",
    "stt_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="Arguments for the STT service in the form of key=value. "
    "Can be specified multiple times.",
)
@click.option(
    "--tts-arg",
    "tts_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="Arguments for the TTS service in the form of key=value. "
    "Can be specified multiple times.",
)
@click.option(
    "--transcription-controller-arg",
    "transcription_controller_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="Arguments for the transcription controller in the form of key=value. "
    "Can be specified multiple times.",
)
@click.option(
    "--speech-controller-arg",
    "speech_controller_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="Arguments for the speech controller in the form of key=value. "
    "Can be specified multiple times.",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase logging verbosity (can be used multiple times).",
    default=1,
)
@click.option(
    "--auto-leave/--no-auto-leave",
    is_flag=True,
    help="Automatically leave the meeting once all other participants have left.",
    default=True,
    show_default=True,
)
@click.option(
    "--memory/--no-memory",
    "memory_enabled",
    default=None,
    envvar="JOINLY_MEMORY_ENABLED",
    help=(
        "Enable persistent memory. Defaults to auto when Azure storage env vars "
        "exist."
    ),
)
@click.option(
    "--memory-connection-string-env",
    type=str,
    default="AZURE_STORAGE_CONNECTION_STRING",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_MEMORY_CONNECTION_STRING_ENV",
    help="Environment variable containing the Azure Storage connection string.",
)
@click.option(
    "--memory-agent-id",
    type=str,
    default=None,
    show_envvar=True,
    envvar="JOINLY_MEMORY_AGENT_ID",
    help="Stable memory identity for this single agent. Defaults to the bot name.",
)
@click.option(
    "--memory-project-id",
    type=str,
    default=_DEFAULT_MEMORY_PROJECT_ID,
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_MEMORY_PROJECT_ID",
    help="Project scope used for cross-meeting memory and summaries.",
)
@click.option(
    "--memory-client-id",
    type=str,
    default=None,
    show_envvar=True,
    envvar="JOINLY_MEMORY_CLIENT_ID",
    help="Optional client scope used when loading or writing client memories.",
)
@click.option(
    "--memory-ttl-secs",
    type=float,
    default=300.0,
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_MEMORY_TTL_SECS",
    help="Memory query cache TTL in seconds.",
)
@click.option(
    "--memory-cache-maxsize",
    type=int,
    default=256,
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_MEMORY_CACHE_MAXSIZE",
    help="Maximum cached memory query result entries.",
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress all but error and critical logging."
)
@click.argument(
    "meeting-url",
    type=str,
    required=True,
)
def cli(  # noqa: PLR0913
    *,
    joinly_url: str,
    name: str,
    llm_provider: str,
    llm_model: str,
    prompt: str | None,
    prompt_file: str | None,
    prompt_style: str,
    name_trigger: bool,
    mcp_config: str | None,
    auto_leave: bool,
    memory_enabled: bool | None,
    memory_connection_string_env: str,
    memory_agent_id: str | None,
    memory_project_id: str | None,
    memory_client_id: str | None,
    memory_ttl_secs: float,
    memory_cache_maxsize: int,
    meeting_url: str,
    verbose: int,
    quiet: bool,
    **settings: Any,  # noqa: ANN401
) -> None:
    """Run the joinly client."""
    from rich.logging import RichHandler
    from joinly.utils.logging import install_deepgram_shutdown_filter

    log_level = logging.WARNING
    if quiet:
        log_level = logging.ERROR
    elif verbose == 1:
        log_level = logging.INFO
    elif verbose == 2:  # noqa: PLR2004
        log_level = logging.DEBUG

    logging.basicConfig(
        level=logging.WARNING if not quiet else logging.ERROR,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    logging.getLogger("joinly_client").setLevel(log_level)
    install_deepgram_shutdown_filter()

    if prompt_file and not prompt:
        try:
            with Path(prompt_file).open("r") as f:
                prompt = f.read().strip()
        except Exception:
            logger.exception("Failed to load prompt file")
            prompt = None

    mcp_config_dict: dict[str, Any] | None = None
    if mcp_config:
        try:
            with Path(mcp_config).open("r") as f:
                mcp_config_dict = json.load(f)
        except Exception:
            logger.exception("Failed to load MCP configuration file")
            mcp_config_dict = None

    try:
        asyncio.run(
            run(
                joinly_url=joinly_url,
                meeting_url=meeting_url,
                llm_provider=llm_provider,
                llm_model=llm_model,
                prompt=prompt,
                prompt_style=prompt_style,
                name=name,
                name_trigger=name_trigger,
                mcp_config=mcp_config_dict,
                auto_leave=auto_leave,
                memory_enabled=memory_enabled,
                memory_connection_string_env=memory_connection_string_env,
                memory_agent_id=memory_agent_id,
                memory_project_id=memory_project_id,
                memory_client_id=memory_client_id,
                memory_ttl_secs=memory_ttl_secs,
                memory_cache_maxsize=memory_cache_maxsize,
                settings={k: v for k, v in settings.items() if v is not None},
            )
        )
    except KeyboardInterrupt:
        logger.info("Exiting due to keyboard interrupt.")


async def run(  # noqa: PLR0913, C901
    joinly_url: str | FastMCP,
    meeting_url: str,
    llm_provider: str,
    llm_model: str,
    *,
    prompt: str | None = None,
    prompt_style: str | None = None,
    name: str | None = None,
    name_trigger: bool = False,
    mcp_config: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    auto_leave: bool = True,
    memory_enabled: bool | None = None,
    memory_connection_string_env: str = "AZURE_STORAGE_CONNECTION_STRING",
    memory_agent_id: str | None = None,
    memory_project_id: str | None = _DEFAULT_MEMORY_PROJECT_ID,
    memory_client_id: str | None = None,
    memory_ttl_secs: float = 300.0,
    memory_cache_maxsize: int = 256,
) -> None:
    """Run the joinly client.

    Args:
        joinly_url (str | FastMCP): The URL of the joinly server or a FastMCP instance.
        meeting_url (str): The URL of the meeting to join.
        llm_provider (str): The provider of the LLM model to use.
        llm_model (str): The name of the LLM model to use.
        prompt (str | None): System prompt to use for the model.
        prompt_style (str | None): Default prompt to use if no custom one is provided.
        name (str | None): The name of the participant.
        name_trigger (bool): Whether to trigger the agent only when the name is
            mentioned.
        mcp_config (dict[str, Any] | None): Configuration for additional MCP servers.
        settings (dict[str, Any] | None): Additional settings for the client.
        auto_leave (bool): Automatically leave the meeting once all other
            participants have left.
        memory_enabled (bool | None): Enable memory, disable it, or auto-detect
            Azure storage configuration when None.
        memory_connection_string_env (str): Env var containing the Azure Storage
            connection string used by the memory store.
        memory_agent_id (str | None): Stable memory namespace for this agent.
        memory_project_id (str | None): Project namespace for cross-meeting memory.
        memory_client_id (str | None): Optional client namespace for org context.
        memory_ttl_secs (float): Memory query cache TTL.
        memory_cache_maxsize (int): Maximum cached query result entries.
    """
    # Event set by the LLM tool when a participant asks the bot to leave.
    # The main monitoring loop watches this and triggers summary + leave.
    requested_leave_event = asyncio.Event()

    client = JoinlyClient(
        joinly_url,
        name=name,
        name_trigger=name_trigger,
        settings=settings,
    )
    memory_agent_id = memory_agent_id or _derive_single_agent_id(client.name)
    memory_project_id = memory_project_id or _DEFAULT_MEMORY_PROJECT_ID
    meeting_series_id = _derive_meeting_series_id(meeting_url)
    meeting_occurrence_id = _derive_meeting_occurrence_id(meeting_url)
    memory_store = _build_single_agent_memory_store(
        memory_enabled,
        memory_connection_string_env,
        memory_ttl_secs,
        memory_cache_maxsize,
    )
    if memory_store:
        logger.info(
            "Persistent memory enabled for agent=%s project=%s "
            "meeting_series=%s meeting_occurrence=%s.",
            memory_agent_id,
            memory_project_id,
            meeting_series_id,
            meeting_occurrence_id,
        )

    meeting_exclusions: list[_MeetingExclusion] = []

    mcp_config = _normalize_mcp_config(mcp_config)

    additional_clients = (
        {
            name: Client({"mcpServers": {name: config}})
            for name, config in mcp_config["mcpServers"].items()
        }
        if mcp_config
        else {}
    )

    last_participant_activity_at: datetime | None = None
    assistive_tracker = _AssistiveModeTracker(agent_name=client.name)

    async def log_segments(segments: list[TranscriptSegment]) -> None:
        """Log segments received from the client."""
        nonlocal last_participant_activity_at
        assistive_tracker.observe_segments(segments)
        for segment in segments:
            logger.info('%s: "%s"', segment.speaker or "Participant", segment.text)
            if (
                segment.role == SpeakerRole.participant
                and segment.speaker
                and _normalize_participant_name(segment.speaker)
                != _normalize_participant_name(client.name)
            ):
                last_participant_activity_at = datetime.now(tz=UTC)

    client.add_segment_callback(log_segments)
    llm = get_llm(llm_provider, llm_model)

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(client)
        for client_name, additional_client in additional_clients.items():
            logger.info("Connecting to %s", client_name)
            await stack.enter_async_context(additional_client)
            logger.debug("Connected to %s", client_name)

        joinly_config = McpClientConfig(client=client.client, exclude=["join_meeting", "leave_meeting"])

        # ── request_leave tool ───────────────────────────────────────────────────
        # Registered as an in-process FastMCP tool so the LLM can trigger a
        # graceful leave (summary → chat → leave_meeting) without calling
        # leave_meeting directly, which would skip the summary step.
        internal_mcp = FastMCP("joinly-internal")
        post_meeting_summary_callback: Any = None

        @internal_mcp.tool()
        async def request_leave() -> str:
            """Signal that the bot should generate the meeting summary, post it to
            chat, and then gracefully leave the meeting. Call this when any
            participant explicitly asks the bot to leave the meeting.
            """
            logger.info("request_leave tool invoked — triggering graceful leave.")
            requested_leave_event.set()
            return "Leave requested. I will post the meeting summary and leave shortly."

        @internal_mcp.tool()
        async def post_meeting_summary() -> str:
            """Generate and post the full structured meeting summary to chat.

            Use this when a participant asks for a meeting summary but does not
            ask the bot to leave the meeting.
            """
            logger.info("post_meeting_summary tool invoked.")
            if post_meeting_summary_callback is None:
                return "Meeting summary is not ready yet."
            await _speak_action_filler(client, "I am bringing up the summary.")
            return await post_meeting_summary_callback()

        agent_ref: Any | None = None

        async def _refresh_agent_memory_prompt(
            meeting_start_str: str | None = None,
        ) -> None:
            if agent_ref is None or memory_store is None:
                return
            refreshed_memory_block = ""
            if memory_module is not None:
                refreshed_memory_block = await memory_module.build_memory_block(
                    memory_store,
                    memory_agent_id,
                    project_id=memory_project_id,
                    client_id=memory_client_id,
                    meeting_id=meeting_occurrence_id,
                    meeting_series_id=meeting_series_id,
                )
                refreshed_memory_block = _filter_memory_text_by_exclusions(
                    refreshed_memory_block,
                    meeting_exclusions,
                )
            refreshed_instructions = _compose_memory_instructions(
                prompt,
                prompt_style,
                refreshed_memory_block,
                True,
            )
            refreshed_instructions = _compose_action_instructions(
                refreshed_instructions,
                prompt_style,
            )
            agent_ref._prompt = get_prompt(  # noqa: SLF001
                instructions=refreshed_instructions,
                prompt_style=prompt_style,
                name=client.name,
                meeting_start=meeting_start_str,
            )

        @internal_mcp.tool()
        async def exclude_from_summary(
            text_or_topic: str,
            reason: str | None = None,
        ) -> str:
            """Exclude matching current-meeting content from summary and memory.

            Call this when a participant asks to remove, ignore, redact, forget,
            or not remember a specific part of the current meeting. Provide the
            exact phrase when available, otherwise a concise topic such as
            "football" or "weekend chat".
            """
            text = text_or_topic.strip()
            if len(text) < _EXCLUSION_MIN_TEXT_CHARS:
                return "No exclusion added: provide the text or topic to omit."
            await _speak_action_filler(client, "Yeah, doing it now.")
            exclusion = _MeetingExclusion(
                text_or_topic=text,
                reason=reason.strip() if reason else None,
            )
            meeting_exclusions.append(exclusion)
            deleted_count = 0
            if memory_store:
                deleted_count = await _delete_matching_memories_for_exclusion(
                    memory_store,
                    agent_id=memory_agent_id,
                    project_id=memory_project_id,
                    meeting_series_id=meeting_series_id,
                    meeting_occurrence_id=meeting_occurrence_id,
                    meeting_start_time=meeting_start_time,
                    exclusion=exclusion,
                )
            await _refresh_agent_memory_prompt(meeting_start_str)
            scrubbed_message_count = 0
            if agent_ref is not None:
                scrubbed_message_count = _scrub_agent_messages_for_exclusions(
                    agent_ref._messages,  # noqa: SLF001
                    meeting_exclusions,
                )
            logger.info(
                "Added runtime current-meeting summary/memory exclusion: %r (%s)",
                text,
                reason or "no reason provided",
            )
            if deleted_count:
                logger.info(
                    "Deleted %d persisted memory item(s) matching exclusion %r.",
                    deleted_count,
                    text,
                )
            if scrubbed_message_count:
                logger.info(
                    "Scrubbed %d active agent message part(s) matching exclusion %r.",
                    scrubbed_message_count,
                    text,
                )
            return f"Excluded from this meeting's summary and memory: {text}"

        internal_client = Client(internal_mcp)
        await stack.enter_async_context(internal_client)

        async def _speak_filler(tool_name: str, args: dict[str, Any]) -> None:
            import random
            fillers = [
                "Let me check that for you.",
                "Sure, give me just a moment.",
                "On it — one second.",
                "Let me look that up real quick.",
                "Good question, pulling that up now."
            ]
            asyncio.create_task(client.speak_text(random.choice(fillers)))

        tools, tool_executor = await load_tools(
            {
                "joinly": joinly_config,
                "joinly-internal": McpClientConfig(client=internal_client),
                **(
                    {
                        n: McpClientConfig(c, pre_callback=_speak_filler)
                        for n, c in additional_clients.items()
                    }
                    if additional_clients
                    else {}
                ),
            }
        )
        tool_executor = _with_explicit_speech_mute_control(client, tool_executor)
        memory_module: Any | None = None
        if memory_store:
            import joinly_client.memory as memory_module

            tools = [
                *tools,
                memory_module.REMEMBER_TOOL_DEFINITION,
                memory_module.RECALL_MEMORY_TOOL_DEFINITION,
            ]
            original_tool_executor = tool_executor

            async def tool_executor_with_memory(
                tool_name: str,
                args: dict[str, Any],
            ) -> Any:  # noqa: ANN401
                if tool_name == "remember":
                    content = args["content"]
                    if _matches_any_meeting_exclusion(content, meeting_exclusions):
                        logger.info(
                            "Suppressed remember tool call because content matches "
                            "a current-meeting exclusion."
                        )
                        return (
                            "Not remembered because it matches a current-meeting "
                            "summary/memory exclusion."
                        )
                    return await memory_module.write_memory(
                        memory_store,
                        memory_agent_id,
                        memory_project_id,
                        memory_client_id,
                        meeting_series_id,
                        set(),
                        content=content,
                        scope=args["scope"],
                        memory_type=args["memory_type"],
                        participant_id=args.get("participant_id"),
                    )
                if tool_name == "recall_memory":
                    meeting_context = args.get("meeting_context", "all")
                    recall_meeting_id = args.get("meeting_id")
                    if not recall_meeting_id and meeting_context == "current_link":
                        recall_meeting_id = meeting_series_id
                    elif (
                        not recall_meeting_id
                        and meeting_context == "current_occurrence"
                    ):
                        recall_meeting_id = meeting_occurrence_id
                    recalled = await memory_module.recall_memory(
                        memory_store,
                        memory_agent_id,
                        memory_project_id,
                        memory_client_id,
                        scope=args.get("scope", "meeting"),
                        meeting_id=recall_meeting_id,
                        memory_type=args.get("memory_type"),
                        participant_id=args.get("participant_id"),
                        limit=args.get("limit", 10),
                        excluded_meeting_ids=_split_excluded_meeting_ids(),
                    )
                    return _filter_memory_text_by_exclusions(
                        recalled,
                        meeting_exclusions,
                    )
                return await original_tool_executor(tool_name, args)

            tool_executor = tool_executor_with_memory

        memory_block = ""
        if memory_store and memory_module is not None:
            try:
                memory_block = await memory_module.build_memory_block(
                    memory_store,
                    memory_agent_id,
                    project_id=memory_project_id,
                    client_id=memory_client_id,
                    meeting_id=meeting_occurrence_id,
                    meeting_series_id=meeting_series_id,
                )
                memory_block = _filter_memory_text_by_exclusions(
                    memory_block,
                    meeting_exclusions,
                )
            except Exception:
                logger.exception("Failed to load persistent memory context.")
            else:
                if memory_block:
                    logger.info("Loaded persistent memory context for agent prompt.")
                else:
                    logger.info("No prior persistent memory context found.")

        prompt_instructions = _compose_memory_instructions(
            prompt,
            prompt_style,
            memory_block,
            memory_store is not None,
        )
        prompt_instructions = _compose_action_instructions(
            prompt_instructions,
            prompt_style,
        )
        agent = client.create_agent(
            llm,
            tools,
            tool_executor,
            prompt=get_prompt(
                instructions=prompt_instructions,
                prompt_style=prompt_style,
                name=client.name,
            ),
        )
        async with agent:
            agent_ref = agent
            meeting_start_time = datetime.now(tz=UTC)
            meeting_start_str = meeting_start_time.strftime("%H:%M UTC")
            # Re-inject prompt with actual meeting start time
            agent._prompt = get_prompt(  # noqa: SLF001
                instructions=prompt_instructions,
                prompt_style=prompt_style,
                name=client.name,
                meeting_start=meeting_start_str,
            )
            await client.join_meeting(meeting_url)

            # Keep the bot unmuted for the whole session. Repeated mute/unmute
            # around every response causes Teams selector collisions and delay.
            with contextlib.suppress(Exception):
                await client.unmute()

            # Pre-open the Teams chat panel so the first send_chat_message is
            # instant rather than waiting for the panel to animate open.
            # Retry up to 5 times — the meeting UI may not have fully settled
            # immediately after join.
            logger.info("Pre-opening chat panel...")
            _prewarm_ok = False
            for _attempt in range(1, 6):
                try:
                    await client.open_chat_panel()
                    logger.info("Chat panel confirmed open (attempt %d).", _attempt)
                    _prewarm_ok = True
                    break
                except Exception as _e:  # noqa: BLE001
                    if _attempt < 5:
                        logger.info(
                            "Chat panel pre-warm attempt %d/5 failed (%s) — retrying in 3 s.",
                            _attempt,
                            _e,
                        )
                        await asyncio.sleep(3)
                    else:
                        logger.warning(
                            "Chat panel pre-warm failed after 5 attempts: %s", _e
                        )
            if not _prewarm_ok:
                logger.warning(
                    "Chat panel could not be pre-opened; first send_chat_message "
                    "will open it on demand."
                )

            # Set up graceful shutdown on SIGTERM / SIGINT (docker stop)
            shutdown_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, shutdown_event.set)
            except NotImplementedError:
                # Windows fallback — add_signal_handler not supported
                def _win_shutdown(signum: int, frame: object) -> None:
                    loop.call_soon_threadsafe(shutdown_event.set)

                signal.signal(signal.SIGINT, _win_shutdown)
                with contextlib.suppress(OSError, ValueError):
                    signal.signal(signal.SIGTERM, _win_shutdown)

            chat_trigger_tracker = _ChatTriggerTracker(
                agent_name=client.name,
                sent_chat_matcher=client,
            )
            try:
                initial_history = await client.get_chat_history()
                chat_trigger_tracker.baseline(initial_history.messages)
                logger.info(
                    "Chat polling initialized after %d existing message(s).",
                    len(initial_history.messages),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Chat polling baseline failed: %s", e)

            async def poll_chat() -> None:
                """Poll the chat history and trigger agent on new mentions."""
                while not shutdown_event.is_set():
                    try:
                        history = await client.get_chat_history()
                        for msg in chat_trigger_tracker.new_participant_messages(
                            history.messages,
                        ):
                            nonlocal last_participant_activity_at
                            last_participant_activity_at = datetime.now(tz=UTC)
                            assistive_tracker.observe_chat_message(msg)
                            if client.name.strip().casefold() not in msg.text.casefold():
                                continue
                            logger.info("Triggered by chat message: %s", msg.text)
                            import time
                            segment = TranscriptSegment(
                                text=f"[In Meeting Chat] {msg.text}",
                                start=time.time(),
                                end=time.time() + 1,
                                speaker=msg.sender or "Participant",
                                role=SpeakerRole.participant,
                            )
                            # Feed it directly into the agent
                            await agent.on_utterance([segment])
                        nudge = assistive_tracker.maybe_build_nudge()
                        if nudge:
                            logger.info("Posting assistive-mode nudge to chat.")
                            await client.send_chat_message(nudge)
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"Chat polling failed: {e}")
                    await asyncio.sleep(2)

            chat_poll_task = asyncio.create_task(poll_chat())

            # Wire up the on-demand summary tool now that agent + meeting state
            # are fully initialised.
            async def _do_post_meeting_summary() -> str:
                """Generate and post the current-meeting summary to chat."""
                from pydantic_ai.direct import model_request as _mr
                from pydantic_ai.messages import (
                    ModelRequest as _MR,
                )
                from pydantic_ai.messages import (
                    ModelResponse as _MResp,
                )
                from pydantic_ai.messages import (
                    SystemPromptPart as _SP,
                )
                from pydantic_ai.messages import (
                    TextPart as _TP,
                )
                from pydantic_ai.messages import (
                    ToolCallPart as _TCP,
                )
                from pydantic_ai.messages import (
                    UserPromptPart as _UP,
                )

                from joinly_client.client import TRANSCRIPT_URL
                from joinly_client.types import Transcript

                elapsed = datetime.now(tz=UTC) - meeting_start_time
                mins = int(elapsed.total_seconds() // 60)

                convo_lines: list[str] = []
                try:
                    resource = await client.client.read_resource(TRANSCRIPT_URL)
                    resource_text = getattr(resource[0], "text", "")
                    live_transcript = Transcript.model_validate_json(resource_text)
                    for s in live_transcript.compact(max_gap=2.0).segments:
                        convo_lines.append(f"{s.speaker or 'Participant'}: {s.text}")
                except Exception:  # noqa: BLE001
                    pass

                try:
                    chat_history = await client.get_chat_history()
                    chat_lines = _chat_history_lines_for_summary(
                        chat_history.messages,
                        sent_chat_matcher=client,
                    )
                    if chat_lines:
                        convo_lines.extend(chat_lines)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to include meeting chat in summary context: %s",
                        exc,
                    )

                if not convo_lines:
                    for m in agent._messages:  # noqa: SLF001
                        for p in getattr(m, "parts", []):
                            if isinstance(p, _UP) and isinstance(p.content, str) and ": " in p.content and len(p.content) > 5:
                                convo_lines.append(p.content)
                    for m in agent._messages:  # noqa: SLF001
                        if isinstance(m, _MResp):
                            for p in m.parts:
                                if isinstance(p, _TCP) and p.tool_name == "joinly_speak_text" and isinstance(p.args, str):
                                    try:
                                        import json as _json
                                        txt = _json.loads(p.args).get("text", "")
                                        if txt:
                                            convo_lines.append(f"Alex: {txt}")
                                    except Exception:  # noqa: BLE001
                                        pass

                convo_lines, excluded_count = _apply_meeting_exclusions(
                    convo_lines,
                    meeting_exclusions,
                )
                if excluded_count:
                    logger.info(
                        "Excluded %d transcript line(s) from on-demand summary.",
                        excluded_count,
                    )

                convo_text = "\n".join(convo_lines[-120:])
                summary_text = _fallback_summary_text(mins, convo_lines)

                if convo_text.strip():
                    try:
                        resp = await _mr(
                            llm,
                            [_MR(parts=[_SP(_MEETING_SUMMARY_SYSTEM_PROMPT), _UP(f"Meeting duration: {mins} minutes.\n\nTranscript:\n{convo_text}")])],
                            model_request_parameters=__import__("pydantic_ai.models", fromlist=["ModelRequestParameters"]).ModelRequestParameters(
                                function_tools=[], allow_text_output=True, output_tools=[]
                            ),
                        )
                        model_text = next((p.content for p in resp.parts if isinstance(p, _TP)), None)
                        if model_text:
                            summary_text = model_text
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Summary LLM failed; using fallback: %s", e)

                summary_text, summary_excluded_count = _apply_meeting_exclusions_to_text(
                    summary_text,
                    meeting_exclusions,
                )
                if summary_excluded_count:
                    logger.info(
                        "Excluded %d generated summary line(s) from on-demand summary.",
                        summary_excluded_count,
                    )

                header = f"📋 Meeting Summary ({mins} min)\n\n"
                full_msg = header + summary_text
                if memory_store:
                    with contextlib.suppress(Exception):
                        await _save_meeting_summary_memory(
                            memory_store,
                            agent_id=memory_agent_id,
                            project_id=memory_project_id,
                            meeting_series_id=meeting_series_id,
                            meeting_occurrence_id=meeting_occurrence_id,
                            meeting_start_time=meeting_start_time,
                            summary_text=summary_text,
                        )
                await _send_chat_message_with_retries(client, full_msg)
                logger.info("On-demand summary posted to chat.")
                return "Summary posted to chat."

            post_meeting_summary_callback = _do_post_meeting_summary
            auto_leave_summary_posted = False

            try:
                if auto_leave:
                    logger.info(
                        "Auto-leave enabled. Monitoring participant count..."
                    )
                    consecutive_participant_failures = 0
                    alone_since: datetime | None = None
                    while not shutdown_event.is_set():
                        # Check both shutdown signal AND requested-leave every 10s
                        try:
                            done, _ = await asyncio.wait(
                                [
                                    asyncio.create_task(shutdown_event.wait()),
                                    asyncio.create_task(requested_leave_event.wait()),
                                ],
                                timeout=10,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        except Exception:  # noqa: BLE001
                            pass  # keep looping

                        if shutdown_event.is_set():
                            logger.info(
                                "Shutdown requested — posting summary before exit."
                            )
                            auto_leave_summary_posted = await _post_summary_before_leave(
                                post_meeting_summary_callback,
                                summary_posted=auto_leave_summary_posted,
                                reason="shutdown",
                            )
                            break  # SIGTERM / docker stop

                        if requested_leave_event.is_set():
                            logger.info(
                                "Participant requested leave — generating summary and leaving."
                            )
                            auto_leave_summary_posted = await _post_summary_before_leave(
                                post_meeting_summary_callback,
                                summary_posted=auto_leave_summary_posted,
                                reason="requested leave",
                            )
                            break

                        try:
                            participants = await client.get_participants()
                            # Count real active other participants.
                            # _active_other_participants already filters out:
                            #   - the bot itself (by name / self_aliases)
                            #   - explicit non-meeting rows:
                            #     Offline, Left, Not in meeting, Invited, etc.
                            # If the remaining count is 0 the bot is alone.
                            active_others = _active_other_participants(
                                participants.root,
                                self_name=client.name,
                                self_aliases=_split_env_list(
                                    os.environ.get("JOINLY_SELF_ALIASES"),
                                ),
                            )
                            consecutive_participant_failures = 0
                            now = datetime.now(tz=UTC)
                            elapsed_meeting = (
                                now - meeting_start_time
                            ).total_seconds()

                            # Grace period: don't start the alone timer for the
                            # first _AUTOLEAVE_GRACE_SECONDS of the meeting.
                            # This prevents the bot from leaving before the
                            # meeting has actually started (e.g. host joining 5
                            # minutes late).
                            if elapsed_meeting < _AUTOLEAVE_GRACE_SECONDS:
                                if not active_others:
                                    logger.info(
                                        "No other participants yet but still in "
                                        "grace period (%.0f/%.0fs elapsed, roster=%s).",
                                        elapsed_meeting,
                                        _AUTOLEAVE_GRACE_SECONDS,
                                        [
                                            f"{p.name} ({', '.join(p.infos)})"
                                            for p in participants.root
                                        ],
                                    )
                                    alone_since = None  # don't start timer yet
                                    continue

                            # If no active others remain → alone.
                            if not active_others:
                                if alone_since is None:
                                    alone_since = now
                                alone_seconds = (
                                    now - alone_since
                                ).total_seconds()
                                action = _auto_leave_action(
                                    alone_seconds,
                                    summary_posted=auto_leave_summary_posted,
                                )
                                if action == "post_summary":
                                    logger.info(
                                        "No other participants for %.0fs. "
                                        "Posting summary before auto-leave.",
                                        alone_seconds,
                                    )
                                    auto_leave_summary_posted = (
                                        await _post_summary_before_leave(
                                            post_meeting_summary_callback,
                                            summary_posted=auto_leave_summary_posted,
                                            reason="auto-leave",
                                        )
                                    )
                                elif action == "leave":
                                    logger.info(
                                        "No other participants for %.0fs. Auto-leaving...",
                                        alone_seconds,
                                    )
                                    break
                                else:
                                    logger.info(
                                        "No other participants — summary in %.0fs, "
                                        "leaving in %.0fs "
                                        "(%.0f/%ds alone, meeting %.0fs old, roster=%s).",
                                        max(
                                            0,
                                            _AUTOLEAVE_SUMMARY_SECONDS - alone_seconds,
                                        ),
                                        _AUTOLEAVE_ALONE_SECONDS - alone_seconds,
                                        alone_seconds,
                                        _AUTOLEAVE_ALONE_SECONDS,
                                        elapsed_meeting,
                                        [
                                            f"{p.name} ({', '.join(p.infos)})"
                                            for p in participants.root
                                        ],
                                    )
                            else:
                                if alone_since is not None:
                                    logger.info(
                                        "Participant rejoined — resetting alone timer."
                                    )
                                auto_leave_summary_posted = False
                                logger.info(
                                    "Active participants in meeting (%d): %s",
                                    len(active_others),
                                    [
                                        f"{p.name} ({', '.join(p.infos)})"
                                        for p in active_others
                                    ],
                                )
                                alone_since = None
                        except Exception as e:  # noqa: BLE001
                            consecutive_participant_failures += 1
                            logger.warning(
                                "Failed to check participants (%d consecutive, will retry): %s",
                                consecutive_participant_failures,
                                e,
                            )
                            if consecutive_participant_failures >= 3:
                                logger.info(
                                    "Participant check failed 3 times in a row "
                                    "— assuming meeting ended, auto-leaving."
                                )
                                break
                else:
                    # No auto-leave: wait for SIGTERM or a requested-leave
                    await asyncio.wait(
                        [
                            asyncio.create_task(shutdown_event.wait()),
                            asyncio.create_task(requested_leave_event.wait()),
                        ],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if requested_leave_event.is_set():
                        auto_leave_summary_posted = await _post_summary_before_leave(
                            post_meeting_summary_callback,
                            summary_posted=auto_leave_summary_posted,
                            reason="requested leave",
                        )
            finally:
                chat_poll_task.cancel()
                # --- End-of-meeting summary ---
                summary_error: Exception | None = None
                try:
                    if auto_leave_summary_posted:
                        logger.info(
                            "Meeting summary already posted during auto-leave."
                        )
                        raise StopAsyncIteration

                    from pydantic_ai.direct import model_request
                    from pydantic_ai.messages import (
                        ModelRequest,
                        SystemPromptPart,
                        UserPromptPart,
                    )
                    from pydantic_ai.models import (
                        ModelRequestParameters,
                    )

                    from joinly_client.client import TRANSCRIPT_URL
                    from joinly_client.types import Transcript

                    elapsed = datetime.now(tz=UTC) - meeting_start_time
                    mins = int(elapsed.total_seconds() // 60)

                    convo_lines = []
                    # 1. Try to read full live transcript from joinly server resource
                    try:
                        resource = await client.client.read_resource(TRANSCRIPT_URL)
                        resource_text = getattr(resource[0], "text", "")
                        live_transcript = Transcript.model_validate_json(resource_text)
                        compacted = live_transcript.compact(max_gap=2.0)
                        for s in compacted.segments:
                            speaker_name = s.speaker or "Participant"
                            convo_lines.append(f"{speaker_name}: {s.text}")
                        logger.info("Retrieved %d transcript lines from server resource.", len(convo_lines))
                    except Exception as resource_err:
                        logger.warning("Could not read live transcript resource: %s", resource_err)

                    # 2. Fallback to agent message history if resource was empty or failed
                    if not convo_lines:
                        # Capture ONLY real human participant utterances (UserPromptPart lines
                        # that look like "Speaker: text") — excludes system prompts, tool
                        # returns, and Alex's internal speak_text calls so the transcript
                        # reflects the true conversation and nothing gets silently dropped.
                        for m in agent._messages:  # noqa: SLF001
                            for p in getattr(m, "parts", []):
                                if (
                                    isinstance(p, UserPromptPart)
                                    and isinstance(p.content, str)
                                    and ": " in p.content          # "Speaker: text" shape
                                    and len(p.content) > 5
                                ):
                                    convo_lines.append(p.content)

                        # Also capture Alex's spoken replies (joinly_speak_text tool args)
                        from pydantic_ai.messages import (
                            ModelResponse,
                            ToolCallPart,
                        )
                        for m in agent._messages:  # noqa: SLF001
                            if isinstance(m, ModelResponse):
                                for p in m.parts:
                                    if (
                                        isinstance(p, ToolCallPart)
                                        and p.tool_name == "joinly_speak_text"
                                        and isinstance(p.args, str)
                                    ):
                                        try:
                                            import json
                                            text = json.loads(p.args).get("text", "")
                                            if text:
                                                convo_lines.append(f"Alex: {text}")
                                        except Exception:  # noqa: BLE001
                                            pass
                        logger.info("Retrieved %d transcript lines from agent message history.", len(convo_lines))

                    convo_lines, excluded_count = _apply_meeting_exclusions(
                        convo_lines,
                        meeting_exclusions,
                    )
                    if excluded_count:
                        logger.info(
                            "Excluded %d transcript line(s) from final summary.",
                            excluded_count,
                        )

                    # Sort is not needed — messages are already chronological.
                    # Cap at last 120 lines to include long meetings.
                    convo_text = "\n".join(convo_lines[-120:])

                    summary_text = _fallback_summary_text(mins, convo_lines)

                    if convo_text.strip():
                        try:
                            summary_response = await model_request(
                                llm,
                                [
                                    ModelRequest(parts=[
                                        SystemPromptPart(
                                            _MEETING_SUMMARY_SYSTEM_PROMPT
                                        ),
                                        UserPromptPart(
                                            f"Meeting duration: {mins} minutes.\n\n"
                                            f"Transcript:\n{convo_text}"
                                        ),
                                    ])
                                ],
                                model_request_parameters=ModelRequestParameters(
                                    function_tools=[],
                                    allow_text_output=True,
                                    output_tools=[],
                                ),
                            )
                            from pydantic_ai.messages import TextPart
                            model_summary_text = next(
                                (p.content for p in summary_response.parts if isinstance(p, TextPart)),
                                None,
                            )
                            if model_summary_text:
                                summary_text = model_summary_text
                            else:
                                logger.warning(
                                    "Model summary response had no text; using fallback summary."
                                )
                        except Exception as model_summary_err:  # noqa: BLE001
                            logger.warning(
                                "Model summary failed; using fallback summary: %s",
                                model_summary_err,
                            )
                    else:
                        logger.warning(
                            "No participant conversation found; posting fallback summary."
                        )

                    summary_text, summary_excluded_count = (
                        _apply_meeting_exclusions_to_text(
                            summary_text,
                            meeting_exclusions,
                        )
                    )
                    if summary_excluded_count:
                        logger.info(
                            "Excluded %d generated summary line(s) from final summary.",
                            summary_excluded_count,
                        )

                    header = f"📋 Meeting Summary ({mins} min)\n\n"
                    full_msg = header + summary_text
                    logger.info("Full summary (%d chars):\n%s", len(full_msg), full_msg)
                    if memory_store:
                        await _save_meeting_summary_memory(
                            memory_store,
                            agent_id=memory_agent_id,
                            project_id=memory_project_id,
                            meeting_series_id=meeting_series_id,
                            meeting_occurrence_id=meeting_occurrence_id,
                            meeting_start_time=meeting_start_time,
                            summary_text=summary_text,
                        )
                    await _send_chat_message_with_retries(client, full_msg)
                    logger.info("Meeting summary posted to chat.")
                except StopAsyncIteration:
                    pass
                except Exception as summary_err:
                    summary_error = summary_err
                    logger.exception(
                        "Failed to post meeting summary before leaving."
                    )

                # Gracefully leave the meeting on any exit (SIGTERM, auto-leave, etc.)
                logger.info("Leaving meeting before exit...")
                try:
                    await client.leave_meeting()
                except Exception:  # noqa: BLE001
                    pass  # already left or meeting ended
                if summary_error is not None:
                    msg = "Meeting summary was not posted before leaving."
                    raise RuntimeError(msg) from summary_error
                usage = agent.usage.merge(await client.get_usage())
                if usage.root:
                    logger.info("Usage:\n%s", usage)

                if memory_store:
                    close_memory_store = getattr(memory_store, "close", None)
                    if close_memory_store is not None:
                        with contextlib.suppress(Exception):
                            await close_memory_store()


if __name__ == "__main__":
    cli()
