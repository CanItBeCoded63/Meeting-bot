import asyncio
import logging
from pathlib import Path

import click

from joinly.providers.browser.browser_session import BrowserSession
from joinly.providers.browser.devices.virtual_display import VirtualDisplay

logger = logging.getLogger(__name__)


@click.command()
@click.option(
    "--profile-dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Persistent Chromium profile directory to use for browser login state.",
)
@click.option(
    "--url",
    default="https://teams.microsoft.com/v2/",
    show_default=True,
    help="URL to open for the one-time login flow.",
)
@click.option(
    "--vnc-port",
    type=int,
    default=5900,
    show_default=True,
    help="VNC port exposed by x11vnc.",
)
def cli(profile_dir: Path, url: str, vnc_port: int) -> None:
    """Open Teams in a persistent browser profile for one-time login."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run(profile_dir=profile_dir, url=url, vnc_port=vnc_port))


async def _run(*, profile_dir: Path, url: str, vnc_port: int) -> None:
    env: dict[str, str] = {}
    async with (
        VirtualDisplay(env=env, use_vnc_server=True, vnc_port=vnc_port),
        BrowserSession(env=env, profile_dir=profile_dir) as browser,
    ):
        page = await browser.get_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        logger.info("Opened %s", url)
        logger.info("Connect a VNC viewer to localhost:%s and sign in.", vnc_port)
        logger.info("Press Ctrl+C in this terminal after Teams is fully signed in.")
        stop = asyncio.Event()
        await stop.wait()


if __name__ == "__main__":
    cli()
