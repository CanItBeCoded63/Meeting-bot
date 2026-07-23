from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LLMConfig(BaseModel):
    """LLM provider and model configuration."""

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str

    @classmethod
    def from_string(cls, s: str) -> LLMConfig:
        """Parse 'provider:model' string."""
        if ":" not in s:
            msg = f"llm must be 'provider:model', got: {s!r}"
            raise ValueError(msg)
        provider, model = s.split(":", 1)
        return cls(provider=provider.strip(), model=model.strip())


class JoinlySettingsOverrides(BaseModel):
    """Per-agent overrides sent to joinly server via HTTP header."""

    model_config = ConfigDict(frozen=True)

    tts: str | None = None
    stt: str | None = None
    vad: str | None = None
    tts_voice: str | None = None
    language: str | None = None

    def to_settings_dict(self, name: str) -> dict[str, str]:
        """Build settings dict to pass to JoinlyClient, including agent name."""
        return {"name": name, **self.model_dump(exclude_none=True)}


class ExternalMcpConfig(BaseModel):
    """Configuration for an external MCP server."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(
        description=(
            "Tool name prefix, e.g. 'analytics' → tools become 'analytics_tool_name'"
        )
    )
    url: str
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def name_no_spaces(cls, v: str) -> str:
        """Validate that name is alphanumeric with underscores/hyphens."""
        if " " in v or not v.replace("_", "").replace("-", "").isalnum():
            msg = (
                "ExternalMcpConfig.name must be alphanumeric with underscores/hyphens,"
                f" got: {v!r}"
            )
            raise ValueError(msg)
        return v


class MemoryConfig(BaseModel):
    """Memory backend configuration for an agent."""

    model_config = ConfigDict(frozen=True)

    backend: Literal["azure_table"] = "azure_table"
    connection_string_env: str = Field(
        default="AZURE_STORAGE_CONNECTION_STRING",
        description=(
            "Environment variable name containing the Azure Storage connection string"
        ),
    )
    ttl_secs: float = Field(
        default=300.0, description="Cache TTL in seconds (default 5 min)"
    )
    cache_maxsize: int = Field(default=256, description="Max LRU cache entries")


class AgentProfile(BaseModel):
    """Complete configuration for a single agent persona."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: str = Field(description="Unique agent identifier, e.g. 'sam'")
    name: str = Field(description="Display name in meeting, e.g. 'Sam'")
    joinly_url: str = Field(description="URL of this agent's joinly server instance")
    joinly_settings: JoinlySettingsOverrides = Field(
        default_factory=JoinlySettingsOverrides,
        description="TTS/STT/VAD overrides for this agent's server",
    )
    trigger_names: list[str] = Field(
        min_length=1,
        description="Names that activate this agent when mentioned in transcript",
    )
    llm: LLMConfig = Field(description="LLM provider and model")
    persona: str = Field(description="Identity paragraph injected into system prompt")
    priority: int = Field(
        default=0,
        description="Conflict resolution priority; lower number = higher priority",
    )
    exclusive: bool = Field(
        default=False,
        description="If triggered, suppress all other agents from responding",
    )
    instructions_mode: Literal["dyadic", "mpc", "custom"] = Field(
        default="mpc",
        description=(
            "Prompt style: 'dyadic' for 1:1, 'mpc' for group,"
            " 'custom' to use custom_instructions"
        ),
    )
    custom_instructions: str | None = Field(
        default=None,
        description="Custom instructions block; used when instructions_mode='custom'",
    )
    external_mcps: list[ExternalMcpConfig] = Field(
        default_factory=list,
        description="External MCP servers this agent can access",
    )
    memory_config: MemoryConfig | None = Field(
        default=None,
        description="Memory persistence config; None disables memory for this agent",
    )
    max_messages: int = Field(default=500, description="Max messages in agent history")
    max_agent_iter: int = Field(default=15, description="Max LLM iterations per turn")
    timeout_secs: float = Field(
        default=30.0,
        description="Timeout for agent initialization (join meeting + load tools)",
    )
    disabled: bool = Field(
        default=False,
        description="Disable this agent without removing from config",
    )

    @field_validator("trigger_names")
    @classmethod
    def trigger_names_not_empty(cls, v: list[str]) -> list[str]:
        """Validate that trigger_names has no empty strings."""
        if any(not t.strip() for t in v):
            msg = "trigger_names must not contain empty strings"
            raise ValueError(msg)
        return [t.strip() for t in v]

    @field_validator("joinly_url")
    @classmethod
    def joinly_url_not_empty(cls, v: str) -> str:
        """Validate that joinly_url is not empty."""
        if not v.strip():
            msg = "joinly_url must not be empty"
            raise ValueError(msg)
        return v
