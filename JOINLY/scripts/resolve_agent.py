"""Resolve one agent's runtime config from agents.yaml for shell scripts.

agents.yaml is the single source of truth for agent persona/voice config —
azure-deploy/meeting_manager reads it directly for cloud deploys
(main.py:_resolve_agent_profiles). This script lets local join scripts read
the exact same file instead of duplicating name/persona/voice values.

Prints KEY=VALUE lines (one per line, no shell-special characters in values
except the persona file path) for the caller to parse. Exits 1 with nothing
on stdout if the agent id isn't found, so callers can fall back.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from joinly_client.registry import AgentRegistry, RegistryConfigError


def main() -> int:
    # Force LF-only stdout — on Windows, Python's default text-mode stdout
    # translates \n to \r\n, and shell script parsers (read loops) don't
    # strip the trailing \r, corrupting values used as exact-match lookups
    # (e.g. a TTS provider name becomes "deepgram\r", which fails to import).
    sys.stdout.reconfigure(newline="\n")

    if len(sys.argv) != 3:
        print("usage: resolve_agent.py <agents-yaml-path> <agent-id>", file=sys.stderr)
        return 1

    yaml_path, agent_id = Path(sys.argv[1]), sys.argv[2]

    try:
        registry = AgentRegistry.from_yaml(yaml_path)
    except (RegistryConfigError, FileNotFoundError) as exc:
        print(f"Failed to load {yaml_path}: {exc}", file=sys.stderr)
        return 1

    profile = registry.get(agent_id)
    if profile is None:
        return 1

    persona_file = Path(
        tempfile.mkstemp(prefix=f"joinly-persona-{agent_id}-", suffix=".txt")[1]
    )
    persona_file.write_text(profile.persona.strip(), encoding="utf-8")

    print(f"AGENT_NAME={profile.name}")
    print(f"AGENT_TTS={profile.joinly_settings.tts or ''}")
    print(f"AGENT_STT={profile.joinly_settings.stt or ''}")
    print(f"AGENT_TTS_VOICE={profile.joinly_settings.tts_voice or ''}")
    print(f"AGENT_PERSONA_FILE={persona_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
