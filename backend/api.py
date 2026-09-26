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


SCHEMA_STATEMENTS = [
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


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    sheet: str
    cyan_mm: float
    magenta_mm: float
    urgent: bool = False


class PolicyIn(BaseModel):
    threshold: int
    slow_seconds: int


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
        raise HTTPException(status_code=403, detail="仅印刷员可送复核")
    return user


app = FastAPI(title="印刷套准复核台")


@app.on_event("startup")
def startup():
    with connect() as conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            now = datetime.now(timezone.utc)
            conn.execute(
                """INSERT INTO jobs (sheet, cyan_mm, magenta_mm, status, verdict, reason, created_by, created_at)
                   VALUES
                   ('封面-01', 0.05, -0.04, 'pending', '', '', 'printer', %s),
                   ('内页-09', 0.40, 0.02, 'pending', '', '', 'printer', %s)""",
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
            "SELECT id, sheet, cyan_mm, magenta_mm, status, verdict, reason, urgent, created_by FROM jobs ORDER BY id DESC"
        ).fetchall()


@app.post("/api/jobs", status_code=202)
def enqueue(body: JobIn, user: dict = Depends(require_writer)):
    with connect() as conn:
        row = conn.execute(
            """INSERT INTO jobs (sheet, cyan_mm, magenta_mm, status, urgent, created_by, created_at)
               VALUES (%s, %s, %s, 'pending', %s, %s, %s)
               RETURNING id, sheet, status, verdict""",
            (body.sheet.strip(), body.cyan_mm, body.magenta_mm, body.urgent, user["username"], datetime.now(timezone.utc)),
        ).fetchone()
        conn.commit()
    return row


@app.get("/api/throttle/policy")
def get_throttle_policy(_user: dict = Depends(current_user)):
    with connect() as conn:
        policy = conn.execute("SELECT threshold, slow_seconds FROM throttle_policy WHERE id").fetchone()
        state = conn.execute("SELECT slow_until FROM throttle_state WHERE id").fetchone()
    now = datetime.now(timezone.utc)
    active = state["slow_until"] is not None and state["slow_until"] > now
    return {
        "threshold": policy["threshold"],
        "slow_seconds": policy["slow_seconds"],
        "active": active,
        "slow_until": state["slow_until"] if active else None,
    }


@app.put("/api/throttle/policy")
def update_throttle_policy(body: PolicyIn, _user: dict = Depends(require_writer)):
    if not 1 <= body.threshold <= 20:
        raise HTTPException(status_code=400, detail="连续失败阈值需在 1-20 之间")
    if not 1 <= body.slow_seconds <= 3600:
        raise HTTPException(status_code=400, detail="缓领秒数需在 1-3600 之间")
    with connect() as conn:
        conn.execute(
            "UPDATE throttle_policy SET threshold = %s, slow_seconds = %s WHERE id",
            (body.threshold, body.slow_seconds),
        )
        conn.commit()
    return {"threshold": body.threshold, "slow_seconds": body.slow_seconds}


@app.get("/api/throttle/events")
def list_throttle_events(_user: dict = Depends(current_user)):
    with connect() as conn:
        return conn.execute(
            "SELECT id, event, detail, created_at FROM throttle_events ORDER BY id DESC LIMIT 100"
        ).fetchall()
