"""00. 먼저 실행할 환경·코퍼스 점검.

모델 답변을 만들기 전에 설정·API 키·업무 코퍼스·Skills·서버 상태를 확인한다.
이 점검은 모델 API를 호출하지 않는다. 로컬 전처리(01, 06 기본 모드)는 키·서버 없이도
준비되어야 하고, OpenViking 검색·답변(02~05)은 여기에 서버 시작과 적재가 추가로
필요하다. server.status는 안내값이며 이 점검의 실패 조건이 아니다.

관찰: api_key_set과 corpus 문서 수, corpus_status의 approved/draft 분포가 이후
검색 범위(RetrievalContext)의 출발점이다. as_of 기준일에 따라 유효 문서가 달라진다.
"""
from __future__ import annotations

import json
import platform
import sys

from day02.settings import Settings, read_json


def skill_names(root):
    import yaml

    from day02.validation import skill_spec
    names = []
    for path in sorted((root / "skills").glob("*/SKILL.md")):
        frontmatter = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
        if frontmatter.get("name") != path.parent.name or not frontmatter.get("description"):
            raise ValueError(f"Skill frontmatter 오류: {path.parent.name}")
        skill_spec(root, path.parent.name)
        names.append(frontmatter["name"])
    if len(names) != 5:
        raise ValueError("5개 답변 Skill이 필요합니다.")
    return names


def main():
    from day02.evidence import Metadata
    settings = Settings.load()
    statuses: dict[str, int] = {}
    for record in read_json(settings.root / "data/business/catalog.json"):
        metadata = Metadata.model_validate(record["metadata"])
        statuses[metadata.status] = statuses.get(metadata.status, 0) + 1
    server_state = {"initialized": (settings.state_dir / "client.json").exists(),
                    "config": (settings.state_dir / "ov.conf").exists()}
    try:
        from day02.openviking import server
        # server.status()는 running/healthy/url/version을 준다. "status" 키는 없다.
        state = server.status(settings)
        server_state["status"] = (
            "running" if state["running"] and state["healthy"]
            else "unhealthy" if state["running"]
            else "stopped"
        )
        server_state["url"] = state["url"]
    except Exception as exc:  # 서버 미시작은 점검 실패가 아니라 안내로 남긴다.
        server_state["status"] = f"unavailable: {type(exc).__name__}"
    report = {
        "python": platform.python_version(),
        "python_ok": sys.version_info >= (3, 13),
        "api_key_set": bool(settings.api_key),
        "model": settings.model,
        "remote_override": bool(settings.remote_url),
        "corpus_documents": sum(statuses.values()),
        "corpus_status": statuses,
        "skills": skill_names(settings.root),
        "rhwp_installed": (settings.root / "bin/rhwp").exists(),
        "server": server_state,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["python_ok"]:
        raise SystemExit("준비 실패: Python 3.13이 필요합니다.")
    return 0


if __name__ == "__main__":
    main()
