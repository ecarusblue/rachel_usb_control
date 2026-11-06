"""관리자 앱 제어 상태 로그 저장

- 파일명: log_yyyymmdd.log (로컬 날짜 기준, 하루 1개, UTF-8, 자정이 지나면 자동으로 새 파일)
- 보관: 오늘 포함 14일치만 유지하고, 그보다 오래된 log_yyyymmdd.log 는 자동 삭제
        (이름이 log_yyyymmdd.log 형식이 아닌 파일은 절대 건드리지 않음)
- 위치: %LOCALAPPDATA%\\UsbGuardAdmin\\logs  (환경변수 USBGUARD_LOG_DIR 로 변경 가능)

기록 내용 (ControlLogger)
  * 프로그램 시작/종료, 관리자 동작(정책 변경, PC 등록·삭제), 서버 연결 끊김/복구
  * PC별 제어 상태 "변화" 즉시 기록: 상태 변경, 정책 변경, USB 저장장치 연결/해제, 설정 변조 감지
  * PC별 제어 상태 "스냅샷" 주기 기록(기본 10분): 변화가 없어도 제어가 유지되고 있음을 남김
"""
from __future__ import annotations

import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

RETENTION_DAYS = 14        # 오늘 포함 14일치 보관
SNAPSHOT_SEC = 600         # 변화가 없어도 이 주기로 현재 제어 상태를 기록
OFFLINE_SEC = 100          # server/app.py 의 OFFLINE_SEC 와 동일 기준
FILE_RE = re.compile(r"^log_(\d{8})\.log$")

STATE_LABEL = {"ok": "제어 정상", "pending": "반영 대기", "warn": "확인 필요", "danger": "위험", "unknown": "미확인"}
MODE_LABEL = {"block": "차단", "read_only": "읽기 전용", "allow": "허용"}
LEVEL_OF_STATE = {"ok": "INFO", "pending": "INFO", "unknown": "INFO", "warn": "WARNING", "danger": "ERROR"}


