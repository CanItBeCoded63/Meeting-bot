from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from joinly_client.profiles import (
    AgentProfile,
    ExternalMcpConfig,
    JoinlySettingsOverrides,
    LLMConfig,
    MemoryConfig,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_AGENT_PROFILE_FIELDS = {
    "id",
    "name",
    "joinly_url",
    "joinly_settings",
    "trigger_names",
    "llm",
    "persona",
    "priority",
    "exclusive",
    "instructions_mode",
    "custom_instructions",
    "external_mcps",
    "memory_config",
    "max_messages",
    "max_agent_iter",
    "timeout_secs",
    "disabled",
}


class RegistryConfigError(ValueError):
    """Raised for invalid agent registry configuration."""


def _parse_profile(raw: dict[str, Any]) -> AgentProfile:
    """Parse a raw YAML agent dict into AgentProfile.

    Raises RegistryConfigError on bad input.
    """
    # Allow "memory" as YAML alias for memory_config field
    unknown = set(raw.keys()) - _AGENT_PROFILE_FIELDS - {"memory"}
    if unknown:
        agent_id = raw.get("id", "?")
        msg = f"Unknown agent config keys for agent {agent_id!r}: {sorted(unknown)}"
        raise RegistryConfigError(msg)

    # Parse llm: either LLMConfig dict or "provider:model" string
    llm_raw = raw.get("llm")
    if isinstance(llm_raw, str):
        try:
            llm = LLMConfig.from_string(llm_raw)
        except ValueError as exc:
            raise RegistryConfigError(str(exc)) from exc
    elif isinstance(llm_raw, dict):
        llm = LLMConfig(**llm_raw)
    else:
        agent_id = raw.get("id", "?")
        msg = f"Agent {agent_id!r}: 'llm' is required"
        raise RegistryConfigError(msg)

    # Parse joinly_settings
    settings_raw = raw.get("joinly_settings", {})
    joinly_settings = JoinlySettingsOverrides(**(settings_raw or {}))

    # Parse external_mcps
    external_mcps = [ExternalMcpConfig(**m) for m in raw.get("external_mcps", [])]

    # Parse memory_config (key "memory" in YAML → memory_config field)
    memory_raw = raw.get("memory") or raw.get("memory_config")
    memory_config = MemoryConfig(**(memory_raw or {})) if memory_raw else None

    try:
        return AgentProfile(
            id=raw["id"],
            name=raw["name"],
            joinly_url=raw["joinly_url"],
            joinly_settings=joinly_settings,
            trigger_names=raw.get("trigger_names", []),
            llm=llm,
            persona=raw.get("persona", ""),
            priority=raw.get("priority", 0),
            exclusive=raw.get("exclusive", False),
            instructions_mode=raw.get("instructions_mode", "mpc"),
            custom_instructions=raw.get("custom_instructions"),
            external_mcps=external_mcps,
            memory_config=memory_config,
            max_messages=raw.get("max_messages", 500),
            max_agent_iter=raw.get("max_agent_iter", 15),
            timeout_secs=raw.get("timeout_secs", 30.0),
            disabled=raw.get("disabled", False),
        )
    except (ValidationError, TypeError) as exc:
        agent_id = raw.get("id", "?")
        msg = f"Invalid config for agent {agent_id!r}: {exc}"
        raise RegistryConfigError(msg) from exc


class AgentRegistry:
    """Loads and validates agent profiles from YAML config."""

    def __init__(
        self,
        profiles: list[AgentProfile],
        primary_agent_id: str,
        default_agent_id: str | None,
    ) -> None:
        """Initialize the registry with pre-parsed profiles.

        Args:
            profiles: Parsed AgentProfile instances.
            primary_agent_id: ID of the transcript-authority agent.
            default_agent_id: Optional ID of the fallback responder.
        """
        self._profiles: dict[str, AgentProfile] = {p.id: p for p in profiles}
        self._primary_id = primary_agent_id
        self._default_id = default_agent_id
        self._validate()

    @classmethod
    def from_yaml(cls, path: Path) -> AgentRegistry:
        """Load an AgentRegistry from a YAML config file."""
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            msg = "agents.yaml must be a YAML mapping at the root"
            raise RegistryConfigError(msg)

        primary_id = data.get("primary_agent")
        if not primary_id:
            msg = "agents.yaml must specify 'primary_agent'"
            raise RegistryConfigError(msg)

        default_id = data.get("default_agent")

        agents_raw = data.get("agents", [])
        if not agents_raw:
            msg = "agents.yaml must have at least one agent under 'agents'"
            raise RegistryConfigError(msg)

        profiles = [_parse_profile(raw) for raw in agents_raw]
        return cls(profiles, primary_id, default_id)

    def _validate(self) -> None:
        if self._primary_id not in self._profiles:
            msg = (
                f"primary_agent {self._primary_id!r} not found in agents list. "
                f"Available: {sorted(self._profiles)}"
            )
            raise RegistryConfigError(msg)
        if self._default_id and self._default_id not in self._profiles:
            msg = f"default_agent {self._default_id!r} not found in agents list."
            raise RegistryConfigError(msg)

        # Warn on duplicate trigger names across agents
        seen_triggers: dict[str, str] = {}
        for profile in self._profiles.values():
            for t in profile.trigger_names:
                normalized = t.lower().strip()
                if normalized in seen_triggers:
                    logger.warning(
                        "Trigger name %r used by both %r and %r",
                        t,
                        seen_triggers[normalized],
                        profile.id,
                    )
                else:
                    seen_triggers[normalized] = profile.id

        if self._default_id is None:
            logger.warning(
                "No default_agent configured — utterances with no name trigger"
                " will be silently dropped"
            )

    @property
    def primary_id(self) -> str:
        """Return the ID of the primary agent."""
        return self._primary_id

    @property
    def default_id(self) -> str | None:
        """Return the ID of the default fallback agent, or None."""
        return self._default_id

    def primary(self) -> AgentProfile:
        """Return the primary agent profile.

        Raises RegistryConfigError if not found.
        """
        profile = self._profiles.get(self._primary_id)
        if profile is None:
            msg = f"Primary agent {self._primary_id!r} not in registry"
            raise RegistryConfigError(msg)
        return profile

    def default(self) -> AgentProfile | None:
        """Return the default fallback agent profile, or None if not configured."""
        if self._default_id is None:
            return None
        return self._profiles.get(self._default_id)

    def get(self, agent_id: str) -> AgentProfile | None:
        """Return the profile for the given agent ID, or None."""
        return self._profiles.get(agent_id)

    def list_profiles(self) -> list[AgentProfile]:
        """Return all enabled profiles in insertion order."""
        return [p for p in self._profiles.values() if not p.disabled]

    def profiles_by_id(self) -> dict[str, AgentProfile]:
        """Return a mapping of agent_id → AgentProfile for all enabled agents."""
        return {k: v for k, v in self._profiles.items() if not v.disabled}
