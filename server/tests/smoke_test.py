import os, sys, tempfile, json
from datetime import timedelta
os.environ["USBGUARD_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import app as A
from fastapi.testclient import TestClient

A.set_setting("admin_pw", A.hash_pw("correct-horse-battery"))
c = TestClient(A.app)
def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)

# 인증
check("미로그인 목록 401", c.get("/api/agents").status_code == 401)
check("틀린 비밀번호 401", c.post("/api/login", json={"password": "x"}).status_code == 401)
check("로그인 성공", c.post("/api/login", json={"password": "correct-horse-battery"}).status_code == 200)

# PC 등록
r = c.post("/api/agents", json={"name": "PC1"}); check("PC 등록", r.status_code == 200)
tok = r.json()["token"]
check("중복 이름 409", c.post("/api/agents", json={"name": "PC1"}).status_code == 409)
check("등록 직후 unknown", c.get("/api/agents").json()[0]["state"] == "unknown")
aid = c.get("/api/agents").json()[0]["id"]

hb = lambda **kw: {**{"hostname":"WIN-A","version":"0.2.0","configured_mode":"block","effective_mode":"block",
      "usbstor_start":4,"write_protect":0,"usb_devices":[],"tamper_count":0,
      "last_enforce_utc":"2026-11-06T04:00:00.1234567Z","last_tamper_utc":None,"temp_until_utc":None}, **kw}
H = {"Authorization": f"Bearer {tok}"}

check("토큰 없음 401", c.post("/api/agent/heartbeat", json=hb()).status_code == 401)
check("잘못된 토큰 401", c.post("/api/agent/heartbeat", json=hb(), headers={"Authorization":"Bearer bad"}).status_code == 401)

# 정책 변경 직후엔 pending 이어야 하지만, 등록 시 policy_changed_at 이 지금이므로 실제값이 일치하면 ok
r = c.post("/api/agent/heartbeat", json=hb(), headers=H)
check("heartbeat 응답 정책(block)", r.status_code == 200 and r.json()["mode"] == "block")
a = c.get("/api/agents").json()[0]
check("정상 판정(ok)", a["state"] == "ok")

# 차단인데 USB 연결됨 -> warn
c.post("/api/agent/heartbeat", json=hb(usb_devices=[{"model":"SanDisk","serial":"1","pnp_id":"USB\\X"}]), headers=H)
a = c.get("/api/agents").json()[0]
check("차단 모드+USB 연결 -> warn", a["state"] == "warn" and "SanDisk" in " ".join(a["reasons"]))

# 최근 변조 -> danger
now = A.iso(A.now_utc()).replace("Z", "0Z")  # 7자리 소수초
c.post("/api/agent/heartbeat", json=hb(tamper_count=1, last_tamper_utc=now), headers=H)
check("최근 변조 -> danger", c.get("/api/agents").json()[0]["state"] == "danger")

# 정책 변경 allow -> 에이전트 아직 block 보고 -> pending, 응답엔 allow
c.post(f"/api/agents/{aid}/policy", json={"mode":"allow"})
check("정책 변경 직후 pending", c.get("/api/agents").json()[0]["state"] == "pending")
r = c.post("/api/agent/heartbeat", json=hb(), headers=H)
check("heartbeat 응답이 allow", r.json()["mode"] == "allow")
# 에이전트가 allow 적용했다고 보고
c.post("/api/agent/heartbeat", json=hb(configured_mode="allow", effective_mode="allow", usbstor_start=3), headers=H)
check("allow 반영 후 ok", c.get("/api/agents").json()[0]["state"] == "ok")

# 임시허용 후 block 복귀 확인용: read_only
c.post(f"/api/agents/{aid}/policy", json={"mode":"read_only"})
c.post("/api/agent/heartbeat", json=hb(configured_mode="read_only", effective_mode="read_only", usbstor_start=3, write_protect=1), headers=H)
check("read_only 반영 후 ok", c.get("/api/agents").json()[0]["state"] == "ok")

# 임시 허용
c.post(f"/api/agents/{aid}/policy", json={"temp_minutes":30})
r = c.post("/api/agent/heartbeat", json=hb(configured_mode="read_only", effective_mode="allow", usbstor_start=3, write_protect=0), headers=H)
check("임시허용 응답 temp_until 포함", r.json()["temp_until_utc"] is not None)
check("임시허용 반영 후 ok", c.get("/api/agents").json()[0]["state"] == "ok")
check("잘못된 minutes 422", c.post(f"/api/agents/{aid}/policy", json={"temp_minutes":9999}).status_code == 422)

# 오프라인: last_seen 을 과거로
with A.tx() as db:
    db.execute("UPDATE agents SET last_seen=?", (A.iso(A.now_utc() - timedelta(seconds=300)),))
a = c.get("/api/agents").json()[0]
check("보고 끊김 -> danger(오프라인)", a["state"] == "danger" and "보고 없음" in a["reasons"][0])

# 이벤트 기록
ev = c.get("/api/events").json()
kinds = {e["kind"] for e in ev}
check("이벤트 기록(state/mode_changed/temp_allow/agent_created/admin_login)",
      {"state","mode_changed","temp_allow","agent_created","admin_login"} <= kinds)

# 삭제 후 토큰 거부
check("PC 삭제", c.delete(f"/api/agents/{aid}").status_code == 200)
check("삭제된 PC 토큰 401", c.post("/api/agent/heartbeat", json=hb(), headers=H).status_code == 401)

# 로그인 잠금
c2 = TestClient(A.app)
for _ in range(5): c2.post("/api/login", json={"password":"bad"})
check("5회 실패 후 잠금(429)", c2.post("/api/login", json={"password":"correct-horse-battery"}).status_code == 429)

# GUI 서빙, 로그아웃
check("GUI 서빙", "UsbGuard" in c.get("/").text)
c.post("/api/logout"); check("로그아웃 후 401", c.get("/api/agents").status_code == 401)
print("ALL OK")
