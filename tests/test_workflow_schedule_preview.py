from datetime import datetime, timezone
import pytest

from nerya.api.routes_triggers import routes
from nerya.triggers.schedule_preview import PreviewError, preview_schedule, resolve_wall_time


def test_wall_time_uses_selected_timezone_not_browser():
    assert resolve_wall_time("2026-10-06T09:00", "Asia/Shanghai") == "2026-10-06T01:00:00Z"
    assert resolve_wall_time("2026-10-06T09:00", "America/New_York") == "2026-10-06T13:00:00Z"


def test_dst_gap_and_fold_are_explicit():
    with pytest.raises(PreviewError, match="nonexistent_local_time"):
        resolve_wall_time("2026-03-08T02:30", "America/New_York")
    with pytest.raises(PreviewError, match="ambiguous_local_time") as error:
        resolve_wall_time("2026-11-01T01:30", "America/New_York")
    assert len(error.value.choices) == 2
    assert resolve_wall_time("2026-11-01T01:30", "America/New_York", fold=0) == "2026-11-01T05:30:00Z"
    assert resolve_wall_time("2026-11-01T01:30", "America/New_York", fold=1) == "2026-11-01T06:30:00Z"
    assert resolve_wall_time("2026-11-01T01:30", "America/New_York", existing_instant="2026-11-01T06:30:00Z") == "2026-11-01T06:30:00Z"


def test_preview_is_read_only_and_respects_time_window():
    handler = next(fn for method, path, fn in routes() if path == "/triggers/schedules/preview")
    # Deliberately no client: reaching workspace/kernel/delivery would fail this test.
    result = handler(None, {"schedule": {"cron": "0 9 * * 1-5", "timezone": "Asia/Shanghai", "enabled": False}})
    assert result["ok"] and result["read_only"] and len(result["occurrences"]) == 3
    now = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
    result = preview_schedule({"schedule": {"cron": "0 9 * * *", "timezone": "Asia/Shanghai",
        "starts_at": "2026-10-07T00:00:00Z", "ends_at": "2026-10-08T00:00:00Z"}}, now=now)
    assert [item["utc"] for item in result["occurrences"]] == ["2026-10-07T01:00:00Z"]


def test_once_and_interval_preview():
    now = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
    result = preview_schedule({"schedule": {"wall_time": "2026-10-06T09:00", "timezone": "Asia/Shanghai"}}, now=now)
    assert result["schedule"]["run_at"] == "2026-10-06T01:00:00Z"
    assert len(result["occurrences"]) == 1
    result = preview_schedule({"every_seconds": 300}, now=now)
    assert len(result["occurrences"]) == 3
    assert result["interval_unanchored"]


def test_cleared_wall_time_does_not_preview_a_stale_instant():
    with pytest.raises(PreviewError, match="invalid_local_time"):
        preview_schedule({"schedule": {"wall_time": "", "run_at": "2027-01-01T01:00:00Z", "timezone": "Asia/Shanghai"}})


@pytest.mark.parametrize("payload", [{"cron": "bad"}, {"every_seconds": 0}, {"cron": "* * * * *", "every_seconds": 60}, {"cron": "* * * * *", "timezone": "bad/zone"}])
def test_invalid_schedule_returns_explicit_error(payload):
    handler = next(fn for _, path, fn in routes() if path == "/triggers/schedules/preview")
    result = handler(None, payload)
    assert result["ok"] is False and result["_status"] == 400
