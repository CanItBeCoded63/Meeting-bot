from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self

from fastmcp import Client

from joinly_client.agent import ConversationalToolAgent
from joinly_client.client import JoinlyClient
from joinly_client.memory import MemoryStore, build_memory_block
from joinly_client.router import ConversationState
from joinly_client.types import McpClientConfig, TranscriptSegment
from joinly_client.utils import get_llm, get_prompt, load_tools

if TYPE_CHECKING:
    from joinly_client.conflict_resolver import ConflictResolver
    from joinly_client.profiles import AgentProfile
    from joinly_client.registry import AgentRegistry
    from joinly_client.router import AgentRouter

logger = logging.getLogger(__name__)


class MultiAgentSession:
    """Orchestrates N ConversationalToolAgents across N joinly server instances."""

    def __init__(  # noqa: PLR0913
        self,
        meeting_url: str,
        registry: AgentRegistry,
        router: AgentRouter,
        conflict_resolver: ConflictResolver,
        *,
        memory_store: MemoryStore | None = None,
        project_id: str | None = None,
        client_id: str | None = None,
        passcode: str | None = None,
    ) -> None:
        """Initialize with meeting URL, registry, router, resolver, and memory."""
        self._meeting_url = meeting_url
        self._registry = registry
        self._router = router
        self._conflict_resolver = conflict_resolver
        self._memory_store = memory_store
        self._project_id = project_id
        self._client_id = client_id
        self._passcode = passcode
        self._meeting_id = self._derive_meeting_id()

        self._clients: dict[str, JoinlyClient] = {}
        self._agents: dict[str, ConversationalToolAgent] = {}
        self._state = ConversationState()
        self._stack = AsyncExitStack()

    def _derive_meeting_id(self) -> str:
        """Derive a stable meeting identifier from URL + timestamp."""
        import hashlib

        date_str = datetime.now(UTC).strftime("%Y%m%d")
        return hashlib.sha256(f"{self._meeting_url}:{date_str}".encode()).hexdigest()[
            :16
        ]

    async def __aenter__(self) -> Self:
        """Enter the session context, initializing all agents."""
        await self._stack.__aenter__()
        profiles = self._registry.list_profiles()

        # Init all agents in parallel, with per-agent timeout and graceful degradation
        init_results = await asyncio.gather(
            *[
                asyncio.wait_for(
                    self._init_agent(p),
                    timeout=p.timeout_secs,
                )
                for p in profiles
            ],
            return_exceptions=True,
        )

        for profile, result in zip(profiles, init_results, strict=False):
            if isinstance(result, Exception):
                logger.exception(
                    "Agent %r failed to initialize — skipping: %s",
                    profile.id,
                    result,
                )
            else:
                logger.info("Agent %r initialized successfully", profile.id)

        if not self._agents:
            await self._stack.aclose()
            msg = (
                "No agents initialized successfully — cannot start multi-agent session"
            )
            raise RuntimeError(msg)

        primary_id = self._registry.primary_id
        if primary_id not in self._clients:
            await self._stack.aclose()
            msg = f"Primary agent {primary_id!r} failed to initialize"
            raise RuntimeError(msg)

        # Register single utterance callback on primary client only
        self._clients[primary_id].add_utterance_callback(self._on_utterance)
        logger.info(
            "MultiAgentSession ready: %d agents initialized, primary=%r",
            len(self._agents),
            primary_id,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the session context, cleaning up all agents and clients."""
        # AsyncExitStack handles cleanup of all clients and agents
        # that were entered via enter_async_context — no manual cleanup needed
        await self._stack.aclose()

    async def _init_agent(self, profile: AgentProfile) -> None:  # noqa: C901
        """Initialize one agent: connect, join meeting, load tools, build prompt."""
        # Build LLM
        llm = get_llm(profile.llm.provider, profile.llm.model)

        # Build JoinlyClient for this agent's server
        settings = profile.joinly_settings.to_settings_dict(profile.name)
        client = JoinlyClient(
            profile.joinly_url,
            name=profile.name,
            name_trigger=False,  # routing handled centrally by MultiAgentSession
            settings=settings,
        )
        await self._stack.enter_async_context(client)
        self._clients[profile.id] = client

        # Join meeting as this agent
        await client.join_meeting(self._meeting_url, passcode=self._passcode)

        # Load tools: this agent's joinly server + external MCPs
        joinly_cfg = McpClientConfig(client=client.client, exclude=["join_meeting"])

        if profile.external_mcps:
            external_clients: dict[str, McpClientConfig] = {}
            for mcp_cfg in profile.external_mcps:
                mcp_server_cfg: dict[str, Any] = {"url": mcp_cfg.url}
                if mcp_cfg.headers:
                    mcp_server_cfg["headers"] = mcp_cfg.headers
                ext_client = Client({"mcpServers": {mcp_cfg.name: mcp_server_cfg}})
                await self._stack.enter_async_context(ext_client)
                external_clients[mcp_cfg.name] = McpClientConfig(client=ext_client)

            all_clients: McpClientConfig | dict[str, McpClientConfig] = {
                "joinly": joinly_cfg,
                **external_clients,
            }
        else:
            all_clients = joinly_cfg

        tools, tool_executor = await load_tools(all_clients)

        # Add remember() tool if memory is configured
        if self._memory_store and profile.memory_config:
            from joinly_client.memory import (
                REMEMBER_TOOL_DEFINITION,
                write_memory,
            )

            tools = [*tools, REMEMBER_TOOL_DEFINITION]
            original_executor = tool_executor

            async def tool_executor_with_memory(
                tool_name: str,
                args: dict[str, Any],
                _profile: AgentProfile = profile,
            ) -> Any:  # noqa: ANN401
                if tool_name == "remember":
                    return await write_memory(
                        self._memory_store,  # type: ignore[arg-type]
                        _profile.id,
                        self._project_id,
                        self._client_id,
                        self._meeting_id,
                        # participant roster — TODO: wire from client.get_participants()
                        set(),
                        content=args["content"],
                        scope=args["scope"],
                        memory_type=args["memory_type"],
                        participant_id=args.get("participant_id"),
                    )
                return await original_executor(tool_name, args)

            tool_executor = tool_executor_with_memory

        # Build memory block for prompt
        memory_block = ""
        if self._memory_store and profile.memory_config:
            try:
                memory_block = await build_memory_block(
                    self._memory_store,
                    profile.id,
                    project_id=self._project_id,
                    client_id=self._client_id,
                    meeting_id=self._meeting_id,
                )
            except Exception:
                logger.exception("Failed to load memory for agent %r", profile.id)

        # Build system prompt
        meeting_start = datetime.now(tz=UTC).strftime("%H:%M UTC")
        if profile.instructions_mode == "custom" and profile.custom_instructions:
            instructions = profile.custom_instructions
            if memory_block:
                instructions = f"{instructions}\n{memory_block}"
        else:
            instructions = memory_block or None

        is_custom = profile.instructions_mode == "custom"
        prompt = get_prompt(
            instructions=instructions,
            prompt_style=None if is_custom else profile.instructions_mode,
            name=profile.name,
            meeting_start=meeting_start,
        )

        # Create and enter agent context
        agent = ConversationalToolAgent(
            llm,
            tools,
            tool_executor,
            prompt=prompt,
            on_status=client.on_agent_status,
            max_messages=profile.max_messages,
            max_agent_iter=profile.max_agent_iter,
        )
        await self._stack.enter_async_context(agent)
        self._agents[profile.id] = agent

    async def _on_utterance(self, segments: list[TranscriptSegment]) -> None:
        profiles = self._registry.list_profiles()

        # Fast path: single agent, skip routing overhead
        if len(self._agents) == 1:
            agent = next(iter(self._agents.values()))
            await agent.on_utterance(segments)
            return

        agent_ids, new_state = self._router.route(
            segments,
            profiles,
            self._state,
            self._registry.default_id,
        )
        self._state = new_state

        final_ids = self._conflict_resolver.resolve(
            agent_ids,
            self._registry.profiles_by_id(),
        )

        for aid in final_ids:
            agent = self._agents.get(aid)
            if agent is not None:
                await agent.on_utterance(segments)
            else:
                logger.debug(
                    "Agent %r in final_ids but not initialized — skipping", aid
                )

    @property
    def agents(self) -> dict[str, ConversationalToolAgent]:
        """Initialized agents by agent_id."""
        return dict(self._agents)

    @property
    def clients(self) -> dict[str, JoinlyClient]:
        """JoinlyClients by agent_id."""
        return dict(self._clients)
