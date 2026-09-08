"""09. 통제 비교 — 청킹 전략 × 검색 변형 × 사례 × 반복으로 검색 품질을 잰다.

한 번에 한 가지 변수만 바꾼 결과를 남긴다. 조건은 아래 상수에서 편집하고 CLI 플래그로
바꾸지 않는다(변경 이유가 코드 리뷰에 남고, 재실행이 같은 조건을 보장하기 위해서다).
검색 품질은 data/gold.jsonl의 기대 문서와 대조한 recall@k로 계산하며, gold는 이
판정(functions below)만 읽는다. 답변 문장의 사실성 평가는 이 스크립트의 범위가 아니다.

관찰:
- 절 단위 묶음(bounded)과 고정 청킹(fixed)의 recall 차이가 어느 사례 유형에서 나는지
  본다. versioned 유형은 시점 필터가 없으면 v1이 섞여 들어온다.
- unknown/scope-trap 유형은 기대 문서가 없다. 검색이 후보를 돌려주는 것 자체는
  오류가 아니며(보류는 답변 수준의 판단이다), 빈 결과율은 참고 정보로만 남긴다.
- 실패한 행을 지우지 않는다. bm25는 결정적이라 REPEATS를 늘려도 같은 값이 나온다.
  반복이 의미 있는 것은 dense·rerank처럼 분산이 있는 변형이다.

심화 연결:
- 13_mini_pjt.py: 이 스크립트의 두 실행 결과를 짝 비교해 한 가지 변경의 효과를 분석한다.

실패 상황(이 비교가 지켜야 할 것):
- 여러 변수를 동시에 바꾼 두 실행을 비교하면 원인 귀속이 불가능하다. 13의 분석기가
  선언되지 않은 변수 변경을 거절한다.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
from datetime import date
from pathlib import Path

from day02.evidence import Metadata
from day02.ingestion.chunking import bounded_chunks, fixed_chunks
from day02.ranking import KoreanBM25, rrf
from day02.settings import Settings, read_json

# ── 실험 조건 ────────────────────────────────────────────────────────────────
# 기본값은 모델 API·서버 없이 실행되는 최소 비교다. 서버·모델 변형을 추가하려면
# 아래 상수만 편집한다. RETRIEVERS에 dense/rrf를 넣으면 OpenViking 서버(02 적재 완료)가
# 필요하다. LIVE=True면 의미 청킹(임베딩 API)을 CHUNKINGS에 추가한다.
CHUNKINGS = ["bounded", "fixed"]
RETRIEVERS = ["bm25"]
CASE_TYPES = ["simple", "versioned", "table", "procedural", "cross", "single", "unknown", "scope-trap"]
REPEATS = 1
K = 6
LIVE = False
SERVER = False

ROOT = Path(__file__).resolve().parent


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def scope_filter(catalog, case):
    as_of = date.fromisoformat(case["as_of"])
    return {r["path"].rsplit("/", 1)[-1].removesuffix(".md"): Metadata.model_validate(r["metadata"])
            for r in catalog
            if Metadata.model_validate(r["metadata"]).applies(case["entity"], as_of)}


def chunk_texts(text: str, chunking: str):
    if chunking == "bounded":
        return [c.text for c in bounded_chunks(text)]
    if chunking == "fixed":
        return [c.text for c in fixed_chunks(text)]
    if chunking == "semantic":
        from day02.models import make_embeddings
        from day02.ingestion.chunking import semantic_chunks
        return [c.text for c in semantic_chunks(text, make_embeddings(Settings.load()).embed_documents)]
    raise ValueError(f"알 수 없는 청킹 전략: {chunking}")


def local_ranking(case, documents, chunking):
    units = {f"{doc}:{i}": chunk for doc, text in documents.items()
             for i, chunk in enumerate(chunk_texts(text, chunking), 1)}
    if not units:
        return []
    bm25 = KoreanBM25(units, user_words=["크레딧", "추가약정", "가용률"])
    return list(dict.fromkeys(key.rsplit(":", 1)[0] for key in bm25.search(case["question"], 18)))


def dense_ranking(case):
    from day02.ingestion.pipeline import load_manifest
    from day02.openviking.client import VikingClient
    settings = Settings.load()
    with VikingClient.configured(settings) as client:
        manifest = load_manifest(settings, client, case["namespace"])
        as_of = date.fromisoformat(case["as_of"])
        hits = [h["uri"] for h in client.find(case["question"], manifest["target_uri"], 12)]
        docs = []
        for uri in hits:
            record = manifest["documents"].get(uri)
            meta = Metadata.model_validate(record["metadata"]) if record else None
            # gold의 expected_docs와 bm25 순위는 doc_id 단위다. 제목을 돌려주면
            # 대조가 항상 어긋나 dense recall이 구조적으로 0이 된다.
            if meta and meta.applies(case["entity"], as_of) and meta.doc_id not in docs:
                docs.append(meta.doc_id)
        return docs


def recall(retrieved, expected):
    return len(set(retrieved) & set(expected)) / len(expected) if expected else None


def main():
    if LIVE and "semantic" not in CHUNKINGS:
        print("LIVE=True면 CHUNKINGS에 semantic 추가와 함께 실행하세요.")
        return 2
    if not SERVER and [r for r in RETRIEVERS if r in {"dense", "rrf", "rerank"}]:
        print("dense/rrf/rerank는 OpenViking 서버가 필요합니다. 02_ingest.py 후 SERVER=True로 실행하세요.")
        return 2
    cases = [c for c in load_jsonl(ROOT / "data/cases.jsonl") if c["type"] in CASE_TYPES]
    catalog = read_json(ROOT / "data/business/catalog.json")
    business = {r["path"].rsplit("/", 1)[-1].removesuffix(".md"):
                (ROOT / r["path"]).read_text(encoding="utf-8") for r in catalog}
    run_id = time.strftime("%Y%m%dT%H%M%S")
    out = ROOT / "outputs/rag-comparison" / run_id
    out.mkdir(parents=True)
    rows = []
    for repeat in range(1, REPEATS + 1):
        for case in cases:
            documents = {doc: text for doc, text in business.items()
                         if doc in scope_filter(catalog, case)}
            for chunking in CHUNKINGS:
                for retriever in RETRIEVERS:
                    row = {"case_id": case["id"], "type": case["type"], "repeat": repeat,
                           "chunking": chunking, "retriever": retriever, "status": "ok", "retrieved": []}
                    try:
                        if retriever == "bm25":
                            row["retrieved"] = local_ranking(case, documents, chunking)[:K]
                        elif retriever == "dense":
                            row["retrieved"] = dense_ranking(case)[:K]
                        elif retriever == "rrf":
                            row["retrieved"] = list(dict.fromkeys(
                                rrf([dense_ranking(case), local_ranking(case, documents, chunking)])))[:K]
                        else:
                            raise ValueError(f"알 수 없는 검색기: {retriever}")
                    except Exception as exc:  # 실패 행도 결과의 일부로 보존한다.
                        row.update(status="error", error=f"{type(exc).__name__}: {exc}")
                    rows.append(row)
    with (out / "runs.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    # 판정 단계에서만 gold를 읽는다.
    gold = {g["id"]: g for g in load_jsonl(ROOT / "data/gold.jsonl")}
    summary = []
    for chunking in CHUNKINGS:
        for retriever in RETRIEVERS:
            subset = [r for r in rows if r["chunking"] == chunking and r["retriever"] == retriever]
            answered = [r for r in subset if gold[r["case_id"]]["expected_docs"]]
            abstain = [r for r in subset if not gold[r["case_id"]]["expected_docs"]]
            summary.append({
                "chunking": chunking, "retriever": retriever, "rows": len(subset),
                "errors": sum(r["status"] == "error" for r in subset),
                "mean_recall": round(sum(recall(r["retrieved"], gold[r["case_id"]]["expected_docs"])
                                         or 0 for r in answered) / len(answered), 4) if answered else None,
                "empty_retrieval_rate": round(sum(not r["retrieved"] for r in abstain) / len(abstain), 4)
                if abstain else None,
            })
    provenance = {name: hashlib.sha256((ROOT / "data" / name).read_bytes()).hexdigest()
                  for name in ["cases.jsonl", "gold.jsonl"]}
    config = {"chunkings": CHUNKINGS, "retrievers": RETRIEVERS, "case_types": CASE_TYPES,
              "repeats": REPEATS, "k": K, "live": LIVE, "server": SERVER}
    write = {"run_id": run_id, "config": config, "provenance": provenance, "summary": summary}
    (out / "summary.json").write_text(json.dumps(write, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (out / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    lines = ["# 검색 비교 결과", "", f"- run_id: {run_id}", f"- 조건: {config}", "",
             f"| 청킹 | 검색기 | 행 | 오류 | 평균 recall@{K} | 기대 없음 유형의 빈 결과율 |",
             "|---|---|---:|---:|---:|---:|"]
    lines += [f"| {s['chunking']} | {s['retriever']} | {s['rows']} | {s['errors']} | "
              f"{s['mean_recall']} | {s['empty_retrieval_rate']} |" for s in summary]
    lines += ["", "실패 행과 오류 행을 결과에서 제외하지 않았다. bm25는 결정적이라 반복 간 차이가 없다.",
              "recall은 문서 수준 기대와의 대조 결과이며 답변 문장의 사실성 평가가 아니다.",
              "보류(근거 부족) 판단은 답변 수준의 성질이라 검색 지표로 측정하지 않는다."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"비교 완료: {out / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
