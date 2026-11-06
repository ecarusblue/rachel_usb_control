# UsbGuard — Windows USB 저장장치 제어 + 중앙 상태 확인

대상 PC에는 GUI 없이 **서비스(에이전트)** 만 설치하고, **관리자만 웹 GUI** 로 모든 PC의 USB 제어 상태를 확인/변경합니다.

```
[대상 PC] UsbGuard 서비스 ──(30초마다 HTTP, PC→서버)──▶ [서버 PC] FastAPI + SQLite ◀── 관리자 웹 GUI (비밀번호 로그인)
   정책 적용 / 변조 감지 / 상태 수집                      상태 판정 / 정책 배포 / 이벤트 이력
```

## 구성
| 경로 | 설명 |
|---|---|
| `Service/` | SYSTEM 권한 Windows 서비스 (정책 적용, 상태 수집·보고, 로컬 CLI 처리) |
| `Admin/` | PC 로컬 CLI `usbguard.exe` (서버 등록, 비상용 로컬 제어) |
| `Common/` | 프로토콜 정의 |
| `server/` | 중앙 서버 (FastAPI) + 웹 현황판 (`server/static/index.html`) |
| `admin_gui/` | **관리자 데스크톱 GUI** (PySide6) — 서버 API에 접속하는 Windows 창 프로그램 |

## 1. 서버 설치 (관리자 PC / 서버 PC, Python 3.11+)
```
cd server
pip install -r requirements.txt
python manage.py set-admin-password      # 관리자 비밀번호 설정 (10자 이상)
uvicorn app:app --host 0.0.0.0 --port 8080
```
브라우저에서 `http://<서버IP>:8080` → 로그인 → **+ PC 등록** → 토큰 발급(한 번만 표시).
Windows 방화벽에서 TCP 8080 인바운드를 서버 PC에 허용하세요. 데이터는 `server/data/usbguard.db`(환경변수 `USBGUARD_DATA`로 변경).

## 2. 대상 PC 설치 (.NET 8, 관리자 권한)
```
dotnet publish Service -c Release -r win-x64 --self-contained false -o C:\UsbGuard\svc
dotnet publish Admin   -c Release -r win-x64 --self-contained false -o C:\UsbGuard\admin

sc.exe create UsbGuardSvc binPath= "C:\UsbGuard\svc\UsbGuard.Service.exe" start= auto obj= LocalSystem
sc.exe failure UsbGuardSvc reset= 0 actions= restart/5000/restart/5000/restart/5000
sc.exe start UsbGuardSvc

C:\UsbGuard\admin\usbguard.exe setup                               # 로컬 비밀번호(비상용) 최초 설정
C:\UsbGuard\admin\usbguard.exe enroll http://<서버IP>:8080 <토큰>   # 서버 등록
```
등록 후 서버가 정책을 관리하며, 로컬 CLI의 모드 변경은 거부됩니다. 서버를 못 쓰는 비상시 `usbguard unenroll`(로컬 비밀번호 필요).

## 3. 관리자 데스크톱 GUI (브라우저 없이)
![desktop](docs/desktop_gui.png)

관리자 PC 에서 실행하는 창 프로그램입니다. 서버 주소와 관리자 비밀번호로 로그인하면 아래 현황판이 표시됩니다.
```
cd admin_gui
pip install -r requirements.txt
python usbguard_gui.py
```
단일 exe 로 배포하려면:
```
pip install pyinstaller
pyinstaller --noconsole --onefile --name UsbGuardAdmin usbguard_gui.py
```
- 상단 **로그 폴더** 버튼으로 로그 폴더를 바로 열 수 있음
- 서버 주소는 마지막 입력값을 기억하고, 세션이 만료되면 재로그인 창이 뜸
- 네트워크 호출은 별도 스레드에서 수행해 화면이 멈추지 않음 (10초 자동 갱신)
- 테스트: `python admin_gui/tests/logstore_test.py` (로그 단위), `pip install PySide6-Essentials httpx && QT_QPA_PLATFORM=offscreen python admin_gui/tests/gui_test.py` (GUI 통합)

### 제어 상태 로그 (관리자 앱)
관리자 앱이 **실행 중인 동안** PC별 제어 상태를 파일로 기록합니다.

| 항목 | 내용 |
|---|---|
| 파일명 | `log_yyyymmdd.log` (로컬 날짜 기준 하루 1개, UTF-8, 자정에 자동으로 새 파일) |
| 보관 | **오늘 포함 14일치**만 유지. 그보다 오래된 `log_yyyymmdd.log` 는 앱 시작 시와 날짜가 바뀔 때 자동 삭제 (삭제 사실도 로그에 남김). 형식이 다른 파일·폴더는 삭제하지 않음 |
| 위치 | `%LOCALAPPDATA%\UsbGuardAdmin\logs` (환경변수 `USBGUARD_LOG_DIR` 로 변경) |
| 변화 즉시 기록 | 상태 변경(정상/확인 필요/위험/오프라인), 적용 정책 변경, USB 저장장치 연결·해제, 설정 변조 감지 |
| 주기 기록 | 변화가 없어도 10분마다 PC별 "제어 상태 점검" 1줄 → 제어가 유지되고 있었음을 확인 가능 |
| 기타 | 앱 시작·종료, 관리자 동작(정책 변경, 30분 허용, PC 등록·삭제, 로그아웃), 서버 연결 실패·복구, 세션 만료 |

