"""
Standalone test for the fixed meeting summary generation logic.
Simulates the exact message structure the agent stores in agent._messages
and runs the transcript extraction + LLM summary call directly.
Run with: python test_summary.py
"""
import asyncio
import json
import os
import sys

# ── Add client package to path ────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "client"))

from datetime import datetime, timezone
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
    TextPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.direct import model_request

UTC = timezone.utc


def get_llm():
    """Load the same LLM used by the bot (Azure OpenAI via env)."""
    from joinly_client.main import get_llm as _get_llm  # noqa: PLC0415
    return _get_llm(
        os.environ.get("JOINLY_LLM_PROVIDER", "openai"),
        os.environ.get("JOINLY_LLM_MODEL", "gpt-5.2-chat"),
    )


# ── Simulated agent._messages ─────────────────────────────────────────────────
# This mirrors exactly what ConversationalToolAgent stores during a meeting.
FAKE_MESSAGES = [
    # System prompt (should NOT appear in summary)
    ModelRequest(parts=[SystemPromptPart("You are Alex, a meeting assistant.")]),

    # Participant 1 speaks
    ModelRequest(parts=[UserPromptPart(
        content="Yashwardhan Singh Chouhan: Hey Alex, let's discuss the orchestration layer and calendar integration.",
        timestamp=datetime(2026, 6, 22, 10, 0, 0, tzinfo=UTC),
    )]),

    # Alex's spoken reply (joinly_speak_text ToolCallPart)
    ModelResponse(parts=[ToolCallPart(
        tool_name="joinly_speak_text",
        args=json.dumps({"text": "Sure, let me note that down. What's the plan for calendar integration?"}),
        tool_call_id="call_001",
    )], model_name="gpt-5.2-chat", timestamp=datetime(2026, 6, 22, 10, 0, 5, tzinfo=UTC)),

    # Tool return (should NOT appear as a speaker line)
    ModelRequest(parts=[ToolReturnPart(
        tool_name="joinly_speak_text",
        content="Finished speaking.",
        tool_call_id="call_001",
        timestamp=datetime(2026, 6, 22, 10, 0, 10, tzinfo=UTC),
    )]),

    # Participant discusses action items
    ModelRequest(parts=[UserPromptPart(
        content="Yashwardhan Singh Chouhan: We need to build the orchestration layer first. I will look into calendar integration today.",
        timestamp=datetime(2026, 6, 22, 10, 1, 0, tzinfo=UTC),
    )]),

    ModelRequest(parts=[UserPromptPart(
        content="Yashwardhan Singh Chouhan: Also, the team needs to decide how meetings will be tagged to projects and clients for memory isolation.",
        timestamp=datetime(2026, 6, 22, 10, 1, 30, tzinfo=UTC),
    )]),

    ModelResponse(parts=[ToolCallPart(
        tool_name="joinly_speak_text",
        args=json.dumps({"text": "Got it. So two action items: you will look into calendar integration today, and the team will decide on meeting tagging for memory isolation. I'll keep track of that."}),
        tool_call_id="call_002",
    )], model_name="gpt-5.2-chat", timestamp=datetime(2026, 6, 22, 10, 1, 45, tzinfo=UTC)),

    ModelRequest(parts=[UserPromptPart(
        content="Yashwardhan Singh Chouhan: Yes exactly. Also let's decide that we will use ephemeral Docker containers — one per meeting.",
        timestamp=datetime(2026, 6, 22, 10, 2, 30, tzinfo=UTC),
    )]),

    ModelResponse(parts=[ToolCallPart(
        tool_name="joinly_speak_text",
        args=json.dumps({"text": "Decision noted: ephemeral Docker containers, one per meeting for isolation."}),
        tool_call_id="call_003",
    )], model_name="gpt-5.2-chat", timestamp=datetime(2026, 6, 22, 10, 2, 40, tzinfo=UTC)),

    # Internal tool noise — should NOT appear
    ModelRequest(parts=[ToolReturnPart(
        tool_name="global_weather_get_current_conditions",
        content="# Current Weather Conditions\n**Temperature:** 82°F",
        tool_call_id="call_004",
        timestamp=datetime(2026, 6, 22, 10, 3, 0, tzinfo=UTC),
    )]),
]


