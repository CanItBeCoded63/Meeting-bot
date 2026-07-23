from __future__ import annotations

from joinly_client.main import _normalize_mcp_config


def test_normalize_mcp_config_removes_disabled_servers() -> None:
    """Disabled MCP servers should not be passed to FastMCP clients."""
    normalized = _normalize_mcp_config(
        {
            "mcpServers": {
                "hr": {"url": "https://example.com/hr-mcp/mcp"},
                "global_weather": {
                    "command": "weather-mcp",
                    "args": [],
                    "disabled": True,
                },
            }
        }
    )

    assert normalized == {
        "mcpServers": {
            "hr": {"url": "https://example.com/hr-mcp/mcp"},
        }
    }


def test_normalize_mcp_config_renames_external_joinly_server() -> None:
    """External configs named joinly should not collide with the core client."""
    normalized = _normalize_mcp_config(
        {
            "mcpServers": {
                "joinly": {"url": "https://example.com/mcp"},
            }
        }
    )

    assert normalized == {
        "mcpServers": {
            "_joinly": {"url": "https://example.com/mcp"},
        }
    }
