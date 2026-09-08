"""결과 재현에 필요한 코드·자료·의존성의 hash만 기록한다. .env는 읽지 않는다.

가르치는 것:
- 재현성의 최소 조건: "어떤 코드·어떤 사례·어떤 lock 파일로 돌았나"를 hash로 남긴다.
  설정이 같아도 코드가 다른 전후 비교는 implementation 변수로 선언해야 한다.
- 기록의 경계: 코드와 자료만 hash한다. 키·환경 변수 값은 결과에 남기지 않는다.
"""

import hashlib
import json
import platform

from .config import DAY03


def run_provenance():
    # 번호 실행 파일과 공통 구현, 학생 구현을 포함한다. archive/출력/자격 증명은 제외한다.
    # 수정 전후 코드는 같은 파일명이어도 hash가 달라지므로 실험 결과에 연결할 수 있다.
    paths = sorted(set(DAY03.glob("[0-9][0-9]_*.py")) | set((DAY03 / "common").glob("*.py")))
    if (DAY03 / "student_tasks.py").exists():
        paths.append(DAY03 / "student_tasks.py")
    files = {
        str(path.relative_to(DAY03)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    assets = {}
    for name in ("uv.lock", "data/cases.jsonl", "data/gold.jsonl"):
        path = DAY03 / name
        assets[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return {
        "source_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        "files": files,
        "assets": assets,
        "python": platform.python_version(),
    }
