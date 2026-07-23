from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine  # noqa: TC003
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_DAYS: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


class ScheduleConfigError(ValueError):
    """Raised when the scheduler configuration file is invalid."""


@dataclass(frozen=True, slots=True)
class ScheduledMeeting:
    """A single scheduled meeting definition."""

    meeting_id: str
    url: str
    start_time: time
    days: frozenset[int]
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """Parsed scheduler configuration."""

    timezone_name: str
    timezone: ZoneInfo
    meetings: tuple[ScheduledMeeting, ...]


def _parse_time(value: object, *, meeting_id: str) -> time:
    """Parse a HH:MM time value."""
    if not isinstance(value, str):
        msg = f"Meeting '{meeting_id}' has non-string 'time' value."
        raise ScheduleConfigError(msg)

    raw_value = value.strip()
    try:
        hours_raw, minutes_raw = raw_value.split(":", maxsplit=1)
        hours = int(hours_raw)
        minutes = int(minutes_raw)
        return time(hour=hours, minute=minutes)
    except (TypeError, ValueError) as exc:
        msg = (
            f"Meeting '{meeting_id}' has invalid 'time' value '{value}'. "
            "Use 24-hour HH:MM format."
        )
        raise ScheduleConfigError(msg) from exc


def _parse_days(value: object, *, meeting_id: str) -> frozenset[int]:
    """Parse day tokens (mon..sun) from string or list input."""
    if value is None:
        return frozenset(range(7))

    raw_tokens: list[str]
    if isinstance(value, str):
        raw_tokens = [token.strip() for token in value.split(",") if token.strip()]
    elif isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            msg = f"Meeting '{meeting_id}' has non-string items in 'days'."
            raise ScheduleConfigError(msg)
        raw_tokens = [item.strip() for item in value if item.strip()]
    else:
        msg = f"Meeting '{meeting_id}' has invalid 'days' value."
        raise ScheduleConfigError(msg)

    if not raw_tokens:
        msg = f"Meeting '{meeting_id}' has an empty 'days' value."
        raise ScheduleConfigError(msg)

    parsed_days: set[int] = set()
    for token in raw_tokens:
        short_token = token.lower()[:3]
        if short_token not in _DAYS:
            msg = (
                f"Meeting '{meeting_id}' has invalid day token '{token}'. "
                "Use mon,tue,wed,thu,fri,sat,sun."
            )
            raise ScheduleConfigError(msg)
        parsed_days.add(_DAYS[short_token])

    return frozenset(parsed_days)


def _parse_meeting(item: object, *, index: int) -> ScheduledMeeting:
    """Parse a single meeting entry from scheduler JSON."""
    if not isinstance(item, dict):
        msg = f"Meeting entry #{index + 1} is not an object."
        raise ScheduleConfigError(msg)

    meeting_id_raw = item.get("id", f"meeting-{index + 1}")
    if not isinstance(meeting_id_raw, str) or not meeting_id_raw.strip():
        msg = f"Meeting entry #{index + 1} has invalid 'id'."
        raise ScheduleConfigError(msg)
    meeting_id = meeting_id_raw.strip()

    url = item.get("url")
    if not isinstance(url, str) or not url.strip():
        msg = f"Meeting '{meeting_id}' is missing a non-empty 'url'."
        raise ScheduleConfigError(msg)

    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        msg = f"Meeting '{meeting_id}' has non-boolean 'enabled' value."
        raise ScheduleConfigError(msg)

    return ScheduledMeeting(
        meeting_id=meeting_id,
        url=url.strip(),
        start_time=_parse_time(item.get("time"), meeting_id=meeting_id),
        days=_parse_days(item.get("days"), meeting_id=meeting_id),
        enabled=enabled,
    )


