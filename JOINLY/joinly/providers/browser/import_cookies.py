import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast

import click
from playwright.async_api import BrowserContext

from joinly.providers.browser.browser_session import BrowserSession
from joinly.providers.browser.devices.virtual_display import VirtualDisplay

logger = logging.getLogger(__name__)

_TEAMS_URL = "https://teams.microsoft.com/v2/"
_SAME_SITE_VALUES = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
}
_COOKIE_FIELDS = {
    "name",
    "value",
    "url",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
}


@click.command()
@click.option(
    "--profile-dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Persistent Chromium profile directory to write cookie state into.",
)
@click.option(
    "--cookies-file",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Cookie JSON file exported from a signed-in browser.",
)
@click.option(
    "--url",
    default=_TEAMS_URL,
    show_default=True,
    help="URL to open after cookies are imported.",
)
def cli(profile_dir: Path, cookies_file: Path, url: str) -> None:
    """Import browser cookies into a persistent Chromium profile."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run(profile_dir=profile_dir, cookies_file=cookies_file, url=url))


async def _run(*, profile_dir: Path, cookies_file: Path, url: str) -> None:
    cookies = _load_cookies(cookies_file)
    env: dict[str, str] = {}
    async with (
        VirtualDisplay(env=env, use_vnc_server=False),
        BrowserSession(env=env, profile_dir=profile_dir) as browser,
    ):
        page = await browser.get_page()
        await page.context.add_cookies(cast("Any", cookies))
        logger.info("Imported %s cookies into %s", len(cookies), profile_dir)
        await _restore_local_storage(page.context, cookies_file)
        await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        await page.wait_for_timeout(10_000)
        logger.info("Opened %s so Chromium can persist the imported auth state", url)


def _load_cookies(cookies_file: Path) -> list[dict[str, Any]]:
    payload = json.loads(cookies_file.read_text(encoding="utf-8"))
    raw_cookies = payload.get("cookies") if isinstance(payload, dict) else payload
    if not isinstance(raw_cookies, list):
        msg = "Cookie file must be a JSON list or a Playwright storage_state object."
        raise click.ClickException(msg)

    cookies = [_normalize_cookie(cookie) for cookie in raw_cookies]
    if not cookies:
        msg = "Cookie file did not contain any cookies."
        raise click.ClickException(msg)
    return cookies


def _normalize_cookie(cookie: object) -> dict[str, Any]:
    if not isinstance(cookie, dict):
        msg = "Each cookie entry must be a JSON object."
        raise click.ClickException(msg)

    normalized = {key: value for key, value in cookie.items() if key in _COOKIE_FIELDS}
    if "expirationDate" in cookie and "expires" not in normalized:
        normalized["expires"] = cookie["expirationDate"]

    if "name" not in normalized or "value" not in normalized:
        msg = "Each cookie must include name and value."
        raise click.ClickException(msg)

    if "url" not in normalized and "domain" not in normalized:
        msg = "Each cookie must include either url or domain."
        raise click.ClickException(msg)

    if "domain" in normalized and "path" not in normalized:
        normalized["path"] = "/"

    if normalized.get("expires") in (None, -1):
        normalized.pop("expires", None)

    same_site = normalized.get("sameSite")
    if not isinstance(same_site, str):
        normalized.pop("sameSite", None)
    else:
        mapped = _SAME_SITE_VALUES.get(same_site.lower())
        if mapped is None:
            normalized.pop("sameSite", None)
        else:
            normalized["sameSite"] = mapped

    return normalized


async def _restore_local_storage(context: BrowserContext, cookies_file: Path) -> None:
    payload = json.loads(cookies_file.read_text(encoding="utf-8"))
    origins = payload.get("origins", []) if isinstance(payload, dict) else []
    if not origins:
        return

    page = await context.new_page()
    try:
        for origin in origins:
            if not isinstance(origin, dict):
                continue
            origin_url = origin.get("origin")
            local_storage = origin.get("localStorage", [])
            if not isinstance(origin_url, str) or not isinstance(local_storage, list):
                continue
            await page.goto(origin_url, wait_until="domcontentloaded", timeout=60_000)
            for item in local_storage:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    await page.evaluate(
                        "([name, value]) => localStorage.setItem(name, value)",
                        [item["name"], item["value"]],
                    )
    finally:
        await page.close()


if __name__ == "__main__":
    cli()
