import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Self

from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from joinly.utils.logging import LOGGING_TRACE

logger = logging.getLogger(__name__)

_CDP_RE = re.compile(r"DevTools listening on (ws://.*)")
_CHROMIUM_LOCK_FILES = ("SingletonCookie", "SingletonLock", "SingletonSocket")


class BrowserSession:
    """A class to represent a browser session using Playwright."""

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        cdp_port: int = 0,
        profile_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        """Initialize the browser params.

        Args:
            env: Environment variables to set for the browser (default: None)
            cdp_port (int): The port for the CDP connection (default: 0, auto-assign)
            profile_dir: Persistent Chromium profile directory. If omitted, a
                temporary profile is used and removed on shutdown.
        """
        self._env: dict[str, str] = env if env is not None else os.environ.copy()
        self._cdp_port: int = cdp_port
        self._persistent_profile_dir = Path(profile_dir) if profile_dir else None

        self._proc: asyncio.subprocess.Process | None = None
        self._profile_dir: tempfile.TemporaryDirectory | None = None
        self._playwright: Playwright | None = None
        self._pw_browser: PlaywrightBrowser | None = None
        self._pw_context: BrowserContext | None = None
        self._default_page: Page | None = None
        self._pages = list[Page]()
        self.cdp_url: str | None = None

    async def __aenter__(self) -> Self:
        """Start and connect to the Playwright browser."""
        self._playwright = await async_playwright().start()

        bin_path = Path(self._playwright.chromium.executable_path)
        logger.debug("Chromium binary path: %s", bin_path)
        if not bin_path.exists():
            msg = "Chromium binary not found"
            logger.error(msg)
            raise RuntimeError(msg)

        if self._persistent_profile_dir is None:
            self._profile_dir = tempfile.TemporaryDirectory(prefix="pw-profile_")
            profile_dir = self._profile_dir.name
            logger.debug("Profile directory created at: %s", profile_dir)
        else:
            self._persistent_profile_dir.mkdir(parents=True, exist_ok=True)
            self._remove_stale_profile_locks(self._persistent_profile_dir)
            profile_dir = str(self._persistent_profile_dir)
            logger.info("Using persistent browser profile: %s", profile_dir)

        logger.debug("Launching Chromium browser.")
        self._proc = await asyncio.create_subprocess_exec(
            str(bin_path),
            f"--remote-debugging-port={self._cdp_port}",
            f"--user-data-dir={profile_dir}",
            "--use-fake-ui-for-media-stream",
            "--alsa-output-device=pulse",
            f"--alsa-input-device={self._env.get('PULSE_SOURCE')}",
            "--autoplay-policy=no-user-gesture-required",
            "--allow-http-screen-capture",
            "--auto-select-desktop-capture-source=Entire",
            "--enable-usermedia-screen-capturing",
            "--enable-features=WebRTCPipeWireCapturer",
            "--ozone-platform=x11",
            "--disable-gpu",
            "--disable-focus-on-load",
            "--window-size=1280,720",
            "--lang=en-US",
            "--test-type",
            "--no-sandbox",  # required for docker
            "--disable-dev-shm-usage",
            "--disable-gpu-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--no-xshm",
            "--force-device-scale-factor=1",
            "--disable-features=TranslateUI,MediaRouter,WebRtcAutomaticGainControl",
            "--disable-backgrounding-occluded-windows",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            start_new_session=True,
        )
        logger.debug("Chromium browser launched.")

        cdp_endpoint: str | None = None
        stderr_lines: list[str] = []
        while line := await self._proc.stderr.readline():  # type: ignore[attr-defined]
            stderr_line = line.decode(errors="replace").strip()
            stderr_lines.append(stderr_line)
            stderr_lines = stderr_lines[-20:]
            logger.log(LOGGING_TRACE, "[chromium] %s", stderr_line)
            m = _CDP_RE.search(stderr_line)
            if m:
                cdp_endpoint = m.group(1)
                break

        if cdp_endpoint is None:
            await self._terminate_chromium()
            msg = "Could not find DevTools URL in Chromium stderr."
            if stderr_lines:
                msg = f"{msg} Last Chromium output: {' | '.join(stderr_lines)}"
            logger.error(msg)
            raise RuntimeError(msg)
        logger.debug("DevTools URL: %s", cdp_endpoint)
        self.cdp_url = cdp_endpoint

        self._pw_browser = await self._playwright.chromium.connect_over_cdp(
            cdp_endpoint
        )
        self._pw_context = self._pw_browser.contexts[0]
        self._default_page = (
            self._pw_context.pages[0] if self._pw_context.pages else None
        )

        logger.debug("Playwright started.")

        return self

    def _remove_stale_profile_locks(self, profile_dir: Path) -> None:
        """Remove stale Chromium singleton locks from a persisted profile."""
        for file_name in _CHROMIUM_LOCK_FILES:
            lock_path = profile_dir / file_name
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove Chromium profile lock: %s", lock_path)

    async def _terminate_chromium(self) -> None:
        """Terminate the Chromium process if it is still alive."""
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            self._proc.terminate()
        except ProcessLookupError:
            return
        await self._proc.wait()

    async def __aexit__(self, *exc: object) -> None:
        """Stop the browser."""
        logger.debug("Stopping browser.")

        for page in self._pages:
            if page is not self._default_page and not page.is_closed():
                await page.close()
        if self._playwright:
            await self._playwright.stop()

        if self._proc and self._proc.returncode is None:
            logger.debug("Terminating browser process.")
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=1)
            except TimeoutError:
                logger.warning("Browser process did not terminate, killing it.")
                self._proc.kill()
                await self._proc.wait()
        logger.debug("Browser stopped.")

        if self._profile_dir is not None:
            self._profile_dir.cleanup()
            logger.debug("Profile directory removed: %s", self._profile_dir.name)

        self._pw_context = None
        self._pw_browser = None
        self._playwright = None
        self._proc = None
        self._profile_dir = None
        self._default_page = None
        self._pages = []
        self.cdp_url = None

    async def get_page(self) -> Page:
        """Get a new page in the browser context."""
        if self._pw_context is None:
            msg = "Playwright context is not initialized."
            raise RuntimeError(msg)

        page = await self._pw_context.new_page()
        logger.debug("New page created in the browser context.")

        page.on(
            "console",
            lambda msg: logger.log(
                LOGGING_TRACE, "[console][%s] %s", msg.type, msg.text
            ),
        )
        self._pages.append(page)

        return page
