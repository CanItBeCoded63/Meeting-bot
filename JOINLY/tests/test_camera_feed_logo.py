from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from joinly.providers.browser import camera_feed

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_default_camera_logo_is_alex_svg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default virtual camera logo should be the built-in Alex SVG."""
    monkeypatch.delenv("JOINLY_CAMERA_LOGO_PATH", raising=False)

    logo_src = camera_feed._resolve_logo_src()  # noqa: SLF001

    assert logo_src.startswith("data:image/svg+xml,")
    assert "ALEX" in logo_src


def test_camera_logo_can_load_from_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured image path should be embedded as a data URI."""
    logo_path = tmp_path / "alex.png"
    logo_bytes = b"fake png bytes"
    logo_path.write_bytes(logo_bytes)
    monkeypatch.setenv("JOINLY_CAMERA_LOGO_PATH", str(logo_path))

    logo_src = camera_feed._resolve_logo_src()  # noqa: SLF001

    assert logo_src == "data:image/png;base64," + base64.b64encode(
        logo_bytes
    ).decode("ascii")