def default_log_dir() -> Path:
    env = os.environ.get("USBGUARD_LOG_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "UsbGuardAdmin" / "logs"
    return Path.home() / ".usbguard_admin" / "logs"


class DailyLog:
    """log_yyyymmdd.log 일별 파일 기록 + 14일 초과 파일 삭제"""

    def __init__(self, directory: Optional[Path | str] = None, retention_days: int = RETENTION_DAYS,
                 now: Callable[[], datetime] = datetime.now):
        self.dir = Path(directory) if directory else default_log_dir()
        self.retention_days = retention_days
        self._now = now
        self._lock = threading.Lock()
        self._last_cleanup: Optional[date] = None
        self.last_error: Optional[str] = None

    def path_for(self, d: date) -> Path:
        return self.dir / f"log_{d:%Y%m%d}.log"

    def write(self, level: str, message: str, agent: Optional[str] = None) -> None:
        now = self._now()
        line = f"{now:%Y-%m-%d %H:%M:%S} [{level}]" + (f" [{agent}]" if agent else "") + f" {message}\n"
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                with open(self.path_for(now.date()), "a", encoding="utf-8") as f:
                    f.write(line)
                self.last_error = None
        except OSError as e:
            self.last_error = str(e)

    def cleanup(self) -> list[str]:
        """보관기간(오늘 포함 N일)을 넘긴 log_yyyymmdd.log 삭제. 삭제한 파일명 목록 반환."""
        today = self._now().date()
        cutoff = today - timedelta(days=self.retention_days)     # 이 날짜 이전(포함)은 삭제
        removed: list[str] = []
        self._last_cleanup = today
        try:
            entries = list(self.dir.iterdir())
        except OSError:
            return removed
        for p in entries:
            m = FILE_RE.match(p.name)
            if not m or p.is_symlink() or not p.is_file():
                continue
            try:
                d = datetime.strptime(m.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if d <= cutoff:
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError as e:
                    self.last_error = str(e)
        return sorted(removed)

    def maybe_cleanup(self) -> None:
        """하루에 한 번(날짜가 바뀌었을 때, 또는 시작 직후) 정리하고 결과를 로그에 남김."""
        if self._last_cleanup == self._now().date():
            return
        for name in self.cleanup():
            self.write("INFO", f"보관기간({self.retention_days}일) 초과 로그 삭제: {name}")


def _fmt_age(sec: Optional[int]) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec}초 전"
    if sec < 3600:
        return f"{sec // 60}분 전"
    return f"{sec // 3600}시간 전"


def _v(x) -> str:
    return "-" if x is None else str(x)


def _is_offline(a: dict) -> bool:
    return a.get("age_sec") is not None and a["age_sec"] > OFFLINE_SEC


def state_label(a: dict) -> str:
    if a["state"] == "danger" and _is_offline(a):
        return "오프라인(확인 불가)"
    return STATE_LABEL.get(a["state"], a["state"])


def describe(a: dict) -> str:
    """PC 한 대의 현재 제어 상태를 한 줄로."""
    devs = a.get("usb_devices") or []
    parts = [
        f"상태={state_label(a)}",
        f"적용정책={MODE_LABEL.get(a.get('expected_mode'), '-')}",
        f"PC보고={MODE_LABEL.get(a.get('effective_mode'), '-')}",
        f"USBSTOR={_v(a.get('usbstor_start'))}",
        f"WriteProtect={_v(a.get('write_protect'))}",
        f"USB장치={len(devs)}개",
        f"변조감지={a.get('tamper_count') or 0}회",
        f"마지막보고={_fmt_age(a.get('age_sec'))}",
    ]
    if a.get("temp_until"):
        parts.insert(2, f"임시허용~{a['temp_until']}")
    line = " | ".join(parts)
    if a["state"] in ("warn", "danger") and a.get("reasons"):
        line += " | 사유: " + "; ".join(a["reasons"])
    return line


def _dev_names(a: dict) -> frozenset:
    return frozenset((d.get("model") or d.get("pnp_id") or "?") for d in (a.get("usb_devices") or []))


class ControlLogger:
    """관리자 앱이 받아오는 PC 현황을 지켜보며 제어 상태 로그를 남긴다."""

    def __init__(self, log: Optional[DailyLog] = None, snapshot_sec: int = SNAPSHOT_SEC,
                 now: Callable[[], datetime] = datetime.now):
        self.log = log or DailyLog(now=now)
        self.snapshot_sec = snapshot_sec
        self._now = now
        self._prev: dict[int, dict] = {}
        self._initialized = False
        self._last_snapshot: Optional[datetime] = None
        self._conn_ok: Optional[bool] = None

    # ── 일반 기록 ──
    def info(self, msg: str, agent: Optional[str] = None):
        self.log.write("INFO", msg, agent)

    def action(self, msg: str):
        self.log.write("INFO", f"[관리자] {msg}")

    def connection(self, ok: bool, detail: str = ""):
        """서버 연결 상태가 바뀔 때만 기록."""
        if ok == self._conn_ok:
            return
        if ok:
            if self._conn_ok is False:
                self.log.write("INFO", "서버 연결 복구")
        else:
            self.log.write("ERROR", f"서버 연결 실패 — 제어 상태를 확인할 수 없음 ({detail})")
        self._conn_ok = ok

    # ── PC 상태 관찰 ──
    def observe(self, agents: list[dict]) -> None:
        self.log.maybe_cleanup()
        now = self._now()
        snap = self._last_snapshot is None or (now - self._last_snapshot).total_seconds() >= self.snapshot_sec
        first = not self._initialized
        seen = set()

        for a in agents:
            aid, name = a["id"], a["name"]
            seen.add(aid)
            cur = {"name": name, "state": state_label(a), "level": LEVEL_OF_STATE.get(a["state"], "INFO"),
                   "mode": a.get("expected_mode"), "devs": _dev_names(a), "tamper": a.get("tamper_count") or 0}
            prev = self._prev.get(aid)
            logged = False

            if prev is None:
                msg = ("제어 상태 확인 시작" if first else "신규 PC 감지") + " | " + describe(a)
                self.log.write(cur["level"], msg, name)
                logged = True
            else:
                if cur["state"] != prev["state"]:
                    self.log.write(cur["level"], f"상태 변경: {prev['state']} → {cur['state']} | {describe(a)}", name)
                    logged = True
                if cur["mode"] != prev["mode"]:
                    self.log.write("INFO", f"적용 정책 변경: {MODE_LABEL.get(prev['mode'], '-')} → "
                                           f"{MODE_LABEL.get(cur['mode'], '-')}", name)
                    logged = True
                added, removed = cur["devs"] - prev["devs"], prev["devs"] - cur["devs"]
                if added:
                    lvl = "WARNING" if cur["mode"] == "block" else "INFO"
                    self.log.write(lvl, f"USB 저장장치 연결 감지: {', '.join(sorted(added))} (적용정책="
                                        f"{MODE_LABEL.get(cur['mode'], '-')})", name)
                    logged = True
                if removed:
                    self.log.write("INFO", f"USB 저장장치 해제: {', '.join(sorted(removed))}", name)
                    logged = True
                if cur["tamper"] > prev["tamper"]:
                    self.log.write("ERROR", f"설정 변조 감지 (+{cur['tamper'] - prev['tamper']}, 누적 {cur['tamper']}회)"
                                            " — 레지스트리가 수동 변경되어 자동 복구됨", name)
                    logged = True

            if snap and not logged:
                self.log.write(cur["level"], "제어 상태 점검 | " + describe(a), name)
            self._prev[aid] = cur

        for aid in list(self._prev):
            if aid not in seen:
                self.log.write("INFO", f"PC 목록에서 제거됨: {self._prev[aid]['name']}")
                del self._prev[aid]

        if snap:
            self._last_snapshot = now
        self._initialized = True
