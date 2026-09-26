import os
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from rules import judge

DSN = os.environ["DATABASE_URL"]

DDL = [
    """CREATE TABLE IF NOT EXISTS jobs (
        id serial PRIMARY KEY,
        sheet text NOT NULL,
        cyan_mm double precision NOT NULL,
        magenta_mm double precision NOT NULL,
        status text NOT NULL,
        verdict text NOT NULL DEFAULT '',
        reason text NOT NULL DEFAULT '',
        urgent boolean NOT NULL DEFAULT false,
        created_by text NOT NULL,
        created_at timestamptz NOT NULL
    )""",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS urgent boolean NOT NULL DEFAULT false",
    """CREATE TABLE IF NOT EXISTS throttle_policy (
        id boolean PRIMARY KEY DEFAULT true CHECK (id),
        threshold integer NOT NULL DEFAULT 3,
        slow_seconds integer NOT NULL DEFAULT 30
    )""",
    """CREATE TABLE IF NOT EXISTS throttle_state (
        id boolean PRIMARY KEY DEFAULT true CHECK (id),
        slow_until timestamptz,
        last_trigger_job_id integer NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS throttle_events (
        id serial PRIMARY KEY,
        event text NOT NULL,
        detail text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL
    )""",
    "INSERT INTO throttle_policy (id) VALUES (true) ON CONFLICT (id) DO NOTHING",
    "INSERT INTO throttle_state (id) VALUES (true) ON CONFLICT (id) DO NOTHING",
]


def connect():
    last = None
    for _ in range(40):
        try:
            return psycopg.connect(DSN, row_factory=dict_row)
        except psycopg.OperationalError as exc:
            last = exc
            time.sleep(1)
    raise last


def ensure():
    with connect() as conn:
        for statement in DDL:
            conn.execute(statement)
        conn.commit()


def refresh_throttle(conn):
    """维护缓领状态：窗口到期写结束流水；最近连续套不准达到阈值则开窗写开始流水。

    返回当前是否处于缓领中（普通任务暂停领取，急件照常）。
    """
    policy = conn.execute("SELECT threshold, slow_seconds FROM throttle_policy WHERE id").fetchone()
    state = conn.execute(
        "SELECT slow_until, last_trigger_job_id FROM throttle_state WHERE id FOR UPDATE"
    ).fetchone()
    now = datetime.now(timezone.utc)
    slow_until = state["slow_until"]
    if slow_until is not None and slow_until <= now:
        conn.execute(
            "INSERT INTO throttle_events (event, detail, created_at) VALUES ('end', %s, %s)",
            ("缓领窗口结束，恢复领取普通任务", now),
        )
        conn.execute("UPDATE throttle_state SET slow_until = NULL WHERE id")
        slow_until = None
    if slow_until is None:
        threshold = policy["threshold"]
        recent = conn.execute(
            "SELECT id, verdict FROM jobs WHERE status = 'done' ORDER BY id DESC LIMIT %s",
            (threshold,),
        ).fetchall()
        fresh = recent[0]["id"] > state["last_trigger_job_id"] if recent else False
        if (
            len(recent) == threshold
            and fresh
            and all(row["verdict"] == "套不准" for row in recent)
        ):
            slow_until = now + timedelta(seconds=policy["slow_seconds"])
            conn.execute(
                "INSERT INTO throttle_events (event, detail, created_at) VALUES ('start', %s, %s)",
                (f"最近 {threshold} 笔结论连续套不准，缓领 {policy['slow_seconds']} 秒", now),
            )
            conn.execute(
                "UPDATE throttle_state SET slow_until = %s, last_trigger_job_id = %s WHERE id",
                (slow_until, recent[0]["id"]),
            )
    return slow_until is not None and slow_until > now


def claim_once(conn, urgent_only=False):
    urgent_clause = "AND urgent" if urgent_only else ""
    row = conn.execute(
        f"""WITH picked AS (
             SELECT id FROM jobs
             WHERE status = 'pending' {urgent_clause}
             ORDER BY id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
           )
           UPDATE jobs SET status = 'running'
           FROM picked
           WHERE jobs.id = picked.id
           RETURNING jobs.id, jobs.cyan_mm, jobs.magenta_mm"""
    ).fetchone()
    return row


def main():
    ensure()
    while True:
        with connect() as conn:
            slowing = refresh_throttle(conn)
            row = claim_once(conn, urgent_only=slowing)
            if row is None:
                conn.commit()
            else:
                verdict, reason = judge(row["cyan_mm"], row["magenta_mm"])
                conn.execute(
                    "UPDATE jobs SET status = 'done', verdict = %s, reason = %s WHERE id = %s",
                    (verdict, reason, row["id"]),
                )
                conn.commit()
        if row is None:
            time.sleep(0.4)


if __name__ == "__main__":
    main()
