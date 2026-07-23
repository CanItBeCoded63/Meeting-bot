import asyncio
import hmac
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import yaml
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from container_manager import AzureContainerManager

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUBSCRIPTION_ID = os.environ["AZURE_SUBSCRIPTION_ID"]
RESOURCE_GROUP = os.environ.get("AZURE_RESOURCE_GROUP", "joinly-rg")
LOCATION = os.environ.get("AZURE_LOCATION", "eastus")
DEFAULT_LLM_PROVIDER = os.environ.get("JOINLY_LLM_PROVIDER", "anthropic")
DEFAULT_LLM_MODEL = os.environ.get("JOINLY_LLM_MODEL", "claude-sonnet-4-6")
DEFAULT_AGENT_NAME = os.environ.get("JOINLY_NAME", "Alex")
MANAGER_API_KEY = os.environ["MANAGER_API_KEY"]
AGENTS_CONFIG_PATH = os.environ.get("AGENTS_CONFIG_PATH", "/app/agents.yaml")

_ALLOWED_PROVIDERS = {"anthropic", "openai", "google", "azure", "ollama"}
_NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,64}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9.\-:]{1,128}$")

_agents_config: dict[str, Any] | None = None


def _load_agents_config() -> dict[str, Any] | None:
    path = Path(AGENTS_CONFIG_PATH)
    if not path.exists():
        logger.warning("agents.yaml not found at %s — multi-agent mode unavailable", path)
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.error("agents.yaml must be a YAML mapping")
            return None
        logger.info(
            "Loaded %d agent profiles from %s", len(data.get("agents", [])), path
        )
        return data
    except Exception as exc:
        logger.error("Failed to load agents.yaml: %s", exc)
        return None


def _resolve_agent_profiles(
    agent_ids: list[str],
    api_key: str | None = None,
) -> list[dict]:
    """Map agent IDs to ACI deploy configs via agents.yaml."""
    if _agents_config is None:
        raise HTTPException(
            status_code=503,
            detail="agents.yaml not loaded — multi-agent mode unavailable",
        )

    profiles_by_id: dict[str, dict] = {
        a["id"]: a for a in _agents_config.get("agents", [])
    }
    primary_id: str = _agents_config.get("primary_agent", "")

    resolved = []
    for agent_id in agent_ids:
        raw = profiles_by_id.get(agent_id)
        if not raw:
            raise HTTPException(
                status_code=400, detail=f"Agent {agent_id!r} not found in agents.yaml"
            )
        if raw.get("disabled"):
            raise HTTPException(status_code=400, detail=f"Agent {agent_id!r} is disabled")

        llm_str = raw.get("llm", f"{DEFAULT_LLM_PROVIDER}:{DEFAULT_LLM_MODEL}")
        if ":" in llm_str:
            llm_provider, llm_model = llm_str.split(":", 1)
        else:
            llm_provider, llm_model = DEFAULT_LLM_PROVIDER, llm_str

        settings = raw.get("joinly_settings") or {}
        tts = settings.get("tts", "deepgram")
        tts_voice = settings.get("tts_voice") or (settings.get("tts_args") or {}).get(
            "model_name"
        )
        tts_args = {"model_name": tts_voice} if tts_voice else None

        resolved.append(
            {
                "id": agent_id,
                "name": raw["name"],
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "api_key": api_key,
                "persona": raw.get("persona"),
                "tts": tts,
                "tts_args": tts_args,
                # Primary agent has no name-trigger so it responds by default
                "name_trigger": agent_id != primary_id,
            }
        )
    return resolved


