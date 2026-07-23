import asyncio
import json
import logging
import os

from azure.identity import DefaultAzureCredential
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerinstance.models import (
    AzureFileVolume,
    Container,
    ContainerGroup,
    ContainerGroupRestartPolicy,
    EnvironmentVariable,
    ImageRegistryCredential,
    IpAddress,
    OperatingSystemTypes,
    Port,
    ResourceRequests,
    ResourceRequirements,
    Volume,
    VolumeMount,
)

logger = logging.getLogger(__name__)

JOINLY_IMAGE = os.environ.get("JOINLY_IMAGE", "joinlyreg.azurecr.io/joinly:latest")
# 1.0 CPU / 2GB was too tight: the audio pacing loop runs continuously from
# container start (see virtual_microphone.py), and under contention on a
# single vCPU it starves the event loop enough that Playwright's join-button
# click hangs past its timeout (observed repeatedly in prod — "Missed N mic
# pacing intervals" immediately preceding a join failure).
CONTAINER_CPU = 2.0
CONTAINER_MEMORY_GB = 4.0
CONTAINER_GROUP_PREFIX = "joinly-meeting-"

# Env vars the Meeting Manager forwards from its own environment into each ACI container.
_FORWARD_ENV_VARS = [
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "OPENAI_API_VERSION",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "JOINLY_STT",
    "JOINLY_TTS",
    "DEEPGRAM_API_KEY",
    "AZURE_STORAGE_ACCOUNT_NAME",
    "AZURE_STORAGE_ACCOUNT_KEY",
    "AZURE_FILE_SHARE_NAME",
]

_BROWSER_PROFILE_MOUNT_PATH = "/browser-profile"
_BROWSER_PROFILE_VOLUME_NAME = "browser-profile"


