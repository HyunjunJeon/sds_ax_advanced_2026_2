"""실행별 결과 파일을 보존한다. 점수 계산이나 합격 판정은 하지 않는다.

OUT에는 마지막 실행을, <파일명>_runs에는 실행 ID별 기록을 남긴다.
재실행으로 이전 실패 기록을 잃지 않도록 02·03·07·08·09에서 같은 저장 규칙을 쓴다.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4


def make_run_id() -> str:
    """같은 초에 실행해도 충돌하지 않도록 UTC 시각과 무작위 접미사를 합친다."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]


def save_run_report(report: dict, out: str | Path) -> Path:
    """전체 기록을 마지막 실행 경로와 실행별 보관 경로에 저장한다.

    report는 run_id를 포함해야 한다. 실패·미채점 행도 호출자가 넘긴 그대로
    저장한다. 임시 파일을 쓴 뒤 교체해, 저장 도중 잘린 JSON이 읽히는 일을 줄인다.
    키를 제거하는 함수는 아니므로 호출자는 비밀 값이 없는 설정만 전달해야 한다.
    """
    path = Path(out)
    history = path.parent / f"{path.stem}_runs" / f"{report['run_id']}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    for target in (history, path):
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{report['run_id']}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(target)
    return history
