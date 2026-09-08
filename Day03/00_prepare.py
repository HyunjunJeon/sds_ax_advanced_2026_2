"""00. 먼저 실행할 환경·코퍼스 점검.

가르치는 것:
- 비교 실험은 준비 상태가 같을 때만 성립한다. ready 문서 수와 corpus snapshot이
  같아야 모든 구조가 같은 출발점에서 시작하며, 이 점검을 건너뛴 비교는 해석할 수 없다.
- Registry(로컬 문서 상태)와 Backend(실제 원문·검색 접근)의 역할 분리. 둘 중 하나만
  준비됐다고 "검색 가능한 코퍼스"라고 판단하지 않는다.

읽는 순서: LabConfig → Settings → Registry → Backend.prepare → snapshot.
모델 답변을 만들기 전에 원문 파일, 고객/기준일 메타데이터, 서버 색인의 연결을 확인한다.
backend="local"은 동봉한 문서로 어휘 검색을 하며 키·서버가 필요 없다. OpenViking
서버를 쓰는 실험은 backend="viking"과 viking_env를 지정한다. local은 viking의
자동 대체재가 아니므로 서버 성능으로 해석하지 않는다.

관찰: ready 문서 수와 corpus snapshot이 같아야 비교 실험의 출발점도 같다.
한번 준비한 workspace를 계속 사용하면 재임베딩 비용을 피할 수 있다.
"""

import json

from common.config import key_presence
from common.lab import LabConfig, settings_from
from common.rag_backend import Backend
from common.runtime import Meter
from common.source_registry import Registry

# ── 실험 조건 ────────────────────────────────────────────────────────────────
# 첫 준비: local 검색으로 즉시 확인한다. 서버를 쓰려면 아래 주석처럼 바꾼다.
#   backend="viking", viking_env=".openviking/rag-client.env", ingest=True
# ingest=True는 이 workspace에 내부 문서를 새로 적재한다(서버 요약·임베딩 포함, 오래 걸림).
실험 = LabConfig(
    backend="local",
    workspace="work/rag/lab00",
    ingest=False,
)


def main():
    settings = settings_from(실험)
    registry = Registry(settings.workspace / "registry.sqlite")
    meter = Meter(settings)
    # Registry는 로컬 문서 상태, Backend는 실제 원문/검색 접근을 담당한다.
    # 둘 중 하나만 준비됐다고 검색 가능한 코퍼스라고 판단하지 않는다.
    backend = Backend(settings, registry, meter)
    try:
        backend.prepare(ingest=실험.ingest)
        print(
            json.dumps(
                {
                    "keys_set": key_presence(),
                    "backend": settings.backend,
                    "internal_documents": len(registry.rows("internal")),
                    "web_documents": len(registry.rows("web")),
                    "corpus": registry.snapshot(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        backend.close()


if __name__ == "__main__":
    main()