class AzureContainerManager:
    def __init__(self, subscription_id: str, resource_group: str, location: str) -> None:
        self._subscription_id = subscription_id
        self._resource_group = resource_group
        self._location = location
        self._credential = DefaultAzureCredential()
        self._client = ContainerInstanceManagementClient(
            self._credential, self._subscription_id
        )

    def _group_name(self, meeting_id: str, agent_id: str = "default") -> str:
        return f"{CONTAINER_GROUP_PREFIX}{meeting_id}-{agent_id}"

    def _registry_credentials(self) -> list[ImageRegistryCredential]:
        acr_server = os.environ.get("ACR_LOGIN_SERVER")
        acr_user = os.environ.get("ACR_USERNAME")
        acr_pass = os.environ.get("ACR_PASSWORD")
        if acr_server and acr_user and acr_pass:
            return [
                ImageRegistryCredential(
                    server=acr_server, username=acr_user, password=acr_pass
                )
            ]
        return []

    async def create_agent_container(
        self,
        meeting_id: str,
        agent_id: str,
        meeting_url: str,
        *,
        name: str,
        llm_provider: str,
        llm_model: str,
        api_key: str | None = None,
        persona: str | None = None,
        tts: str = "deepgram",
        tts_args: dict | None = None,
        name_trigger: bool = True,
    ) -> str:
        """Create one ACI container for one agent. Returns the container group name."""
        env_var_name = self._api_key_env_var(llm_provider)

        env_map: dict[str, str] = {
            "JOINLY_LLM_PROVIDER": llm_provider,
            "JOINLY_LLM_MODEL": llm_model,
            "JOINLY_NAME": name,
            "JOINLY_TTS": tts,
        }
        if tts_args:
            env_map["JOINLY_TTS_ARGS"] = json.dumps(tts_args)
        if persona:
            env_map["JOINLY_PROMPT"] = persona

        for k in _FORWARD_ENV_VARS:
            if k not in env_map and os.environ.get(k):
                env_map[k] = os.environ[k]

        env_vars = [EnvironmentVariable(name=k, value=v) for k, v in env_map.items()]
        if api_key:
            env_vars.append(EnvironmentVariable(name=env_var_name, secure_value=api_key))

        volumes, volume_mounts = self._file_share_volume()

        if volumes:
            # copy-profile.sh copies the File Share mount to local disk (SMB lacks POSIX locks)
            # then execs joinly with the remaining args
            command = ["/bin/bash", "/app/scripts/copy-profile.sh", "--client", meeting_url]
            if name_trigger:
                command.append("--name-trigger")
            command += ["--meeting-provider-arg", "browser_profile_dir=/tmp/browser-profile"]
        else:
            command = ["/app/.venv/bin/joinly", "--client", meeting_url]
            if name_trigger:
                command.append("--name-trigger")

        container = Container(
            name=f"joinly-{meeting_id}-{agent_id}",
            image=JOINLY_IMAGE,
            command=command,
            environment_variables=env_vars,
            resources=ResourceRequirements(
                requests=ResourceRequests(
                    cpu=CONTAINER_CPU, memory_in_gb=CONTAINER_MEMORY_GB
                )
            ),
            ports=[Port(port=8000)],
            volume_mounts=volume_mounts if volume_mounts else None,
        )

        registry_creds = self._registry_credentials()
        group = ContainerGroup(
            location=self._location,
            containers=[container],
            os_type=OperatingSystemTypes.LINUX,
            restart_policy=ContainerGroupRestartPolicy.NEVER,
            ip_address=IpAddress(ports=[Port(port=8000)], type="Public"),
            image_registry_credentials=registry_creds if registry_creds else None,
            volumes=volumes if volumes else None,
        )

        group_name = self._group_name(meeting_id, agent_id)
        logger.info("Creating container group %s (agent=%s)", group_name, agent_id)

        await asyncio.to_thread(
            self._client.container_groups.begin_create_or_update,
            self._resource_group,
            group_name,
            group,
        )
        return group_name

    async def create_meeting_containers(
        self,
        meeting_id: str,
        meeting_url: str,
        agents: list[dict],
    ) -> list[str]:
        """Create N agent containers in parallel. Returns list of created group names."""
        tasks = [
            self.create_agent_container(
                meeting_id=meeting_id,
                agent_id=a["id"],
                meeting_url=meeting_url,
                name=a["name"],
                llm_provider=a["llm_provider"],
                llm_model=a["llm_model"],
                api_key=a.get("api_key"),
                persona=a.get("persona"),
                tts=a.get("tts", "deepgram"),
                tts_args=a.get("tts_args"),
                name_trigger=a.get("name_trigger", True),
            )
            for a in agents
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        group_names = []
        for agent, result in zip(agents, results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "Failed to create container for agent %s: %s", agent["id"], result
                )
            else:
                group_names.append(result)
        return group_names

    async def delete_meeting_containers(self, meeting_id: str) -> None:
        """Delete ALL agent containers for a meeting by prefix."""
        prefix = self._group_name(meeting_id, "")
        groups = await asyncio.to_thread(
            lambda: list(
                self._client.container_groups.list_by_resource_group(self._resource_group)
            )
        )
        targets = [g.name for g in groups if g.name and g.name.startswith(prefix)]
        if not targets:
            logger.warning("No containers found for meeting %s", meeting_id)
            return

        delete_tasks = [
            asyncio.to_thread(
                self._client.container_groups.begin_delete(
                    self._resource_group, group_name
                ).result
            )
            for group_name in targets
        ]
        results = await asyncio.gather(*delete_tasks, return_exceptions=True)
        for group_name, result in zip(targets, results, strict=True):
            if isinstance(result, Exception):
                logger.error("Failed to delete %s: %s", group_name, result)
            else:
                logger.info("Deleted %s", group_name)

    def _get_state(self, group: object) -> str:
        """Extract container state from a fully-fetched ContainerGroup (not list result)."""
        containers = getattr(group, "containers", None) or []
        container = containers[0] if containers else None
        iv = getattr(container, "instance_view", None) if container else None
        cs = getattr(iv, "current_state", None) if iv else None
        return getattr(cs, "state", None) or "Unknown"

    async def get_meeting_containers_status(self, meeting_id: str) -> list[dict]:
        """Return per-agent status for all containers in a meeting."""
        prefix = self._group_name(meeting_id, "")
        # list_by_resource_group does NOT populate instance_view — must GET each group
        all_names = await asyncio.to_thread(
            lambda: [
                g.name
                for g in self._client.container_groups.list_by_resource_group(self._resource_group)
                if g.name and g.name.startswith(prefix)
            ]
        )
        statuses = []
        for name in all_names:
            group = await asyncio.to_thread(
                self._client.container_groups.get, self._resource_group, name
            )
            agent_id = name[len(prefix):]
            state = self._get_state(group)
            statuses.append(
                {
                    "agent_id": agent_id,
                    "container_group": name,
                    "state": state,
                    "provisioning_state": group.provisioning_state,
                    "ip": group.ip_address.ip if group.ip_address else None,
                }
            )
        return statuses

    # ── Legacy single-agent methods (backward compat) ─────────────────────────

    async def create_meeting_container(
        self,
        meeting_id: str,
        meeting_url: str,
        name: str,
        llm_provider: str,
        llm_model: str,
        api_key: str,
    ) -> str:
        """Legacy: create single-agent container (agent_id='default')."""
        return await self.create_agent_container(
            meeting_id=meeting_id,
            agent_id="default",
            meeting_url=meeting_url,
            name=name,
            llm_provider=llm_provider,
            llm_model=llm_model,
            api_key=api_key,
            name_trigger=False,
        )

    async def get_container_status(self, meeting_id: str) -> dict:
        """Legacy: status for single-agent meeting (agent_id='default')."""
        group_name = self._group_name(meeting_id, "default")
        try:
            group = await asyncio.to_thread(
                self._client.container_groups.get,
                self._resource_group,
                group_name,
            )
            container = group.containers[0]
            instance_view = container.instance_view
            state = instance_view.current_state.state if instance_view else "Unknown"
            return {
                "meeting_id": meeting_id,
                "container_group": group_name,
                "state": state,
                "provisioning_state": group.provisioning_state,
                "ip": group.ip_address.ip if group.ip_address else None,
            }
        except Exception as exc:
            logger.warning("Could not get status for %s: %s", group_name, exc)
            return {"meeting_id": meeting_id, "state": "NotFound", "error": str(exc)}

    async def delete_container(self, meeting_id: str) -> None:
        """Legacy: delete single-agent container (agent_id='default')."""
        group_name = self._group_name(meeting_id, "default")
        logger.info("Deleting container group %s", group_name)
        try:
            await asyncio.to_thread(
                self._client.container_groups.begin_delete(
                    self._resource_group, group_name
                ).result
            )
        except Exception as exc:
            logger.error("Failed to delete %s: %s", group_name, exc)
            raise

    async def list_active_meetings(self) -> list[dict]:
        """List active meetings, deduplicated by meeting_id with per-agent breakdown."""
        # First pass: collect names cheaply via list
        all_names = await asyncio.to_thread(
            lambda: [
                g.name
                for g in self._client.container_groups.list_by_resource_group(self._resource_group)
                if g.name and g.name.startswith(CONTAINER_GROUP_PREFIX)
            ]
        )
        # Second pass: GET each group individually to get instance_view with real state
        meetings: dict[str, dict] = {}
        for name in all_names:
            group = await asyncio.to_thread(
                self._client.container_groups.get, self._resource_group, name
            )
            suffix = name[len(CONTAINER_GROUP_PREFIX):]
            parts = suffix.split("-", 1)
            meeting_id = parts[0]
            agent_id = parts[1] if len(parts) > 1 else "default"
            state = self._get_state(group)

            if meeting_id not in meetings:
                meetings[meeting_id] = {
                    "meeting_id": meeting_id,
                    "agents": [],
                    "state": "Stopped",
                }
            meetings[meeting_id]["agents"].append(
                {"agent_id": agent_id, "container_group": name, "state": state}
            )
            if state == "Running":
                meetings[meeting_id]["state"] = "Running"

        return list(meetings.values())

    @staticmethod
    def _file_share_volume() -> tuple[list[Volume], list[VolumeMount]]:
        """Return ACI Volume + VolumeMount for the shared browser profile File Share.

        Returns empty lists if the required env vars are not set, allowing the
        container to fall back to an ephemeral (guest) browser profile.
        """
        storage_account = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
        storage_key = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
        share_name = os.environ.get("AZURE_FILE_SHARE_NAME")
        if not (storage_account and storage_key and share_name):
            return [], []

        volume = Volume(
            name=_BROWSER_PROFILE_VOLUME_NAME,
            azure_file=AzureFileVolume(
                share_name=share_name,
                storage_account_name=storage_account,
                storage_account_key=storage_key,
            ),
        )
        mount = VolumeMount(
            name=_BROWSER_PROFILE_VOLUME_NAME,
            mount_path=_BROWSER_PROFILE_MOUNT_PATH,
        )
        return [volume], [mount]

    @staticmethod
    def _api_key_env_var(llm_provider: str) -> str:
        mapping = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "google": "GOOGLE_API_KEY",
            "azure": "AZURE_OPENAI_API_KEY",
            "deepgram": "DEEPGRAM_API_KEY",
            "elevenlabs": "ELEVENLABS_API_KEY",
        }
        return mapping.get(llm_provider.lower(), "LLM_API_KEY")
