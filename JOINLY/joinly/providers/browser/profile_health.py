"""Utilities for checking the freshness of a Chromium browser profile.

Reads the Chromium Cookies SQLite database to detect when Teams authentication
tokens are due to expire, so callers can warn operators before a bot joins a
meeting with stale credentials.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Chromium stores cookie expiry as microseconds since 1601-01-01 (Windows FILETIME epoch).
# Converting to Unix epoch: subtract the number of microseconds between 1601-01-01 and 1970-01-01.
_CHROMIUM_EPOCH_OFFSET_MICROSECONDS = 11_644_473_600_000_000

TEAMS_COOKIE_DOMAIN = ".teams.microsoft.com"
DEFAULT_WARN_DAYS = 30


def _chromium_timestamp_to_datetime(chromium_ts: int) -> datetime:
    """Convert a Chromium cookie expiry timestamp to a UTC datetime."""
    unix_microseconds = chromium_ts - _CHROMIUM_EPOCH_OFFSET_MICROSECONDS
    unix_seconds = unix_microseconds / 1_000_000
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc)


def check_profile_freshness(profile_dir: Path) -> dict:
    """Check whether Teams auth cookies in the profile are still valid.

    Reads the Chromium ``Cookies`` SQLite file inside *profile_dir* and looks
    for cookies scoped to ``teams.microsoft.com``.  Returns a dict with:

    - ``fresh`` (bool): True if at least one Teams cookie is still valid.
    - ``days_remaining`` (int): Days until the soonest-expiring Teams cookie
      expires.  ``-1`` if no Teams cookies were found.
    - ``expires_at`` (datetime | None): UTC expiry of the soonest-expiring
      Teams cookie, or ``None`` if no Teams cookies were found.

    Args:
        profile_dir: Path to the Chromium profile directory (e.g. ``/browser-profile``).

    Returns:
        A dict with keys ``fresh``, ``days_remaining``, and ``expires_at``.

    Raises:
        FileNotFoundError: If the Cookies file does not exist.
    """
    cookies_path = profile_dir / "Default" / "Cookies"
    if not cookies_path.exists():
        # Fallback: some profiles store Cookies directly in profile_dir
        cookies_path = profile_dir / "Cookies"
    if not cookies_path.exists():
        raise FileNotFoundError(
            f"Chromium Cookies database not found in {profile_dir}. "
            "Ensure the profile has been populated (run auth-login or import-cookies first)."
        )

    now = datetime.now(tz=timezone.utc)

    # Open read-only to avoid corrupting a live profile
    uri = f"file:{cookies_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT expires_utc FROM cookies WHERE host_key LIKE ? AND expires_utc > 0",
            (f"%{TEAMS_COOKIE_DOMAIN}%",),
        )
        rows = cursor.fetchall()

    if not rows:
        return {"fresh": False, "days_remaining": -1, "expires_at": None}

    expiry_datetimes = [_chromium_timestamp_to_datetime(row["expires_utc"]) for row in rows]
    soonest = min(expiry_datetimes)
    delta = soonest - now
    days_remaining = int(delta.total_seconds() / 86_400)
    fresh = days_remaining > 0

    return {"fresh": fresh, "days_remaining": days_remaining, "expires_at": soonest}


def warn_if_stale(profile_dir: Path, warn_days: int = DEFAULT_WARN_DAYS) -> None:
    """Log a warning if Teams cookies will expire within *warn_days* days.

    Silently returns if the profile has no Cookies database yet (e.g. on first
    run before auth-login has been performed) — this avoids spurious warnings
    during container startup.

    Args:
        profile_dir: Path to the Chromium profile directory.
        warn_days: Number of days before expiry at which to start warning.
                   Defaults to 30.
    """
    try:
        result = check_profile_freshness(profile_dir)
    except FileNotFoundError:
        logger.debug("No Cookies database found in %s — skipping freshness check.", profile_dir)
        return

    if not result["fresh"]:
        logger.error(
            "Teams auth tokens in browser profile '%s' have EXPIRED. "
            "Run auth-login or import-cookies and re-upload the profile before joining meetings.",
            profile_dir,
        )
        return

    days = result["days_remaining"]
    expires_at = result["expires_at"]
    if days <= warn_days:
        logger.warning(
            "Teams auth tokens in browser profile '%s' expire in %d day(s) (at %s). "
            "Re-authenticate before tokens expire to avoid bot disconnections.",
            profile_dir,
            days,
            expires_at.isoformat() if expires_at else "unknown",
        )
    else:
        logger.debug(
            "Browser profile '%s' tokens are fresh — %d days remaining (expires %s).",
            profile_dir,
            days,
            expires_at.isoformat() if expires_at else "unknown",
        )
