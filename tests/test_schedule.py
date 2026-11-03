from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from utils.formatting import fmt_range, parse_time


def test_parse_time_keeps_utc_offset_from_api_timestamp():
    parsed = parse_time("2026-11-01T12:30:00Z")

    assert parsed == datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc)


def test_schedule_date_range_formats_same_and_cross_month_ranges():
    assert fmt_range("2026-11-02", "2026-11-02") == "Nov 2"
    assert fmt_range("2026-11-29", "2026-12-03") == "Nov 29 – Dec 3"


def test_queue_schedule_uses_local_dst_offset():
    utc_time = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)

    assert utc_time.astimezone(ZoneInfo("Europe/London")).hour == 19
