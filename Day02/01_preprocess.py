"""01. 전처리 — 청킹·원문 좌표·표 품질·관리 메타데이터가 답변의 근거 단위를 결정한다.

고정 청킹(fixed)은 크기만 보는 기준선이다. 실제 적재 파이프라인은 절 단위로 묶는
bounded_chunks를 사용하며, 크기 목표를 채우려고 절이나 표를 자르지 않는다. 모든 청크는
원문 offset을 보존해서 인용 L 번호·PDF 페이지·PPTX 슬라이드로 되돌아갈 수 있다.
관리 메타데이터 sidecar는 source_sha256이 일치할 때만 적용되고, LLM이 제안하는
메타데이터 후보는 승인 상태나 유효일로 자동 승격하지 않는다. --live는 임베딩 경계
기반 의미 청킹과 메타데이터 후보 비교를 추가한다(모델 API 호출).

관찰:
- outputs/examples/01_preprocess.json에서 fixed와 sections의 경계가 어디에서 갈리는지,
  크기 목표가 절 경계를 무시하는 지점을 찾는다.
- tables의 missing_cells/duplicate_rows/duplicate_headers가 0이 아닌 이유를 원문에서
  확인한다. 결측·중복이 있는 표는 table_query 집계가 보류된다.
- catalog의 각 문서가 source_start_line/source_end_line 좌표를 어떻게 가지는지 확인한다.

심화 연결:
- 11_table_challenge.py: 표를 바이트 예산과 절충할 때 단위·조건 열까지 보존하는 선택 과제.
- 10_version_challenge.py: 이 단계가 만드는 version/valid_from/status가 검색 단계에서
  어떻게 쓰이는지(여기서는 후보만 만든다).

실패 상황(이 단계가 지켜야 할 것):
- 크기 목표를 채우려고 표의 단위 열을 다음 청크로 넘기면 이후 답변에서 조건 없는
  수치("20%")가 된다. 이 파일은 표를 자르지 않는 절 경계와 품질 보고로 이를 노출한다.
"""
import argparse
from dataclasses import asdict

from day02.ingestion.chunking import fixed_chunks, section_chunks, semantic_chunks
from day02.ingestion.metadata import suggest_metadata
from day02.ingestion.pipeline import prepare
from day02.models import make_embeddings
from day02.settings import Settings, write_json
from day02.tools.tables import markdown_tables, quality_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    path = settings.root / "data/business/sla-a-v2.md"
    text = path.read_text(encoding="utf-8")
    report = {
        "fixed": [asdict(c) for c in fixed_chunks(text)],
        "sections": [asdict(c) for c in section_chunks(text)],
        "tables": [quality_report(t) for t in markdown_tables(text)],
        "catalog": str(prepare(settings, path, entity="알파").relative_to(settings.root)),
    }
    if args.live:
        embeddings = make_embeddings(settings)
        report["semantic"] = [asdict(c) for c in semantic_chunks(text, embeddings.embed_documents)]
        report["metadata_suggestion"] = suggest_metadata(settings, text).model_dump()
    write_json(settings.root / "outputs/examples/01_preprocess.json", report)
    print(f"전처리 완료: fixed={len(report['fixed'])}, sections={len(report['sections'])}, tables={len(report['tables'])}")


if __name__ == "__main__":
    main()
