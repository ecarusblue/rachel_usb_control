# UsbGuard (Windows USB 저장장치 제어 · 뼈대)

구성
- `Service/` : SYSTEM 권한 Windows 서비스. 정책 적용(5초마다 재적용), 비밀번호 검증, 명령 수신
- `Admin/`   : 관리자 CLI (`usbguard.exe`). Named Pipe 로 서비스에 명령 전송
- `Common/`  : 요청/응답 프로토콜

## 빌드 (.NET 8 SDK, Windows)
```
dotnet publish Service -c Release -r win-x64 --self-contained false -o out\svc
dotnet publish Admin   -c Release -r win-x64 --self-contained false -o out\admin
```

## 설치 (관리자 PowerShell)
```
sc.exe create UsbGuardSvc binPath= "C:\UsbGuard\svc\UsbGuard.Service.exe" start= auto obj= LocalSystem
sc.exe failure UsbGuardSvc reset= 0 actions= restart/5000/restart/5000/restart/5000
sc.exe start UsbGuardSvc
```

## 사용
```
usbguard setup            # 최초 1회: 비밀번호 설정
usbguard status
usbguard block            # USB 메모리 인식 차단 (기본)
usbguard readonly         # 읽기 전용
usbguard allow            # 전체 허용
usbguard tempallow 30     # 30분간 허용 후 자동 복귀
usbguard passwd
```

## 동작 원리
- Block   : `HKLM\...\Services\USBSTOR\Start = 4`
- ReadOnly: `Start = 3` + `HKLM\...\Control\StorageDevicePolicies\WriteProtect = 1`
- 서비스가 5초마다 값을 재적용하므로 레지스트리를 수동으로 바꿔도 원복됨

## 한계 / 다음 단계
- 이미 연결된 USB 는 즉시 해제되지 않음(재연결 시 적용). 필요 시 SetupAPI 로 장치 비활성화 추가
- 로컬 관리자 권한 사용자는 서비스 중지·삭제로 우회 가능 -> 일반 사용자 권한 운영 전제
- 시리얼 번호 기반 화이트리스트, 이벤트 로그/중앙 전송, 언인스톨 보호는 미구현
- 이 코드는 Windows 환경에서 아직 빌드/실행 검증하지 않은 초안
