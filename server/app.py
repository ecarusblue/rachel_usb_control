"""UsbGuard 중앙 관리 서버 (FastAPI + SQLite)

- 에이전트(PC 서비스)가 30초마다 /api/agent/heartbeat 로 상태 보고 -> 응답으로 정책 수신
- 관리자는 웹 GUI(/)에서 로그인 후 PC별 상태 확인 / 정책 변경
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from logstore import FILE_RE, MODE_LABEL, RETENTION_DAYS, ControlLogger, DailyLog

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("USBGUARD_DATA", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "usbguard.db"
LOG_DIR = Path(os.environ.get("USBGUARD_LOG_DIR") or DATA_DIR / "logs")

OFFLINE_SEC = 100          # 하트비트(30초) 3번 연속 누락 -> 오프라인
PENDING_SEC = 75           # 정책 변경 직후 반영 대기 허용 시간
TAMPER_WINDOW_SEC = 600    # 최근 변조 감지로 취급하는 시간
SESSION_MAX_AGE = 8 * 3600

Mode = Literal["block", "read_only", "allow"]


# ───────────────────────── 유틸 ─────────────────────────
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


_FRAC = re.compile(r"(\.\d{6})\d+")


def parse(s: Optional[str]) -> Optional[datetime]:
    """ISO8601 파싱. .NET 의 7자리 소수초도 허용."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(_FRAC.sub(r"\1", s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def hash_pw(pw: str, salt: Optional[bytes] = None, iters: int = 200_000) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    return f"{iters}${salt.hex()}${h.hex()}"


