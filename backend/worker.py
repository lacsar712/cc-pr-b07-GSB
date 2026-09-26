import os
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from rules import judge

DSN = os.environ["DATABASE_URL"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
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
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS urgent boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS cooldown_settings (
    id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    fail_threshold integer NOT NULL CHECK (fail_threshold >= 1),
    cooldown_seconds integer NOT NULL CHECK (cooldown_seconds >= 1),
    cooldown_until timestamptz
);

CREATE TABLE IF NOT EXISTS cooldown_events (
    id serial PRIMARY KEY,
    kind text NOT NULL,
    fail_threshold integer NOT NULL,
    cooldown_seconds integer NOT NULL,
    streak integer NOT NULL DEFAULT 0,
    note text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL
);
"""


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
        conn.execute(SCHEMA)
        conn.execute(
            """INSERT INTO cooldown_settings (id, fail_threshold, cooldown_seconds)
               VALUES (1, 3, 10)
               ON CONFLICT (id) DO NOTHING"""
        )
        conn.commit()


def read_settings(conn):
    return conn.execute(
        "SELECT fail_threshold, cooldown_seconds, cooldown_until FROM cooldown_settings WHERE id = 1"
    ).fetchone()


def expire_cooldown_if_due(conn):
    """缓领到期则清空截止时间并写结束流水，返回当前是否仍在缓领。"""
    settings = read_settings(conn)
    until = settings["cooldown_until"]
    now = datetime.now(timezone.utc)
    if until is None:
        return False, settings
    if until > now:
        return True, settings
    conn.execute("UPDATE cooldown_settings SET cooldown_until = NULL WHERE id = 1")
    conn.execute(
        """INSERT INTO cooldown_events (kind, fail_threshold, cooldown_seconds, streak, note, created_at)
           VALUES ('end', %s, %s, 0, '缓领结束，恢复领取普通任务', %s)""",
        (settings["fail_threshold"], settings["cooldown_seconds"], now),
    )
    return False, settings


def claim_once(conn, cooling: bool):
    """缓领中只领急件，否则按顺序领取所有待处理任务。"""
    urgent_only = "AND urgent = true" if cooling else ""
    row = conn.execute(
        f"""WITH picked AS (
             SELECT id FROM jobs
             WHERE status = 'pending' {urgent_only}
             ORDER BY urgent DESC, id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
           )
           UPDATE jobs SET status = 'running'
           FROM picked
           WHERE jobs.id = picked.id
           RETURNING jobs.id, jobs.cyan_mm, jobs.magenta_mm, jobs.urgent""",
    ).fetchone()
    return row


def consecutive_failures(conn, limit: int) -> int:
    rows = conn.execute(
        "SELECT verdict FROM jobs WHERE status = 'done' ORDER BY id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    streak = 0
    for r in rows:
        if r["verdict"] != "套不准":
            break
        streak += 1
    return streak


def main():
    ensure()
    while True:
        claimed = None
        with connect() as conn:
            cooling, settings = expire_cooldown_if_due(conn)
            claimed = claim_once(conn, cooling)
            if claimed is None:
                conn.commit()
            else:
                verdict, reason = judge(claimed["cyan_mm"], claimed["magenta_mm"])
                conn.execute(
                    "UPDATE jobs SET status = 'done', verdict = %s, reason = %s WHERE id = %s",
                    (verdict, reason, claimed["id"]),
                )
                # 最近已出结论里连续套不准达到阈值即开始缓领；缓领期间不因急件重复触发。
                if verdict == "套不准" and not cooling:
                    settings = read_settings(conn)
                    streak = consecutive_failures(conn, settings["fail_threshold"])
                    if streak >= settings["fail_threshold"]:
                        now = datetime.now(timezone.utc)
                        until = now + timedelta(seconds=settings["cooldown_seconds"])
                        conn.execute(
                            "UPDATE cooldown_settings SET cooldown_until = %s WHERE id = 1",
                            (until,),
                        )
                        conn.execute(
                            """INSERT INTO cooldown_events
                               (kind, fail_threshold, cooldown_seconds, streak, note, created_at)
                               VALUES ('start', %s, %s, %s, %s, %s)""",
                            (
                                settings["fail_threshold"],
                                settings["cooldown_seconds"],
                                streak,
                                f"连续 {streak} 套套不准达到阈值，暂停领取普通任务 "
                                f"{settings['cooldown_seconds']} 秒，急件仍可领",
                                now,
                            ),
                        )
                conn.commit()
        if claimed is None:
            time.sleep(0.4)


if __name__ == "__main__":
    main()