def _require_auth(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    if not hmac.compare_digest(x_api_key, MANAGER_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


AuthDep = Annotated[None, Depends(_require_auth)]


def get_manager() -> AzureContainerManager:
    return AzureContainerManager(
        subscription_id=SUBSCRIPTION_ID,
        resource_group=RESOURCE_GROUP,
        location=LOCATION,
    )


CLEANUP_INTERVAL_SECS = int(os.environ.get("CLEANUP_INTERVAL_SECS", "300"))
# ACI container states that mean the process exited and the group can be deleted
_TERMINAL_STATES = {"Terminated", "Stopped", "Failed"}
# Max wall-clock lifetime: delete Running containers older than this (agent hung/stuck)
MAX_CONTAINER_AGE_SECS = int(os.environ.get("MAX_CONTAINER_AGE_SECS", str(4 * 3600)))


async def _cleanup_stopped_containers() -> None:
    """Background task: delete ACI container groups whose process has exited OR exceeded max age."""
    import datetime as dt  # noqa: PLC0415

    manager = get_manager()
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECS)
        try:
            from azure.mgmt.containerinstance import ContainerInstanceManagementClient  # noqa: PLC0415

            client: ContainerInstanceManagementClient = manager._client  # noqa: SLF001
            # list_by_resource_group does NOT populate instance_view — collect names then GET each
            all_names = [
                g.name
                for g in client.container_groups.list_by_resource_group(RESOURCE_GROUP)
                if g.name and g.name.startswith("joinly-meeting-")
            ]
            now = dt.datetime.now(tz=dt.timezone.utc)
            to_delete: list[tuple[str, str]] = []  # (name, reason)
            for name in all_names:
                g = client.container_groups.get(RESOURCE_GROUP, name)
                container = g.containers[0] if g.containers else None
                iv = container.instance_view if container else None
                state = iv.current_state.state if iv and iv.current_state else None
                start_time = iv.current_state.start_time if iv and iv.current_state else None

                if state in _TERMINAL_STATES:
                    to_delete.append((name, f"state={state}"))
                elif state == "Running" and start_time:
                    age = (now - start_time).total_seconds()
                    if age > MAX_CONTAINER_AGE_SECS:
                        to_delete.append((name, f"exceeded max age {age/3600:.1f}h"))

            for name, reason in to_delete:
                try:
                    await asyncio.to_thread(
                        client.container_groups.begin_delete(
                            RESOURCE_GROUP, name
                        ).result
                    )
                    logger.info("Auto-cleanup: deleted container %s (%s)", name, reason)
                except Exception as exc:
                    logger.warning("Auto-cleanup: failed to delete %s: %s", name, exc)
        except Exception as exc:
            logger.warning("Auto-cleanup error (will retry): %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _agents_config  # noqa: PLW0603
    _agents_config = _load_agents_config()
    logger.info(
        "Meeting Manager starting — subscription=%s rg=%s",
        SUBSCRIPTION_ID,
        RESOURCE_GROUP,
    )
    cleanup_task = asyncio.create_task(_cleanup_stopped_containers())
    yield
    cleanup_task.cancel()
    logger.info("Meeting Manager shutting down")


app = FastAPI(title="Joinly Meeting Manager", version="1.1.0", lifespan=lifespan)


class JoinRequest(BaseModel):
    url: HttpUrl
    name: str = Field(default=DEFAULT_AGENT_NAME, min_length=1, max_length=64)
    # Multi-agent: list of agent IDs from agents.yaml
    agents: list[str] = Field(default_factory=list)
    # Optional overrides — defaults come from manager env vars
    llm_provider: str = DEFAULT_LLM_PROVIDER
    llm_model: str = DEFAULT_LLM_MODEL
    # api_key optional: if omitted, manager forwards its own LLM key to ACI
    api_key: str | None = Field(default=None, min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("name contains invalid characters")
        return v

    @field_validator("llm_provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        if v.lower() not in _ALLOWED_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {sorted(_ALLOWED_PROVIDERS)}")
        return v.lower()

    @field_validator("llm_model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        if not _MODEL_RE.match(v):
            raise ValueError("llm_model contains invalid characters")
        return v


class AgentContainerInfo(BaseModel):
    agent_id: str
    container_group: str


class JoinResponse(BaseModel):
    meeting_id: str
    status: str
    agent_containers: list[AgentContainerInfo] = Field(default_factory=list)
    container_group: str = ""  # legacy compat: first container group


class AgentStatus(BaseModel):
    agent_id: str
    container_group: str
    state: str
    provisioning_state: str | None = None
    ip: str | None = None


class MeetingStatus(BaseModel):
    meeting_id: str
    agents: list[AgentStatus] = Field(default_factory=list)
    state: str  # aggregate: Running if any agent Running
    error: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/meetings/join", response_model=JoinResponse, status_code=202)
async def join_meeting(
    req: JoinRequest,
    _auth: AuthDep,
    manager: Annotated[AzureContainerManager, Depends(get_manager)],
):
    meeting_id = uuid.uuid4().hex[:12]

    if req.agents:
        try:
            agent_configs = _resolve_agent_profiles(req.agents, api_key=req.api_key)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            group_names = await manager.create_meeting_containers(
                meeting_id=meeting_id,
                meeting_url=str(req.url),
                agents=agent_configs,
            )
        except Exception as exc:
            logger.exception(
                "Failed to create multi-agent containers for meeting %s", meeting_id
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        agent_containers = [
            AgentContainerInfo(
                agent_id=agent_configs[i]["id"],
                container_group=group_names[i],
            )
            for i in range(len(group_names))
        ]
        return JoinResponse(
            meeting_id=meeting_id,
            status="starting",
            agent_containers=agent_containers,
            container_group=group_names[0] if group_names else "",
        )

    # Legacy single-agent path
    try:
        container_group = await manager.create_meeting_container(
            meeting_id=meeting_id,
            meeting_url=str(req.url),
            name=req.name,
            llm_provider=req.llm_provider,
            llm_model=req.llm_model,
            api_key=req.api_key,  # type: ignore[arg-type]
        )
    except Exception as exc:
        logger.exception("Failed to create container for meeting %s", meeting_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JoinResponse(
        meeting_id=meeting_id,
        status="starting",
        container_group=container_group,
        agent_containers=[
            AgentContainerInfo(agent_id="default", container_group=container_group)
        ],
    )


@app.get("/meetings/{meeting_id}", response_model=MeetingStatus)
async def get_meeting(
    meeting_id: str,
    _auth: AuthDep,
    manager: Annotated[AzureContainerManager, Depends(get_manager)],
):
    statuses = await manager.get_meeting_containers_status(meeting_id)
    if not statuses:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")
    agents = [AgentStatus(**s) for s in statuses]
    agg_state = (
        "Running" if any(a.state == "Running" for a in agents) else agents[0].state
    )
    return MeetingStatus(meeting_id=meeting_id, agents=agents, state=agg_state)


@app.delete("/meetings/{meeting_id}", status_code=204)
async def leave_meeting(
    meeting_id: str,
    _auth: AuthDep,
    manager: Annotated[AzureContainerManager, Depends(get_manager)],
):
    statuses = await manager.get_meeting_containers_status(meeting_id)
    if not statuses:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")
    try:
        await manager.delete_meeting_containers(meeting_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/meetings")
async def list_meetings(
    _auth: AuthDep,
    manager: Annotated[AzureContainerManager, Depends(get_manager)],
):
    meetings = await manager.list_active_meetings()
    return {"meetings": meetings, "count": len(meetings)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
