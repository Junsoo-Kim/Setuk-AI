from __future__ import annotations

import os
import sys
from pathlib import Path

LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "localhost"}


def resolve_bind_host() -> str:
    """Setuk-AI C버전은 인증이 없다 — 그 대신 로컬 루프백 바인딩을 강제한다.

    인증 부재가 안전한 이유는 "단일 사용자, 로컬 실행"이라는 전제 하나뿐이다.
    그 전제는 코드가 지키지 않으면 그냥 관례일 뿐이다. `SETUK_BIND_HOST`를
    루프백이 아닌 주소로 설정하려면 `SETUK_ALLOW_NON_LOOPBACK=1`을 함께 켜야
    한다 — 실수로 `0.0.0.0`으로 바꾸거나 컨테이너에서 포트를 그대로 노출해도
    여기서 막힌다.
    """
    host = os.environ.get("SETUK_BIND_HOST", "127.0.0.1").strip()
    if host in LOOPBACK_ADDRESSES:
        return host
    if os.environ.get("SETUK_ALLOW_NON_LOOPBACK", "").strip() in {"1", "true", "TRUE", "yes"}:
        return host
    raise RuntimeError(
        f"SETUK_BIND_HOST={host!r}는 루프백 주소가 아닙니다. 이 서버는 인증이 없으므로 "
        "기본적으로 로컬에서만 접근 가능해야 합니다. 학교 네트워크 등에 실제로 열 계획이면 "
        "먼저 인증·RBAC을 붙인 뒤 SETUK_ALLOW_NON_LOOPBACK=1로 명시적으로 허용하세요."
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from version_C.server import app, start_recovery_worker

    start_recovery_worker()
    app.run(host=resolve_bind_host(), port=5000, debug=False, threaded=True)
