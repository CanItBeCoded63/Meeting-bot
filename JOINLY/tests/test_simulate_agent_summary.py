"""Simulate the agent posting a meeting summary with bold formatting.

Spins up a headless Chromium with a Teams-like chat mockup, fires the same
_markdown_to_html + _try_html_paste pipeline the real Teams provider uses,
captures a screenshot, and asserts that every bold section/name is rendered
as an actual <b> element — not as literal asterisks.

Run standalone:
    uv run pytest tests/test_simulate_agent_summary.py -v -s
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright

from joinly.providers.browser.platforms.teams import TeamsBrowserPlatformController

# ── Realistic meeting summary identical in structure to what the LLM produces ──
SAMPLE_SUMMARY = """\
**📋 Meeting Summary (23 min)**

**OVERVIEW**
The team reviewed the Joinly orchestration layer, finalized the calendar\
 integration approach, and aligned on the memory isolation strategy for\
 multi-project meetings.

**KEY DECISIONS**
- The orchestration layer will be built on top of FastMCP with per-session\
 ContextVars for isolation.
- Calendar integration will use the Google Calendar API for the first release.
- Memory will be scoped per meeting series (link) and per occurrence.

**ACTION ITEMS**
- **Yashwardhan Singh Chouhan** to wire up the FastMCP session container by\
 Friday.
- **Aria** to draft the Google Calendar OAuth flow. Owner: **Aria**.\
 Deadline: next Monday.
- **Sam** to document the memory isolation spec. Owner: **Sam**.

**FOLLOW-UPS**
- **Yash** to decide whether Zoom support should land in the same sprint.
- Open question: should multi-agent meetings share a single memory namespace\
 or keep separate ones?
"""

# Where to save the screenshot (project root)
SCREENSHOT_PATH = Path(__file__).parent.parent / "simulation_bold_summary.png"

# ── Minimal Teams-like chat HTML mockup ────────────────────────────────────
_CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Teams Chat Mockup</title>
<style>
  body { font-family: 'Segoe UI', sans-serif; background:#f3f2f1; margin:0; }
  .chat-panel {
    width: 640px; margin: 20px auto; background: #fff;
    border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.15);
    display: flex; flex-direction: column; height: 520px;
  }
  .chat-header {
    background: #6264a7; color: #fff; padding: 12px 16px;
    font-weight: 600; border-radius: 8px 8px 0 0; font-size: 14px;
  }
  .chat-history {
    flex: 1; overflow-y: auto; padding: 12px 16px; display: flex;
    flex-direction: column; gap: 8px;
  }
  .chat-bubble {
    background: #f5f5f5; border-radius: 6px;
    padding: 8px 12px; font-size: 13px; line-height: 1.5;
    max-width: 90%; border: 1px solid #e0e0e0;
  }
  .chat-bubble .sender { font-weight: 600; color: #6264a7; font-size: 12px;
    margin-bottom: 4px; }
  .composer-bar {
    border-top: 1px solid #e0e0e0; padding: 8px 12px;
    display: flex; align-items: flex-start; gap: 8px;
  }
  #editor {
    flex: 1; min-height: 60px; border: 1px solid #c8c6c4;
    border-radius: 4px; padding: 6px 8px; font-size: 13px;
    font-family: inherit; outline: none; line-height: 1.5;
  }
  #editor b { font-weight: 700; }
  #send-btn {
    background: #6264a7; color: #fff; border: none; border-radius: 4px;
    padding: 6px 14px; font-size: 13px; cursor: pointer; margin-top: 2px;
  }
  #send-btn:hover { background: #4f52a0; }
</style>
</head>
<body>
<div class="chat-panel">
  <div class="chat-header">💬 Joinly Meeting Chat</div>
  <div id="history" class="chat-history">
    <div class="chat-bubble">
      <div class="sender">Yash</div>
      Alex, please post the meeting summary.
    </div>
  </div>
  <div class="composer-bar">
    <div id="editor" contenteditable="true"
         aria-label="Type a message"
         data-placeholder="Type a message…"></div>
    <button id="send-btn">Send</button>
  </div>
</div>
<script>
  document.getElementById('send-btn').addEventListener('click', () => {
    const editor = document.getElementById('editor');
    const html = editor.innerHTML;
    if (!html.trim()) return;
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    bubble.innerHTML = '<div class="sender">Alex (Bot)</div>' + html;
    document.getElementById('history').appendChild(bubble);
    editor.innerHTML = '';
    bubble.scrollIntoView({ behavior: 'smooth' });
  });
</script>
</body>
</html>"""


async def _simulate_send(page: Page, controller: TeamsBrowserPlatformController,
                         message: str) -> None:
    """Paste the message into the editor and click Send, mirroring agent flow."""
    chat_input = page.locator("#editor")
    await chat_input.click()
    # Clear
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Backspace")

    html_content = controller._markdown_to_html(message)
    pasted = await controller._try_html_paste(page, chat_input, html_content)
    if not pasted:
        await page.keyboard.insert_text(message)

    # Click the Send button (mirrors Teams' send button click)
    await page.locator("#send-btn").click()
    await page.wait_for_timeout(400)


@pytest.mark.asyncio
async def test_simulate_agent_sends_bold_summary(tmp_path: Path) -> None:
    """Full end-to-end simulation: agent pipeline → chat mockup → bold verified."""
    controller = TeamsBrowserPlatformController()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 700, "height": 600})
        await page.set_content(_CHAT_HTML)

        # ── Simulate the agent sending the meeting summary ─────────────────
        await _simulate_send(page, controller, SAMPLE_SUMMARY)

        # ── Screenshot: save to project root for visual inspection ─────────
        screenshot_dest = SCREENSHOT_PATH
        await page.screenshot(path=str(screenshot_dest), full_page=False)
        print(f"\n📸 Screenshot saved → {screenshot_dest}")

        # ── Assert bold elements in the rendered chat bubble ───────────────
        sent_bubble = page.locator(".chat-bubble").nth(1)  # first is Yash's

        bold_elements = await sent_bubble.locator("b").all()
        bold_texts = [await b.inner_text() for b in bold_elements]

        print(f"   Bold elements found ({len(bold_elements)}): {bold_texts}")

        # Required bold items
        required_bold = [
            "📋 Meeting Summary (23 min)",
            "OVERVIEW",
            "KEY DECISIONS",
            "ACTION ITEMS",
            "FOLLOW-UPS",
            "Yashwardhan Singh Chouhan",
            "Aria",
            "Sam",
            "Yash",
        ]
        for expected in required_bold:
            assert expected in bold_texts, (
                f"'{expected}' should be bold but was not found in {bold_texts}"
            )

        # Make sure NO raw asterisks survived in the rendered bubble text
        bubble_text = await sent_bubble.inner_text()
        raw_asterisks = re.findall(r"\*\*[^*]+\*\*", bubble_text)
        assert not raw_asterisks, (
            f"Literal **asterisks** found in rendered output: {raw_asterisks}"
        )

        print(f"   ✓ All {len(required_bold)} bold items confirmed.")
        print("   ✓ No raw **asterisks** in rendered output.")

        await browser.close()
