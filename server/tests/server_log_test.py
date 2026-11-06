"""서버 로그 통합 테스트:  python server/tests/server_log_test.py   (pip install httpx)

관리자 앱 없이도 서버가 스스로 log_yyyymmdd.log 를 남기고, 14일 초과 파일을 지우는지 검증한다.
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ["USBGUARD_DATA"] = DATA
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import app as A
from fastapi.testclient import TestClient


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)


LOGS = Path(DATA) / "logs"
today = datetime.now()
fn = LOGS / f"log_{today:%Y%m%d}.log"
read = lambda: fn.read_text("utf-8") if fn.exists() else ""

# 보관기간 초과 파일/무관 파일을 미리 만들어 둔다 (서버 시작 시 정리되어야 함)
LOGS.mkdir(parents=True, exist_ok=True)
for days in (13, 14, 15, 40):
    d = today - timedelta(days=days)
    (LOGS / f"log_{d:%Y%m%d}.log").write_text("old\n", "utf-8")
(LOGS / "notes.txt").write_text("keep", "utf-8")

A.set_setting("admin_pw", A.hash_pw("correct-horse-battery"))

with TestClient(A.app) as c:        # lifespan(서버 시작/종료) 실행
    check("서버 시작 기록 (관리자 앱 없이)", "서버 시작 | 로그 위치=" in read() and "보관 14일" in read())
    check("파일명 log_yyyymmdd.log", re.fullmatch(r"log_\d{8}\.log", fn.name) is not None and fn.exists())
    names = {p.name for p in LOGS.iterdir()}
    keep = lambda days: f"log_{(today - timedelta(days=days)):%Y%m%d}.log" in names
    check("14일 이내(오늘-13일) 보관 / 14일째·15일·40일 전 삭제", keep(13) and not keep(14) and not keep(15) and not keep(40))
    check("무관한 파일 보존", "notes.txt" in names)
    check("삭제 내역 로그 기록", "보관기간(14일) 초과 로그 삭제" in read())

    # 비로그인 접근 차단
    check("로그 API 비로그인 401", c.get("/api/logs").status_code == 401 and c.get(f"/api/logs/{fn.name}").status_code == 401)

    c.post("/api/login", json={"password": "bad"})
    c.post("/api/login", json={"password": "correct-horse-battery"})
    tok = c.post("/api/agents", json={"name": "PC1"}).json()["token"]
    aid = c.get("/api/agents").json()[0]["id"]
    H = {"Authorization": f"Bearer {tok}"}
    hb = lambda **kw: c.post("/api/agent/heartbeat", headers=H, json={**{
        "hostname": "WIN-A", "version": "0.2.0", "configured_mode": "block", "effective_mode": "block",
        "usbstor_start": 4, "write_protect": 0, "usb_devices": [], "tamper_count": 0,
        "last_enforce_utc": None, "last_tamper_utc": None, "temp_until_utc": None}, **kw})

    check("미등록 토큰 거부 기록(같은 IP 중복 방지)",
          c.post("/api/agent/heartbeat", json={}, headers={"Authorization": "Bearer nope"}).status_code in (401, 422))
    c.post("/api/agent/heartbeat", json={"hostname": "x"}, headers={"Authorization": "Bearer nope"})
    c.post("/api/agent/heartbeat", json={"hostname": "x"}, headers={"Authorization": "Bearer nope"})

    # 관리자 동작 기록
    t = read()
    check("로그인 실패/성공 기록", "[WARNING] [관리자] 로그인 실패" in t and "[INFO] [관리자] 로그인 |" in t)
    check("PC 등록 기록", "[관리자] PC 등록: PC1" in t)

    # 상태 관찰(감시 루프가 10초마다 호출하는 함수를 직접 호출)
    hb(); A.log_observe()
    check("제어 상태 확인 시작 기록", "[PC1] 제어 상태 확인 시작 | 상태=제어 정상 | 적용정책=차단" in read())

    hb(usb_devices=[{"model": "SanDisk", "serial": "1", "pnp_id": "USB\\X"}]); A.log_observe()
    t = read()
    check("차단 중 USB 연결 WARNING", "[WARNING] [PC1] USB 저장장치 연결 감지: SanDisk" in t and "상태 변경: 제어 정상 → 확인 필요" in t)

    now = A.iso(A.now_utc())
    hb(tamper_count=1, last_tamper_utc=now); A.log_observe()
    check("변조 감지 ERROR", "[ERROR] [PC1] 설정 변조 감지 (+1, 누적 1회)" in read())

    c.post(f"/api/agents/{aid}/policy", json={"mode": "allow"})
    c.post(f"/api/agents/{aid}/policy", json={"temp_minutes": 30})
    A.log_observe()
    t = read()
    check("정책 변경이 한국어로 기록", "[관리자] 정책 변경: PC1 | 차단 → 허용" in t)
    check("임시 허용 기록", "[관리자] 임시 허용: PC1 | 30분" in t)
    check("적용 정책 변경 기록", "적용 정책 변경: 차단 → 허용" in t)

    # 보고 끊김(오프라인)
    with A.tx() as db:
        db.execute("UPDATE agents SET last_seen=?", (A.iso(A.now_utc() - timedelta(seconds=300)),))
    A.log_observe()
    check("오프라인 ERROR 기록", re.search(r"\[ERROR\] \[PC1\] 상태 변경: .* → 오프라인\(확인 불가\)", read()) is not None)

    # ── 로그 조회 API ──
    r = c.get("/api/logs").json()
    check("로그 목록 API: 오늘 파일, 보관일수 14", r["retention_days"] == 14 and r["files"][0]["name"] == fn.name)
    check("목록에는 log_yyyymmdd.log 만", all(re.fullmatch(r"log_\d{8}\.log", f["name"]) for f in r["files"]))
    r = c.get(f"/api/logs/{fn.name}?tail=5").json()
    check("로그 내용 API: tail 5줄", len(r["lines"]) == 5 and r["lines"][-1] == read().rstrip("\n").split("\n")[-1])
    check("경로 조작 차단", c.get("/api/logs/..%2Fusbguard.db").status_code in (400, 404)
          and c.get("/api/logs/notes.txt").status_code == 400
          and c.get("/api/logs/log_20000101.log").status_code == 404)

    c.delete(f"/api/agents/{aid}")
    A.log_observe()
    check("PC 삭제/제거 기록", "[관리자] PC 삭제: PC1" in read() and "PC 목록에서 제거됨: PC1" in read())
    reject_count = read().count("에이전트 보고 거부: 등록되지 않은 토큰")
    check("미등록 토큰 거부는 같은 IP당 1회만 기록", reject_count == 1)

check("서버 종료 기록", "서버 종료" in read())

# ── 감시 루프가 (관리자 앱도, 직접 호출도 없이) 스스로 기록하는지: 10초 주기 대기 ──
import time
with TestClient(A.app) as c2:
    c2.post("/api/login", json={"password": "correct-horse-battery"})
    tok2 = c2.post("/api/agents", json={"name": "PC2"}).json()["token"]
    c2.post("/api/agent/heartbeat", headers={"Authorization": f"Bearer {tok2}"}, json={
        "hostname": "WIN-B", "version": "0.2.0", "configured_mode": "block", "effective_mode": "block",
        "usbstor_start": 4, "write_protect": 0, "usb_devices": [], "tamper_count": 0})
    time.sleep(11.5)
    check("감시 루프가 자동으로 기록(관리자 앱 꺼짐 상태)", "[PC2] 신규 PC 감지 | 상태=제어 정상" in read())
print("ALL OK")
