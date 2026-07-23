from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from joinly_common.types import Transcript, TranscriptSegment

from joinly_client.utils import name_in_transcript, normalize

if TYPE_CHECKING:
    from joinly_client.profiles import AgentProfile


@dataclass
class ConversationState:
    """Routing state — replaced (not mutated) on each route() call."""

    active_agent_ids: list[str] = field(default_factory=list)
    last_trigger_time: float = 0.0
    last_active_speaker: str | None = None
    conversation_window_secs: float = 60.0


class AgentRouter:
    """Routes utterances to agent(s).

    Pure function design — state flows in and out, input state is never mutated.
    """

    def route(
        self,
        segments: list[TranscriptSegment],
        profiles: list[AgentProfile],
        state: ConversationState,
        default_agent_id: str | None = None,
        *,
        now: float | None = None,
    ) -> tuple[list[str], ConversationState]:
        """Determine which agents should respond to these segments.

        Returns (agent_ids, new_state). Input state is never mutated.

        Routing rules applied in order:

        1. Name-trigger match — any agent whose trigger_names appear in
           the transcript.
        2. Conversation window — same speaker continuing within the window.
        3. Default agent fallback — use default_agent_id if set, else
           return empty list.

        Args:
            segments: New transcript segments to route.
            profiles: Enabled agent profiles to match against.
            state: Current conversation state (not mutated).
            default_agent_id: Agent ID to use when no trigger matches.
            now: Override for current monotonic time (for testing).

        Returns:
            Tuple of (matched_agent_ids, updated_state).
        """
        if not segments:
            return [], state

        current_time = now if now is not None else time.monotonic()
        current_speaker_raw = segments[-1].speaker or ""
        current_speaker = normalize(current_speaker_raw)

        transcript = Transcript(segments=segments)

        # Rule 1: name-trigger match
        triggered = [
            p.id
            for p in profiles
            if any(name_in_transcript(transcript, t) for t in p.trigger_names)
        ]

        if triggered:
            new_state = ConversationState(
                active_agent_ids=triggered,
                last_trigger_time=current_time,
                last_active_speaker=current_speaker_raw,
                conversation_window_secs=state.conversation_window_secs,
            )
            return triggered, new_state

        # Rule 2: conversation window — same speaker continuing
        last_speaker = normalize(state.last_active_speaker or "")
        window_active = (
            state.active_agent_ids
            and current_speaker
            and last_speaker == current_speaker
            and (current_time - state.last_trigger_time)
            <= state.conversation_window_secs
        )
        if window_active:
            new_state = ConversationState(
                active_agent_ids=state.active_agent_ids,
                last_trigger_time=current_time,
                last_active_speaker=state.last_active_speaker,
                conversation_window_secs=state.conversation_window_secs,
            )
            return list(state.active_agent_ids), new_state

        # Rule 3: default agent fallback
        defaults = [default_agent_id] if default_agent_id else []
        new_state = ConversationState(
            active_agent_ids=defaults,
            last_trigger_time=current_time if defaults else 0.0,
            last_active_speaker=current_speaker_raw if defaults else None,
            conversation_window_secs=state.conversation_window_secs,
        )
        return defaults, new_state
