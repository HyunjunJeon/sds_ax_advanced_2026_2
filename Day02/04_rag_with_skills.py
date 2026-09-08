"""04. 답변 — 같은 DeepAgents RAG에서 Skill만 바꿔 답변 형식을 선택한다.

Agent는 검색·원문 읽기·표 집계·시각 확인 도구만 가진다. 답변 형식은 skills/<name>/
SKILL.md의 절차와 output.json의 절 계약이 정의하고, Python 렌더러가 검사·렌더링한다.
검증은 (1) Skill 전문을 실제로 읽었는지, (2) 이번 턴에 검색했는지, (3) 절 제목·순서가
계약과 같은지, (4) 인용 ID가 현재 턴 근거이고 해시·적용 범위가 일치하는지를 본다.
이 후 별도 모델 호출이 각 주장의 근거 지지를 검토하고, 실패 시 한도 내에서 재작성한다.

관찰:
- 출력된 답변의 각 주장에 [E-...] 인용이 붙고, outputs/runs/<run_id>.json의 trace에
  search/read/model/validation_retry 이벤트가 기록되는지 확인한다.
- --format auto는 Agent가 형식을 고르고, 근거가 부족하면 insufficient-evidence로
  전환하는지 확인한다.

심화 연결:
- 08_citation_student.py: 이 검증 단계(인용 ID·해시·범위·재작성 판정)를 직접 구현하는 과제.

실패 상황(이 구조가 지켜야 할 것):
- Skill 지침만으로 사실성이 보장되지 않는다. 검증 없이 렌더링된 Markdown은 근거 없는
  주장을 그대로 노출한다. validation_retry 이벤트가 이를 잡는다.
"""
import argparse
from datetime import date

from day02.agents.rag import ask
from day02.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", default="grounded-qa", choices=[
        "auto", "grounded-qa", "comparison", "procedure", "table-analysis", "insufficient-evidence",
    ])
    args = parser.parse_args()
    questions = {
        "grounded-qa": "알파 SLA의 서비스 크레딧 신청 기한은 언제인가요?",
        "auto": "알파 SLA의 서비스 크레딧 신청 기한은 언제인가요?",
        "comparison": "알파 SLA의 월간 가용률 구간별 서비스 크레딧 비율을 비교해주세요.",
        "procedure": "알파 SLA 서비스 크레딧을 신청하는 절차와 제출 자료를 알려주세요.",
        "table-analysis": "알파 SLA 표의 크레딧 비율 중 최댓값을 table_query로 계산하고, 단위와 계산 기준을 설명해주세요.",
        "insufficient-evidence": "알파 고객의 2035년 화성 지사 주소와 우편번호는 무엇인가요?",
    }
    result = ask(Settings.load(), questions[args.format], as_of=date(2026, 9, 8), answer_format=args.format)
    print(result["markdown"])
    print(f"\n실행 기록: outputs/runs/{result['run_id']}.json")


if __name__ == "__main__":
    main()
