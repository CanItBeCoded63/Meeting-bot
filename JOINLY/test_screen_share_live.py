"""Quick live screen-share test: connects to the Joinly MCP server running
inside the Docker container via its HTTP port (8000) and calls share_screen
then stop_sharing.

Usage (run from host while joinly-alex container is running):
    uv run python test_screen_share_live.py
"""

import asyncio

from fastmcp import Client

JOINLY_URL = "http://localhost:8000/mcp"
SHARE_URL = "https://en.wikipedia.org/wiki/Python_(programming_language)"


async def main() -> None:
    print(f"Connecting to Joinly MCP at {JOINLY_URL}")
    async with Client(JOINLY_URL) as client:
        tools = await client.list_tools()
        tool_names = [t.name for t in tools]
        print(f"Available tools: {tool_names}")

        if "share_screen" not in tool_names:
            print("ERROR: share_screen tool not available — bot may not be joined yet.")
            return

        print(f"\nStarting screen share: {SHARE_URL}")
        result = await client.call_tool("share_screen", {"url": SHARE_URL})
        print(f"share_screen result: {result}")

        print("\nSharing for 20 seconds...")
        await asyncio.sleep(20)

        print("Stopping share...")
        result = await client.call_tool("stop_sharing", {})
        print(f"stop_sharing result: {result}")

        print("\nScreen share test complete.")


if __name__ == "__main__":
    asyncio.run(main())
