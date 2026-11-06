import sys, time, threading
from datetime import timedelta
import os; R=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..",".."); sys.path.insert(0, os.path.join(R,"server")); sys.path.insert(0, os.path.join(R,"admin_gui"))
import app as A, uvicorn
from fastapi.testclient import TestClient

A.set_setting("admin_pw", A.hash_pw("demo-password-1"))
c = TestClient(A.app); c.post("/api/login", json={"password": "demo-password-1"})
toks = {n: c.post("/api/agents", json={"name": n}).json()["token"] for n in ["공부방-PC1", "공부방-PC2", "상담실-PC"]}
def hb(tok, **kw):
    b = {"hostname":"WIN-X","version":"0.2.0","configured_mode":"block","effective_mode":"block","usbstor_start":4,"write_protect":0,
         "usb_devices":[],"tamper_count":0,"last_enforce_utc":A.iso(A.now_utc()),"last_tamper_utc":None,"temp_until_utc":None}
    b.update(kw); return c.post("/api/agent/heartbeat", json=b, headers={"Authorization":f"Bearer {tok}"})
hb(toks["공부방-PC1"], hostname="STUDYROOM-PC1")
hb(toks["공부방-PC2"], hostname="STUDYROOM-PC2", usb_devices=[{"model":"SanDisk Ultra USB Device","serial":"1","pnp_id":"USB\\X"}])
hb(toks["상담실-PC"], hostname="OFFICE-PC")
with A.tx() as db:
    db.execute("UPDATE agents SET policy_changed_at=?", (A.iso(A.now_utc()-timedelta(hours=1)),))
    db.execute("UPDATE agents SET last_seen=? WHERE name='상담실-PC'", (A.iso(A.now_utc()-timedelta(minutes=7)),))
A.refresh_states()

srv = uvicorn.Server(uvicorn.Config(A.app, host="127.0.0.1", port=8766, log_level="error"))
threading.Thread(target=srv.run, daemon=True).start(); time.sleep(1.5)

from PySide6.QtWidgets import QApplication
import usbguard_gui as G
app = QApplication([]); app.setStyleSheet(G.STYLE)
def pump(sec):
    end = time.time() + sec
    while time.time() < end: app.processEvents(); time.sleep(0.02)
def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)

# 로그인 대화상자 동작
d = G.LoginDialog(); d.url.setText("http://127.0.0.1:8766"); d.pw.setText("wrong"); d.try_login()
check("틀린 비밀번호 -> 오류 문구", "올바르지" in d.err.text() and d.api is None)
d.url.setText("http://127.0.0.1:1"); d.pw.setText("x"); d.try_login()
check("서버 없음 -> 연결 오류 문구", "연결할 수 없습니다" in d.err.text())
d.url.setText("http://127.0.0.1:8766"); d.pw.setText("demo-password-1"); d.try_login()
check("로그인 성공", d.api is not None and d.result() == 1)

w = G.MainWindow(d.api); w.show(); pump(2.5)
check("PC 카드 3개 렌더", sum(1 for i in range(w.grid.count()) if isinstance(w.grid.itemAt(i).widget(), G.PcCard)) == 3)
check("이벤트 목록 채움", w.events.count() > 0)
check("KPI 6개", w.kpi_row.count() == 6)
w.grab().save("/tmp/gui.png")

# 정책 변경: PC1 -> allow
a1 = next(a for a in d.api.call("GET","/api/agents") if a["name"]=="공부방-PC1")
w.set_mode(a1["id"], "allow"); pump(1.5)
a1 = next(a for a in d.api.call("GET","/api/agents") if a["name"]=="공부방-PC1")
check("GUI에서 정책 변경(allow) 반영", a1["desired_mode"] == "allow")
w.temp_allow(a1["id"], 30); pump(1.5)
a1 = next(a for a in d.api.call("GET","/api/agents") if a["name"]=="공부방-PC1")
check("30분 임시 허용 반영", a1["temp_until"] is not None)

# PC 등록 대화상자
ad = G.AddDialog(w); ad.name.setText("신규-PC"); ad.create(); pump(1.5)
check("PC 등록 + enroll 명령 표시", ad.cmd.text().startswith("usbguard enroll http://127.0.0.1:8766 ") and not ad.cmd.isHidden())
check("서버에 신규 PC 존재", any(a["name"]=="신규-PC" for a in d.api.call("GET","/api/agents")))

# 삭제 (확인창 자동 Yes)
from PySide6.QtWidgets import QMessageBox
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
nid = next(a["id"] for a in d.api.call("GET","/api/agents") if a["name"]=="신규-PC")
w.delete_agent(nid, "신규-PC"); pump(1.5)
check("PC 삭제", not any(a["name"]=="신규-PC" for a in d.api.call("GET","/api/agents")))

# 세션 만료(401) 처리 -> 재로그인 흐름
called = []
w.relogin = lambda: called.append(1)
d.api.call("POST", "/api/logout"); w.refresh(); pump(1.5)
check("401 -> 재로그인 호출", called)
w.timer.stop(); srv.should_exit = True
print("ALL OK")
