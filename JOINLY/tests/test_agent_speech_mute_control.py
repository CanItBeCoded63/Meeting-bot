from __future__ import annotations

from typing import Any, cast

import pytest
from joinly_client.main import _speak_action_filler, _with_explicit_speech_mute_control


class _FakeClient:
    """Client fake that records mute calls."""

    def __init__(self) -> None:
        self.actions: list[str] = []

    async def unmute(self) -> None:
        """Record an unmute action."""
        self.actions.append("unmute")

    async def mute(self) -> None:
        """Record a mute action."""
        self.actions.append("mute")

    async def speak_text(self, text: str) -> None:
        """Record a spoken filler."""
        self.actions.append(f"speak:{text}")


class _FailingSpeakClient:
    """Client fake whose speech fails."""

    async def speak_text(self, _text: str) -> None:
        """Raise like a transient speech failure."""
        msg = "speech unavailable"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_llm_speech_tool_passes_through() -> None:
    """Speech tool calls pass through without mute toggling.

    The bot unmutes once after joining and stays unmuted for the session.
    Per-speech mute/unmute was removed because it caused Teams selector
    collisions and delays.
    """
    client = _FakeClient()

    async def tool_executor(tool_name: str, args: dict[str, Any]) -> str:
        client.actions.append(f"tool:{tool_name}:{args['text']}")
        return "Finished speaking."

    wrapped = _with_explicit_speech_mute_control(
        cast("Any", client),
        tool_executor,
    )

    result = await wrapped("joinly_speak_text", {"text": "Yes, I can hear you."})

    assert result == "Finished speaking."
    # No mute/unmute calls — the wrapper is a deliberate pass-through.
    assert "unmute" not in client.actions
    assert "mute" not in client.actions
    assert "tool:joinly_speak_text:Yes, I can hear you." in client.actions


@pytest.mark.asyncio
async def test_action_filler_speaks_acknowledgement() -> None:
    """Action fillers should speak short acknowledgement text."""
    client = _FakeClient()

    spoken = await _speak_action_filler(
        cast("Any", client),
        "I am bringing up the summary.",
    )

    assert spoken is True
    assert client.actions == ["speak:I am bringing up the summary."]


@pytest.mark.asyncio
async def test_action_filler_failure_does_not_block_action() -> None:
    """Filler speech failures should not fail the requested action tool."""
    spoken = await _speak_action_filler(
        cast("Any", _FailingSpeakClient()),
        "Yeah, doing it now.",
    )

    assert spoken is False


@pytest.mark.asyncio
async def test_non_speech_tool_does_not_change_mute_state() -> None:
    """Non-speech tools should pass through without mute control."""
    client = _FakeClient()

    async def tool_executor(tool_name: str, _args: dict[str, Any]) -> str:
        client.actions.append(f"tool:{tool_name}")
        return "ok"

    wrapped = _with_explicit_speech_mute_control(
        cast("Any", client),
        tool_executor,
    )

    result = await wrapped("joinly_send_chat_message", {"message": "hello"})

    assert result == "ok"
    assert client.actions == ["tool:joinly_send_chat_message"]


@pytest.mark.asyncio
async def test_llm_speech_tool_propagates_failure() -> None:
    """Speech tool failures propagate without any mute side effects."""
    client = _FakeClient()

    async def tool_executor(_tool_name: str, _args: dict[str, Any]) -> str:
        client.actions.append("tool:failed")
        msg = "speech failed"
        raise RuntimeError(msg)

    wrapped = _with_explicit_speech_mute_control(
        cast("Any", client),
        tool_executor,
    )

    with pytest.raises(RuntimeError, match="speech failed"):
        await wrapped("joinly_speak_text", {"text": "hello"})

    assert "tool:failed" in client.actions
    # No mute/unmute — pass-through wrapper doesn't change mute state.
    assert "mute" not in client.actions
    assert "unmute" not in client.actions


def test_agent_does_not_end_turn_after_speak_only() -> None:
    """speak_text alone does not end the turn — the LLM decides when to stop.

    End-turn only triggers on: no tool calls, end_turn tool, speech interrupt,
    or leave_meeting. A plain speak_text completion keeps the turn open so
    the LLM can continue responding.
    """
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.openai import OpenAIModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from pydantic_ai.tools import ToolDefinition

    from joinly_client.agent import ConversationalToolAgent

    llm = OpenAIModel("gpt-5.2-chat", provider=OpenAIProvider(api_key="test"))

    async def _noop(_n: str, _a: dict[str, object]) -> str:
        return "ok"

    agent = ConversationalToolAgent(
        llm=llm,
        tools=[ToolDefinition(name="dummy", description="d", parameters_json_schema={"type": "object", "properties": {}})],
        tool_executor=_noop,
    )

    response = ModelResponse(parts=[ToolCallPart(tool_name="joinly_speak_text", args='{"text":"hi"}', tool_call_id="c1")])
    request = ModelRequest(parts=[ToolReturnPart(tool_name="joinly_speak_text", content="Finished speaking.", tool_call_id="c1")])

    # speak_text completion is NOT a turn-ending event in the current design.
    assert agent._check_end_turn(response, request) is False  # noqa: SLF001


def test_agent_does_not_end_turn_when_chat_also_pending() -> None:
    """speak_only_finished should be False when LLM also called a chat tool."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.openai import OpenAIModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from pydantic_ai.tools import ToolDefinition

    from joinly_client.agent import ConversationalToolAgent

    llm = OpenAIModel("gpt-5.2-chat", provider=OpenAIProvider(api_key="test"))

    async def _noop(_n: str, _a: dict[str, object]) -> str:
        return "ok"

    agent = ConversationalToolAgent(
        llm=llm,
        tools=[ToolDefinition(name="dummy", description="d", parameters_json_schema={"type": "object", "properties": {}})],
        tool_executor=_noop,
    )

    response = ModelResponse(parts=[
        ToolCallPart(tool_name="joinly_speak_text", args='{"text":"hi"}', tool_call_id="c1"),
        ToolCallPart(tool_name="joinly_send_chat_message", args='{"message":"hi"}', tool_call_id="c2"),
    ])
    request = ModelRequest(parts=[
        ToolReturnPart(tool_name="joinly_speak_text", content="Finished speaking.", tool_call_id="c1"),
        ToolReturnPart(tool_name="joinly_send_chat_message", content="Message sent.", tool_call_id="c2"),
    ])

    assert agent._check_end_turn(response, request) is False  # noqa: SLF001
