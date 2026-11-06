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
| `server/` | 중앙 서버 (FastAPI) + 웹 GUI (`server/static/index.html`) |

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

## 3. GUI 상태 표시
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
