"""Tests for Teams bold formatting via HTML clipboard paste.

Covers:
  - _markdown_to_html(): pure conversion unit tests
  - _try_html_paste(): Playwright-based integration test that verifies the
    execCommand-copy/Ctrl+V pipeline inserts proper <b> elements into a
    contenteditable div.
"""

import re

import pytest
from playwright.async_api import async_playwright

from joinly.providers.browser.platforms.teams import TeamsBrowserPlatformController


# ---------------------------------------------------------------------------
# Unit tests – _markdown_to_html
# ---------------------------------------------------------------------------


class TestMarkdownToHtml:
    """Pure unit tests for the markdown → HTML conversion helper."""

    def test_bold_section_header(self) -> None:
        result = TeamsBrowserPlatformController._markdown_to_html("**OVERVIEW**")
        assert "<b>OVERVIEW</b>" in result

    def test_bold_name_in_action_item(self) -> None:
        result = TeamsBrowserPlatformController._markdown_to_html(
            "- **Alice** to review the document."
        )
        assert "<b>Alice</b>" in result
        assert "to review the document." in result

    def test_multiple_bold_sections(self) -> None:
        text = "**OVERVIEW**\n**KEY DECISIONS**\n**ACTION ITEMS**"
        result = TeamsBrowserPlatformController._markdown_to_html(text)
        assert result.count("<b>") == 3
        assert "<b>OVERVIEW</b>" in result
        assert "<b>KEY DECISIONS</b>" in result
        assert "<b>ACTION ITEMS</b>" in result

    def test_newlines_become_br(self) -> None:
        result = TeamsBrowserPlatformController._markdown_to_html("line1\nline2")
        assert "<br>" in result
        assert "line1" in result
        assert "line2" in result

    def test_plain_text_passthrough(self) -> None:
        result = TeamsBrowserPlatformController._markdown_to_html("No bold here")
        assert "<b>" not in result
        assert "No bold here" in result

    def test_html_special_chars_escaped(self) -> None:
        """HTML injection via message content must be neutralised."""
        result = TeamsBrowserPlatformController._markdown_to_html(
            "**Alert** <script>alert('xss')</script>"
        )
        assert "<b>Alert</b>" in result
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_emoji_title_bold(self) -> None:
        result = TeamsBrowserPlatformController._markdown_to_html(
            "**📋 Meeting Summary (5 min)**"
        )
        assert "<b>📋 Meeting Summary (5 min)</b>" in result

    def test_full_summary_structure(self) -> None:
        summary = (
            "**📋 Meeting Summary (5 min)**\n\n"
            "**OVERVIEW**\n"
            "The meeting ran for 5 minutes.\n\n"
            "**KEY DECISIONS**\n"
            "None\n\n"
            "**ACTION ITEMS**\n"
            "- **Yash** to follow up on the pipeline.\n\n"
            "**FOLLOW-UPS**\n"
            "None"
        )
        result = TeamsBrowserPlatformController._markdown_to_html(summary)
        bold_tags = re.findall(r"<b>(.+?)</b>", result, flags=re.DOTALL)
        assert "📋 Meeting Summary (5 min)" in bold_tags
        assert "OVERVIEW" in bold_tags
        assert "KEY DECISIONS" in bold_tags
        assert "ACTION ITEMS" in bold_tags
        assert "FOLLOW-UPS" in bold_tags
        assert "Yash" in bold_tags


class TestTeamsChatSendVerification:
    """Unit tests for Teams chat send verification."""

    def test_verification_token_uses_rendered_bold_text(self) -> None:
        controller = TeamsBrowserPlatformController()

        token = controller._chat_verification_token(
            "**FORMAT TEST SUMMARY**\n\n**PROJECTS DISCUSSED**\n- Anti-Gravity"
        )

        assert token == "FORMAT TEST SUMMARY PROJECTS DISCUSSED - Anti-Gravity"

    @pytest.mark.asyncio
    async def test_send_chat_message_retries_when_message_not_visible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller = TeamsBrowserPlatformController()
        attempts: list[str] = []

        async def fake_send(_page: object, message: str) -> None:
            attempts.append(message)

        async def fake_wait(_page: object, _message: str) -> bool:
            return len(attempts) == 2

        monkeypatch.setattr(controller, "_send_chat_message_once", fake_send)
        monkeypatch.setattr(controller, "_wait_for_chat_message", fake_wait)

        await controller.send_chat_message(object(), "summary")  # type: ignore[arg-type]

        assert attempts == ["summary", "summary"]

    @pytest.mark.asyncio
    async def test_send_chat_message_raises_when_message_never_visible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller = TeamsBrowserPlatformController()
        attempts: list[str] = []

        async def fake_send(_page: object, message: str) -> None:
            attempts.append(message)

        async def fake_wait(_page: object, _message: str) -> bool:
            return False

        monkeypatch.setattr(controller, "_send_chat_message_once", fake_send)
        monkeypatch.setattr(controller, "_wait_for_chat_message", fake_wait)

        with pytest.raises(RuntimeError, match="not visible after 3 send attempts"):
            await controller.send_chat_message(object(), "summary")  # type: ignore[arg-type]

        assert attempts == ["summary", "summary", "summary"]


# ---------------------------------------------------------------------------
# Playwright integration test – _try_html_paste
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_paste_renders_bold_in_contenteditable() -> None:
    """HTML clipboard paste should insert <b> elements into a contenteditable.

    This test exercises the full _try_html_paste pipeline without requiring a
    real Teams meeting: it spins up a headless Chromium, creates a simple
    contenteditable div, and verifies that pasting the HTML-converted summary
    produces actual bold elements in the DOM.
    """
    controller = TeamsBrowserPlatformController()

    summary = (
        "**📋 Meeting Summary**\n\n"
        "**OVERVIEW**\n"
        "Test meeting content.\n\n"
        "**ACTION ITEMS**\n"
        "- **Alice** to deploy the service.\n"
        "- **Bob** to review the PR."
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.set_content(
            """
            <html>
            <body>
              <div id="editor"
                   contenteditable="true"
                   style="width:600px;height:200px;border:1px solid #ccc;padding:4px">
              </div>
            </body>
            </html>
            """
        )

        html_content = controller._markdown_to_html(summary)
        chat_input = page.locator("#editor")

        pasted = await controller._try_html_paste(page, chat_input, html_content)
        assert pasted, "_try_html_paste should return True on success"

        # Check that the <b> elements were inserted into the editor
        bold_elements = await page.locator("#editor b").all()
        bold_texts = [await b.inner_text() for b in bold_elements]

        assert len(bold_elements) >= 4, (
            f"Expected at least 4 bold elements (title + 3 headers + names), "
            f"got {len(bold_elements)}: {bold_texts}"
        )
        assert "📋 Meeting Summary" in bold_texts, f"Title not bold. Got: {bold_texts}"
        assert "OVERVIEW" in bold_texts, f"OVERVIEW header not bold. Got: {bold_texts}"
        assert "ACTION ITEMS" in bold_texts, f"ACTION ITEMS not bold. Got: {bold_texts}"
        assert "Alice" in bold_texts, f"Name 'Alice' not bold. Got: {bold_texts}"
        assert "Bob" in bold_texts, f"Name 'Bob' not bold. Got: {bold_texts}"

        await browser.close()
