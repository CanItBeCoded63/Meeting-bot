from __future__ import annotations

from datetime import UTC, datetime, timedelta

from joinly_client.main import _AssistiveModeTracker, _ChatTriggerTracker
from joinly_client.types import MeetingChatMessage, SpeakerRole, TranscriptSegment


class _SentChatMatcher:
    """Minimal sent-message matcher for chat trigger tests."""

    def __init__(self, sent_messages: set[str] | None = None) -> None:
        self.sent_messages = {self._normalize(msg) for msg in sent_messages or set()}

    def is_sent_chat_message(self, text: str) -> bool:
        """Return whether the message text was sent by Alex."""
        normalized = self._normalize(text)
        return any(
            sent == normalized or sent in normalized or normalized in sent
            for sent in self.sent_messages
        )

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.split()).casefold()


def _tracker(sent_messages: set[str] | None = None) -> _ChatTriggerTracker:
    return _ChatTriggerTracker(
        agent_name="Alex",
        sent_chat_matcher=_SentChatMatcher(sent_messages),
    )


def _msg(
    text: str,
    *,
    sender: str | None = "Yash",
    timestamp: str | None = "2026-07-06T07:00:00Z",
) -> MeetingChatMessage:
    return MeetingChatMessage(text=text, sender=sender, timestamp=timestamp)


def test_new_chat_mention_triggers_once() -> None:
    """A new participant chat mention should trigger Alex exactly once."""
    tracker = _tracker()
    message = _msg("Hey Alex, can you help me?")

    assert tracker.new_trigger_messages([message]) == [message]
    assert tracker.new_trigger_messages([message]) == []


def test_baselined_chat_history_is_ignored() -> None:
    """Old Teams chat history should not replay after joining."""
    tracker = _tracker()
    old_message = _msg("Hey Alex, can you summarize the meeting?")

    tracker.baseline([old_message])

    assert tracker.new_trigger_messages([old_message]) == []


def test_own_chat_messages_are_ignored() -> None:
    """Alex should not trigger himself from his own visible Teams chat."""
    tracker = _tracker({"PATCHED_REPLY_OK"})

    own_sender_message = _msg("Hey Alex, this was sent by Alex", sender="Alex")
    own_text_message = _msg("PATCHED_REPLY_OK", sender=None)

    assert tracker.new_trigger_messages([own_sender_message, own_text_message]) == []


def test_teams_timestamp_mutation_does_not_retrigger_same_message() -> None:
    """Teams can mutate metadata for the same rendered message across polls."""
    tracker = _tracker()
    original = _msg(
        "Hey Alex, can you help me in the chat?",
        timestamp="2026-07-06T07:00:00Z",
    )
    rerendered = _msg(
        "Hey Alex, can you help me in the chat?",
        timestamp="2026-07-06T07:00:30Z",
    )

    assert tracker.new_trigger_messages([original]) == [original]
    assert tracker.new_trigger_messages([rerendered]) == []


def test_repeated_real_chat_message_with_same_text_can_trigger_again() -> None:
    """A real second message with the same text should still be processed."""
    tracker = _tracker()
    first = _msg("Hey Alex, status?", timestamp="2026-07-06T07:00:00Z")
    second = _msg("Hey Alex, status?", timestamp="2026-07-06T07:01:00Z")

    assert tracker.new_trigger_messages([first]) == [first]
    assert tracker.new_trigger_messages([first, second]) == [second]


def test_summary_or_leave_chat_request_triggers_agent() -> None:
    """Summary and leave requests sent in chat should reach the agent."""
    tracker = _tracker()
    request = _msg(
        "Hey Alex, please leave and post the meeting summary in chat first."
    )

    assert tracker.new_trigger_messages([request]) == [request]


def test_new_participant_chat_can_be_observed_without_triggering_agent() -> None:
    """Ordinary chat should be observable without invoking the LLM trigger path."""
    tracker = _tracker()
    message = _msg("Project Alpha needs to close the billing blocker by Friday.")

    assert tracker.new_participant_messages([message]) == [message]
    assert tracker.new_trigger_messages([message]) == []


