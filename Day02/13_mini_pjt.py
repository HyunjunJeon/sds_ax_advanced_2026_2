"""13. 미니 프로젝트 — 짝 비교로 한 가지 변경의 효과를 입증한다.

아래 상수를 09_compare.py의 두 실행 결과 경로로 바꿔 분석한다. 분석기는 모델을
호출하지 않고 결정적으로 동작한다. 빠진 짝이나 선언하지 않은 변수 변경을 거절한다.
출력의 delta는 항상 후보 − 기준선이다.

관찰:
- 평균만 보지 않고 유형별 개선/악화 사례를 함께 본다. recall이 올라도 보류 정확도가
  깍이면(없는 것을 찾아오는 경우) 그 손실을 함께 보고해야 한다.
- bm25만 쓴 실행은 결정적이라 delta가 전부 0이 나온다. 이는 "변경 효과 없음"의 증거가
  아니라 그 변수가 이 변형에 영향을 주지 않는다는 관찰이다.

심화 연결:
- WORKSHEET D: 답변 수준의 전후 비교는 outputs/runs의 실행 기록과 gold 사실 대조로
  별도 수행한다. 이 파일은 검색 수준의 짝 비교 엔진이다.

실패 상황(이 분석이 지켜야 할 것):
- 여러 변수를 동시에 바꾼 실행을 비교하면 원인 귀속이 불가능하다. 거절된다.
- 기준선에만 있는 사례 행은 몰래 빼면 안 된다. 빠진 짝은 오류로 보고된다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── 비교 조건 ────────────────────────────────────────────────────────────────
# 09_compare.py를 두 번 실행한 결과 폴더 이름(run_id)을 아래에 넣는다.
BASELINE = Path("outputs/rag-comparison/여기에-기준선-run-id")
CANDIDATE = Path("outputs/rag-comparison/여기에-후보-run-id")
# 실제로 바꾼 변수만 선언한다. 허용 값: chunking, retriever, k, repeats, live, server, implementation
CHANGED = frozenset({"chunking"})

CONFIG_KEYS = {"chunkings": "chunking", "retrievers": "retriever", "k": "k", "repeats": "repeats",
               "live": "live", "server": "server"}


def load_run(path: Path):
    summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return summary, rows


def main():
    if "여기에" in str(BASELINE) or "여기에" in str(CANDIDATE):
        print("BASELINE/CANDIDATE에 09_compare.py 실행 결과 경로를 지정하세요.")
        return 2
    base_dir, cand_dir = ROOT / BASELINE, ROOT / CANDIDATE
    base_summary, base_rows = load_run(base_dir)
    cand_summary, cand_rows = load_run(cand_dir)
    base_config, cand_config = base_summary["config"], cand_summary["config"]
    differing = {CONFIG_KEYS.get(key, key) for key in base_config
                 if base_config[key] != cand_config.get(key)}
    differing |= {CONFIG_KEYS.get(key, key) for key in cand_config if key not in base_config}
    undeclared = differing - CHANGED
    if undeclared:
        print(f"거절: 선언하지 않은 변수가 바뀌었습니다: {sorted(undeclared)}")
        return 1
    if base_summary["provenance"] != cand_summary["provenance"]:
        print("거절: 두 실행의 사례·정답 파일이 다릅니다. 같은 cases/gold로 실행하세요.")
        return 1
    for name in sorted(CHANGED - differing):
        print(f"경고: {name}을(를) 선언했지만 실제로는 바뀌지 않았습니다.")

    def key(row):
        return row["case_id"], row["repeat"]

    golds = {g["id"]: g for g in
             (json.loads(line) for line in (ROOT / "data/gold.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip())}

    base_map = {key(r): r for r in base_rows}
    pairs, missing = [], []
    for row in cand_rows:
        partner = base_map.get(key(row))
        if partner is None:
            missing.append(key(row))
            continue
        pairs.append((partner, row))
    if missing:
        print(f"거절: 기준선에 없는 짝 {len(missing)}개: {sorted(missing)[:5]} ...")
        return 1
    analysis = []
    for base, cand in pairs:
        gold = golds[base["case_id"]]
        entry = {"case_id": base["case_id"], "type": base["type"], "repeat": base["repeat"],
                 "delta": None}
        if gold["expected_docs"]:
            def score(row):
                got = set(row["retrieved"]) & set(gold["expected_docs"])
                return len(got) / len(gold["expected_docs"])
            entry["baseline"] = round(score(base), 4)
            entry["candidate"] = round(score(cand), 4)
            entry["delta"] = round(score(cand) - score(base), 4)
        # 기대 문서가 없는 유형은 보류 판단이 답변 수준의 성질이라 검색 delta를 매기지 않는다.
        analysis.append(entry)
    scored = [a for a in analysis if a["delta"] is not None]
    improved = [a for a in scored if a["delta"] > 0]
    worsened = [a for a in scored if a["delta"] < 0]
    out = base_dir.parent / f"paired-{base_summary['run_id']}-{cand_summary['run_id']}"
    out.mkdir(parents=True, exist_ok=True)
    report = {"changed": sorted(CHANGED), "declared_and_differing": sorted(differing),
              "pairs": len(analysis), "scored_pairs": len(scored),
              "improved": len(improved), "worsened": len(worsened),
              "mean_delta": round(sum(a["delta"] for a in scored) / len(scored), 4) if scored else None,
              "rows": analysis}
    (out / "analysis.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    by_type = {}
    for a in scored:
        by_type.setdefault(a["type"], []).append(a["delta"])
    lines = ["# 짝 비교 분석", "",
             f"- 변경 변수: {sorted(differing)} (선언: {sorted(CHANGED)})",
             f"- 짝 {len(analysis)}개(점수 매김 {len(scored)}) 중 개선 {len(improved)}, 악화 {len(worsened)}, "
             f"평균 delta {report['mean_delta']}", "", "| 유형 | 평균 delta | 행 |", "|---|---:|---:|"]
    lines += [f"| {t} | {round(sum(d)/len(d), 4)} | {len(d)} |" for t, d in sorted(by_type.items())]
    lines += ["", "delta는 후보 − 기준선이다. 개선·악화 사례의 목록은 analysis.json에서 확인한다.",
              "이 분석은 검색 수준 recall이며 답변 문장의 사실성 평가는 WORKSHEET D에서 별도로 수행한다."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"짝 비교 완료: {out / 'REPORT.md'} (개선 {len(improved)}, 악화 {len(worsened)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
