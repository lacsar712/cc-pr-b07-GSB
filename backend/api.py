import os
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from psycopg.rows import dict_row

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54394/printreg")
SECRET = os.environ.get("JWT_SECRET", "print-register-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "printer": {"role": "writer", "password_hash": pwd.hash("print123456")},
    "checker": {"role": "reader", "password_hash": pwd.hash("check123456")},
}


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


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


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    sheet: str
    cyan_mm: float
    magenta_mm: float
    urgent: bool = False


class PolicyIn(BaseModel):
    fail_threshold: int
    cooldown_seconds: int


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    if payload.get("sub") not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": payload["sub"], "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=403, detail="仅印刷员可操作")
    return user


app = FastAPI(title="印刷套准复核台")


@app.on_event("startup")
def startup():
    with connect() as conn:
        conn.execute(SCHEMA)
        conn.execute(
            """INSERT INTO cooldown_settings (id, fail_threshold, cooldown_seconds)
               VALUES (1, 3, 10)
               ON CONFLICT (id) DO NOTHING"""
        )
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            now = datetime.now(timezone.utc)
            # 先插内页-09 后插封面-01：按 id 领取后最后一条种子结论为套准，
            # 连续套不准计数从 0 开始，便于按阈值精确复现缓领。
            conn.execute(
                """INSERT INTO jobs (sheet, cyan_mm, magenta_mm, status, verdict, reason, urgent, created_by, created_at)
                   VALUES
                   ('内页-09', 0.40, 0.02, 'pending', '', '', false, 'printer', %s),
                   ('封面-01', 0.05, -0.04, 'pending', '', '', false, 'printer', %s)""",
                (now, now),
            )
        conn.commit()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "print-register-review"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode({"sub": body.username.strip(), "role": user["role"], "exp": exp}, SECRET, algorithm="HS256")
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/jobs")
def list_jobs(_user: dict = Depends(current_user)):
    with connect() as conn:
        return conn.execute(
            """SELECT id, sheet, cyan_mm, magenta_mm, status, verdict, reason, urgent, created_by
               FROM jobs ORDER BY id DESC"""
        ).fetchall()


@app.post("/api/jobs", status_code=202)
def enqueue(body: JobIn, user: dict = Depends(require_writer)):
    with connect() as conn:
        row = conn.execute(
            """INSERT INTO jobs (sheet, cyan_mm, magenta_mm, status, urgent, created_by, created_at)
               VALUES (%s, %s, %s, 'pending', %s, %s, %s)
               RETURNING id, sheet, status, verdict, urgent""",
            (body.sheet.strip(), body.cyan_mm, body.magenta_mm, body.urgent,
             user["username"], datetime.now(timezone.utc)),
        ).fetchone()
        conn.commit()
    return row


@app.get("/api/cooldown-policy")
def get_policy(_user: dict = Depends(current_user)):
    with connect() as conn:
        settings = conn.execute(
            "SELECT fail_threshold, cooldown_seconds, cooldown_until FROM cooldown_settings WHERE id = 1"
        ).fetchone()
        events = conn.execute(
            """SELECT id, kind, fail_threshold, cooldown_seconds, streak, note, created_at
               FROM cooldown_events ORDER BY id DESC LIMIT 100"""
        ).fetchall()
    now = datetime.now(timezone.utc)
    cooling = bool(settings["cooldown_until"] and settings["cooldown_until"] > now)
    remaining = max(0, int((settings["cooldown_until"] - now).total_seconds())) if cooling else 0
    return {
        "fail_threshold": settings["fail_threshold"],
        "cooldown_seconds": settings["cooldown_seconds"],
        "cooldown_until": settings["cooldown_until"],
        "cooling": cooling,
        "remaining_seconds": remaining,
        "events": events,
    }


@app.put("/api/cooldown-policy")
def update_policy(body: PolicyIn, _user: dict = Depends(require_writer)):
    if body.fail_threshold < 1:
        raise HTTPException(status_code=422, detail="连续失败阈值至少为 1")
    if body.cooldown_seconds < 1:
        raise HTTPException(status_code=422, detail="缓领秒数至少为 1")
    with connect() as conn:
        row = conn.execute(
            """UPDATE cooldown_settings
               SET fail_threshold = %s, cooldown_seconds = %s
               WHERE id = 1
               RETURNING fail_threshold, cooldown_seconds, cooldown_until""",
            (body.fail_threshold, body.cooldown_seconds),
        ).fetchone()
        conn.execute(
            """INSERT INTO cooldown_events (kind, fail_threshold, cooldown_seconds, streak, note, created_at)
               VALUES ('policy', %s, %s, 0, '印刷员更新缓领策略', %s)""",
            (body.fail_threshold, body.cooldown_seconds, datetime.now(timezone.utc)),
        )
        conn.commit()
    return row