async def test_summary():
    print("=" * 60)
    print("Testing fixed summary extraction + prompt")
    print("=" * 60)

    meeting_start_time = datetime(2026, 6, 22, 10, 0, 0, tzinfo=UTC)
    meeting_end_time   = datetime(2026, 6, 22, 10, 5, 0, tzinfo=UTC)
    elapsed = meeting_end_time - meeting_start_time
    mins = int(elapsed.total_seconds() // 60)

    # ── Step 1: Extract transcript (same logic as the fixed main.py) ──────────
    convo_lines = []

    # Human speech
    for m in FAKE_MESSAGES:
        for p in getattr(m, "parts", []):
            if (
                isinstance(p, UserPromptPart)
                and isinstance(p.content, str)
                and ": " in p.content
                and len(p.content) > 5
            ):
                convo_lines.append(p.content)

    # Alex's spoken replies
    for m in FAKE_MESSAGES:
        if isinstance(m, ModelResponse):
            for p in m.parts:
                if (
                    isinstance(p, ToolCallPart)
                    and p.tool_name == "joinly_speak_text"
                    and isinstance(p.args, str)
                ):
                    try:
                        text = json.loads(p.args).get("text", "")
                        if text:
                            convo_lines.append(f"Alex: {text}")
                    except Exception:
                        pass

    convo_text = "\n".join(convo_lines[-120:])

    print("\nExtracted transcript:")
    print("-" * 40)
    print(convo_text)
    print("-" * 40)

    # ── Step 2: Run LLM summary ───────────────────────────────────────────────
    if not convo_text.strip():
        print("\n[FAIL] No transcript lines extracted!")
        return

    print(f"\n[OK] Extracted {len(convo_lines)} lines. Calling LLM for summary...\n")

    llm = get_llm()

    summary_response = await model_request(
        llm,
        [
            ModelRequest(parts=[
                SystemPromptPart(
                    "You are a professional meeting summariser. "
                    "Given a meeting transcript, you MUST always produce ALL of the "
                    "following sections, even if a section has nothing to report "
                    "(write 'None' in that case):\n\n"
                    "OVERVIEW\n"
                    "2-4 sentences describing what the meeting was about and main outcomes.\n\n"
                    "KEY DECISIONS\n"
                    "Bullet list of concrete decisions made. Include 'None' if no decisions.\n\n"
                    "ACTION ITEMS\n"
                    "Bullet list with: action, owner (person responsible), and deadline if mentioned. "
                    "Include ALL commitments, follow-ups, and 'will look into it' type statements. "
                    "If no owner was named, write 'Owner: unassigned'. "
                    "Include 'None' if no action items.\n\n"
                    "FOLLOW-UPS\n"
                    "Bullet list of open questions, topics deferred, or items needing further discussion. "
                    "Include 'None' if no follow-ups.\n\n"
                    "Use plain text. Keep each bullet point concise (1 line)."
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

    summary_text = next(
        (p.content for p in summary_response.parts if isinstance(p, TextPart)),
        None,
    )

    if summary_text:
        header = f"[Meeting Summary] ({mins} min)\n\n"
        full_msg = header + summary_text
        print("=" * 60)
        print("FINAL SUMMARY (what would be posted to Teams chat):")
        print("=" * 60)
        print(full_msg)
        print(f"\n[OK] Total chars: {len(full_msg)}")
        # Verify all 4 sections present
        for section in ["OVERVIEW", "KEY DECISIONS", "ACTION ITEMS", "FOLLOW-UPS"]:
            found = section in summary_text.upper()
            status = "[PASS]" if found else "[FAIL]"
            print(f"  {status} Section '{section}' present: {found}")
    else:
        print("\n[FAIL] LLM returned no text content in summary response")


if __name__ == "__main__":
    asyncio.run(test_summary())
