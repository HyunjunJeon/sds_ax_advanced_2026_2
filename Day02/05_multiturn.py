"""05. 세션 — 후속 질문은 대화를 참조하되 근거와 예산은 매 턴 다시 확보한다.

세션은 고객·기준일·namespace가 같은 대화만 공유하고 최근 4턴만 넘긴다. 후속 질문도
매 턴 다시 검색하며(이 파일이 두 턴 모두 search >= 1인지 assert로 확인한다), 이전 턴
인용 ID를 검증 없이 재사용하지 않는다. 근거 카드 바이트·검색·모델 호출 예산은 턴
단위로 초기화된다.

관찰:
- outputs/examples/05_multiturn.json에서 두 턴 모두 trace.counts.search >= 1인지,
  두 번째 턴의 evidence_id가 어디서 왔는지 확인한다. 같은 문서가 답이면 ID는 같을 수
  있다 — ID가 같다는 것은 재사용의 증거가 아니라 매 턴 다시 검색해 같은 원문을 얻었다는
  뜻이다. 재사용 여부는 trace.counts.search로 판단한다.
- 같은 session 이름도 --entity/--as-of/--namespace가 다르면 대화를 공유하지 않는다.

심화 연결:
- 12_refresh_challenge.py: 턴 사이 문서 갱신 시 세션이 어떤 버전의 인용을 믿어야
  하는지(스냅샷 vs 재검색)를 다루는 과제.

실패 상황(이 구조가 지켜야 할 것):
- 이전 턴 근거를 재사용하면 문서가 바뀌었을 때 이전 답변의 근거가 현재 원문과
  어긋난다. 매 턴 재검색이 이 어긋남을 노출한다.
"""
import uuid
from datetime import date

from day02.agents.rag import ask
from day02.settings import Settings, write_json


def main():
    settings = Settings.load()
    session = "demo-" + uuid.uuid4().hex[:12]
    results = []
    for question in ["알파 SLA의 서비스 크레딧 신청 기한은 언제인가요?", "그 신청에 어떤 자료를 제출해야 하나요?"]:
        result = ask(settings, question, as_of=date(2026, 9, 8), session=session)
        results.append(result)
        print(question)
        print(result["markdown"])
        print()
    assert all(r["trace"]["counts"].get("search", 0) >= 1 for r in results)
    write_json(settings.root / "outputs/examples/05_multiturn.json", results)


if __name__ == "__main__":
    main()
