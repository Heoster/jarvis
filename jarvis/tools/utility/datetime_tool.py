"""Date/time tools, India-first.

Heoster is in ``Asia/Kolkata`` and quotes IST, so these tools default to IST while
still being able to convert to anywhere else.  Also included: ISO parsing,
human-readable deltas ("in 3 days") and simple scheduling arithmetic, which the
agent needs for deadlines, exams and reminders.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...utils.logger import get_logger
from ..base import tool

log = get_logger("utility.time")

__all__ = ["now", "get_current_datetime", "convert_timezone", "time_since",
           "add_time", "DATETIME_TOOLS"]

DEFAULT_TZ = "Asia/Kolkata"


def _zone(name: str) -> tzinfo:
    """Resolve a zone name, degrading to a fixed UTC offset instead of raising.

    Windows has no system tz database, so ``ZoneInfo`` needs the ``tzdata``
    package (declared in pyproject for win32). If it is still missing — a
    stripped-down install, a frozen binary — ``ZoneInfo("UTC")`` fails too, and
    the date tools must keep working rather than take the whole turn down.
    """
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        log.debug(f"unknown timezone '{name}' ({exc}), using UTC")
    try:
        return ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        log.warning("no tz database available (pip install tzdata) — using a "
                    "fixed UTC offset, so named zones will not resolve")
        return timezone.utc


def now(tz: str = DEFAULT_TZ) -> datetime:
    return datetime.now(_zone(tz))


def _format(moment: datetime) -> str:
    return moment.strftime("%A, %d %B %Y at %H:%M:%S %Z (UTC%z)")


@tool(category="utility", timeout=10)
def get_current_datetime(timezone_name: str = DEFAULT_TZ) -> str:
    """Get the current date and time. ALWAYS use this — never guess the date.

    Args:
        timezone_name: IANA timezone (default "Asia/Kolkata" = IST for Heoster)
    """
    zone = _zone(timezone_name)
    current = datetime.now(zone)
    utc = current.astimezone(timezone.utc)
    week = current.isocalendar()
    return "\n".join([
        f"Local : {_format(current)}",
        f"UTC   : {utc.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"ISO   : {current.isoformat()}",
        f"Week  : {week.year}-W{week.week:02d} (day {week.weekday} of 7)",
        f"Unix  : {int(current.timestamp())}",
    ])


@tool(category="utility", timeout=15)
def convert_timezone(moment: str, to_timezone: str, from_timezone: str = DEFAULT_TZ) -> str:
    """Convert a date/time between timezones.

    Args:
        moment: ISO string like "2026-03-01 18:30" or "2026-03-01T18:30"
        to_timezone: Target IANA timezone, e.g. "UTC" or "America/New_York"
        from_timezone: Source timezone (default IST)
    """
    cleaned = moment.strip().replace("Z", "").replace("T", " ")
    parsed: Optional[datetime] = None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M",
                    "%d-%m-%Y %H:%M"):
        try:
            parsed = datetime.strptime(cleaned, pattern)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return f"Cannot parse '{moment}'. Use ISO format like 2026-03-01 18:30."
    source = parsed.replace(tzinfo=_zone(from_timezone))
    target = source.astimezone(_zone(to_timezone))
    return (f"{_format(source)}\n  = {_format(target)}\n"
            f"(difference: {(target.utcoffset() - source.utcoffset())})")


@tool(category="utility", timeout=15)
def time_since(moment: str, timezone_name: str = DEFAULT_TZ) -> str:
    """How long ago (or until) a date is, in human terms.

    Args:
        moment: ISO-ish date/time, e.g. "2026-01-15" or "2026-01-15 09:00"
        timezone_name: Timezone to interpret it in
    """
    cleaned = moment.strip().replace("T", " ")
    parsed = None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(cleaned, pattern)
            break
        except ValueError:
            continue
    if parsed is None:
        return f"Cannot parse '{moment}'."
    target = parsed.replace(tzinfo=_zone(timezone_name))
    current = datetime.now(_zone(timezone_name))
    delta = current - target
    seconds = delta.total_seconds()
    past = seconds >= 0
    seconds = abs(seconds)
    days, remainder = divmod(int(seconds), 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and not days:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    human = ", ".join(parts) or "less than a minute"
    return (f"{_format(target)} was **{human} ago**" if past
            else f"{_format(target)} is **in {human}**")


@tool(category="utility", timeout=15)
def add_time(moment: str, amount: float, unit: str = "days",
             timezone_name: str = DEFAULT_TZ) -> str:
    """Add (or subtract) time from a date.

    Args:
        moment: Starting date/time ("now" is allowed)
        amount: How much to add (negative to subtract)
        unit: "minutes", "hours", "days", "weeks"
        timezone_name: Timezone for the result
    """
    if moment.strip().lower() in ("now", "today", ""):
        base = datetime.now(_zone(timezone_name))
    else:
        cleaned = moment.strip().replace("T", " ")
        base = None
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                base = datetime.strptime(cleaned, pattern).replace(
                    tzinfo=_zone(timezone_name))
                break
            except ValueError:
                continue
        if base is None:
            return f"Cannot parse '{moment}'."
    factors = {"minutes": 60, "hours": 3_600, "days": 86_400, "weeks": 604_800}
    if unit not in factors:
        return f"Unknown unit '{unit}'. Use minutes/hours/days/weeks."
    result = base + timedelta(seconds=amount * factors[unit])
    return f"{_format(base)}\n  {amount:+g} {unit}\n= {_format(result)}"


DATETIME_TOOLS = [get_current_datetime, convert_timezone, time_since, add_time]
