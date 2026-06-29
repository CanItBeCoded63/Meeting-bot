import asyncio
import contextlib
import logging
import re
from typing import Any, ClassVar

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from joinly.providers.browser.platforms.base import BaseBrowserPlatformController
from joinly.settings import get_settings
from joinly.types import MeetingChatHistory, MeetingChatMessage, MeetingParticipant

logger = logging.getLogger(__name__)

_CHAT_INPUT_SELECTOR = (
    "div[contenteditable='true'], "
    "[data-tid='ckeditor-chatMessageStream'], "
    "textarea[placeholder*='message'], "
    "div[aria-label*='Type a message']"
)
_CHAT_PANE_SELECTOR = (
    '[data-tid="chat-pane"], '
    '[data-tid="message-pane"], '
    '[data-tid="chat-pane-list"], '
    '[data-tid="message-pane-list"]'
)
_CHAT_BUTTON_SELECTOR = (
    'button[aria-label*="chat" i], '
    'button[aria-label*="conversation" i], '
    'button[data-tid*="chat" i], '
    'button:has-text("Chat")'
)


class TeamsBrowserPlatformController(BaseBrowserPlatformController):
    """Controller for managing Teams browser meetings."""

    url_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?:https?://)?(?:[a-z0-9-]+\.)?(?:teams\.microsoft\.com|teams\.live\.com|teams\.microsoft\.us|dod\.teams\.microsoft\.us)/"
    )

    def __init__(self, *, admission_timeout_seconds: int = 600) -> None:
        """Initialize the Teams browser platform controller."""
        self._state: dict[str, Any] = {}
        self._admission_timeout_seconds = admission_timeout_seconds

    @property
    def active_speaker(self) -> str | None:
        """Get the name of the active speaker in the Teams meeting."""
        return self._state.get("active_speaker")

    async def join(
        self,
        page: Page,
        url: str,
        name: str,
        passcode: str | None = None,  # noqa: ARG002
    ) -> None:
        """Join the Teams meeting.

        Args:
            page: The Playwright page instance.
            url: The URL of the Teams meeting.
            name: The name of the participant.
            passcode: The passcode for the meeting (if required).
        """
        # Check if this is a gov.teams URL
        if "teams.microsoft.us" in url or "dod.teams.microsoft.us" in url:
            await self._join_gov_teams(page, url, name)
        else:
            await self._join_standard_teams(page, url, name)

        if not await self._check_joined(page, timeout=self._admission_timeout_seconds):
            msg = "Join check failed: Failed to join the Teams meeting."
            raise RuntimeError(msg)

        await self._setup_active_speaker_observer(page)

    async def _join_standard_teams(
        self,
        page: Page,
        url: str,
        name: str,
    ) -> None:
        """Join a standard Teams meeting.

        Handles both classic and new-style (teams.microsoft.com/meet/...) URLs.
        New-style URLs redirect through a launcher page that offers to open the
        desktop app — we click "Continue on this browser" / "Join on the web"
        to bypass it and land on the web join page.

        Args:
            page: The Playwright page instance.
            url: The URL of the Teams meeting.
            name: The name of the participant.
        """
        # Use a longer timeout — new meet/ URLs do an extra launcher redirect
        await page.goto(url, wait_until="load", timeout=40000)

        async def _dismiss_dialog(page: Page) -> None:
            with contextlib.suppress(PlaywrightTimeoutError):
                await page.click('div[role="dialog"] button', timeout=0)

        async def _click_join_browser(page: Page) -> None:
            """Click through the Teams launcher page to the web join page."""
            with contextlib.suppress(PlaywrightTimeoutError):
                btn_pattern = re.compile(
                    r"join.*browser|continue.*web|join.*web|continue.*browser",
                    re.IGNORECASE,
                )
                join_browser_btn = page.get_by_role("button", name=btn_pattern)
                await join_browser_btn.click(timeout=15000)

        dismiss_dialog = asyncio.create_task(_dismiss_dialog(page))
        join_browser = asyncio.create_task(_click_join_browser(page))

        try:
            # Use a broader locator + longer timeout to handle the extra redirect
            name_field = page.locator(
                'input[placeholder*="name" i], input[aria-label*="name" i]'
            ).first
            await name_field.fill(name, timeout=40000)

            join_btn = page.get_by_role(
                "button", name=re.compile(r"join", re.IGNORECASE)
            )
            await join_btn.click(timeout=10000)

        finally:
            for task in [dismiss_dialog, join_browser]:
                if not task.done():
                    task.cancel()

    async def _join_gov_teams(
        self,
        page: Page,
        url: str,
        name: str,
    ) -> None:
        """Join a government Teams meeting.

        Supports teams.microsoft.us or dod.teams.microsoft.us domains.

        Args:
            page: The Playwright page instance.
            url: The URL of the Teams meeting.
            name: The name of the participant.
        """
        # Government Teams may have redirects, use longer timeout
        await page.goto(url, wait_until="load", timeout=60000)

        async def _dismiss_dialog(page: Page) -> None:
            with contextlib.suppress(PlaywrightTimeoutError):
                await page.click('div[role="dialog"] button', timeout=1000)

        async def _click_join_browser(page: Page) -> None:
            with contextlib.suppress(PlaywrightTimeoutError):
                btn_pattern = re.compile(r"join.*browser|continue.*web", re.IGNORECASE)
                join_browser_btn = page.get_by_role("button", name=btn_pattern)
                await join_browser_btn.click(timeout=1000)

        dismiss_dialog = asyncio.create_task(_dismiss_dialog(page))
        join_browser = asyncio.create_task(_click_join_browser(page))

        try:
            name_field = page.locator(
                'input[placeholder*="name" i], input[aria-label*="name" i]'
            ).first
            await name_field.fill(name, timeout=40000)

            join_btn = page.get_by_role(
                "button", name=re.compile(r"join", re.IGNORECASE)
            )
            await join_btn.click(timeout=10000)

        finally:
            for task in [dismiss_dialog, join_browser]:
                if not task.done():
                    task.cancel()

    async def leave(self, page: Page) -> None:
        """Leave the Teams meeting.

        Args:
            page: The Playwright page instance.
        """
        leave_btn = page.get_by_role(
            "button", name=re.compile(r"leave|hangup", re.IGNORECASE)
        ).first
        if not await leave_btn.is_visible():
            fallback_btn = page.locator(
                'button[aria-label*="leave" i], button[data-tid="call-hangup"]'
            ).first
            if await fallback_btn.is_visible():
                leave_btn = fallback_btn
            else:
                msg = "Leave button not found or not visible."
                raise RuntimeError(msg)
        await leave_btn.click(timeout=1500)
        await page.wait_for_timeout(500)

    async def send_chat_message(self, page: Page, message: str) -> None:
        """Send a chat message in the Teams meeting.

        Args:
            page: The Playwright page instance.
            message: The message to send.
        """
        await self._open_chat(page, required=True)

        chat_input = page.locator(_CHAT_INPUT_SELECTOR).first

        if not await chat_input.is_visible():
            placeholder_input = page.get_by_placeholder(
                re.compile(r"Type a message", re.IGNORECASE)
            ).first
            if await placeholder_input.is_visible():
                chat_input = placeholder_input
            else:
                msg = "Chat input not found or not visible."
                raise RuntimeError(msg)

        await chat_input.fill(message)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")

    async def get_chat_history(self, page: Page) -> MeetingChatHistory:
        """Get the chat history from the Teams meeting.

        Args:
            page: The Playwright page instance.

        Returns:
            MeetingChatHistory: The chat history of the meeting.
        """
        try:
            if not await self._open_chat(page, required=False):
                logger.warning(
                    "Teams chat is not available yet; returning empty history."
                )
                return MeetingChatHistory(messages=[])
        except RuntimeError as exc:
            logger.warning("Teams chat history unavailable yet: %s", exc)
            return MeetingChatHistory(messages=[])

        messages: list[MeetingChatMessage] = []

        chat_items = await page.locator(
            '[data-tid="chat-pane-item"], '
            '[data-tid="message-pane-item"], '
            'div[data-ui-id="chat-message"]'
        ).all()
        # Fallback to general list items in the chat pane if specific data-tids fail
        if not chat_items:
            chat_pane = page.locator(
                '[data-tid="chat-pane"], aside, div[role="complementary"]'
            ).first
            if await chat_pane.is_visible():
                chat_items = await chat_pane.get_by_role("listitem").all()

        for el in chat_items:
            content_el = el.locator(
                '[data-tid="chat-pane-message"], [data-tid="message-body"]'
            ).first
            if await content_el.is_visible():
                text = (await content_el.inner_text()).strip()
            else:
                text = (await el.inner_text()).strip()

            if not text:
                continue

            ts_el = el.locator("time[datetime]").first
            ts = (
                await ts_el.get_attribute("datetime")
                if await ts_el.is_visible()
                else None
            )

            author_locator = el.locator('[data-tid="message-author-name"]').first
            if await author_locator.is_visible():
                sender_text = await author_locator.text_content() or ""
                sender = sender_text.strip() or None
            else:
                sender = None

            messages.append(MeetingChatMessage(text=text, timestamp=ts, sender=sender))

        return MeetingChatHistory(messages=messages)

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:
        """Get the list of participants in the Teams meeting.

        Args:
            page: The Playwright page instance.

        Returns:
            list[MeetingParticipant]: A list of participants in the meeting.
        """
        participants_list = page.locator(
            'div[aria-label="Attendees"][role="tree"]'
        ).first
        is_participant_list_visible = await participants_list.is_visible()

        if not is_participant_list_visible:
            participants_button = page.get_by_role(
                "button", name=re.compile(r"^people", re.IGNORECASE)
            ).first
            if not await participants_button.is_visible():
                fallback_btn = page.locator(
                    'button:has-text("People"), button[aria-label*="People" i]'
                ).first
                if await fallback_btn.is_visible():
                    participants_button = fallback_btn
                else:
                    msg = "Participants button not found or not visible."
                    raise RuntimeError(msg)
            await participants_button.click()
            await page.wait_for_timeout(1000)
            if not await participants_list.is_visible():
                await page.wait_for_timeout(1000)

        participants: list[MeetingParticipant] = []
        for item in await participants_list.locator(
            "[data-cid='roster-participant'][aria-label]"
        ).all():
            if aria_label := await item.get_attribute("aria-label"):
                labels = aria_label.split(", ")
                name = labels[0].strip()
                infos = labels[1:] if len(labels) > 1 else []
                participants.append(MeetingParticipant(name=name, infos=infos))

        return participants

    async def mute(self, page: Page) -> None:
        """Mute the participant in the Teams meeting.

        Args:
            page: The Playwright page instance.
        """
        mute_btn = page.get_by_role("button", name=re.compile(r"^mute", re.IGNORECASE))
        if await mute_btn.is_visible():
            await mute_btn.click(timeout=1000)
        elif not await page.get_by_role(
            "button", name=re.compile(r"^unmute", re.IGNORECASE)
        ).is_visible():
            msg = "Mute button not found or not visible."
            raise RuntimeError(msg)

    async def unmute(self, page: Page) -> None:
        """Unmute the participant in the Teams meeting.

        Args:
            page: The Playwright page instance.
        """
        unmute_btn = page.get_by_role(
            "button", name=re.compile(r"^unmute", re.IGNORECASE)
        )
        if await unmute_btn.is_visible():
            await unmute_btn.click(timeout=1000)
        elif not await page.get_by_role(
            "button", name=re.compile(r"^mute", re.IGNORECASE)
        ).is_visible():
            msg = "Unmute button not found or not visible."
            raise RuntimeError(msg)

    async def share_screen(self, page: Page) -> None:
        """Start sharing screen in the Teams meeting.

        Clicks the share toolbar button. If Teams opens a share tray with options,
        selects the "Screen" option to trigger getDisplayMedia.

        Args:
            page: The Playwright page instance.
        """
        share_btn = page.get_by_role(
            "button", name=re.compile(r"share\b", re.IGNORECASE)
        )
        if not await share_btn.is_visible():
            msg = "Share button not found or not visible."
            raise RuntimeError(msg)
        await share_btn.click(timeout=2000)
        await page.wait_for_timeout(1000)

        screen_option = page.locator(
            'button:has-text("Screen"), '
            'button:has-text("Entire screen"), '
            '[role="menuitem"]:has-text("Screen"), '
            '[aria-label*="screen" i][role="button"], '
            '[aria-label*="Screen"][role="menuitem"]'
        ).first
        try:
            await screen_option.wait_for(state="visible", timeout=3000)
            await screen_option.click(timeout=2000)
            await page.wait_for_timeout(1000)
        except PlaywrightTimeoutError:
            pass

    async def _open_chat(
        self,
        page: Page,
        *,
        required: bool,
        timeout_ms: int = 15000,
    ) -> bool:
        """Open the Teams chat pane, retrying while the meeting UI settles."""
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        clicked = False

        while asyncio.get_running_loop().time() < deadline:
            if await self._is_chat_ready(page):
                return True

            chat_button = await self._visible_chat_button(page)
            if chat_button is not None:
                with contextlib.suppress(PlaywrightTimeoutError):
                    await chat_button.click(timeout=1500)
                    clicked = True

            await page.wait_for_timeout(750 if clicked else 1000)

        if required:
            msg = f"Chat button not found or not visible after {timeout_ms}ms."
            raise RuntimeError(msg)

        return False

    async def _is_chat_ready(self, page: Page) -> bool:
        """Return true when the chat input or chat pane is visible."""
        chat_input = page.locator(_CHAT_INPUT_SELECTOR).first
        if await chat_input.is_visible():
            return True

        placeholder_input = page.get_by_placeholder(
            re.compile(r"type a message", re.IGNORECASE)
        ).first
        if await placeholder_input.is_visible():
            return True

        return await page.locator(_CHAT_PANE_SELECTOR).first.is_visible()

    async def _visible_chat_button(self, page: Page) -> Locator | None:
        """Return a visible Teams chat button candidate, if one exists."""
        candidates = [
            page.get_by_role(
                "button", name=re.compile(r"chat|conversation", re.IGNORECASE)
            ).first,
            page.locator(_CHAT_BUTTON_SELECTOR).first,
        ]

        for candidate in candidates:
            if await candidate.is_visible():
                return candidate

        return None

    async def stop_sharing(self, page: Page) -> None:
        """Stop sharing screen in the Teams meeting.

        The Share button is a toggle — clicking it again stops sharing.

        Args:
            page: The Playwright page instance.
        """
        share_btn = page.get_by_role(
            "button",
            name=re.compile(r"(share|stop\s+(sharing|presenting))\b", re.IGNORECASE),
        )
        if not await share_btn.first.is_visible():
            msg = "Share button not found or not visible."
            raise RuntimeError(msg)
        await share_btn.first.click(timeout=2000)
        await page.wait_for_timeout(500)

    async def _check_joined(self, page: Page, timeout: float = 90) -> bool:  # noqa: ASYNC109
        """Check if the Teams meeting has been joined successfully.

        Waits for a definitive in-meeting signal (Leave button). Lobby
        indicators are logged but do not count as a successful join because
        chat/participant actions are not reliable before admission.

        Args:
            page: The Playwright page instance.
            timeout: The timeout in seconds for checking the join status.

        Returns:
            bool: True if joined, False otherwise.
        """
        lobby_locators = [
            ("Lobby (please wait)", page.locator("span >> text=/please wait/i")),
            (
                "Lobby (will let you in)",
                page.locator("span >> text=/will let you in/i"),
            ),
            ("Lobby (waiting)", page.locator("span >> text=/waiting/i")),
            (
                "Lobby (someone in meeting)",
                page.locator("span >> text=/someone in the meeting/i"),
            ),
        ]
        leave_button = page.get_by_role(
            "button", name=re.compile(r"leave", re.IGNORECASE)
        )

        logger.info("Waiting up to %s seconds for meeting admission...", timeout)
        deadline = asyncio.get_running_loop().time() + timeout
        last_lobby_marker: str | None = None

        while asyncio.get_running_loop().time() < deadline:
            if await leave_button.first.is_visible():
                logger.info(
                    "Join check succeeded: detected indicator '%s'",
                    "Meeting Room (leave button)",
                )
                return True

            for marker_name, locator in lobby_locators:
                if await locator.first.is_visible():
                    if marker_name != last_lobby_marker:
                        logger.info(
                            "Join check: detected '%s'; still waiting for admission.",
                            marker_name,
                        )
                        last_lobby_marker = marker_name
                    break

            await page.wait_for_timeout(1000)

        logger.warning(
            "Join check timed out: meeting admission indicator not detected in "
            "%s seconds.",
            timeout,
        )
        return False

    async def _setup_active_speaker_observer(self, page: Page) -> None:
        """Setup the active speaker observer for Teams."""
        await page.expose_binding(
            "report",
            lambda _, name: self._state.update({"active_speaker": name}),
        )
        await page.evaluate(
            """
            (nameArg) => {
                const emit = n => window.report(n);
                const find = () => {
                    for (
                        const t of document.querySelectorAll(
                            'div[data-tid="stage-layout"] div[role="menuitem"]'
                        )
                    ) {
                        if (!!t.querySelector(
                            'div[data-tid="voice-level-stream-outline"].vdi-frame-occlusion'
                        )) {
                            let el = t.querySelector(
                                'div[data-tid="participant-info-nametag"]'
                            );
                            if (!el) {
                                el = t.querySelector('div:not(:has(*)):not(:empty)');
                            }
                            const name = el?.textContent.trim();
                            if (name && name.length > 0 && name !== nameArg)
                                return name;
                        }
                    }
                    return null;
                };

                let last = null, cur;
                new MutationObserver(() => {
                    cur = find();
                    if (cur !== last) { last = cur; emit(cur); }
                }).observe(
                    document,
                    {
                        subtree: true,
                        childList: true,
                        attributes: true,
                        attributeFilter: ['class']
                    }
                );
                emit(find());
            }
            """,
            get_settings().name,
        )
