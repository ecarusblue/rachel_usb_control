"""UsbGuard 관리자 데스크톱 GUI (PySide6)

중앙 서버(server/app.py)의 API 에 접속해 모든 대상 PC 의 USB 제어 현황을 보여주고 정책을 변경한다.
실행:  python usbguard_gui.py
"""
from __future__ import annotations

import json
import re
import sys
import threading
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

REFRESH_MS = 10_000
OFFLINE_SEC = 100

# state -> (라벨, 글자색, 배경색, 띠색)
STATE = {
    "ok":      ("정상",      "#168a4f", "#e1f5e9", "#168a4f"),
    "pending": ("반영 대기", "#3a56d4", "#e6ebff", "#3a56d4"),
    "warn":    ("확인 필요", "#a56200", "#fff2cf", "#e0a100"),
    "danger":  ("위험",      "#c0291b", "#fde6e3", "#c0291b"),
    "unknown": ("미확인",    "#6a7488", "#eceff4", "#9aa3b5"),
}
MODE = {"block": "차단", "read_only": "읽기 전용", "allow": "허용"}
MODE_COLOR = {"block": "#168a4f", "read_only": "#e0a100", "allow": "#c0291b"}

STYLE = """
* { font-family: "Malgun Gothic", "Segoe UI", "Noto Sans CJK KR", sans-serif; font-size: 13px; color: #1b2333; }
QMainWindow, QDialog { background: #f2f4f8; }
QPushButton { background: #fff; border: 1px solid #d5dae5; border-radius: 6px; padding: 5px 12px; }
QPushButton:hover { background: #eef1f7; }
QPushButton[primary="true"] { background: #2b4acb; border-color: #2b4acb; color: #fff; }
QPushButton[primary="true"]:hover { background: #2340b0; }
QPushButton[active="true"] { background: #2b4acb; border-color: #2b4acb; color: #fff; }
QPushButton[danger="true"] { color: #c0291b; }
QLineEdit { background: #fff; border: 1px solid #d5dae5; border-radius: 6px; padding: 6px 10px; }
QListWidget { background: #fff; border: none; }
QListWidget::item { border-bottom: 1px solid #e2e6ee; padding: 6px 2px; }
QScrollArea { border: none; background: transparent; }
QFrame#panel, QFrame#kpi { background: #fff; border: 1px solid #e2e6ee; border-radius: 10px; }
"""


# ───────────────────────── API 클라이언트 ─────────────────────────
class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"} if data else {})
        try:
            with self._opener.open(req, timeout=8) as r:
                return json.loads(r.read() or b"{}")
        except HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except Exception:
                detail = None
            raise ApiError(e.code, str(detail or f"HTTP {e.code}"))
        except (URLError, TimeoutError, OSError) as e:
            raise ApiError(0, f"서버에 연결할 수 없습니다: {getattr(e, 'reason', e)}")

    def login(self, password: str):
        return self.call("POST", "/api/login", {"password": password})


# ───────────────────────── 표시 헬퍼 ─────────────────────────
_FRAC = re.compile(r"(\.\d{6})\d+")


