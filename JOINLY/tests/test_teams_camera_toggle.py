"""Tests for the post-join Teams camera-on flow.

The implementation:
1. Runs after `_check_joined` has already confirmed admission (in-meeting
   toolbar, not the pre-join screen, is on screen) — kept off the
   join-critical-path since it used to share the CPU-contention window with
   the join-button click.
2. Uses page.evaluate() with JS to find and click the camera toggle in the
   OFF state.
These tests verify the correct behaviour for each JS result code.
"""
from __future__ import annotations

import pytest

from joinly.providers.browser.platforms.teams import TeamsBrowserPlatformController

_SETTLE_DELAY_MS = 800


class _FakePage:
    """Minimal Page fake for _ensure_camera_on.

    Lets tests inject a fixed JS evaluation result and records how many
    wait_for_timeout calls were made and with what values.
    """

    def __init__(self, evaluate_result: str | None) -> None:
        self._evaluate_result = evaluate_result
        self.timeouts: list[int] = []

    async def evaluate(self, _script: str) -> str | None:
        """Return the pre-configured JS result."""
        return self._evaluate_result

    async def wait_for_timeout(self, ms: int) -> None:
        """Record requested pauses."""
        self.timeouts.append(ms)


@pytest.mark.asyncio
async def test_camera_on_clicked_and_settles() -> None:
    """A 'clicked:...' JS result should trigger the post-click settle delay."""
    page = _FakePage("clicked:camera. off")

    await TeamsBrowserPlatformController()._ensure_camera_on(page)

    # Post-click settle must be present; render-wait (600ms) also recorded.
    assert _SETTLE_DELAY_MS in page.timeouts


@pytest.mark.asyncio
async def test_camera_already_on_no_settle() -> None:
    """An 'already_on:...' result should NOT trigger the settle delay."""
    page = _FakePage("already_on:camera is on")

    await TeamsBrowserPlatformController()._ensure_camera_on(page)

    assert _SETTLE_DELAY_MS not in page.timeouts


@pytest.mark.asyncio
async def test_camera_toggle_not_found_no_settle() -> None:
    """A None JS result (no button found) should not crash and not settle."""
    page = _FakePage(None)

    await TeamsBrowserPlatformController()._ensure_camera_on(page)

    assert _SETTLE_DELAY_MS not in page.timeouts
