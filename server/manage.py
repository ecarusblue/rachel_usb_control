"""서버 관리 도구 (서버 PC에서 직접 실행)

  python manage.py set-admin-password   관리자 비밀번호 설정/변경
"""
import getpass
import sys

import app


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "set-admin-password":
        print(__doc__)
        return 1
    pw = getpass.getpass("새 관리자 비밀번호(10자 이상): ")
    if len(pw) < 10:
        print("10자 이상이어야 합니다.")
        return 1
    if getpass.getpass("확인: ") != pw:
        print("불일치")
        return 1
    app.set_setting("admin_pw", app.hash_pw(pw))
    print("관리자 비밀번호가 설정되었습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
