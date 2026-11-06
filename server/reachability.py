"""PC 생존 확인 (ping)

보고(heartbeat)가 끊긴 PC 가 "꺼진 것"인지 "켜져 있는데 에이전트만 멈춘/제거된 것"인지 구분하는 데 쓴다.
 - 응답 있음 + 보고 없음  -> 에이전트(서비스) 중지/제거 의심
 - 응답 없음              -> PC 꺼짐/네트워크 단절 추정
※ 대상 PC 방화벽이 서버 IP 의 ICMP echo 를 허용해야 한다 (README 참고).
"""
from __future__ import annotations

import ipaddress
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000


def parse_reply(stdout: str) -> bool:
    """ping 출력에서 '실제 echo 응답'이 있었는지만 판단한다.

    Windows 는 'Destination host unreachable' 응답에도 종료코드 0 을 줄 수 있어 종료코드를 믿지 않고,
    실제 응답에만 들어 있는 'TTL=' (Windows/Linux, 한글 Windows 포함) 로 판단한다.
    """
    return "ttl=" in (stdout or "").lower()


def ping(ip: str, timeout_ms: int = 1500) -> bool:
    """ip 로 ICMP echo 1회. 응답이 있으면 True. (셸을 쓰지 않고, 유효한 IP 만 허용)"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), str(addr)]
        flags = CREATE_NO_WINDOW
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), str(addr)]
        flags = 0
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout_ms / 1000 + 3, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return False
    return parse_reply(r.stdout)
