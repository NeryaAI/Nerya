"""Calendar occurrences: stable identity, explicit timezone and bounded catch-up."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import math

from .schedule import _parse_cron, _cron_matches


def next_due(entry, after):
    if entry.archived:return None
    if entry.run_at:
        target=datetime.fromisoformat(entry.run_at.replace("Z","+00:00"))
        if target.tzinfo is None:raise ValueError("run_at requires a timezone")
        stamp=target.timestamp()
        return stamp if stamp>after else None
    if entry.every_seconds is not None:
        interval=int(entry.every_seconds)
        if interval<=0:raise ValueError("interval must be positive")
        anchor=float(entry.anchor_at if entry.anchor_at is not None else after)
        return anchor+max(1,math.floor((after-anchor)/interval)+1)*interval
    fields=_parse_cron(entry.cron)
    zone=ZoneInfo(entry.timezone or "UTC")
    local=datetime.fromtimestamp(after,tz=zone)
    start=local.date()
    for offset in range(8*366):
        day=start+timedelta(days=offset)
        choices=[]
        for hour in sorted(fields[1]):
            for minute in sorted(fields[0]):
                naive=datetime(day.year,day.month,day.day,hour,minute)
                if not _cron_matches(entry.cron,naive):continue
                for fold in (0,1):
                    candidate=naive.replace(tzinfo=zone,fold=fold)
                    stamp=candidate.timestamp()
                    # DST gaps are not valid local occurrences.
                    if stamp>after and datetime.fromtimestamp(stamp,zone).replace(tzinfo=None)==naive:
                        choices.append(stamp)
        if choices:return min(choices)
    raise ValueError("cron has no occurrence within eight years")


def due_occurrence(config,entry,now_ts):
    import time
    from dataclasses import asdict
    from ..agent.task_runs import revision
    from ..db.sqlite import connect
    digest=revision(asdict(entry))
    con=connect(config.paths.db)
    try:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT * FROM task_schedule_state WHERE task_id=?",(entry.id,)).fetchone()
        if row is None or row["source_revision"]!=digest:
            # Legacy intervals fired immediately; explicit anchors use future slots.
            due=(datetime.fromisoformat(entry.run_at.replace("Z","+00:00")).timestamp() if entry.run_at else
                 next_due(entry,now_ts-1) if entry.cron else now_ts if entry.anchor_at is None else next_due(entry,now_ts-0.001))
            con.execute("""INSERT INTO task_schedule_state(task_id,source_revision,next_due,updated_at)
                VALUES (?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET source_revision=excluded.source_revision,
                next_due=excluded.next_due,updated_at=excluded.updated_at""",(entry.id,digest,due,time.time()))
            row=con.execute("SELECT * FROM task_schedule_state WHERE task_id=?",(entry.id,)).fetchone()
        if row["paused_reason"] or row["next_due"] is None or row["next_due"]>now_ts:
            con.commit();return None
        due=float(row["next_due"])
        following=(due+(math.floor((now_ts-due)/entry.every_seconds)+1)*entry.every_seconds
                   if entry.every_seconds else next_due(entry,now_ts))
        # 推进发生在 Run+Command 的准入事务，避免崩溃丢掉本次触发。
        con.commit()
        return {"scheduled_at":due,"missed":now_ts-due>300,"next_due":following}
    except BaseException:
        con.rollback();raise
    finally:con.close()