```
2026-11-06 14:12:45 [INFO] [공부방-PC1] 제어 상태 확인 시작 | 상태=제어 정상 | 적용정책=차단 | PC보고=차단 | USBSTOR=4 | WriteProtect=0 | USB장치=0개 | 변조감지=0회 | 마지막보고=1초 전
2026-11-06 14:12:45 [WARNING] [공부방-PC2] 제어 상태 확인 시작 | 상태=확인 필요 | ... | USB장치=1개 | ... | 사유: 차단 모드지만 USB 저장장치가 연결되어 있음: SanDisk ...
2026-11-06 14:12:47 [INFO] [관리자] 정책 변경 요청: 공부방-PC1 → 허용
```
레벨: 정상·반영 대기 `INFO` / 확인 필요·차단 모드 중 USB 연결 `WARNING` / 위험·오프라인·변조 감지·서버 연결 실패 `ERROR`.
**주의: 앱이 꺼져 있는 동안에는 기록되지 않습니다.** (서버 DB 의 이벤트 기록은 앱과 별개로 계속 쌓임)

## 4. 웹 현황판 (선택, 같은 기능)
서버에 내장된 웹 화면(`http://<서버IP>:8080`)으로도 동일한 현황을 볼 수 있습니다.

### 현황판 구성
![dashboard](docs/dashboard.png)
*(데모 데이터 화면)*

- **전체 상태 배너**: 모든 PC 정상 / 확인 필요 / 위험을 한 줄로 요약
- **KPI**: 관리 PC 수, 정상·확인 필요·위험 수, 차단 적용 중 PC 수, 연결된 USB 저장장치 수
- **PC 카드**: PC별 제어 상태(차단/읽기 전용/허용/임시 허용), **제어 검증 3종**(정책 적용 · 레지스트리 실제값 · USB 장치), 마지막 보고 시각, 변조 감지 횟수, 에이전트 버전·등록일, 정책 변경 버튼
- **정책 분포**와 **최근 이벤트**: 우측 패널 (10초 자동 갱신)
- 보고가 끊긴 PC는 마지막 값으로 "정상"처럼 보이지 않도록 검증 칸을 **확인 불가**로 표시

### 상태 의미
| 상태 | 의미 |
|---|---|
| 정상 | 목표 정책과 PC 실제값(서비스 판정 + 레지스트리 실제값)이 일치하고, 차단 모드에서 USB 저장장치가 없음 |
| 반영 대기 | 정책을 방금 바꿨고 PC의 다음 보고(최대 30초)를 기다리는 중 |
| 확인 필요 | 정책 불일치, 또는 **차단 모드인데 USB 저장장치가 연결되어 있음** (이미 꽂혀 있던 장치) |
| 위험 | 100초 이상 보고 없음(PC 꺼짐/네트워크 단절/서비스 중지), 또는 최근 10분 내 레지스트리 변조 감지 |
| 미확인 | 등록만 되고 아직 보고 없음 |

상태 변화, 정책 변경, 로그인(성공/실패)은 하단 **최근 이벤트**에 기록됩니다.

## 동작 원리
- Block: `HKLM\SYSTEM\CurrentControlSet\Services\USBSTOR\Start = 4`
- ReadOnly: `Start = 3` + `...\Control\StorageDevicePolicies\WriteProtect = 1`
- 서비스가 5초마다 값을 재적용하고, 같은 모드인데 값이 바뀌어 있었으면 변조로 카운트해 서버에 보고
- 서버 불가 시 마지막으로 받은 정책을 유지 (모드는 PC 설정에 저장)

## 테스트
```
cd server && pip install httpx && python tests/smoke_test.py
```

## 한계 / 보안 메모
- **에이전트 자기 보고 기반**: 로컬 관리자 권한 사용자는 서비스를 중지·삭제할 수 있고, 이 경우 서버는 "보고 끊김(위험)"만 알 수 있음. 일반 사용자 권한 운영이 전제
- **HTTP 평문**: 사내망 전용 가정. 토큰이 평문 전송되므로 외부 노출 금지. 필요 시 HTTPS 리버스 프록시(Caddy/nginx) 뒤에 두고 `https_only=True` 로 전환
- 이미 꽂혀 있는 USB 는 즉시 해제되지 않음(재연결 시 차단). WMI `InterfaceType='USB'` 기준이라 UASP 방식 일부 외장 SSD 는 목록에서 빠질 수 있음
- 서버 측 temp 만료 시각은 UTC 기준이며 PC 시계 오차에 영향을 받음 (같은 대역 3대 규모에선 무시 가능)
- C# 코드는 아직 Windows 에서 빌드/실행 검증 전 (서버 쪽 Python 은 `tests/smoke_test.py` 로 검증)
- 미구현: 시리얼 번호 화이트리스트, 장치 연결 시도 이벤트 수집, mTLS, 언인스톨 보호
