"""06. 멀티모달 — PDF/PPTX/HWPX/HWP를 같은 근거 파이프라인에 연결한다.

PDF는 텍스트 검색 후 원본 페이지를 시각 도구(inspect_source_image)로 확인하고, 그
관찰은 원문 텍스트 인용과 다른 종류(visual_observation)로 기록된다. PPTX는 MarkItDown
변환과 실제 물리 슬라이드 번호를, HWPX는 네이티브 XML 파서를, HWP는 SHA256 검증 후
설치한 rhwp CLI를 사용한다. 확인하지 않은 물리 페이지 번호를 만들어 붙이지 않는다.
기본 모드는 네 형식의 로컬 전처리만 검증하고, --live가 적재·답변·PDF 시각 확인까지
실행한다(모델 API 호출).

관찰:
- --live 실행 결과 outputs/runs/<run_id>.json의 evidence에 kind가 visual_observation인
  항목이 source_text와 구분되어 있는지 확인한다.
- 여섯 번째 케이스가 아니라 네 케이스 각각의 catalog가 서로 다른 namespace(pdf/pptx/
  hwpx/hwp)로 적재되는지 확인한다.

심화 연결:
- 08_citation_student.py: 근거 종류(source_text/visual_observation)별 검증 규칙 과제.

실패 상황(이 구조가 지켜야 할 것):
- 시각 모델이 본 내용을 텍스트 인용처럼 취급하면 행 번호·해시 기반 원문 검증이 적용되지
  않는다. 관찰 근거를 별도 종류로 남기는 이유다.
"""
import argparse
from datetime import date

from day02.agents.rag import ask
from day02.ingestion.pipeline import ingest, prepare
from day02.openviking.client import VikingClient
from day02.settings import Settings, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    cases = [
        ("pdf", "slides-excerpt.pdf", [0], "PDF 원본 이미지를 inspect_source_image로 확인해서 서비스 A v2.1의 외부 공유 지원 여부를 알려주세요."),
        ("pptx", "day02.pptx", [38], "슬라이드에서 스키마 검사와 근거 ID 검사는 어떻게 다른가요?"),
        ("hwpx", "alpha-sla.hwpx", None, "알파 서비스 크레딧의 신청 기한과 제출 자료를 알려주세요."),
        ("hwp", "alpha-sla.hwp", None, "알파 서비스 크레딧의 신청 기한과 제출 자료를 알려주세요."),
    ]
    results = []
    for namespace, filename, pages, question in cases:
        catalog = prepare(settings, settings.root / "data/samples" / filename, pages=pages)
        result = {"namespace": namespace, "catalog": str(catalog.relative_to(settings.root))}
        if args.live:
            with VikingClient.configured(settings) as client:
                ingest(settings, client, catalog, namespace=namespace)
            answer = ask(settings, question, namespace=namespace, as_of=date(2026, 9, 8))
            result["answer"] = answer
            print(answer["markdown"])
        else:
            print(f"전처리 완료: {namespace}")
        results.append(result)
    write_json(settings.root / "outputs/examples/06_multimodal.json", results)


if __name__ == "__main__":
    main()