def test_assistive_tracker_waits_for_quiet_gap_before_nudge() -> None:
    """Assistive mode should not post while discussion is still active."""
    tracker = _AssistiveModeTracker(
        agent_name="Alex",
        quiet_gap_seconds=10,
        cooldown_seconds=60,
    )
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    tracker.observe_text(
        "I will finish the Teams regression test by Friday.",
        speaker="Yash",
        source="transcript",
        now=now,
    )

    assert tracker.maybe_build_nudge(now=now + timedelta(seconds=5)) is None
    nudge = tracker.maybe_build_nudge(now=now + timedelta(seconds=11))

    assert nudge is not None
    assert "Teams regression test" in nudge
    assert "Owner: Yash" in nudge
    assert "Deadline: by Friday" in nudge


def test_assistive_tracker_respects_cooldown_and_dedupes() -> None:
    """Assistive mode should avoid repeated reminders for the same item."""
    tracker = _AssistiveModeTracker(
        agent_name="Alex",
        quiet_gap_seconds=1,
        cooldown_seconds=60,
    )
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    tracker.observe_text(
        "We need to assign the API bug owner tomorrow.",
        speaker="Yash",
        source="chat",
        now=now,
    )
    tracker.observe_text(
        "We need to assign the API bug owner tomorrow.",
        speaker="Yash",
        source="chat",
        now=now + timedelta(seconds=1),
    )

    first = tracker.maybe_build_nudge(now=now + timedelta(seconds=2))
    second = tracker.maybe_build_nudge(now=now + timedelta(seconds=30))

    assert first is not None
    assert second is None
    assert len(tracker.pending_items) == 1


def test_assistive_tracker_observes_participant_segments() -> None:
    """Assistive mode should capture pending work from transcript segments."""
    tracker = _AssistiveModeTracker(
        agent_name="Alex",
        quiet_gap_seconds=1,
        cooldown_seconds=60,
    )
    now = datetime.now(UTC)

    tracker.observe_segments(
        [
            TranscriptSegment(
                text="I will update the memory docs tomorrow.",
                start=1,
                end=2,
                speaker="Yash",
                role=SpeakerRole.participant,
            )
        ]
    )

    nudge = tracker.maybe_build_nudge(now=now + timedelta(seconds=5))

    assert nudge is not None
    assert "memory docs" in nudge


def test_assistive_tracker_extracts_named_owner_without_deadline_confusion() -> None:
    """A deadline like 'by Friday' should not be treated as the owner."""
    tracker = _AssistiveModeTracker(
        agent_name="Alex",
        quiet_gap_seconds=1,
        cooldown_seconds=60,
    )
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    tracker.observe_text(
        "Project Alpha is blocked by token refresh. Yash will investigate it by Friday.",
        speaker="Avery",
        source="chat",
        now=now,
    )

    nudge = tracker.maybe_build_nudge(now=now + timedelta(seconds=2))

    assert nudge is not None
    assert "Owner: Yash" in nudge
    assert "Owner: Friday" not in nudge
    assert "Deadline: by Friday" in nudge


def test_assistive_tracker_does_not_treat_project_name_as_owner() -> None:
    """Project labels should not be mistaken for person owners."""
    tracker = _AssistiveModeTracker(
        agent_name="Alex",
        quiet_gap_seconds=1,
        cooldown_seconds=60,
    )
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

    tracker.observe_text(
        "Project Gamma needs to confirm the onboarding checklist owner by "
        "Friday. Priya will send the final checklist tomorrow.",
        speaker="Avery",
        source="chat",
        now=now,
    )

    nudge = tracker.maybe_build_nudge(now=now + timedelta(seconds=2))

    assert nudge is not None
    assert "Owner: Priya" in nudge
    assert "Owner: Gamma" not in nudge
