import asyncio
import contextlib
import html as _html_mod
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
    # Scoped to chat pane first — avoids grabbing wrong contenteditable in Teams UI
    "[data-tid='chat-pane'] div[contenteditable='true'], "
    "[data-tid='message-pane'] div[contenteditable='true'], "
    "[data-tid='ckeditor-chatMessageStream'], "
    "div[contenteditable='true'][aria-label*='message' i], "
    "div[aria-label*='Type a message'], "
    "textarea[placeholder*='message']"
)
_CHAT_PANE_SELECTOR = (
    '[data-tid="chat-pane"], '
    '[data-tid="message-pane"], '
    '[data-tid="chat-pane-list"], '
    '[data-tid="message-pane-list"], '
    '[data-tid="conversation-pane"], '
    '[data-tid="calling-chat-panel"], '
    'div[aria-label*="Meeting chat" i], '
    'div[role="region"][aria-label*="chat" i]'
)
_CHAT_BUTTON_SELECTOR = (
    'button[aria-label*="chat" i], '
    'button[aria-label*="conversation" i], '
    'button[data-tid*="chat" i], '
    'button:has-text("Chat")'
)
_CHAT_HISTORY_ITEM_TIMEOUT_MS = 1500
_PARTICIPANT_ROSTER_ITEM_TIMEOUT_MS = 1500
_PARTICIPANT_INFO_RE = re.compile(
    r"\b(muted|unmuted|organizer|presenter|guest|external|left|not\s+in"
    r"\s+(?:the\s+)?meeting|invited|waiting|available|busy|away|offline)\b",
    re.IGNORECASE,
)
_ROSTER_ROW_SELECTOR = (
    "[data-cid='roster-participant'][aria-label], "
    "[data-tid*='participant' i][aria-label], "
    "[data-tid*='roster' i][aria-label], "
    "[role='treeitem'][aria-label], "
    "[role='listitem'][aria-label], "
    "button[aria-label]"
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
        passcode: str | None = None,
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

        # Run after admission is confirmed, not during pre-join — pre-join
        # shares the same CPU-contention window as the join-button click
        # (see the mic-pacing-loop starvation fix), and running it here also
        # covers the gov Teams path, which never called it before.
        await self._ensure_camera_on(page)
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
        # Teams' light meeting page can keep subresources open for a long time,
        # so wait for DOM readiness and then drive the visible controls directly.
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        async def _dismiss_dialog(page: Page) -> None:
            with contextlib.suppress(PlaywrightTimeoutError):
                await page.click('div[role="dialog"] button', timeout=1000)
                logger.info("Dismissed Teams launch dialog.")

        async def _click_join_browser(page: Page) -> None:
            """Click through the Teams launcher page to the web join page."""
            with contextlib.suppress(PlaywrightTimeoutError):
                btn_pattern = re.compile(
                    r"join.*browser|continue.*web|join.*web|continue.*browser",
                    re.IGNORECASE,
                )
                join_browser_btn = page.get_by_role("button", name=btn_pattern)
                await join_browser_btn.click(timeout=3000)
                logger.info("Clicked Teams browser-launch button.")

        logger.info("Teams meeting page loaded; preparing pre-join controls.")
        await _dismiss_dialog(page)
        await _click_join_browser(page)
        await self._fill_name_if_present(page, name)
        await self._click_final_join_button(page)

    async def _fill_name_if_present(self, page: Page, name: str) -> None:
        """Fill the guest name field when Teams shows one."""
        name_field = page.locator(
            'input[placeholder*="name" i], '
            'input[aria-label*="name" i], '
            'input[placeholder="Type your name"]'
        ).first
        try:
            await name_field.fill(name, timeout=5000)
            logger.info("Filled Teams guest name field.")
        except PlaywrightTimeoutError:
            logger.info("Teams guest name field not shown; assuming signed-in profile.")

    async def _ensure_camera_on(self, page: Page) -> None:
        """Turn Teams video on so the virtual Alex camera tile is visible.

        Called after `_check_joined` has already confirmed admission (the
        in-meeting toolbar, not the pre-join screen, is on screen) — keeps
        this off the join-critical-path entirely, since it used to share the
        same CPU-contention window as the join-button click. Uses JavaScript
        to enumerate button elements and click the camera toggle if it looks
        to be in the OFF state.
        """
        # Brief pause for the in-meeting toolbar to finish attaching handlers.
        await page.wait_for_timeout(600)

        clicked: str | None = await page.evaluate("""
            () => {
                const CAMERA_WORDS = ['camera', 'video', 'webcam'];
                const OFF_SIGNALS = [
                    'turn on', 'enable', ' off', 'disabled', 'start',
                    'unmute video',
                ];
                const ON_SIGNALS = ['turn off', 'disable', 'stop', 'mute video'];

                const elems = Array.from(
                    document.querySelectorAll(
                        'button, [role="button"]'
                        + ', [data-tid*="video"], [data-tid*="camera"]'
                    )
                );

                for (const el of elems) {
                    const label = (
                        el.getAttribute('aria-label') ||
                        el.getAttribute('title') ||
                        el.getAttribute('data-tid') ||
                        el.textContent ||
                        ''
                    ).toLowerCase().trim();

                    const isCameraControl = CAMERA_WORDS.some(w => label.includes(w));
                    if (!isCameraControl) continue;

                    // Explicitly on — do NOT click (would turn it off)
                    const isOn =
                        el.getAttribute('aria-pressed') === 'true' ||
                        el.getAttribute('aria-checked') === 'true' ||
                        ON_SIGNALS.some(s => label.includes(s));
                    if (isOn) return 'already_on:' + label;

                    // Explicitly off or unknown state — click to ensure on
                    const isOff =
                        el.getAttribute('aria-pressed') === 'false' ||
                        el.getAttribute('aria-checked') === 'false' ||
                        OFF_SIGNALS.some(s => label.includes(s));

                    if (isOff || isCameraControl) {
                        el.click();
                        return 'clicked:' + label;
                    }
                }
                return null;
            }
        """)

        if clicked is None:
            # Debug dump so we can refine selectors if still not found.
            all_buttons: list[dict[str, str]] = await page.evaluate("""
                () => Array.from(document.querySelectorAll(
                    'button, [role="button"]'
                )).slice(0, 40).map(el => ({
                    ariaLabel: el.getAttribute('aria-label') || '',
                    ariaPressed: el.getAttribute('aria-pressed') || '',
                    dataTid: el.getAttribute('data-tid') || '',
                    title: el.getAttribute('title') || '',
                    text: (el.textContent||'').trim().slice(0,60),
                }))
            """) or []
            logger.info(
                "Teams camera toggle not found in-meeting."
                " Buttons found (%d): %s",
                len(all_buttons),
                all_buttons,
            )
        elif clicked.startswith("already_on:"):
            logger.info(
                "Teams camera already on: %s", clicked[len("already_on:"):]
            )
        else:
            label = clicked[len("clicked:"):]
            logger.info("Turned Teams camera on (JS): %s", label)
            # Brief pause so Teams registers the toggle before Join is clicked.
            await page.wait_for_timeout(800)

    async def _click_final_join_button(self, page: Page) -> None:
        """Click the meeting pre-join button after launcher redirects settle."""
        join_button = page.get_by_role(
            "button",
            name=re.compile(r"^(join now|join)$|join meeting", re.IGNORECASE),
        )
        try:
            await join_button.click(timeout=60000)
            logger.info("Clicked Teams final join button.")
        except PlaywrightTimeoutError:
            fallback_button = page.locator(
                'button:has-text("Join now"), button:has-text("Join")'
            ).first
            # Single-attempt clicks here have been observed to hang under CPU
            # contention (the audio pacing loop runs continuously from
            # container start, competing with Chromium for the same vCPU) —
            # the click is sent but never confirms within the timeout. A
            # short retry recovers without needing a full container respin.
            last_error: PlaywrightTimeoutError | None = None
            for attempt in range(1, 4):
                try:
                    await fallback_button.click(timeout=15000)
                    logger.info(
                        "Clicked Teams final join button using fallback "
                        "selector (attempt %d).",
                        attempt,
                    )
                    return
                except PlaywrightTimeoutError as exc:
                    last_error = exc
                    logger.warning(
                        "Fallback join button click timed out (attempt %d/3).",
                        attempt,
                    )
                    if attempt < 3:
                        await asyncio.sleep(1)
            if last_error is not None:
                raise last_error from None
            msg = "Fallback join button click failed without a timeout error."
            raise RuntimeError(msg)

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
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                await self._send_chat_message_once(page, message)
            except Exception as exc:
                last_error = exc
                logger.warning("Send attempt %s failed; retrying.", attempt)
                if attempt < 3:
                    continue
                raise
            if await self._wait_for_chat_message(page, message):
                logger.info(
                    "Teams chat message verified after send attempt %s.", attempt
                )
                return
            logger.warning(
                "Teams chat message not visible after send attempt %s; retrying.",
                attempt,
            )

        msg = "Teams chat message was not visible after 3 send attempts."
        if last_error is not None:
            raise RuntimeError(msg) from last_error
        raise RuntimeError(msg)

    async def _send_chat_message_once(self, page: Page, message: str) -> None:
        """Send one Teams chat message attempt."""
        # Log URL without query string — query may contain meeting passcode.
        _url = page.url.split("?")[0]
        logger.info("send_chat_message: page URL (no query) = %s", _url)
        await self._open_chat(page, required=True, timeout_ms=25000)

        # Prefer in-meeting scoped inputs — avoids typing into Teams sidebar chat
        # (which may belong to a different conversation or previous meeting).
        chat_input = page.locator(
            "[data-tid='chat-pane'] div[contenteditable='true'], "
            "[data-tid='message-pane'] div[contenteditable='true'], "
            "[data-tid='ckeditor-chatMessageStream']"
        ).first
        if not await chat_input.is_visible():
            # Fallback: broader selectors covering Teams v2.
            chat_input = page.locator(
                "div[contenteditable='true'][aria-label*='message' i], "
                "div[contenteditable='true'][data-placeholder*='message' i], "
                "div[contenteditable='true'][aria-placeholder*='message' i], "
                "div[aria-label*='Type a message'], "
                "textarea[placeholder*='message']"
            ).first
            if not await chat_input.is_visible():
                placeholder_input = page.get_by_placeholder(
                    re.compile(r"Type a message|New message", re.IGNORECASE)
                ).first
                if await placeholder_input.is_visible():
                    chat_input = placeholder_input
                else:
                    msg = "Chat input not found or not visible."
                    raise RuntimeError(msg)

        await chat_input.click(timeout=2000)
        # Clear any existing content, then prefer HTML insertion so Teams stores
        # Markdown **bold** markers as actual rich-text formatting.
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Backspace")
        html_content = self._markdown_to_html(message)
        inserted_html = await self._try_html_paste(page, chat_input, html_content)
        if not inserted_html:
            await self._type_with_bold_formatting(page, message)
        await page.wait_for_timeout(500)
        send_button = page.locator(
            'button[aria-label="Send"], '
            'button[aria-label="Send (Ctrl+Enter)"], '
            'button[aria-label*="Send" i], '
            'button[title="Send"], '
            'button[title*="Send" i], '
            'button[data-tid="newMessageCommands-send"], '
            'button[data-tid*="send" i]'
        ).first
        if await send_button.is_visible():
            await send_button.click(timeout=2000)
        else:
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(1500)

    async def _wait_for_chat_message(self, page: Page, message: str) -> bool:
        """Return true once Teams chat history visibly contains the message.

        Uses only the first 80 chars as a verification token — Teams DOM does
        not render the full text of long messages so a full-text check always
        fails for summaries and other large payloads.
        """
        token = self._chat_verification_token(message)
        deadline = asyncio.get_running_loop().time() + 5

        while asyncio.get_running_loop().time() < deadline:
            history = await self.get_chat_history(page)
            for chat_message in history.messages:
                if token in self._normalize_chat_text(chat_message.text):
                    return True
            await page.wait_for_timeout(1000)

        return False

    def _chat_verification_token(self, message: str) -> str:
        """Return the short visible token Teams should show after rich-text send."""
        rendered_text = re.sub(r"\*\*(.*?)\*\*", r"\1", message, flags=re.DOTALL)
        return self._normalize_chat_text(rendered_text[:80])

    def _normalize_chat_text(self, text: str) -> str:
        """Normalize Teams-rendered chat text for send verification."""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    async def _type_with_bold_formatting(page: Page, message: str) -> None:
        """Type a message into the Teams chat composer with bold formatting.

        Splits the message on **bold** markers and uses Ctrl+B to toggle bold
        in the Teams contenteditable composer for each bold segment. Plain
        segments are typed with insert_text. Newlines are sent as Shift+Enter
        to stay within the same message (Enter alone would send it).

        Args:
            page: The Playwright page instance.
            message: The message text, optionally containing **bold** spans.
        """
        # Split on **...** markers — odd-indexed parts are bold segments.
        parts = re.split(r"\*\*", message)
        for idx, part in enumerate(parts):
            if not part:
                continue
            is_bold = (idx % 2) == 1
            if is_bold:
                await page.keyboard.press("Control+b")
            # Type each line separately, using Shift+Enter for newlines
            # (plain Enter would submit the message prematurely).
            lines = part.split("\n")
            for line_idx, line in enumerate(lines):
                if line:
                    await page.keyboard.insert_text(line)
                if line_idx < len(lines) - 1:
                    await page.keyboard.press("Shift+Enter")
            if is_bold:
                await page.keyboard.press("Control+b")



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
            try:
                if await content_el.is_visible(
                    timeout=_CHAT_HISTORY_ITEM_TIMEOUT_MS
                ):
                    text = (
                        await content_el.inner_text(
                            timeout=_CHAT_HISTORY_ITEM_TIMEOUT_MS
                        )
                    ).strip()
                else:
                    text = (
                        await el.inner_text(timeout=_CHAT_HISTORY_ITEM_TIMEOUT_MS)
                    ).strip()
            except PlaywrightTimeoutError:
                logger.debug("Skipping Teams chat item whose text timed out.")
                continue

            if not text:
                continue

            ts_el = el.locator("time[datetime]").first
            ts = None
            with contextlib.suppress(PlaywrightTimeoutError):
                if await ts_el.is_visible(timeout=_CHAT_HISTORY_ITEM_TIMEOUT_MS):
                    ts = await ts_el.get_attribute(
                        "datetime",
                        timeout=_CHAT_HISTORY_ITEM_TIMEOUT_MS,
                    )

            author_locator = el.locator('[data-tid="message-author-name"]').first
            sender = None
            with contextlib.suppress(PlaywrightTimeoutError):
                if await author_locator.is_visible(
                    timeout=_CHAT_HISTORY_ITEM_TIMEOUT_MS
                ):
                    sender_text = await author_locator.text_content(
                        timeout=_CHAT_HISTORY_ITEM_TIMEOUT_MS
                    ) or ""
                    sender = sender_text.strip() or None

            messages.append(MeetingChatMessage(text=text, timestamp=ts, sender=sender))

        return MeetingChatHistory(messages=messages)

    async def open_chat_panel(self, page: Page) -> None:
        """Ensure the in-meeting chat panel is open.

        Retries with a 30-second budget.  Raises RuntimeError when the panel
        cannot be opened (e.g. meeting has not yet started or the UI has not
        settled).

        Args:
            page: The Playwright page instance.
        """
        await self._open_chat(page, required=True, timeout_ms=30000)

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:
        """Get the list of participants in the Teams meeting.

        Args:
            page: The Playwright page instance.

        Returns:
            list[MeetingParticipant]: A list of participants in the meeting.
        """
        participants_list = self._participants_list(page)
        is_participant_list_visible = await participants_list.is_visible()

        if not is_participant_list_visible:
            participants_button = await self._visible_participants_button(page)
            if participants_button is None:
                await self._maximize_meeting_window(page)
                participants_button = await self._visible_participants_button(page)
            if participants_button is None:
                msg = "Teams participants button not found or not visible."
                raise RuntimeError(msg)
            await participants_button.click()
            await page.wait_for_timeout(1000)
            if not await participants_list.is_visible():
                await page.wait_for_timeout(1000)

        participants: list[MeetingParticipant] = []
        for item in await participants_list.locator(_ROSTER_ROW_SELECTOR).all():
            try:
                if not await item.is_visible(
                    timeout=_PARTICIPANT_ROSTER_ITEM_TIMEOUT_MS
                ):
                    continue
            except PlaywrightTimeoutError:
                continue
            participant = await self._participant_from_roster_item(item)
            if participant is not None:
                participants.append(participant)

        if not participants:
            for item in await page.locator(_ROSTER_ROW_SELECTOR).all():
                try:
                    if not await item.is_visible(
                        timeout=_PARTICIPANT_ROSTER_ITEM_TIMEOUT_MS
                    ):
                        continue
                except PlaywrightTimeoutError:
                    continue
                participant = await self._participant_from_roster_item(item)
                if participant is not None:
                    participants.append(participant)

        participants = self._dedupe_participants(participants)
        if not participants:
            if await self._is_only_participant(page):
                return [
                    MeetingParticipant(
                        name=get_settings().name,
                        infos=["Teams compact view reports only participant"],
                    )
                ]
            debug_labels = await self._participant_roster_debug(page)
            logger.warning(
                "Teams participant roster returned no parseable rows. Debug labels: %s",
                debug_labels,
            )

        return participants

    @staticmethod
    async def _participant_from_roster_item(
        item: Locator,
    ) -> MeetingParticipant | None:
        """Parse a Teams roster DOM item into a participant, if possible."""
        try:
            aria_label = (
                await item.get_attribute(
                    "aria-label",
                    timeout=_PARTICIPANT_ROSTER_ITEM_TIMEOUT_MS,
                )
            ) or ""
        except PlaywrightTimeoutError:
            return None

        participant = TeamsBrowserPlatformController._participant_from_label(
            aria_label,
        )
        if participant is not None:
            return participant

        try:
            text = (
                await item.text_content(timeout=_PARTICIPANT_ROSTER_ITEM_TIMEOUT_MS)
            ) or ""
        except PlaywrightTimeoutError:
            return None

        return TeamsBrowserPlatformController._participant_from_label(text)

    @staticmethod
    def _participant_from_label(label: str) -> MeetingParticipant | None:
        """Parse Teams roster aria/text labels like 'Name, Organizer, Unmuted'."""
        normalized = re.sub(r"\s+", " ", label).strip()
        if not normalized or not _PARTICIPANT_INFO_RE.search(normalized):
            return None

        parts = [part.strip() for part in re.split(r",|\n", normalized) if part.strip()]
        if len(parts) < 2:
            return None

        name = parts[0]
        if re.search(r"\b(show|hide|open|close|view|people|participants)\b", name, re.IGNORECASE):
            return None

        infos = [
            part
            for part in parts[1:]
            if not re.search(r"\b(more options|profile card|view profile)\b", part, re.IGNORECASE)
        ]
        if not infos:
            return None

        return MeetingParticipant(name=name, infos=infos)

    @staticmethod
    def _dedupe_participants(
        participants: list[MeetingParticipant],
    ) -> list[MeetingParticipant]:
        """De-duplicate Teams roster rows by display name."""
        deduped: list[MeetingParticipant] = []
        seen: set[str] = set()
        for participant in participants:
            key = participant.name.casefold().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(participant)
        return deduped

    @staticmethod
    async def _participant_roster_debug(page: Page) -> list[str]:
        """Return a small set of visible roster-like labels for diagnostics."""
        return await page.evaluate("""
            () => Array.from(document.querySelectorAll('[aria-label]'))
                .filter((el) => {
                    const r = el.getBoundingClientRect();
                    const label = el.getAttribute('aria-label') || '';
                    return r.width > 0 && r.height > 0
                        && /participant|people|attendee|muted|unmuted|organizer|presenter/i.test(label);
                })
                .map((el) => el.getAttribute('aria-label'))
                .filter(Boolean)
                .slice(0, 30)
        """)

    def _participants_list(self, page: Page) -> Locator:
        """Return the currently supported Teams participant roster locator."""
        return page.locator(
            'div[aria-label="Attendees"][role="tree"], '
            '[role="tree"][aria-label*="attendees" i], '
            '[role="tree"][aria-label*="participants" i], '
            '[role="list"][aria-label*="attendees" i], '
            '[role="list"][aria-label*="participants" i]'
        ).first

    async def _visible_participants_button(self, page: Page) -> Locator | None:
        """Return a visible Teams participants button, if one exists."""
        candidates = [
            page.get_by_role(
                "button",
                name=re.compile(
                    r"people|participants|attendees",
                    re.IGNORECASE,
                ),
            ).first,
            page.locator(
                'button:has-text("People"), '
                'button:has-text("Participants"), '
                'button[aria-label*="People" i], '
                'button[aria-label*="Participants" i], '
                'button[aria-label*="Attendees" i], '
                'button[aria-label*="Show participants" i], '
                'button[aria-label*="View participants" i]'
            ).first,
        ]

        for candidate in candidates:
            if await candidate.is_visible():
                return candidate

        return None

    async def _is_only_participant(self, page: Page) -> bool:
        """Return true when Teams compact view says the bot is alone."""
        body_text = await page.locator("body").inner_text(timeout=1000)
        normalized = re.sub(r"\s+", " ", body_text).casefold()
        return (
            "you are the only one here" in normalized
            or "waiting for others to join" in normalized
        )

    async def _maximize_meeting_window(self, page: Page) -> None:
        """Expand Teams compact meeting view so meeting controls are available."""
        candidates = [
            page.get_by_role(
                "button",
                name=re.compile(r"maximize meeting window", re.IGNORECASE),
            ).first,
            page.locator(
                'button[aria-label*="Maximize meeting window" i], '
                'button[title*="Maximize meeting window" i], '
                '[role="button"][aria-label*="Maximize meeting window" i]'
            ).first,
        ]

        for maximize_button in candidates:
            if await maximize_button.is_visible():
                await maximize_button.click(timeout=2000)
                await page.wait_for_timeout(1500)
                return

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

    @staticmethod
    def _markdown_to_html(text: str) -> str:
        """Convert markdown bold (**text**) to HTML <b>text</b>.

        Escapes HTML special characters first to prevent XSS injection,
        then converts **bold** markers and newlines to <br> tags.
        """
        escaped = _html_mod.escape(text)
        bolded = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)
        return bolded.replace("\n", "<br>")

    async def _try_html_paste(
        self,
        page: Page,
        chat_input: Locator,
        html_content: str,
    ) -> bool:
        """Insert HTML into a contenteditable via execCommand('insertHTML').

        Returns True when the command reports success, False otherwise.
        Falls back silently so callers can try plain-text instead.
        """
        try:
            await chat_input.click(timeout=2000)
            success: bool = await page.evaluate(
                "(html) => document.execCommand('insertHTML', false, html)",
                html_content,
            )
            await page.wait_for_timeout(200)
            return bool(success)
        except Exception:  # noqa: BLE001
            return False

    async def share_screen(self, page: Page) -> None:
        """Start sharing screen in the Teams meeting.

        Clicks the share toolbar button. If Teams opens a share tray with options,
        selects the "Screen" option to trigger getDisplayMedia.

        Args:
            page: The Playwright page instance.
        """
        # Ensure meeting is not in compact mode so the toolbar is visible.
        await self._maximize_meeting_window(page)
        await page.wait_for_timeout(500)

        share_btn = page.get_by_role(
            "button", name=re.compile(r"share\b", re.IGNORECASE)
        ).first
        if not await share_btn.is_visible():
            msg = "Share button not found or not visible."
            raise RuntimeError(msg)
        label: str = await share_btn.get_attribute("aria-label") or "share"
        logger.info("Clicking Teams share button: '%s'", label)
        await share_btn.click(timeout=5000)
        await page.wait_for_timeout(1000)

        screen_option = page.locator(
            'button:has-text("Screen"), '
            'button:has-text("Entire screen"), '
            'button:has-text("Desktop"), '
            'button:has-text("Browser tab"), '
            '[role="menuitem"]:has-text("Screen"), '
            '[role="option"]:has-text("Screen"), '
            '[role="option"]:has-text("Desktop"), '
            '[aria-label*="screen" i][role="button"], '
            '[aria-label*="Screen"][role="menuitem"], '
            '[aria-label*="desktop" i]'
        ).first
        try:
            await screen_option.wait_for(state="visible", timeout=4000)
            label_txt: str = await screen_option.inner_text() or ""
            logger.info("Clicking Teams share option: '%s'", label_txt.strip()[:40])
            await screen_option.click(timeout=2000)
            await page.wait_for_timeout(1000)
        except PlaywrightTimeoutError:
            # Dump ALL text-bearing elements in the page to find the tray structure
            try:
                all_elems: list[dict[str, str]] = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('*')).filter(el => {
                        const t = (el.textContent||'').trim();
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && t.length > 0 && t.length < 80
                            && ['BUTTON','LI','DIV','SPAN','A'].includes(el.tagName)
                            && !el.children.length;
                    }).map(el => ({
                        tag: el.tagName,
                        text: (el.textContent||'').trim().slice(0,60),
                        aria: el.getAttribute('aria-label')||'',
                        role: el.getAttribute('role')||'',
                        cls: el.className.toString().slice(0,40),
                    })).filter(e =>
                        /screen|desktop|window|tab|share|present/i.test(e.text + e.aria)
                    ).slice(0, 20)
                """) or []
                logger.info(
                    "Teams share tray: no Screen option found. "
                    "Matching elements: %s",
                    all_elems,
                )
                await page.screenshot(path="/workspace/share_tray_debug2.png")
                logger.info("Screenshot2 saved")
            except Exception as dbg_e:  # noqa: BLE001
                logger.debug("Debug dump2 failed: %s", dbg_e)

    # Teams keyboard shortcuts to toggle in-meeting chat — tried in order.
    # Ctrl+Shift+Z = classic Teams; Ctrl+Shift+C and Ctrl+3 = Teams v2 variants.
    _CHAT_KBD_SHORTCUTS: ClassVar[list[str]] = [
        "Control+Shift+Z",
        "Control+Shift+C",
        "Control+3",
    ]

    async def _open_chat(
        self,
        page: Page,
        *,
        required: bool,
        timeout_ms: int = 20000,
    ) -> bool:
        """Open the Teams chat pane, retrying while the meeting UI settles."""
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        # Cooldown prevents rapid click → visible-check → click again which
        # would toggle the panel closed before we can detect it as open.
        btn_last_clicked = 0.0
        btn_click_cooldown = 4.0  # seconds
        kbd_idx = 0

        while asyncio.get_running_loop().time() < deadline:
            # Re-attempt maximize on every iteration: Teams can re-collapse to
            # compact view, hiding the Chat toolbar button entirely.
            await self._maximize_meeting_window(page)

            if await self._is_chat_visible(page):
                return True

            now = asyncio.get_running_loop().time()
            chat_button = await self._visible_chat_button(page)
            if (
                chat_button is not None
                and (now - btn_last_clicked) >= btn_click_cooldown
            ):
                with contextlib.suppress(PlaywrightTimeoutError):
                    await chat_button.click(timeout=1500)
                    btn_last_clicked = now
            elif kbd_idx < len(self._CHAT_KBD_SHORTCUTS):
                await page.keyboard.press(self._CHAT_KBD_SHORTCUTS[kbd_idx])
                kbd_idx += 1

            await page.wait_for_timeout(1500)

        if required:
            msg = f"Chat pane not opened after {timeout_ms}ms (tried {kbd_idx} keyboard shortcuts)."
            raise RuntimeError(msg)

        return False

    async def _is_chat_visible(self, page: Page) -> bool:
        """Return true when the IN-MEETING chat input or chat pane is visible.

        Checks classic Teams data-tid selectors first, then Teams v2 indicators
        (aria-pressed on the Chat toolbar button, or a contenteditable input
        with message-related attributes).
        """
        # --- Classic Teams: data-tid scoped chat pane / input ---
        meeting_input = page.locator(
            "[data-tid='chat-pane'] div[contenteditable='true'], "
            "[data-tid='message-pane'] div[contenteditable='true'], "
            "[data-tid='ckeditor-chatMessageStream']"
        ).first
        if await meeting_input.is_visible():
            return True

        if await page.locator(_CHAT_PANE_SELECTOR).first.is_visible():
            return True

        # --- Teams v2: the meeting Chat toolbar button sets aria-pressed=true
        # (or aria-expanded=true) while the chat panel is open ---
        if await page.locator(
            'button[aria-label="Chat"][aria-pressed="true"], '
            'button[aria-label="Chat"][aria-expanded="true"]'
        ).first.is_visible():
            return True

        # --- Teams v2: the chat input is a contenteditable whose aria-label
        # or placeholder text includes "message" ---
        return await page.locator(
            "div[contenteditable='true'][aria-label*='message' i], "
            "div[contenteditable='true'][data-placeholder*='message' i], "
            "div[contenteditable='true'][aria-placeholder*='message' i]"
        ).first.is_visible()

    async def _visible_chat_button(self, page: Page) -> Locator | None:
        """Return a visible Teams chat button candidate, if one exists.

        Prioritises in-meeting call-controls chat buttons so we open the
        meeting chat, not the Teams app sidebar chat (which may be a different
        conversation or a previous meeting's chat).
        """
        # In-call controls bar first — these are meeting-specific
        in_call_candidates = [
            page.locator('[data-tid="call-chat-button"]').first,
            page.locator('[data-tid="chat-calling-button"]').first,
            page.locator('[data-tid="callingButtons-showConversation"]').first,
            page.locator('[data-tid="toggle-chat"]').first,
            page.locator(
                '[data-tid="calling-controls-bar"] button[aria-label*="chat" i], '
                '[data-tid="calling-toolbar"] button[aria-label*="chat" i], '
                '[data-tid="meeting-toolbar"] button[aria-label*="chat" i], '
                'button[aria-label*="Show conversation" i], '
                'button[title*="chat" i], '
                'button[title*="conversation" i]'
            ).first,
        ]
        for candidate in in_call_candidates:
            if await candidate.is_visible():
                logger.debug("Found in-call chat button via data-tid.")
                return candidate

        # Generic fallback — may match app sidebar; acceptable if no in-call button found
        candidates = [
            # Exact aria-label first — avoids the Teams sidebar
            # "Chat (Ctrl+Shift+2)" tab which also matches substring selectors.
            page.locator(
                'button[aria-label="Chat"], '
                'button[aria-label="Show conversation"]'
            ).first,
            page.get_by_role(
                "button",
                name=re.compile(r"^(chat|show conversation)$", re.IGNORECASE),
            ).first,
            page.locator('button:has-text("Chat")').first,
            page.locator(_CHAT_BUTTON_SELECTOR).first,
        ]
        for candidate in candidates:
            if await candidate.is_visible():
                return candidate

        # Last resort: Teams v2 collapses Chat into the "More" overflow menu
        # when the meeting toolbar doesn't have enough room.
        # Check first in case the menu is already expanded from a prior iteration
        # (clicking "More" a second time would close the menu).
        chat_item = page.locator(
            '[role="menuitem"]:has-text("Chat"), '
            '[role="menuitem"][aria-label*="chat" i], '
            'li[role="menuitem"]:has-text("Chat")'
        ).first
        if await chat_item.is_visible():
            logger.debug("Found Chat inside More overflow menu (already expanded).")
            return chat_item

        more_btn = page.locator('button[aria-label="More"]').first
        if await more_btn.is_visible():
            with contextlib.suppress(PlaywrightTimeoutError):
                await more_btn.click(timeout=1500)
                await page.wait_for_timeout(400)
            if await chat_item.is_visible():
                logger.debug("Found Chat inside More overflow menu.")
                return chat_item

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

    async def _check_joined(self, page: Page, timeout: float = 90) -> bool:
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
