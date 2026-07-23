from typing import Any, cast

import pytest
from joinly_client.agent import ConversationalToolAgent
from joinly_client.main import (
    _MEETING_SUMMARY_SYSTEM_PROMPT,
    _SUMMARY_CHAT_CHUNK_SIZE,
    _chat_history_lines_for_summary,
    _chunk_chat_message,
    _fallback_summary_text,
    _send_chat_message_with_retries,
)
from joinly_client.types import MeetingChatMessage
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import ToolDefinition


def _build_agent() -> ConversationalToolAgent:
    # This test does not call the model, but the agent needs a model instance.
    llm = OpenAIModel(
        "gpt-5.2-chat",
        provider=OpenAIProvider(api_key="test-key"),
    )

    async def _noop_tool_executor(_name: str, _args: dict[str, object]) -> str:
        return "ok"

    return ConversationalToolAgent(
        llm=llm,
        tools=[
            ToolDefinition(
                name="dummy",
                description="dummy tool",
                parameters_json_schema={"type": "object", "properties": {}},
            )
        ],
        tool_executor=_noop_tool_executor,
    )


def test_sanitize_messages_replaces_null_content_and_adds_blank_text_part() -> None:
    """Sanitizer should normalize null-like content before LLM calls."""
    agent = _build_agent()

    messages = [
        ModelRequest(
            parts=cast("Any", [
                UserPromptPart(content=cast("Any", [None, "speaker: hello"])),
                ToolReturnPart(tool_name="dummy", content=None, tool_call_id="1"),
            ])
        ),
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="dummy", args=None, tool_call_id="2"),
            ]
        ),
    ]

    sanitized = agent._sanitize_messages_for_llm(messages)  # noqa: SLF001

    req = sanitized[0]
    assert isinstance(req, ModelRequest)
    assert isinstance(req.parts[0], UserPromptPart)
    assert req.parts[0].content == ["", "speaker: hello"]
    assert isinstance(req.parts[1], ToolReturnPart)
    assert req.parts[1].content == ""

    resp = sanitized[1]
    assert isinstance(resp, ModelResponse)
    assert isinstance(resp.parts[0], TextPart)
    assert resp.parts[0].content == ""
    assert isinstance(resp.parts[1], ToolCallPart)
    assert resp.parts[1].args == {}


def test_summary_chat_chunks_fit_strict_platform_limit() -> None:
    """Summary chunks should fit Google Meet's shortest chat limit."""
    message = "Heading\n\n" + "Action item with owner and context. " * 60

    chunks = _chunk_chat_message(message)

    assert " ".join(chunks).split() == message.split()
    assert all(len(chunk) <= _SUMMARY_CHAT_CHUNK_SIZE for chunk in chunks)
    assert len(chunks) > 1


def test_fallback_summary_is_generated_without_transcript() -> None:
    """Shutdown should still post a summary when transcription captured nothing."""
    summary = _fallback_summary_text(12, [])

    assert "**PROJECTS DISCUSSED**" in summary
    assert "OVERVIEW" in summary
    assert "no participant conversation was captured" in summary
    assert "KEY DECISIONS" in summary
    assert "ACTION ITEMS" in summary
    assert "FOLLOW-UPS" in summary


def test_meeting_summary_prompt_requires_visible_project_sections() -> None:
    """Model summaries should include project context before decisions/actions."""
    assert "**PROJECTS DISCUSSED**" in _MEETING_SUMMARY_SYSTEM_PROMPT
    assert "project-by-project" in _MEETING_SUMMARY_SYSTEM_PROMPT
    assert "**KEY DECISIONS**" in _MEETING_SUMMARY_SYSTEM_PROMPT
    assert "**ACTION ITEMS**" in _MEETING_SUMMARY_SYSTEM_PROMPT
    assert "visible project subheads" in _MEETING_SUMMARY_SYSTEM_PROMPT


class _SentSummaryMatcher:
    """Fake sent-message matcher for summary context tests."""

    def is_sent_chat_message(self, text: str) -> bool:
        """Treat already-posted summaries as bot output."""
        return text.startswith("📋 Meeting Summary")


def test_summary_context_includes_recent_meeting_chat() -> None:
    """On-demand summaries should include recent Teams chat context."""
    lines = _chat_history_lines_for_summary(
        [
            MeetingChatMessage(
                text="Project Alpha decision: keep Azure Table Storage.",
                sender="Yash",
            ),
            MeetingChatMessage(
                text="Project Alpha decision: keep Azure Table Storage.",
                sender="Yash",
            ),
            MeetingChatMessage(
                text="Project Beta action item: prepare screen-share checklist.",
                sender="Yash",
            ),
            MeetingChatMessage(
                text="📋 Meeting Summary (2 min)\nOld summary",
                sender="Alex",
            ),
        ],
        sent_chat_matcher=_SentSummaryMatcher(),
    )

    assert lines == [
        "Yash: [In Meeting Chat] Project Alpha decision: keep Azure Table Storage.",
        "Yash: [In Meeting Chat] Project Beta action item: prepare screen-share checklist.",
    ]


class _FlakyChatSender:
    """Test chat sender that fails once before recording sent messages."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._remaining_failures = 1

    async def send_chat_message(self, message: str) -> None:
        """Record the message after one transient failure."""
        if self._remaining_failures:
            self._remaining_failures -= 1
            msg = "temporary Teams chat failure"
            raise RuntimeError(msg)
        self.sent.append(message)


@pytest.mark.asyncio
async def test_summary_chat_send_retries_transient_failure() -> None:
    """Summary posting should retry a transient Teams send failure."""
    sender = _FlakyChatSender()

    await _send_chat_message_with_retries(sender, "summary", retry_delay=0)

    assert sender.sent == ["summary"]
