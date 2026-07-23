"""Standalone screen share integration test with screenshot proof."""

import asyncio
import sys
import logging

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("screen_share_test")

MEETING_URL = "https://teams.microsoft.com/meet/275205681727738?p=e9GiXsivC7mSs9UJzv"
SHARE_URL = "https://en.wikipedia.org/wiki/Python_(programming_language)"
SCREENSHOT_PATH = "/workspace/screen_share_proof.png"


async def main() -> int:
    from joinly.settings import Settings, set_settings
    settings = Settings(
        name="Alex",
        stt="deepgram",
        tts="deepgram",
        meeting_provider="browser",
        meeting_provider_args={
            "browser_profile_dir": "/browser-profile",
            "vnc_server": True,
            "vnc_server_port": 5900,
        },
    )
    set_settings(settings)

    from joinly.server import mcp
    from fastmcp import Client
    import joinly.server as server_mod

    results: dict[str, str] = {}

    async with Client(mcp) as c:
        log.info("Step 1: Joining Teams meeting...")
        try:
            await c.call_tool("join_meeting", {"meeting_url": MEETING_URL})
            results["join"] = "PASS"
            log.info("  CHECK Joined meeting")
        except Exception as e:
            results["join"] = f"FAIL: {e}"
            log.error("  FAIL Join: %s", e)
            return 1

        await asyncio.sleep(8)

        log.info("Step 2: Starting screen share => %s", SHARE_URL)
        try:
            await c.call_tool("share_screen", {"url": SHARE_URL})
            results["share_start"] = "PASS"
            log.info("  CHECK Screen share started")
        except Exception as e:
            results["share_start"] = f"FAIL: {e}"
            log.error("  FAIL share: %s", e)

        await asyncio.sleep(5)

        if results.get("share_start") == "PASS":
            log.info("Step 3: Taking screenshot proof of shared content...")
            try:
                ms = server_mod._active_meeting_session
                if ms is not None:
                    page = ms._meeting_provider._page
                    if page and not page.is_closed():
                        await page.screenshot(path=SCREENSHOT_PATH)
                        log.info("  CHECK Screenshot saved: %s", SCREENSHOT_PATH)
                        results["screenshot"] = "PASS"
                    else:
                        results["screenshot"] = "FAIL: page closed"
                else:
                    results["screenshot"] = "FAIL: no active session"
            except Exception as e:
                results["screenshot"] = f"FAIL: {e}"
                log.error("  FAIL screenshot: %s", e)

        await asyncio.sleep(12)

        log.info("Step 4: Stopping screen share...")
        try:
            await c.call_tool("stop_sharing", {})
            results["share_stop"] = "PASS"
            log.info("  CHECK Share stopped")
        except Exception as e:
            results["share_stop"] = f"FAIL: {e}"

        await asyncio.sleep(2)

        log.info("Step 5: Leaving meeting...")
        try:
            await c.call_tool("leave_meeting", {})
            results["leave"] = "PASS"
            log.info("  CHECK Left meeting")
        except Exception as e:
            results["leave"] = f"FAIL: {e}"

    print("")
    print("=" * 50)
    print("SCREEN SHARE TEST RESULTS")
    print("=" * 50)
    all_pass = True
    for step, result in results.items():
        icon = "PASS" if result == "PASS" else "FAIL"
        print(f"  [{icon}] {step}: {result}")
        if result != "PASS":
            all_pass = False
    print("=" * 50)
    print(f"  Overall: {'ALL PASS' if all_pass else 'FAILURES FOUND'}")
    print("=" * 50)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
