from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from joinly_client.profiles import AgentProfile

logger = logging.getLogger(__name__)


class ConflictResolver:
    """Reduces router-triggered agents to final responders.

    Exclusive semantics: an exclusive agent with the highest priority fully
    suppresses all other exclusive agents and all non-exclusive agents from
    the speech channel. If no exclusive agent is present, all triggered agents
    respond, capped at max_concurrent.

    Non-exclusive agents suppressed by an exclusive winner are dropped from
    the speech channel. Whether they may run silently for non-speech tools is
    a concern for the caller.
    """

    def __init__(self, max_concurrent: int = 2) -> None:
        """Initialize the resolver.

        Args:
            max_concurrent: Maximum number of non-exclusive agents that may
                respond simultaneously. Defaults to 2.
        """
        self._max_concurrent = max_concurrent

    def resolve(
        self,
        agent_ids: list[str],
        profiles_by_id: dict[str, AgentProfile],
    ) -> list[str]:
        """Return the final list of agent_ids that should respond.

        Args:
            agent_ids: Candidate agent IDs from the router.
            profiles_by_id: Mapping of agent_id → AgentProfile for all
                enabled agents.

        Returns:
            Ordered list of agent IDs that should respond, respecting
            priority and max_concurrent cap.
        """
        if not agent_ids:
            return []

        # Deduplicate preserving order
        seen: set[str] = set()
        unique_ids = [a for a in agent_ids if not (a in seen or seen.add(a))]  # type: ignore[func-returns-value]

        if len(unique_ids) == 1:
            return unique_ids

        triggered: list[AgentProfile] = []
        for aid in unique_ids:
            profile = profiles_by_id.get(aid)
            if profile is None:
                logger.warning(
                    "Agent %r in route result but not in registry — skipping", aid
                )
                continue
            triggered.append(profile)

        if not triggered:
            return []

        # Rule 1: exclusive agent present → highest priority exclusive wins
        exclusive = [p for p in triggered if p.exclusive]
        if exclusive:
            winner = min(exclusive, key=lambda p: (p.priority, p.id))
            suppressed = [p.id for p in triggered if p.id != winner.id]
            logger.info(
                "Conflict resolution: exclusive agent %r suppresses %s",
                winner.id,
                suppressed,
            )
            return [winner.id]

        # Rule 2: all non-exclusive — sort by priority, cap at max_concurrent
        sorted_agents = sorted(triggered, key=lambda p: (p.priority, p.id))
        final = [p.id for p in sorted_agents[: self._max_concurrent]]

        if len(triggered) > self._max_concurrent:
            dropped = [p.id for p in sorted_agents[self._max_concurrent :]]
            logger.info(
                "Conflict resolution: max_concurrent=%d, keeping %s, dropping %s",
                self._max_concurrent,
                final,
                dropped,
            )

        return final