def parse(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(_FRAC.sub(r"\1", s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fmt_time(s: Optional[str], clock_only: bool = False) -> str:
    d = parse(s)
    if not d:
        return "—"
    d = d.astimezone()
    return d.strftime("%H:%M") if clock_only else d.strftime("%m-%d %H:%M:%S")


def fmt_age(sec: Optional[int]) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec}초 전"
    if sec < 3600:
        return f"{sec // 60}분 전"
    return f"{sec // 3600}시간 전"


def is_offline(a: dict) -> bool:
    return a.get("age_sec") is not None and a["age_sec"] > OFFLINE_SEC


def control_text(a: dict) -> tuple[str, str, str]:
    if a["state"] == "unknown":
        return "⏳", "첫 보고 대기 중", "대상 PC에서 enroll 명령을 실행하세요"
    suffix = " (마지막 확인 기준)" if is_offline(a) else ""
    if a.get("temp_until"):
        return "⏱", "임시 허용 중", f"{fmt_time(a['temp_until'], True)}까지 · 이후 {MODE[a['desired_mode']]}으로 자동 복귀"
    m = a["expected_mode"]
    if m == "block":
        return "🔒", "USB 저장장치 차단 중" + suffix, "새로 연결하는 USB 메모리는 인식되지 않음"
    if m == "read_only":
        return "📖", "USB 읽기 전용" + suffix, "읽기만 가능, 쓰기·복사 불가"
    return "🔓", "USB 허용 중" + suffix, "제한 없이 사용 가능"


def verify(a: dict) -> list[tuple[str, str, str]]:
    """제어 검증 3종: (이름, 결과 pass|fail|na, 표시문구)"""
    if a["state"] == "unknown" or a.get("effective_mode") is None or is_offline(a):
        return [("정책 적용", "na", "확인 불가"), ("레지스트리", "na", "확인 불가"), ("USB 장치", "na", "확인 불가")]
    exp = a["expected_mode"]
    exp_start = 4 if exp == "block" else 3
    exp_wp = 1 if exp == "read_only" else 0
    applied = a["effective_mode"] == exp
    reg = a.get("usbstor_start") == exp_start and (a.get("write_protect") or 0) == exp_wp
    n = len(a["usb_devices"])
    if exp == "block":
        dev = ("USB 장치", "pass", "✓ 없음") if n == 0 else ("USB 장치", "fail", f"{n}개 연결")
    else:
        dev = ("USB 장치", "na", "—")
    return [("정책 적용", "pass" if applied else "fail", "✓ 정상" if applied else "✗ 불일치"),
            ("레지스트리", "pass" if reg else "fail", "✓ 정상" if reg else "✗ 불일치"), dev]


CHK_COLOR = {"pass": "#168a4f", "fail": "#c0291b", "na": "#6a7488"}


def label(text: str = "", style: str = "", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if style:
        lb.setStyleSheet(style)
    lb.setWordWrap(wrap)
    return lb


# ───────────────────────── 위젯 ─────────────────────────
class Kpi(QFrame):
    def __init__(self, title: str, value: int, color: str = "#1b2333"):
        super().__init__()
        self.setObjectName("kpi")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(0)
        lay.addWidget(label(title, "color:#6a7488;font-size:12px;border:none"))
        v = label(str(value), f"color:{color};font-size:26px;font-weight:700;border:none")
        lay.addWidget(v)


class PcCard(QFrame):
    def __init__(self, a: dict, win: "MainWindow"):
        super().__init__()
        st_label, st_fg, st_bg, bar = STATE.get(a["state"], STATE["unknown"])
        self.setObjectName("pc")
        self.setStyleSheet(f"QFrame#pc{{background:#fff;border:1px solid #e2e6ee;"
                           f"border-left:6px solid {bar};border-radius:10px;}}")
        self.setMinimumWidth(380)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        # 머리: 이름 + 상태 배지
        head = QHBoxLayout()
        nm = QVBoxLayout()
        nm.setSpacing(0)
        nm.addWidget(label(a["name"], "font-size:16px;font-weight:700;border:none"))
        sub = (a.get("hostname") or "—") + (f" · {a['last_ip']}" if a.get("last_ip") else "")
        nm.addWidget(label(sub, "color:#6a7488;font-size:12px;border:none"))
        head.addLayout(nm, 1)
        badge = label(st_label, f"color:{st_fg};background:{st_bg};border-radius:10px;padding:2px 10px;"
                                f"font-weight:600;font-size:12px;border:none")
        badge.setAlignment(Qt.AlignCenter)
        head.addWidget(badge, 0, Qt.AlignTop)
        root.addLayout(head)

        # 제어 상태
        ico, t1, t2 = control_text(a)
        ctl = QFrame()
        ctl.setStyleSheet("QFrame{background:#f6f8fc;border:none;border-radius:8px;}")
        cl = QHBoxLayout(ctl)
        cl.setContentsMargins(10, 8, 10, 8)
        cl.addWidget(label(ico, "font-size:22px;border:none;background:transparent"))
        tx = QVBoxLayout()
        tx.setSpacing(0)
        tx.addWidget(label(t1, "font-weight:700;border:none;background:transparent"))
        tx.addWidget(label(t2, "color:#6a7488;font-size:12px;border:none;background:transparent", True))
        cl.addLayout(tx, 1)
        root.addWidget(ctl)

        # 제어 검증 3종
        row = QHBoxLayout()
        row.setSpacing(6)
        for name, res, text in verify(a):
            c = QFrame()
            c.setStyleSheet("QFrame{border:1px solid #e2e6ee;border-radius:8px;}")
            cl2 = QVBoxLayout(c)
            cl2.setContentsMargins(6, 4, 6, 4)
            cl2.setSpacing(0)
            n = label(name, "font-size:11px;border:none")
            n.setAlignment(Qt.AlignCenter)
            v = label(text, f"color:{CHK_COLOR[res]};font-weight:700;border:none")
            v.setAlignment(Qt.AlignCenter)
            cl2.addWidget(n)
            cl2.addWidget(v)
            row.addWidget(c, 1)
        root.addLayout(row)

        # 사유
        bad = a["state"] in ("danger", "warn")
        root.addWidget(label("\n".join(a["reasons"]),
                             f"color:{'#c0291b' if bad else '#6a7488'};font-size:12px;border:none", True))

        # 상세 정보
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(1)
        form.setLabelAlignment(Qt.AlignLeft)

        def add(k: str, v: str):
            form.addRow(label(k, "color:#6a7488;font-size:12px;border:none"), label(v, "font-size:12px;border:none", True))

        add("마지막 보고", fmt_age(a.get("age_sec")))
        add("목표 / PC 보고", f"{MODE.get(a['desired_mode'], '—')} / {MODE.get(a.get('effective_mode'), '—')}")
        add("USBSTOR / WP", f"Start={a.get('usbstor_start', '—')} / WriteProtect={a.get('write_protect', '—')}")
        add("정책 재적용", fmt_time(a.get("last_enforce_utc")))
        tamper = f"{a.get('tamper_count', 0)}회"
        if a.get("last_tamper_utc"):
            tamper += f" (최근 {fmt_time(a['last_tamper_utc'])})"
        add("변조 감지", tamper)
        add("에이전트", (f"v{a['version']}" if a.get("version") else "—") + f" · 등록 {fmt_time(a.get('created_at'))}")
        root.addLayout(form)

        # 연결된 USB (허용/읽기전용 모드에서의 참고 정보)
        if a["usb_devices"] and a["expected_mode"] != "block" and not is_offline(a):
            names = "\n".join(d.get("model") or d.get("pnp_id") or "?" for d in a["usb_devices"])
            root.addWidget(label("🔌 연결된 USB 저장장치\n" + names,
                                 "background:#fff2cf;color:#a56200;border-radius:6px;padding:6px 8px;font-size:12px;border:none", True))

        # 정책 변경 버튼
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#e2e6ee")
        root.addWidget(line)
        btns = QHBoxLayout()
        btns.setSpacing(4)
        for mode in ("block", "read_only", "allow"):
            b = QPushButton(MODE[mode])
            if a["desired_mode"] == mode and not a.get("temp_until"):
                b.setStyleSheet("QPushButton{background:#2b4acb;border:1px solid #2b4acb;color:#fff;"
                                "border-radius:6px;padding:5px 12px;font-weight:600}")
            b.clicked.connect(lambda _=False, i=a["id"], m=mode: win.set_mode(i, m))
            btns.addWidget(b)
        t = QPushButton("30분 허용")
        t.clicked.connect(lambda _=False, i=a["id"]: win.temp_allow(i, 30))
        btns.addWidget(t)
        btns.addStretch(1)
        d = QPushButton("삭제")
        d.setStyleSheet("QPushButton{color:#c0291b}")
        d.clicked.connect(lambda _=False, i=a["id"], n=a["name"]: win.delete_agent(i, n))
        btns.addWidget(d)
        root.addLayout(btns)


class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UsbGuard 관리 콘솔 로그인")
        self.setFixedWidth(380)
        self.settings = QSettings("UsbGuard", "AdminGui")
        self.api: Optional[Api] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(label("🔒 UsbGuard 현황판", "font-size:18px;font-weight:700"))
        lay.addWidget(label("서버 주소"))
        self.url = QLineEdit(self.settings.value("server_url", "http://localhost:8080"))
        lay.addWidget(self.url)
        lay.addWidget(label("관리자 비밀번호"))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.returnPressed.connect(self.try_login)
        lay.addWidget(self.pw)
        self.err = label("", "color:#c0291b", True)
        lay.addWidget(self.err)
        btn = QPushButton("로그인")
        btn.setProperty("primary", True)
        btn.clicked.connect(self.try_login)
        lay.addWidget(btn)
        self.pw.setFocus()

    def try_login(self):
        api = Api(self.url.text().strip())
        self.err.setText("")
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            api.login(self.pw.text())
        except ApiError as e:
            self.err.setText(str(e))
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.settings.setValue("server_url", self.url.text().strip())
        self.api = api
        self.accept()


class AddDialog(QDialog):
    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("PC 등록")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.addWidget(label("등록할 PC 이름"))
        row = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("예: 공부방-PC1")
        self.name.setMaxLength(64)
        self.name.returnPressed.connect(self.create)
        row.addWidget(self.name, 1)
        self.btn = QPushButton("등록하고 토큰 발급")
        self.btn.setProperty("primary", True)
        self.btn.clicked.connect(self.create)
        row.addWidget(self.btn)
        lay.addLayout(row)
        self.info = label("", "", True)
        lay.addWidget(self.info)
        self.cmd = QLineEdit()
        self.cmd.setReadOnly(True)
        self.cmd.hide()
        lay.addWidget(self.cmd)
        self.copy = QPushButton("명령 복사")
        self.copy.hide()
        self.copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.cmd.text()))
        lay.addWidget(self.copy, 0, Qt.AlignRight)

    def create(self):
        name = self.name.text().strip()
        if not name:
            return
        def ok(r):
            self.info.setText(f"<b>{r['name']}</b> 등록 완료. 토큰은 <b>지금 한 번만</b> 표시됩니다.<br>"
                              "대상 PC에서 <b>관리자 권한 명령 프롬프트</b>로 아래 명령을 실행하세요.")
            self.cmd.setText(f"usbguard enroll {self.win.api.base} {r['token']}")
            self.cmd.show()
            self.copy.show()
            self.name.clear()
            self.win.refresh()
        self.win.run(lambda: self.win.api.call("POST", "/api/agents", {"name": name}), ok,
                     lambda e: self.info.setText(f"<span style='color:#c0291b'>{e}</span>"))


class Bus(QObject):
    call = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self, api: Api):
        super().__init__()
        self.api = api
        self._busy = False
        self.setWindowTitle("UsbGuard USB 제어 현황판")
        self.resize(1320, 900)
        self.bus = Bus()
        self.bus.call.connect(lambda f: f())

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        # 상단 바
        top = QHBoxLayout()
        top.addWidget(label("🔒 UsbGuard USB 제어 현황판", "font-size:17px;font-weight:700"))
        top.addStretch(1)
        self.status = label("연결 중…", "color:#6a7488;font-size:12px")
        top.addWidget(self.status)
        add = QPushButton("+ PC 등록")
        add.clicked.connect(lambda: AddDialog(self).exec())
        top.addWidget(add)
        out = QPushButton("로그아웃")
        out.clicked.connect(self.logout)
        top.addWidget(out)
        root.addLayout(top)

        # 배너 + KPI
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        root.addWidget(self.banner)
        self.kpi_row = QHBoxLayout()
        self.kpi_row.setSpacing(12)
        root.addLayout(self.kpi_row)

        # 본문: PC 카드 + 우측 패널
        body = QHBoxLayout()
        body.setSpacing(14)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.cards_host = QWidget()
        self.cards_host.setStyleSheet("background:transparent")
        self.grid = QGridLayout(self.cards_host)
        self.grid.setContentsMargins(0, 0, 4, 0)
        self.grid.setSpacing(12)
        self.scroll.setWidget(self.cards_host)
        body.addWidget(self.scroll, 1)

        side = QVBoxLayout()
        side.setSpacing(12)
        dist = QFrame()
        dist.setObjectName("panel")
        dl = QVBoxLayout(dist)
        dl.addWidget(label("정책 분포", "font-weight:700;border:none"))
        self.dist_bar = QHBoxLayout()
        self.dist_bar.setSpacing(0)
        dl.addLayout(self.dist_bar)
        self.dist_legend = label("", "color:#6a7488;font-size:12px;border:none", True)
        dl.addWidget(self.dist_legend)
        side.addWidget(dist)
        ev = QFrame()
        ev.setObjectName("panel")
        el = QVBoxLayout(ev)
        el.addWidget(label("최근 이벤트", "font-weight:700;border:none"))
        self.events = QListWidget()
        self.events.setWordWrap(True)
        self.events.setFocusPolicy(Qt.NoFocus)
        self.events.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        el.addWidget(self.events, 1)
        side.addWidget(ev, 1)
        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setFixedWidth(340)
        body.addWidget(side_w)
        root.addLayout(body, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.refresh()

    # ── 비동기 호출 (UI 멈춤 방지) ──
    def run(self, fn: Callable, ok: Optional[Callable] = None, err: Optional[Callable] = None):
        def worker():
            try:
                r = fn()
            except ApiError as e:
                self.bus.call.emit(lambda e=e: self._on_err(e, err))
            else:
                if ok:
                    self.bus.call.emit(lambda r=r: ok(r))
        threading.Thread(target=worker, daemon=True).start()

    def _on_err(self, e: ApiError, err: Optional[Callable]):
        if e.code == 401:
            self.relogin()
        elif err:
            err(e)
        else:
            QMessageBox.warning(self, "오류", str(e))

    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def fetch():
            return self.api.call("GET", "/api/agents"), self.api.call("GET", "/api/events?limit=40")

        def ok(r):
            self._busy = False
            self.apply(*r)
            self.status.setText("● 10초 자동 갱신 · " + datetime.now().strftime("%H:%M:%S"))

        def err(e):
            self._busy = False
            self.status.setText(f"갱신 실패: {e}")

        def on_err(e):          # run() 의 401 분기에서도 busy 해제
            self._busy = False
            err(e)
        self.run(fetch, ok, on_err)

    # ── 렌더링 ──
    def apply(self, agents: list, events: list):
        self.render_summary(agents)
        self.render_cards(agents)
        self.render_events(events)

    def render_summary(self, ags: list):
        n = len(ags)
        cnt = lambda s: sum(1 for a in ags if a["state"] == s)
        ok, warn, danger, pend, unk = cnt("ok"), cnt("warn"), cnt("danger"), cnt("pending"), cnt("unknown")
        blocked = sum(1 for a in ags if a["expected_mode"] == "block" and a["state"] in ("ok", "pending"))
        usb = sum(len(a["usb_devices"]) for a in ags)
        tamper = sum(a.get("tamper_count") or 0 for a in ags)

        icon, title, sub, fg, bg = "✅", "모든 PC의 USB 제어가 정상입니다", f"{n}대 중 {ok}대 정상", "#168a4f", "#e1f5e9"
        if n == 0:
            icon, title, sub, fg, bg = "➕", "등록된 PC가 없습니다", "“+ PC 등록”으로 대상 PC를 추가하세요", "#6a7488", "#eceff4"
        elif danger:
            icon, title, sub, fg, bg = "🚨", f"위험 {danger}대 — 즉시 확인이 필요합니다", "보고 끊김 또는 설정 변조가 감지되었습니다", "#c0291b", "#fde6e3"
        elif warn:
            icon, title, sub, fg, bg = "⚠", f"확인 필요 {warn}대", "정책 불일치 또는 차단 모드에서 USB 저장장치가 연결되어 있습니다", "#a56200", "#fff2cf"
        elif pend or unk:
            icon, title, sub, fg, bg = "⏳", "반영/첫 보고를 기다리는 PC가 있습니다", f"반영 대기 {pend}대 · 미확인 {unk}대", "#6a7488", "#eceff4"
        self.banner.setText(f"<span style='font-size:20px'>{icon}</span>&nbsp; <b style='font-size:15px'>{title}</b>"
                            f"<br><span style='font-size:12px'>{sub}</span>")
        self.banner.setStyleSheet(f"background:{bg};color:{fg};border-radius:10px;padding:12px 16px;")

        while self.kpi_row.count():
            w = self.kpi_row.takeAt(0).widget()
            if w:
                w.deleteLater()
        for t, v, c in [("관리 PC", n, "#1b2333"), ("정상", ok, "#168a4f"),
                        ("확인 필요", warn, "#a56200" if warn else "#1b2333"),
                        ("위험 / 오프라인", danger, "#c0291b" if danger else "#1b2333"),
                        ("차단 적용 중", blocked, "#168a4f"),
                        ("연결된 USB 저장장치", usb, "#a56200" if usb else "#1b2333")]:
            self.kpi_row.addWidget(Kpi(t, v, c), 1)

        while self.dist_bar.count():
            w = self.dist_bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        total = n or 1
        legend = []
        for m in ("block", "read_only", "allow"):
            c = sum(1 for a in ags if a["expected_mode"] == m)
            legend.append(f"<span style='color:{MODE_COLOR[m]}'>■</span> {MODE[m]} {c}")
            if c:
                seg = QFrame()
                seg.setFixedHeight(10)
                seg.setStyleSheet(f"background:{MODE_COLOR[m]};border:none")
                self.dist_bar.addWidget(seg, c)
        if not n:
            seg = QFrame()
            seg.setFixedHeight(10)
            seg.setStyleSheet("background:#eceff4;border:none")
            self.dist_bar.addWidget(seg)
        if tamper:
            legend.append(f"변조 감지 누적 {tamper}회")
        self.dist_legend.setText("&nbsp;&nbsp;".join(legend))

    def render_cards(self, ags: list):
        pos = self.scroll.verticalScrollBar().value()
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        cols = 2 if self.width() >= 1100 else 1
        for i, a in enumerate(ags):
            self.grid.addWidget(PcCard(a, self), i // cols, i % cols, Qt.AlignTop)
        for c in range(cols):
            self.grid.setColumnStretch(c, 1)
        self.grid.setRowStretch(len(ags) // cols + 1, 1)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(pos))

    def render_events(self, evs: list):
        kind = {"state": "상태 변화", "mode_changed": "정책 변경", "temp_allow": "임시 허용", "agent_created": "PC 등록",
                "agent_deleted": "PC 삭제", "admin_login": "관리자 로그인", "admin_login_fail": "로그인 실패"}
        self.events.clear()
        for e in evs:
            head = kind.get(e["kind"], e["kind"]) + (f" · {e['agent_name']}" if e.get("agent_name") else "")
            text = f"{fmt_time(e['ts'])}\n{head}" + (f"\n{e['detail']}" if e.get("detail") else "")
            self.events.addItem(QListWidgetItem(text))

    # ── 동작 ──
    def _policy(self, agent_id: int, body: dict):
        self.run(lambda: self.api.call("POST", f"/api/agents/{agent_id}/policy", body), lambda _: self.refresh())

    def set_mode(self, agent_id: int, mode: str):
        self._policy(agent_id, {"mode": mode})

    def temp_allow(self, agent_id: int, minutes: int):
        self._policy(agent_id, {"temp_minutes": minutes})

    def delete_agent(self, agent_id: int, name: str):
        if QMessageBox.question(self, "PC 삭제",
                                f"'{name}' 을(를) 삭제할까요?\n(해당 PC는 서버 보고가 거부되고, 마지막 정책을 유지합니다)") != QMessageBox.Yes:
            return
        self.run(lambda: self.api.call("DELETE", f"/api/agents/{agent_id}"), lambda _: self.refresh())

    def logout(self):
        try:
            self.api.call("POST", "/api/logout")
        except ApiError:
            pass
        self.relogin()

    def relogin(self):
        self.timer.stop()
        dlg = LoginDialog()
        if dlg.exec() == QDialog.Accepted and dlg.api:
            self.api = dlg.api
            self._busy = False
            self.timer.start(REFRESH_MS)
            self.refresh()
        else:
            QApplication.quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    dlg = LoginDialog()
    if dlg.exec() != QDialog.Accepted or not dlg.api:
        return 0
    win = MainWindow(dlg.api)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
