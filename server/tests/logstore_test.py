"""logstore 단위 테스트:  python server/tests/logstore_test.py"""
import os, re, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import logstore as L

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)

class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t

tmp = Path(tempfile.mkdtemp())
clk = Clock(datetime(2026, 11, 6, 23, 59, 50))
log = L.DailyLog(tmp, now=clk)

# 1) 파일명 규칙, 자정 넘김
log.write("INFO", "자정 전")
clk.t = datetime(2026, 11, 7, 0, 0, 5)
log.write("INFO", "자정 후")
check("파일명 log_yyyymmdd.log", (tmp/"log_20261106.log").exists() and (tmp/"log_20261107.log").exists())
check("날짜별 분리 기록", "자정 전" in (tmp/"log_20261106.log").read_text("utf-8") and "자정 후" in (tmp/"log_20261107.log").read_text("utf-8"))
check("로그 라인 형식", (tmp/"log_20261107.log").read_text("utf-8").startswith("2026-11-07 00:00:05 [INFO] 자정 후"))

# 2) 14일 보관 경계: 오늘(11/20) 기준 -> 11/07 ~ 11/20 14개 보관, 11/06 이하 삭제
for d in range(1, 21):
    (tmp/f"log_202611{d:02d}.log").write_text("x", "utf-8")
(tmp/"notes.txt").write_text("keep", "utf-8")           # 무관한 파일
(tmp/"log_2026.log").write_text("keep", "utf-8")          # 형식 불일치
(tmp/"log_20261301.log").write_text("keep", "utf-8")      # 잘못된 날짜(13월)
(tmp/"log_20261001.log.bak").write_text("keep", "utf-8")  # 확장자 다름
(tmp/"log_20261201.log").write_text("future", "utf-8")    # 미래 날짜(시계 오차) 보존
sub = tmp/"log_20260101.log"; sub.mkdir()                 # 폴더는 건드리지 않음
clk.t = datetime(2026, 11, 20, 9, 0, 0)
removed = log.cleanup()
left = sorted(p.name for p in tmp.glob("log_202611[0-3][0-9].log") if p.is_file() and p.name != "log_20261301.log")
check("11/06 이하 삭제", removed == [f"log_202611{d:02d}.log" for d in range(1, 7)])
check("오늘 포함 14일(11/07~11/20) 보관", left == [f"log_202611{d:02d}.log" for d in range(7, 21)] and len(left) == 14)
check("무관/형식불일치/잘못된날짜/폴더/미래 파일 보존",
      all((tmp/n).exists() for n in ["notes.txt","log_2026.log","log_20261301.log","log_20261001.log.bak","log_20261201.log","log_20260101.log"]))

# 3) maybe_cleanup: 하루 한 번만, 삭제 사실을 로그에 남김
(tmp/"log_20261107.log").write_text("old", "utf-8")
log._last_cleanup = None
clk.t = datetime(2026, 11, 21, 8, 0, 0)       # 11/07 -> 14일 초과(11/07 <= 11/07)
log.maybe_cleanup()
check("날짜 변경 시 자동 정리", not (tmp/"log_20261107.log").exists())
check("삭제 내역 로그 기록", "log_20261107.log" in (tmp/"log_20261121.log").read_text("utf-8"))
(tmp/"log_20261001.log").write_text("x", "utf-8")   # 보관기간 초과 파일이 같은 날 뒤늦게 생겨도
log.maybe_cleanup()                                  # 같은 날 재호출에서는 정리하지 않음(하루 1회)
check("같은 날 재정리 안 함", (tmp/"log_20261001.log").exists())
clk.t = datetime(2026, 11, 22, 0, 0, 1); log.maybe_cleanup()
check("다음 날 정리 시 삭제", not (tmp/"log_20261001.log").exists())

# 4) ControlLogger: 변화 즉시 기록 + 스냅샷 주기
tmp2 = Path(tempfile.mkdtemp()); clk2 = Clock(datetime(2026, 11, 6, 10, 0, 0))
cl = L.ControlLogger(L.DailyLog(tmp2, now=clk2), snapshot_sec=600, now=clk2)
def ag(**kw):
    a = {"id": 1, "name": "PC1", "state": "ok", "expected_mode": "block", "effective_mode": "block", "usbstor_start": 4,
         "write_protect": 0, "usb_devices": [], "tamper_count": 0, "age_sec": 3, "reasons": [], "temp_until": None}
    a.update(kw); return a
read = lambda: (tmp2/"log_20261106.log").read_text("utf-8")
cl.observe([ag()])
check("최초 관찰 시 제어 상태 확인 시작", "제어 상태 확인 시작" in read() and "상태=제어 정상" in read() and "[PC1]" in read())
n = len(read().splitlines()); clk2.t += timedelta(seconds=10); cl.observe([ag()])
check("변화 없고 주기 전이면 기록 안 함", len(read().splitlines()) == n)
clk2.t += timedelta(seconds=600); cl.observe([ag()])
check("10분 경과 시 스냅샷 기록", "제어 상태 점검" in read())
clk2.t += timedelta(seconds=10)
cl.observe([ag(state="warn", usb_devices=[{"model":"SanDisk"}], reasons=["차단 모드지만 USB 연결"])])
r = read()
check("상태 변경/USB 연결 감지 WARNING", "상태 변경: 제어 정상 → 확인 필요" in r and "[WARNING] [PC1] USB 저장장치 연결 감지: SanDisk" in r and "사유: 차단 모드지만 USB 연결" in r)
clk2.t += timedelta(seconds=10)
cl.observe([ag(tamper_count=2, state="danger")])
r = read()
check("변조 감지 ERROR + USB 해제", "[ERROR] [PC1] 설정 변조 감지 (+2, 누적 2회)" in r and "USB 저장장치 해제: SanDisk" in r)
clk2.t += timedelta(seconds=10)
cl.observe([ag(state="danger", age_sec=300, tamper_count=2)])
check("오프라인 표기", "오프라인(확인 불가)" in read())
clk2.t += timedelta(seconds=10)
cl.observe([ag(expected_mode="allow", effective_mode="allow", usbstor_start=3)])
check("정책 변경 기록", "적용 정책 변경: 차단 → 허용" in read())
clk2.t += timedelta(seconds=10); cl.observe([])
check("PC 제거 기록", "PC 목록에서 제거됨: PC1" in read())
cl.connection(False, "timeout"); cl.connection(False, "timeout"); cl.connection(True); cl.connection(True)
r = read()
check("서버 연결 실패/복구는 전환 시 1회씩만", r.count("서버 연결 실패") == 1 and r.count("서버 연결 복구") == 1)
cl.action("정책 변경: PC1 → 차단")
check("관리자 동작 기록", "[INFO] [관리자] 정책 변경: PC1 → 차단" in read())

# 4-b) 임시허용 시각은 UTC 원문이 아니라 현지 시각(HH:MM:SS)으로
d = L.describe(ag(temp_until="2026-11-06T05:47:34.5774730Z"))
check("임시허용 시각 현지 표기", re.search(r"임시허용~\d\d:\d\d:\d\d \|", d) is not None and "T05:47" not in d)

# 5) 쓰기 실패 시 예외 없이 last_error 기록
bad = L.DailyLog(Path(tmp2/"log_20261106.log"/"sub"), now=clk2); bad.write("INFO", "x")
check("쓰기 실패해도 예외 없이 last_error", bad.last_error is not None)
print("ALL OK")
