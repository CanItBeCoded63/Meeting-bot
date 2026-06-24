import asyncio
import json
import logging
import signal
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv
from fastmcp import Client, FastMCP

from joinly_client.client import JoinlyClient
from joinly_client.types import McpClientConfig, TranscriptSegment
from joinly_client.utils import get_llm, get_prompt, load_tools

logger = logging.getLogger(__name__)


def _parse_kv(
    _ctx: click.Context, _param: click.Parameter, value: tuple[str]
) -> dict[str, object] | None:
    """Convert (--foo-arg key=value) repeated tuples to dict."""
    out: dict[str, object] = {}
    for item in value:
        try:
            k, v = item.split("=", 1)
        except ValueError as exc:
            msg = f"{item!r} is not of the form key=value"
            raise click.BadParameter(msg) from exc

        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out or None


@click.command()
@click.option(
    "--joinly-url",
    type=str,
    help="The URL of the joinly server to connect to.",
    default="http://localhost:8000/mcp/",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_URL",
)
@click.option(
    "-n",
    "--name",
    type=str,
    help="The meeting participant name.",
    default="joinly",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_NAME",
)
@click.option(
    "--llm-provider",
    "--model-provider",
    type=str,
    help="The provider of the LLM model to use in the client.",
    default="openai",
    show_default=True,
    show_envvar=True,
    envvar=["JOINLY_LLM_PROVIDER", "JOINLY_MODEL_PROVIDER"],
)
@click.option(
    "--llm-model",
    "--model-name",
    type=str,
    help="The name of the LLM model to use in the client.",
    default="gpt-4o",
    show_default=True,
    show_envvar=True,
    envvar=["JOINLY_LLM_MODEL", "JOINLY_MODEL_NAME"],
)
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to a .env file to load environment variables from.",
    default=None,
    show_default=True,
    is_eager=True,
    expose_value=False,
    callback=lambda _ctx, _param, value: load_dotenv(value),
)
@click.option(
    "--prompt",
    type=str,
    help="System prompt to use for the model. If not provided, the default "
    "system prompt will be used.",
    default=None,
    envvar="JOINLY_PROMPT",
)
@click.option(
    "--prompt-file",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to a text file containing the system prompt.",
    default=None,
    show_default=True,
    envvar="JOINLY_PROMPT_FILE",
)
@click.option(
    "--prompt-style",
    type=click.Choice(["dyadic", "mpc"], case_sensitive=False),
    help="The type of default prompt to use if no custom prompt is provided."
    "Options are 'dyadic' for one-on-one meetings or 'mpc' for group meetings.",
    default="mpc",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_PROMPT_STYLE",
)
@click.option(
    "--mcp-config",
    type=str,
    help="Path to a JSON configuration file for additional MCP servers. "
    "The file should contain configuration like: "
    '\'{"mcpServers": {"remote": {"url": "https://example.com/mcp"}}}\'. '
    "See https://gofastmcp.com/clients/client for more details.",
    default=None,
)
@click.option(
    "--name-trigger",
    is_flag=True,
    help="Trigger the agent only when the name is mentioned in the transcript.",
)
@click.option(
    "--language",
    "--lang",
    type=str,
    help="The language to use for transcription and text-to-speech.",
    default=None,
    show_envvar=True,
    envvar="JOINLY_LANGUAGE",
)
@click.option(
    "--vad",
    type=str,
    help='Voice Activity Detection service to use. Options are: "silero", "webrtc".',
    default=None,
    show_envvar=True,
    envvar="JOINLY_VAD",
)
@click.option(
    "--stt",
    type=str,
    help='Speech-to-Text service to use. Options are: "whisper" (local), "deepgram".',
    default=None,
    show_envvar=True,
    envvar="JOINLY_STT",
)
@click.option(
    "--tts",
    type=str,
    help='Text-to-Speech service to use. Options are: "kokoro" (local), '
    '"elevenlabs", "deepgram".',
    default=None,
    show_envvar=True,
    envvar="JOINLY_TTS",
)
@click.option(
    "--vad-arg",
    "vad_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="Arguments for the VAD service in the form of key=value. "
    "Can be specified multiple times.",
)
@click.option(
    "--stt-arg",
    "stt_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="Arguments for the STT service in the form of key=value. "
    "Can be specified multiple times.",
)
@click.option(
    "--tts-arg",
    "tts_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="Arguments for the TTS service in the form of key=value. "
    "Can be specified multiple times.",
)
@click.option(
    "--transcription-controller-arg",
    "transcription_controller_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="Arguments for the transcription controller in the form of key=value. "
    "Can be specified multiple times.",
)
@click.option(
    "--speech-controller-arg",
    "speech_controller_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="Arguments for the speech controller in the form of key=value. "
    "Can be specified multiple times.",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase logging verbosity (can be used multiple times).",
    default=1,
)
@click.option(
    "--auto-leave/--no-auto-leave",
    is_flag=True,
    help="Automatically leave the meeting once all other participants have left.",
    default=True,
    show_default=True,
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress all but error and critical logging."
)
@click.argument(
    "meeting-url",
    type=str,
    required=True,
)
def cli(  # noqa: PLR0913
    *,
    joinly_url: str,
    name: str,
    llm_provider: str,
    llm_model: str,
    prompt: str | None,
    prompt_file: str | None,
    prompt_style: str,
    name_trigger: bool,
    mcp_config: str | None,
    auto_leave: bool,
    meeting_url: str,
    verbose: int,
    quiet: bool,
    **settings: Any,  # noqa: ANN401
) -> None:
    """Run the joinly client."""
    from rich.logging import RichHandler

    log_level = logging.WARNING
    if quiet:
        log_level = logging.ERROR
    elif verbose == 1:
        log_level = logging.INFO
    elif verbose == 2:  # noqa: PLR2004
        log_level = logging.DEBUG

    logging.basicConfig(
        level=logging.WARNING if not quiet else logging.ERROR,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    logging.getLogger("joinly_client").setLevel(log_level)

    if prompt_file and not prompt:
        try:
            with Path(prompt_file).open("r") as f:
                prompt = f.read().strip()
        except Exception:
            logger.exception("Failed to load prompt file")
            prompt = None

    mcp_config_dict: dict[str, Any] | None = None
    if mcp_config:
        try:
            with Path(mcp_config).open("r") as f:
                mcp_config_dict = json.load(f)
        except Exception:
            logger.exception("Failed to load MCP configuration file")
            mcp_config_dict = None

    try:
        asyncio.run(
            run(
                joinly_url=joinly_url,
                meeting_url=meeting_url,
                llm_provider=llm_provider,
                llm_model=llm_model,
                prompt=prompt,
                prompt_style=prompt_style,
                name=name,
                name_trigger=name_trigger,
                mcp_config=mcp_config_dict,
                auto_leave=auto_leave,
                settings={k: v for k, v in settings.items() if v is not None},
            )
        )
    except KeyboardInterrupt:
        logger.info("Exiting due to keyboard interrupt.")


async def run(  # noqa: PLR0913, C901
    joinly_url: str | FastMCP,
    meeting_url: str,
    llm_provider: str,
    llm_model: str,
    *,
    prompt: str | None = None,
    prompt_style: str | None = None,
    name: str | None = None,
    name_trigger: bool = False,
    mcp_config: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    auto_leave: bool = True,
) -> None:
    """Run the joinly client.

    Args:
        joinly_url (str | FastMCP): The URL of the joinly server or a FastMCP instance.
        meeting_url (str): The URL of the meeting to join.
        llm_provider (str): The provider of the LLM model to use.
        llm_model (str): The name of the LLM model to use.
        prompt (str | None): System prompt to use for the model.
        prompt_style (str | None): Default prompt to use if no custom one is provided.
        name (str | None): The name of the participant.
        name_trigger (bool): Whether to trigger the agent only when the name is
            mentioned.
        mcp_config (dict[str, Any] | None): Configuration for additional MCP servers.
        settings (dict[str, Any] | None): Additional settings for the client.
        auto_leave (bool): Automatically leave the meeting once all other
            participants have left.
    """
    # Event set by the LLM tool when a participant asks the bot to leave.
    # The main monitoring loop watches this and triggers summary + leave.
    requested_leave_event = asyncio.Event()

    client = JoinlyClient(
        joinly_url,
        name=name,
        name_trigger=name_trigger,
        settings=settings,
    )

    if mcp_config and "mcpServers" not in mcp_config:
        logger.warning(
            "MCP configuration does not contain 'mcpServers'. "
            "Using the main joinly client only."
        )
        mcp_config = None
    elif mcp_config and "joinly" in mcp_config["mcpServers"]:
        mcp_config["_joinly"] = mcp_config.pop("joinly")

    additional_clients = (
        {
            name: Client({"mcpServers": {name: config}})
            for name, config in mcp_config["mcpServers"].items()
        }
        if mcp_config
        else {}
    )

    async def log_segments(segments: list[TranscriptSegment]) -> None:
        """Log segments received from the client."""
        for segment in segments:
            logger.info('%s: "%s"', segment.speaker or "Participant", segment.text)

    client.add_segment_callback(log_segments)
    llm = get_llm(llm_provider, llm_model)

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(client)
        for client_name, additional_client in additional_clients.items():
            logger.info("Connecting to %s", client_name)
            await stack.enter_async_context(additional_client)
            logger.debug("Connected to %s", client_name)

        joinly_config = McpClientConfig(client=client.client, exclude=["join_meeting"])

        # ── request_leave tool ───────────────────────────────────────────────────
        # Registered as an in-process FastMCP tool so the LLM can trigger a
        # graceful leave (summary → chat → leave_meeting) without calling
        # leave_meeting directly, which would skip the summary step.
        internal_mcp = FastMCP("joinly-internal")

        @internal_mcp.tool()
        async def request_leave() -> str:  # noqa: RUF029
            """Signal that the bot should generate the meeting summary, post it to
            chat, and then gracefully leave the meeting. Call this when any
            participant explicitly asks the bot to leave the meeting."""
            logger.info("request_leave tool invoked — triggering graceful leave.")
            requested_leave_event.set()
            return "Leave requested. I will post the meeting summary and leave shortly."

        async def _speak_filler(tool_name: str, args: dict[str, Any]) -> None:
            import random
            fillers = [
                "Let me check that for you.",
                "Sure, give me just a moment.",
                "On it — one second.",
                "Let me look that up real quick.",
                "Good question, pulling that up now."
            ]
            asyncio.create_task(client.speak_text(random.choice(fillers)))

        tools, tool_executor = await load_tools(
            {
                "joinly": joinly_config,
                "joinly-internal": McpClientConfig(client=internal_mcp),
                **(
                    {
                        n: McpClientConfig(c, pre_callback=_speak_filler)
                        for n, c in additional_clients.items()
                    }
                    if additional_clients
                    else {}
                ),
            }
        )
        agent = client.create_agent(
            llm,
            tools,
            tool_executor,
            prompt=get_prompt(
                instructions=prompt,
                prompt_style=prompt_style,
                name=client.name,
            ),
        )
        async with agent:
            meeting_start_time = datetime.now(tz=UTC)
            meeting_start_str = meeting_start_time.strftime("%H:%M UTC")
            # Re-inject prompt with actual meeting start time
            agent._prompt = get_prompt(  # noqa: SLF001
                instructions=prompt,
                prompt_style=prompt_style,
                name=client.name,
                meeting_start=meeting_start_str,
            )
            await client.join_meeting(meeting_url)

            # Set up graceful shutdown on SIGTERM / SIGINT (docker stop)
            shutdown_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, shutdown_event.set)

            try:
                if auto_leave:
                    logger.info(
                        "Auto-leave enabled. Monitoring participant count..."
                    )
                    while not shutdown_event.is_set():
                        # Check both shutdown signal AND requested-leave every 10s
                        try:
                            done, _ = await asyncio.wait(
                                [
                                    asyncio.create_task(shutdown_event.wait()),
                                    asyncio.create_task(requested_leave_event.wait()),
                                ],
                                timeout=10,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        except Exception:  # noqa: BLE001
                            done = set()

                        if shutdown_event.is_set():
                            break  # SIGTERM / docker stop

                        if requested_leave_event.is_set():
                            logger.info(
                                "Participant requested leave — generating summary and leaving."
                            )
                            # Speak a farewell immediately so participants hear it
                            try:
                                await client.speak_text(
                                    "Sure, I'll wrap up now. Let me post the meeting summary in the chat before I go."
                                )
                            except Exception:  # noqa: BLE001
                                pass
                            break

                        try:
                            participants = await client.get_participants()
                            # If the bot is the only participant left, auto-leave
                            if len(participants.root) <= 1:
                                logger.info(
                                    "No other participants left. Auto-leaving..."
                                )
                                break
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "Failed to check participants (will retry): %s",
                                e,
                            )
                else:
                    # No auto-leave: wait for SIGTERM or a requested-leave
                    await asyncio.wait(
                        [
                            asyncio.create_task(shutdown_event.wait()),
                            asyncio.create_task(requested_leave_event.wait()),
                        ],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if requested_leave_event.is_set():
                        try:
                            await client.speak_text(
                                "Sure, I'll wrap up now. Let me post the meeting summary in the chat before I go."
                            )
                        except Exception:  # noqa: BLE001
                            pass
            finally:
                # --- End-of-meeting summary ---
                try:
                    from pydantic_ai.direct import model_request  # noqa: PLC0415
                    from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart  # noqa: PLC0415
                    from pydantic_ai.models import ModelRequestParameters  # noqa: PLC0415

                    elapsed = datetime.now(tz=UTC) - meeting_start_time
                    mins = int(elapsed.total_seconds() // 60)

                    # Capture ONLY real human participant utterances (UserPromptPart lines
                    # that look like "Speaker: text") — excludes system prompts, tool
                    # returns, and Alex's internal speak_text calls so the transcript
                    # reflects the true conversation and nothing gets silently dropped.
                    convo_lines = []
                    for m in agent._messages:  # noqa: SLF001
                        for p in getattr(m, "parts", []):
                            if (
                                isinstance(p, UserPromptPart)
                                and isinstance(p.content, str)
                                and ": " in p.content          # "Speaker: text" shape
                                and len(p.content) > 5
                            ):
                                convo_lines.append(p.content)

                    # Also capture Alex's spoken replies (joinly_speak_text tool args)
                    from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: PLC0415
                    for m in agent._messages:  # noqa: SLF001
                        if isinstance(m, ModelResponse):
                            for p in m.parts:
                                if (
                                    isinstance(p, ToolCallPart)
                                    and p.tool_name == "joinly_speak_text"
                                    and isinstance(p.args, str)
                                ):
                                    try:
                                        import json  # noqa: PLC0415
                                        text = json.loads(p.args).get("text", "")
                                        if text:
                                            convo_lines.append(f"Alex: {text}")
                                    except Exception:  # noqa: BLE001
                                        pass

                    # Sort is not needed — messages are already chronological.
                    # Cap at last 120 lines to include long meetings.
                    convo_text = "\n".join(convo_lines[-120:])

                    if convo_text.strip():
                        summary_response = await model_request(
                            llm,
                            [
                                ModelRequest(parts=[
                                    SystemPromptPart(
                                        "You are a professional meeting summariser. "
                                        "Given a meeting transcript, you MUST always produce ALL of the "
                                        "following sections, even if a section has nothing to report "
                                        "(write 'None' in that case):\n\n"
                                        "OVERVIEW\n"
                                        "2-4 sentences describing what the meeting was about and main outcomes.\n\n"
                                        "KEY DECISIONS\n"
                                        "Bullet list of concrete decisions made. Include 'None' if no decisions.\n\n"
                                        "ACTION ITEMS\n"
                                        "Bullet list with: action, owner (person responsible), and deadline if mentioned. "
                                        "Include ALL commitments, follow-ups, and 'will look into it' type statements. "
                                        "If no owner was named, write 'Owner: unassigned'. "
                                        "Include 'None' if no action items.\n\n"
                                        "FOLLOW-UPS\n"
                                        "Bullet list of open questions, topics deferred, or items needing further discussion. "
                                        "Include 'None' if no follow-ups.\n\n"
                                        "Use plain text. Keep each bullet point concise (1 line)."
                                    ),
                                    UserPromptPart(
                                        f"Meeting duration: {mins} minutes.\n\n"
                                        f"Transcript:\n{convo_text}"
                                    ),
                                ])
                            ],
                            model_request_parameters=ModelRequestParameters(
                                function_tools=[],
                                allow_text_output=True,
                                output_tools=[],
                            ),
                        )
                        from pydantic_ai.messages import TextPart  # noqa: PLC0415
                        summary_text = next(
                            (p.content for p in summary_response.parts if isinstance(p, TextPart)),
                            None,
                        )
                        if summary_text:
                            header = f"📋 Meeting Summary ({mins} min)\n\n"
                            full_msg = header + summary_text
                            logger.info("Full summary (%d chars):\n%s", len(full_msg), full_msg)
                            # Split into chunks ≤ 1900 chars — Teams allows up to 28k but
                            # chat messages render best when kept readable.
                            chunk_size = 1900
                            for i in range(0, len(full_msg), chunk_size):
                                await client.send_chat_message(full_msg[i:i + chunk_size])
                            logger.info("Meeting summary posted to chat.")
                    else:
                        logger.warning("No participant conversation found — skipping summary.")
                except Exception as summary_err:  # noqa: BLE001
                    logger.warning("Failed to generate meeting summary: %s", summary_err)

                # Gracefully leave the meeting on any exit (SIGTERM, auto-leave, etc.)
                logger.info("Leaving meeting before exit...")
                try:
                    await client.leave_meeting()
                except Exception:  # noqa: BLE001
                    pass  # already left or meeting ended
                usage = agent.usage.merge(await client.get_usage())
                if usage.root:
                    logger.info("Usage:\n%s", usage)


if __name__ == "__main__":
    cli()
