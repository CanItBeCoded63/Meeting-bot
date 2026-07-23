from __future__ import annotations

from joinly_client.main import (
    _AUTOLEAVE_GRACE_SECONDS,
    _AUTOLEAVE_SUMMARY_SECONDS,
    _active_other_participants,
    _auto_leave_action,
    _post_summary_before_leave,
)
from joinly_client.types import MeetingParticipant


def _participant(name: str, *infos: str) -> MeetingParticipant:
    return MeetingParticipant(name=name, infos=list(infos))


def test_auto_leave_keeps_five_minute_startup_grace() -> None:
    """The bot should wait five minutes before leaving an empty startup call."""
    assert _AUTOLEAVE_GRACE_SECONDS == 300


def test_active_other_participants_ignores_self() -> None:
    """The bot alone should not count as an active other participant."""
    participants = [_participant("Alex", "Organizer", "Muted")]

    assert _active_other_participants(participants, self_name="Alex") == []


def test_active_other_participants_ignores_self_alias() -> None:
    """Teams may display the signed-in account name instead of Alex."""
    participants = [_participant("Gantec Teams Assistant", "Unmuted")]

    assert (
        _active_other_participants(
            participants,
            self_name="Alex",
            self_aliases=["Gantec Teams Assistant"],
        )
        == []
    )


def test_active_other_participants_ignores_stale_teams_rows() -> None:
    """Teams can keep roster rows for people who already left the meeting."""
    participants = [
        _participant("Alex", "Muted"),
        _participant("Yashwardhan Singh Chouhan", "Left the meeting"),
        _participant("Sam", "Not in meeting"),
        _participant("Aria", "Invited"),
        _participant("Morgan", "Offline"),
    ]

    assert _active_other_participants(participants, self_name="Alex") == []


def test_active_other_participants_ignores_presence_only_statuses() -> None:
    """Teams can keep non-meeting rows showing only presence/availability."""
    participants = [
        _participant("Alex", "Muted"),
        _participant("Yashwardhan Singh Chouhan", "Available"),
        _participant("Jordan", "Away"),
        _participant("Taylor", "Busy"),
        _participant("Morgan", "Do not disturb"),
    ]

    assert _active_other_participants(participants, self_name="Alex") == []


def test_active_other_participants_keeps_meeting_state_with_presence() -> None:
    """Presence text should not hide rows that also expose meeting state."""
    participants = [
        _participant("Alex", "Muted"),
        _participant("Yashwardhan Singh Chouhan", "Organizer", "Available"),
        _participant("Jordan", "Muted", "Away"),
    ]

    assert _active_other_participants(participants, self_name="Alex") == [
        participants[1],
        participants[2],
    ]


def test_active_other_participants_keeps_present_people() -> None:
    """Present non-self roster entries should keep auto-leave from firing."""
    participants = [
        _participant("Alex", "Muted"),
        _participant("Yashwardhan Singh Chouhan", "Muted"),
    ]

    assert _active_other_participants(participants, self_name="Alex") == [
        participants[1]
    ]


def test_active_other_participants_keeps_unmuted_organizer() -> None:
    """A visible organizer in the call must prevent the alone timer."""
    participants = [
        _participant("Alex", "Muted"),
        _participant("Yashwardhan Singh Chouhan", "Organizer", "Unmuted"),
    ]

    assert _active_other_participants(participants, self_name="Alex") == [
        participants[1]
    ]


def test_auto_leave_posts_summary_before_leaving() -> None:
    """Auto-leave should post the summary at one minute, then leave later."""
    assert _auto_leave_action(30, summary_posted=False) == "wait"
    assert (
        _auto_leave_action(
            _AUTOLEAVE_SUMMARY_SECONDS,
            summary_posted=False,
        )
        == "post_summary"
    )
    assert (
        _auto_leave_action(
            _AUTOLEAVE_SUMMARY_SECONDS + 1,
            summary_posted=True,
        )
        == "wait"
    )
    assert _auto_leave_action(120, summary_posted=True) == "leave"


async def test_post_summary_before_leave_posts_when_needed() -> None:
    """Leave paths should post a summary before exiting the meeting loop."""
    calls = 0

    async def post_summary() -> str:
        nonlocal calls
        calls += 1
        return "Summary posted to chat."

    posted = await _post_summary_before_leave(
        post_summary,
        summary_posted=False,
        reason="test leave",
        timeout_seconds=1,
    )

    assert posted is True
    assert calls == 1


async def test_post_summary_before_leave_does_not_post_twice() -> None:
    """A leave path must not post a duplicate summary after one was sent."""
    calls = 0

    async def post_summary() -> str:
        nonlocal calls
        calls += 1
        return "Summary posted to chat."

    posted = await _post_summary_before_leave(
        post_summary,
        summary_posted=True,
        reason="test leave",
        timeout_seconds=1,
    )

    assert posted is True
    assert calls == 0


async def test_post_summary_before_leave_reports_failure() -> None:
    """If summary posting fails, the leave path can log and fall back cleanly."""
    msg = "simulated summary failure"

    async def post_summary() -> str:
        raise RuntimeError(msg)

    posted = await _post_summary_before_leave(
        post_summary,
        summary_posted=False,
        reason="test leave",
        timeout_seconds=1,
    )

    assert posted is False
