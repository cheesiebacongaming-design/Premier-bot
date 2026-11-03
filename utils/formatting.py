"""Formatting helpers shared by bot commands and renderers."""

from datetime import datetime


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_range(start, end):
    first = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end)
    if first == last:
        return f"{first:%b} {first.day}"
    if first.month == last.month:
        return f"{first:%b} {first.day}\u2013{last.day}"
    return f"{first:%b} {first.day} \u2013 {last:%b} {last.day}"