def verify_pw(pw: str, stored: str) -> bool:
    try:
        iters, salt_hex, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc.hex(), h)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ───────────────────────── DB ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  token_hash TEXT UNIQUE NOT NULL,
  desired_mode TEXT NOT NULL DEFAULT 'block',
  temp_until TEXT,
  policy_changed_at TEXT,
  created_at TEXT NOT NULL,
  last_seen TEXT,
  last_ip TEXT,
  report TEXT,
  last_state TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  agent_id INTEGER,
  agent_name TEXT,
  kind TEXT NOT NULL,
  detail TEXT
);
"""


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def tx():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with tx() as c:
        c.executescript(SCHEMA)
        if not c.execute("SELECT 1 FROM settings WHERE key='session_secret'").fetchone():
            c.execute("INSERT INTO settings VALUES('session_secret', ?)", (secrets.token_hex(32),))


def get_setting(key: str) -> Optional[str]:
    with tx() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with tx() as c:
        c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


daily_log = DailyLog(LOG_DIR)            # log_yyyymmdd.log, 오늘 포함 14일 보관
control_log = ControlLogger(daily_log)   # PC별 제어 상태 변화/주기 점검 기록


def _ko_modes(text: str) -> str:
    return re.sub(r"\b(block|read_only|allow)\b", lambda m: MODE_LABEL[m.group(1)], text or "")


def _file_event(kind: str, detail: str, agent_name: Optional[str]) -> None:
    """관리자 동작은 DB 이벤트와 함께 로그 파일에도 남긴다. (상태 변화는 ControlLogger 가 기록)"""
    n = agent_name or ""
    d = _ko_modes(detail)
    if kind == "admin_login":
        daily_log.write("INFO", f"[관리자] 로그인 | {d}")
    elif kind == "admin_login_fail":
        daily_log.write("WARNING", f"[관리자] 로그인 실패 | {d}")
    elif kind == "agent_created":
        daily_log.write("INFO", f"[관리자] PC 등록: {n}")
    elif kind == "agent_deleted":
        daily_log.write("INFO", f"[관리자] PC 삭제: {n}")
    elif kind == "mode_changed":
        daily_log.write("INFO", f"[관리자] 정책 변경: {n} | {d}")
    elif kind == "temp_allow":
        daily_log.write("INFO", f"[관리자] 임시 허용: {n} | {d}")


def log_event(c: sqlite3.Connection, kind: str, detail: str = "",
              agent_id: Optional[int] = None, agent_name: Optional[str] = None) -> None:
    c.execute("INSERT INTO events(ts, agent_id, agent_name, kind, detail) VALUES(?,?,?,?,?)",
              (iso(now_utc()), agent_id, agent_name, kind, detail))
    _file_event(kind, detail, agent_name)


# ───────────────────────── 상태 판정 ─────────────────────────
def expected_mode(a: sqlite3.Row, now: datetime) -> str:
    tu = parse(a["temp_until"])
    return "allow" if tu and tu > now else a["desired_mode"]


def evaluate(a: sqlite3.Row, now: datetime) -> tuple[str, list[str]]:
    """반환: (state, reasons). state: ok | pending | warn | danger | unknown"""
    if not a["last_seen"]:
        return "unknown", ["아직 보고 없음 (에이전트 등록 대기)"]
    last_seen = parse(a["last_seen"])
    age = (now - last_seen).total_seconds()
    if age > OFFLINE_SEC:
        return "danger", [f"{int(age)}초째 보고 없음 (PC 꺼짐 / 네트워크 단절 / 서비스 중지 가능)"]

    r = json.loads(a["report"] or "{}")
    danger: list[str] = []
    warn: list[str] = []

    lt = parse(r.get("last_tamper_utc"))
    if lt and (now - lt).total_seconds() < TAMPER_WINDOW_SEC:
        danger.append("최근 10분 이내 설정 변조 감지 (레지스트리가 수동 변경되어 자동 복구됨)")

    exp = expected_mode(a, now)
    eff = r.get("effective_mode")
    exp_start = 4 if exp == "block" else 3
    exp_wp = 1 if exp == "read_only" else 0
    mismatch: list[str] = []
    if eff != exp:
        mismatch.append(f"정책 미반영 (기대 {exp}, 실제 {eff})")
    if r.get("usbstor_start") != exp_start:
        mismatch.append(f"USBSTOR Start={r.get('usbstor_start')} (기대 {exp_start})")
    if (r.get("write_protect") or 0) != exp_wp:
        mismatch.append(f"WriteProtect={r.get('write_protect')} (기대 {exp_wp})")

    if mismatch:
        # 방금 정책을 바꿨거나 임시허용이 만료된 직후라면 반영 대기로 본다
        ref = parse(a["policy_changed_at"])
        tu = parse(a["temp_until"])
        if tu and tu <= now and (ref is None or tu > ref):
            ref = tu
        if ref and (now - ref).total_seconds() < PENDING_SEC:
            return "pending", ["정책 변경 반영 대기 (다음 보고에서 확인)"]
        warn.extend(mismatch)

    if exp == "block" and r.get("usb_devices"):
        names = ", ".join((d.get("model") or d.get("pnp_id") or "?") for d in r["usb_devices"])
        warn.append(f"차단 모드지만 USB 저장장치가 연결되어 있음: {names} (이미 꽂혀 있던 장치는 재연결 시 차단)")

    if danger:
        return "danger", danger + warn
    if warn:
        return "warn", warn
    return "ok", ["정상: 정책이 적용되어 있고 USB 저장장치 없음" if exp == "block" else "정상"]


def refresh_states(only_id: Optional[int] = None) -> None:
    """상태 변화(정상↔경고↔위험)를 이벤트로 기록."""
    now = now_utc()
    with tx() as c:
        q = "SELECT * FROM agents" + (" WHERE id=?" if only_id else "")
        for a in c.execute(q, (only_id,) if only_id else ()).fetchall():
            state, reasons = evaluate(a, now)
            if state != a["last_state"]:
                if a["last_state"] is not None or state != "unknown":
                    log_event(c, "state", f"{a['last_state'] or '-'} → {state}: {'; '.join(reasons)}",
                              a["id"], a["name"])
                c.execute("UPDATE agents SET last_state=? WHERE id=?", (state, a["id"]))


# ───────────────────────── 앱 ─────────────────────────
init_db()


def log_observe() -> None:
    """현재 PC 상태를 판정해 제어 상태 로그에 기록 (변화 즉시 + 10분 주기 점검 + 14일 초과 파일 정리)."""
    with tx() as c:
        views = agent_views(c, now_utc())
    control_log.observe(views)


@asynccontextmanager
async def lifespan(_: FastAPI):
    daily_log.write("INFO", f"서버 시작 | 로그 위치={LOG_DIR} | 보관 {RETENTION_DAYS}일")
    daily_log.maybe_cleanup()

    async def watcher():
        while True:
            await asyncio.sleep(10)
            try:
                await asyncio.to_thread(refresh_states)
                await asyncio.to_thread(log_observe)
            except Exception as e:      # 감시가 죽지 않도록
                daily_log.write("ERROR", f"감시 루프 오류: {e!r}")
    task = asyncio.create_task(watcher())
    yield
    task.cancel()
    daily_log.write("INFO", "서버 종료")


app = FastAPI(title="UsbGuard Server", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_setting("session_secret"),
    session_cookie="usbguard_session",
    max_age=SESSION_MAX_AGE,
    same_site="strict",
    https_only=False,   # 사내 HTTP 환경. HTTPS 리버스 프록시를 앞에 둘 경우 True 권장
)


def require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(401, "로그인이 필요합니다")


# 로그인 무차별 대입 방지 (IP 단위, 5회 실패 시 5분 잠금)
_login_fail: dict[str, tuple[int, datetime]] = {}


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "?"
    cnt, until = _login_fail.get(ip, (0, datetime.min.replace(tzinfo=timezone.utc)))
    if until > now_utc():
        raise HTTPException(429, f"로그인 시도가 잠겼습니다. {int((until - now_utc()).total_seconds())}초 후 재시도하세요.")
    stored = get_setting("admin_pw")
    if not stored:
        raise HTTPException(503, "관리자 비밀번호가 설정되지 않았습니다. 서버에서 'python manage.py set-admin-password' 를 실행하세요.")
    if not verify_pw(body.password, stored):
        cnt += 1
        _login_fail[ip] = (0, now_utc() + timedelta(minutes=5)) if cnt >= 5 else (cnt, until)
        with tx() as c:
            log_event(c, "admin_login_fail", f"ip={ip}")
        raise HTTPException(401, "비밀번호가 올바르지 않습니다")
    _login_fail.pop(ip, None)
    request.session["admin"] = True
    with tx() as c:
        log_event(c, "admin_login", f"ip={ip}")
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return {"admin": bool(request.session.get("admin"))}


# ───────────────────────── 관리자 API ─────────────────────────
class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class ModeBody(BaseModel):
    mode: Optional[Mode] = None
    temp_minutes: Optional[int] = Field(default=None, ge=1, le=480)


def agent_views(c: sqlite3.Connection, now: datetime) -> list[dict]:
    """PC별 현재 상태 뷰 (GUI 표시와 제어 상태 로그가 같은 판정을 사용)."""
    out = []
    for a in c.execute("SELECT * FROM agents ORDER BY name").fetchall():
        r = json.loads(a["report"] or "{}")
        state, reasons = evaluate(a, now)
        last_seen = parse(a["last_seen"])
        tu = parse(a["temp_until"])
        out.append({
            "id": a["id"],
            "name": a["name"],
            "hostname": r.get("hostname"),
            "version": r.get("version"),
            "state": state,
            "reasons": reasons,
            "desired_mode": a["desired_mode"],
            "expected_mode": expected_mode(a, now),
            "effective_mode": r.get("effective_mode"),
            "temp_until": iso(tu) if tu and tu > now else None,
            "usb_devices": r.get("usb_devices", []),
            "tamper_count": r.get("tamper_count", 0),
            "configured_mode": r.get("configured_mode"),
            "usbstor_start": r.get("usbstor_start"),
            "write_protect": r.get("write_protect"),
            "last_enforce_utc": r.get("last_enforce_utc"),
            "last_tamper_utc": r.get("last_tamper_utc"),
            "created_at": a["created_at"],
            "last_seen": a["last_seen"],
            "age_sec": int((now - last_seen).total_seconds()) if last_seen else None,
            "last_ip": a["last_ip"],
        })
    return out


@app.get("/api/agents", dependencies=[Depends(require_admin)])
def list_agents():
    refresh_states()
    with tx() as c:
        return agent_views(c, now_utc())


@app.post("/api/agents", dependencies=[Depends(require_admin)])
def create_agent(body: AgentCreate):
    token = secrets.token_urlsafe(32)
    try:
        with tx() as c:
            cur = c.execute(
                "INSERT INTO agents(name, token_hash, created_at, policy_changed_at) VALUES(?,?,?,?)",
                (body.name.strip(), token_hash(token), iso(now_utc()), iso(now_utc())))
            log_event(c, "agent_created", "", cur.lastrowid, body.name.strip())
    except sqlite3.IntegrityError:
        raise HTTPException(409, "같은 이름의 PC가 이미 있습니다")
    # 토큰은 이때 한 번만 보여준다 (DB에는 해시만 저장)
    return {"name": body.name.strip(), "token": token}


@app.post("/api/agents/{agent_id}/policy", dependencies=[Depends(require_admin)])
def set_policy(agent_id: int, body: ModeBody):
    if body.mode is None and body.temp_minutes is None:
        raise HTTPException(400, "mode 또는 temp_minutes 가 필요합니다")
    now = now_utc()
    with tx() as c:
        a = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not a:
            raise HTTPException(404, "PC를 찾을 수 없습니다")
        if body.temp_minutes is not None:
            until = now + timedelta(minutes=body.temp_minutes)
            c.execute("UPDATE agents SET temp_until=?, policy_changed_at=? WHERE id=?",
                      (iso(until), iso(now), agent_id))
            log_event(c, "temp_allow", f"{body.temp_minutes}분", agent_id, a["name"])
        else:
            c.execute("UPDATE agents SET desired_mode=?, temp_until=NULL, policy_changed_at=? WHERE id=?",
                      (body.mode, iso(now), agent_id))
            log_event(c, "mode_changed", f"{a['desired_mode']} → {body.mode}", agent_id, a["name"])
    refresh_states(agent_id)
    return {"ok": True}


@app.delete("/api/agents/{agent_id}", dependencies=[Depends(require_admin)])
def delete_agent(agent_id: int):
    with tx() as c:
        a = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not a:
            raise HTTPException(404, "PC를 찾을 수 없습니다")
        c.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        log_event(c, "agent_deleted", "", agent_id, a["name"])
    return {"ok": True}


@app.get("/api/events", dependencies=[Depends(require_admin)])
def events(limit: int = 60):
    limit = max(1, min(limit, 500))
    with tx() as c:
        rows = c.execute("SELECT ts, agent_name, kind, detail FROM events ORDER BY id DESC LIMIT ?",
                         (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/logs", dependencies=[Depends(require_admin)])
def list_logs():
    files = []
    if LOG_DIR.exists():
        for p in sorted(LOG_DIR.iterdir(), reverse=True):
            if FILE_RE.fullmatch(p.name) and p.is_file() and not p.is_symlink():
                files.append({"name": p.name, "size": p.stat().st_size})
    return {"dir": str(LOG_DIR), "retention_days": RETENTION_DAYS, "files": files}


@app.get("/api/logs/{name}", dependencies=[Depends(require_admin)])
def read_log(name: str, tail: int = 500):
    if not FILE_RE.fullmatch(name):          # log_yyyymmdd.log 형식만 허용 (경로 조작 차단)
        raise HTTPException(400, "잘못된 로그 파일명")
    p = LOG_DIR / name
    if not p.is_file() or p.is_symlink():
        raise HTTPException(404, "로그 파일이 없습니다")
    tail = max(1, min(tail, 5000))
    with open(p, encoding="utf-8", errors="replace") as f:
        lines = deque((ln.rstrip("\r\n") for ln in f), maxlen=tail)
    return {"name": name, "lines": list(lines)}


# ───────────────────────── 에이전트 API ─────────────────────────
class UsbDeviceIn(BaseModel):
    model: str = ""
    serial: Optional[str] = None
    pnp_id: str = ""


class HeartbeatIn(BaseModel):
    hostname: str = ""
    version: str = ""
    configured_mode: Optional[Mode] = None
    effective_mode: Optional[Mode] = None
    usbstor_start: Optional[int] = None
    write_protect: Optional[int] = None
    usb_devices: list[UsbDeviceIn] = []
    tamper_count: int = 0
    last_enforce_utc: Optional[str] = None
    last_tamper_utc: Optional[str] = None
    temp_until_utc: Optional[str] = None


_reject_seen: dict[str, datetime] = {}


def _log_reject(ip: Optional[str], why: str) -> None:
    key = f"{ip}|{why}"
    last = _reject_seen.get(key)
    if last and (now_utc() - last).total_seconds() < 600:
        return
    _reject_seen[key] = now_utc()
    daily_log.write("WARNING", f"에이전트 보고 거부: {why} | ip={ip}")


@app.post("/api/agent/heartbeat")
def heartbeat(body: HeartbeatIn, request: Request, authorization: Optional[str] = Header(default=None)):
    ip = request.client.host if request.client else None
    if not authorization or not authorization.lower().startswith("bearer "):
        _log_reject(ip, "토큰 없음")
        raise HTTPException(401, "토큰 필요")
    th = token_hash(authorization[7:].strip())
    now = now_utc()
    with tx() as c:
        a = c.execute("SELECT * FROM agents WHERE token_hash=?", (th,)).fetchone()
        if not a:
            _log_reject(ip, "등록되지 않은 토큰")
            raise HTTPException(401, "등록되지 않은 토큰")
        c.execute("UPDATE agents SET last_seen=?, last_ip=?, report=? WHERE id=?",
                  (iso(now), ip, json.dumps(body.model_dump(), ensure_ascii=False), a["id"]))
        tu = parse(a["temp_until"])
        reply = {
            "mode": a["desired_mode"],
            "temp_until_utc": iso(tu) if tu and tu > now else None,
        }
        agent_id = a["id"]
    refresh_states(agent_id)
    return reply


# ───────────────────────── GUI ─────────────────────────
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-store"})
