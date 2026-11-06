"""PC 생존 확인(ping) 기반 '꺼짐 vs 에이전트 중지' 구분 테스트:  python server/tests/reachability_test.py"""
import os, sys, tempfile
from datetime import timedelta
DATA = tempfile.mkdtemp()
os.environ["USBGUARD_DATA"] = DATA
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import app as A
import reachability
from fastapi.testclient import TestClient
from pathlib import Path
from datetime import datetime


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)


# ── ping 출력 해석 / 입력 검증 ──
check("TTL 있는 응답 = 성공", reachability.parse_reply("Reply from 10.0.0.5: bytes=32 time<1ms TTL=128"))
check("'대상 호스트 연결 불가'(TTL 없음) = 실패", not reachability.parse_reply("Reply from 10.0.0.1: Destination host unreachable."))
check("빈 출력 = 실패", not reachability.parse_reply(""))
check("IP 아닌 값은 실행 없이 실패(명령 주입 방지)", reachability.ping("1.1.1.1; rm -rf /") is False)

# ── 서버 상태 판정 ──
A.set_setting("admin_pw", A.hash_pw("correct-horse-battery"))
c = TestClient(A.app)
c.post("/api/login", json={"password": "correct-horse-battery"})
tok = c.post("/api/agents", json={"name": "PC1"}).json()["token"]
H = {"Authorization": f"Bearer {tok}"}
hb = {"hostname": "WIN-A", "version": "0.2.0", "configured_mode": "block", "effective_mode": "block",
      "usbstor_start": 4, "write_protect": 0, "usb_devices": [], "tamper_count": 0}
c.post("/api/agent/heartbeat", json=hb, headers=H)
aid = c.get("/api/agents").json()[0]["id"]

PINGS = {"alive": True, "calls": 0}
def fake(ip):
    PINGS["calls"] += 1
    return PINGS["alive"]
A.PROBE = fake

def set_age(sec):
    with A.tx() as cx:
        cx.execute("UPDATE agents SET last_seen=? WHERE id=?", (A.iso(A.now_utc() - timedelta(seconds=sec)), aid))
def view():
    return c.get("/api/agents").json()[0]
def reset_cache():
    A._reach.clear()
def probe_at(dt_offset=0):
    A.probe_agents(A.now_utc() + timedelta(seconds=dt_offset))
    A.refresh_states()

# 1) 정상 보고 중 + ping 응답 -> ping_ok 학습
probe_at()
v = view()
check("정상 PC ping 응답 -> ping_ok 학습", v["ping_ok"] is True and v["state"] == "ok" and v["cause"] is None)
n = PINGS["calls"]; probe_at(5)
check("정상 PC 는 자주 ping 하지 않음(10분 주기)", PINGS["calls"] == n)

# 2) 보고 끊김 + ping 응답 -> 에이전트 중지 의심 (danger)
set_age(300); reset_cache()
probe_at()
v = view()
check("보고 없음 + ping 응답 -> danger/agent_down_suspected", v["state"] == "danger" and v["cause"] == "agent_down_suspected")
check("사유에 '중지/제거 의심' 표기", "의심" in v["reasons"][0] and v["reach"] is True)

# 3) 보고 끊김 + ping 무응답(평소엔 응답) -> offline (꺼짐 추정)
PINGS["alive"] = False; reset_cache()
probe_at()
v = view()
check("보고 없음 + ping 무응답 -> offline/pc_off", v["state"] == "offline" and v["cause"] == "pc_off")
check("offline 사유에 '꺼짐' 표기", "꺼짐" in v["reasons"][0])

# 4) 꺼짐 추정이 24시간 넘게 지속 -> danger 로 격상
set_age(A.OFFLINE_ESCALATE_SEC + 60); reset_cache()
probe_at()
v = view()
check("24시간 초과 무응답 -> danger/pc_off_long", v["state"] == "danger" and v["cause"] == "pc_off_long")

# 5) ICMP 차단 PC(ping_ok=0): 무응답을 '꺼짐'으로 단정하지 않음 (제거가 가려지지 않게 danger 유지)
with A.tx() as cx:
    cx.execute("UPDATE agents SET ping_ok=0 WHERE id=?", (aid,))
set_age(300); reset_cache()
probe_at()
v = view()
check("ping 불가 PC 의 무응답 -> danger/unknown (꺼짐으로 단정 안 함)", v["state"] == "danger" and v["cause"] == "unknown")

# 6) ping 결과가 오래되면(캐시 만료) 판정 보류 -> danger/unknown
with A.tx() as cx:
    cx.execute("UPDATE agents SET ping_ok=1 WHERE id=?", (aid,))
PINGS["alive"] = False; reset_cache(); probe_at()
check("무응답 판정 전제 확인", view()["state"] == "offline")
A._reach[aid]["at"] = A.now_utc() - timedelta(seconds=A.REACH_FRESH_SEC + 5)
check("ping 결과 만료 -> danger/unknown", view()["state"] == "danger" and view()["cause"] == "unknown")

# 7) 복구: 다시 보고 오면 ok
c.post("/api/agent/heartbeat", json=hb, headers=H)
PINGS["alive"] = True; reset_cache(); probe_at()
v = view()
check("보고 재개 -> ok, cause 없음", v["state"] == "ok" and v["cause"] is None)

# 8) 정상 보고 중 ping 이 막히면 ping_ok 0 으로 갱신
PINGS["alive"] = False; reset_cache(); probe_at()
check("정상 보고 중 ping 불가 -> ping_ok=0", view()["ping_ok"] is False and view()["state"] == "ok")

# 9) 로그 라벨/레벨
import logstore
base = {"state": "danger", "age_sec": 300, "cause": "agent_down_suspected"}
check("라벨: 에이전트 중지 의심", logstore.state_label(base) == "에이전트 중지 의심")
check("라벨: offline", logstore.state_label({"state": "offline", "age_sec": 300}) == "PC 꺼짐(추정)")
check("라벨: 원인 불명", logstore.state_label({"state": "danger", "age_sec": 300, "cause": "unknown"}) == "오프라인(확인 불가)")
check("레벨: offline=INFO, danger=ERROR", logstore.LEVEL_OF_STATE["offline"] == "INFO" and logstore.LEVEL_OF_STATE["danger"] == "ERROR")

# 10) 서버 로그 파일: 에이전트 중지 의심이 ERROR 로 기록되고, 학습 결과(ping 불가)가 WARNING 으로 남음
set_age(300); reset_cache(); PINGS["alive"] = True
with A.tx() as cx:
    cx.execute("UPDATE agents SET ping_ok=1 WHERE id=?", (aid,))
probe_at(); A.log_observe()
log = (Path(DATA) / "logs" / f"log_{datetime.now():%Y%m%d}.log").read_text("utf-8")
check("로그: ERROR + 에이전트 중지 의심", "[ERROR]" in log and "에이전트 중지 의심" in log)
check("로그: ping 불가 학습 WARNING", "[WARNING] [PC1] 생존 확인: ping 응답 없음" in log)
print("ALL OK")
