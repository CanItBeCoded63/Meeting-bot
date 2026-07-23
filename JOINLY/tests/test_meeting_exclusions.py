from __future__ import annotations

from joinly_client.main import (
    _apply_meeting_exclusions,
    _apply_meeting_exclusions_to_text,
    _matches_any_meeting_exclusion,
    _MeetingExclusion,
)

REDACTION_CONTROL_LINE_COUNT = 6
SUMMARY_LEAK_LINE_COUNT = 2


def test_meeting_exclusion_removes_matching_topic_lines() -> None:
    """Excluded topics should be removed before summary generation."""
    lines = [
        "Yash: We played football on the weekend.",
        "Yash: The deployment plan is ready for review.",
        "Alex: I will track the release action items.",
    ]

    filtered, removed = _apply_meeting_exclusions(
        lines,
        [_MeetingExclusion(text_or_topic="football")],
    )

    assert removed == 1
    assert filtered == lines[1:]


def test_meeting_exclusion_matches_inflected_phrase() -> None:
    """Simple stemming should match playing/played variants."""
    lines = [
        "Yash: We played football on Saturday.",
        "Yash: The API contract needs a follow-up.",
    ]

    filtered, removed = _apply_meeting_exclusions(
        lines,
        [_MeetingExclusion(text_or_topic="playing football")],
    )

    assert removed == 1
    assert filtered == [lines[1]]


def test_meeting_exclusion_keeps_unrelated_work_content() -> None:
    """Exclusions should not remove unrelated meeting decisions or actions."""
    content = "Yash: We decided to deploy the Teams screen-share fix today."

    assert not _matches_any_meeting_exclusion(
        content,
        [_MeetingExclusion(text_or_topic="football")],
    )


def test_meeting_exclusion_removes_redaction_control_lines() -> None:
    """Redaction requests and confirmations are not summary content."""
    lines = [
        "Yash: Audio check was completed.",
        "Yash: I want to tell you how much I like playing football.",
        "Yash: Yeah. I'm very good at it. You know that?",
        "Alex: Sounds like football is something you take seriously.",
        "Yash: Alex, can you remove this part about football from your memory?",
        "Yash: Like, don't include this.",
        "Alex: I've excluded that football discussion from meeting memory.",
        "Yash: Alex, can you summarize this meeting?",
    ]

    filtered, removed = _apply_meeting_exclusions(
        lines,
        [
            _MeetingExclusion(
                text_or_topic=(
                    "Discussion about Yashwardhan liking football and being very "
                    "good at it"
                ),
            )
        ],
    )

    assert removed == REDACTION_CONTROL_LINE_COUNT
    assert filtered == [lines[0], lines[-1]]


def test_meeting_exclusion_filters_generated_summary_leaks() -> None:
    """Generated summaries are scrubbed if the model mentions redactions."""
    summary = (
        "OVERVIEW\n"
        "The meeting included audio checks.\n"
        "The meeting referenced a private redaction-related topic.\n"
        "A request was made to remove the football discussion from memory.\n"
        "ACTION ITEMS\n"
        "- Remove football from memory. Owner: Alex\n"
        "FOLLOW-UPS\n"
        "None"
    )

    filtered, removed = _apply_meeting_exclusions_to_text(
        summary,
        [_MeetingExclusion(text_or_topic="football")],
    )

    assert removed == SUMMARY_LEAK_LINE_COUNT + 1
    assert "football" not in filtered.casefold()
    assert "redaction" not in filtered.casefold()
    assert "The meeting included audio checks." in filtered