def load_scheduler_config(path: Path) -> SchedulerConfig:
    """Load and validate scheduler configuration from JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"Scheduler file not found: {path}"
        raise ScheduleConfigError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"Scheduler file is not valid JSON: {path}"
        raise ScheduleConfigError(msg) from exc

    if not isinstance(data, dict):
        msg = "Scheduler configuration root must be a JSON object."
        raise ScheduleConfigError(msg)

    timezone_name_raw = data.get("timezone", "UTC")
    if not isinstance(timezone_name_raw, str) or not timezone_name_raw.strip():
        msg = "Scheduler 'timezone' must be a non-empty string."
        raise ScheduleConfigError(msg)

    timezone_name = timezone_name_raw.strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        msg = (
            f"Unknown scheduler timezone '{timezone_name}'. "
            "Use an IANA timezone name like 'Asia/Kolkata'."
        )
        raise ScheduleConfigError(msg) from exc

    meetings_raw = data.get("meetings")
    if not isinstance(meetings_raw, list):
        msg = "Scheduler configuration requires a 'meetings' array."
        raise ScheduleConfigError(msg)

    meetings = tuple(
        _parse_meeting(item, index=index)
        for index, item in enumerate(meetings_raw)
    )

    return SchedulerConfig(
        timezone_name=timezone_name,
        timezone=timezone,
        meetings=meetings,
    )


def _run_key(meeting: ScheduledMeeting, run_at: datetime) -> str:
    """Build a unique key for one scheduled run instance."""
    return (
        f"{meeting.meeting_id}|{run_at.date().isoformat()}|"
        f"{meeting.start_time.strftime('%H:%M')}"
    )


def _get_due_runs(
    config: SchedulerConfig,
    now_local: datetime,
    grace_window: timedelta,
    triggered_runs: set[str],
) -> list[tuple[datetime, ScheduledMeeting, str]]:
    """Return sorted due meeting runs for the current scheduler tick."""
    due_runs: list[tuple[datetime, ScheduledMeeting, str]] = []
    for meeting in config.meetings:
        if not meeting.enabled or now_local.weekday() not in meeting.days:
            continue

        scheduled_at = datetime.combine(
            now_local.date(),
            meeting.start_time,
            tzinfo=config.timezone,
        )
        if not (scheduled_at <= now_local < scheduled_at + grace_window):
            continue

        run_key = _run_key(meeting, scheduled_at)
        if run_key in triggered_runs:
            continue

        due_runs.append((scheduled_at, meeting, run_key))

    due_runs.sort(key=lambda item: item[0])
    return due_runs


async def run_scheduler(  # noqa: C901, PLR0915
    *,
    schedule_file: str,
    run_meeting: Callable[[ScheduledMeeting], Coroutine[Any, Any, None]],
    poll_interval_seconds: int = 15,
    trigger_grace_seconds: int = 300,
) -> None:
    """Run a long-lived scheduler loop and trigger meetings at configured times."""
    if poll_interval_seconds < 1:
        msg = "poll_interval_seconds must be >= 1"
        raise ValueError(msg)
    if trigger_grace_seconds < 1:
        msg = "trigger_grace_seconds must be >= 1"
        raise ValueError(msg)

    schedule_path = Path(schedule_file).resolve()
    config = load_scheduler_config(schedule_path)

    last_loaded_mtime = schedule_path.stat().st_mtime
    last_failed_mtime: float | None = None
    triggered_runs: set[str] = set()
    blocked_notice_runs: set[str] = set()
    grace_window = timedelta(seconds=trigger_grace_seconds)

    active_task: asyncio.Task[None] | None = None
    active_meeting: ScheduledMeeting | None = None

    logger.info(
        "Scheduler started with %d meetings in timezone %s from %s",
        len(config.meetings),
        config.timezone_name,
        schedule_path,
    )

    while True:
        if active_task and active_task.done():
            try:
                await active_task
            except Exception:
                logger.exception(
                    "Scheduled meeting '%s' failed.",
                    active_meeting.meeting_id if active_meeting else "unknown",
                )
            else:
                logger.info(
                    "Scheduled meeting '%s' completed.",
                    active_meeting.meeting_id if active_meeting else "unknown",
                )
            active_task = None
            active_meeting = None

        try:
            current_mtime = schedule_path.stat().st_mtime
            if current_mtime not in {last_loaded_mtime, last_failed_mtime}:
                try:
                    config = load_scheduler_config(schedule_path)
                    last_loaded_mtime = current_mtime
                    last_failed_mtime = None
                    blocked_notice_runs.clear()
                    logger.info(
                        "Reloaded scheduler config (%d meetings, timezone %s).",
                        len(config.meetings),
                        config.timezone_name,
                    )
                except ScheduleConfigError:
                    last_failed_mtime = current_mtime
                    logger.exception(
                        "Failed to reload scheduler config from %s. "
                        "Continuing with previous valid config.",
                        schedule_path,
                    )
        except FileNotFoundError:
            logger.warning("Scheduler file not found: %s", schedule_path)

        now_local = datetime.now(tz=UTC).astimezone(config.timezone)
        due_runs = _get_due_runs(config, now_local, grace_window, triggered_runs)

        if active_task is None and due_runs:
            _, meeting, run_key = due_runs[0]
            triggered_runs.add(run_key)
            active_meeting = meeting
            logger.info(
                "Starting scheduled meeting '%s' for %s (%s).",
                meeting.meeting_id,
                meeting.start_time.strftime("%H:%M"),
                config.timezone_name,
            )
            active_task = asyncio.create_task(run_meeting(meeting))
        elif active_task is not None and due_runs:
            due_keys = {
                _run_key(meeting, scheduled_at)
                for scheduled_at, meeting, _ in due_runs
            }
            unseen_due_keys = due_keys - blocked_notice_runs
            if unseen_due_keys:
                blocked_notice_runs.update(unseen_due_keys)
                due_meeting_ids = ", ".join(
                    sorted({meeting.meeting_id for _, meeting, _ in due_runs})
                )
                logger.warning(
                    "Scheduled meeting(s) due while '%s' is still active: %s. "
                    "They will trigger if still within grace window after "
                    "the current meeting ends.",
                    active_meeting.meeting_id if active_meeting else "unknown",
                    due_meeting_ids,
                )

        await asyncio.sleep(poll_interval_seconds)
