from joinly_client.agent import ConversationalToolAgent
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
            parts=[
                UserPromptPart(content=[None, "speaker: hello"]),
                ToolReturnPart(tool_name="dummy", content=None, tool_call_id="1"),
            ]
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
