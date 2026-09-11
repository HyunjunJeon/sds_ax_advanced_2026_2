"""
[이 파일을 벗어나서 다른 파일을 생성하는 등의 작업을 하지 말것]

Goal: Golden Dataset 에 '답변' 하는 에이전트
- 사용 할 문서: Day05/docs_2026_08.pdf
- Golden Dataset: Day05/economic_report_golden/golden_dataset.csv
- 실제로 이 에이전트가 채워넣어야하는 Golden Dataset 의 컬럼은 "actual_output".
- 에이전트 개발에 사용해야되는 라이브러리는 DeepAgents.
- 에이전트의 실행 기록(Trace) 은 Langfuse 에 저장되어야함.

golden_dataset.csv 의 context/contexts.json 은 골든 정답을 만들 때 쓴 채점용 근거이며,
이 에이전트에는 주지 않는다. 에이전트는 docs_2026_08.pdf 원문만 보고 스스로 찾아 답해야
"실제 문서 검색·근거 확인" 역량이 의미가 있다.

PDF 는 pymupdf4llm 으로 마크다운 변환해 outputs/gold_agent/ 에 캐시한다(재실행 시 재사용).
"""

import csv
from pathlib import Path

from business_lab.connections import connect_langfuse
from business_lab.models import load_agent_llm
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemPermission

PDF_PATH = Path("docs_2026_08.pdf")
GOLDEN_CSV = Path(
    "outputs/gold_agent/golden_dataset_top10.csv"
)  # TODO: 테스트 후 economic_report_golden/golden_dataset.csv 로 되돌릴 것
DOC_CACHE_DIR = Path("outputs/gold_agent/doc_cache")  # 백엔드 root. GOLDEN_CSV(정답 포함)는 이 밖에 둔다.
DOC_CACHE_MD = DOC_CACHE_DIR / "docs_2026_08.md"
RECURSION_LIMIT = 40

SYSTEM_PROMPT = """당신은 한국은행 경제전망보고서(docs_2026_08.pdf 를 변환한 원문)를 근거로
질문에 답하는 에이전트입니다. 반드시 ls/read_file/grep 등 파일시스템 도구로 문서를 검색해
확인한 내용만 답하세요. 문서에서 확인하지 못한 수치나 사실을 지어내지 마세요.
확인이 안 되면 어느 부분을 더 봐야 하는지 설명하고 모른다고 답하세요.
답변은 한국어로, 근거가 된 수치·표현을 인용하며 간결하게 작성하세요."""


def ensure_document_cache() -> Path:
    """PDF를 마크다운으로 변환해 캐시한다. 이미 있으면 재변환하지 않는다."""
    DOC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not DOC_CACHE_MD.exists():
        import pymupdf4llm

        markdown = pymupdf4llm.to_markdown(str(PDF_PATH))
        DOC_CACHE_MD.write_text(markdown, encoding="utf-8")
    return DOC_CACHE_MD


def build_agent(model):
    backend = FilesystemBackend(root_dir=DOC_CACHE_DIR, virtual_mode=True)
    return create_deep_agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
        permissions=[
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")
        ],
        name="day05-gold-agent",
    )


def load_rows():
    with GOLDEN_CSV.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def save_rows(fieldnames, rows):
    with GOLDEN_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def answer(agent, question: str, callbacks) -> str:
    result = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": RECURSION_LIMIT, "callbacks": callbacks},
    )
    return result["messages"][-1].content


def main():
    ensure_document_cache()
    model = load_agent_llm()
    agent = build_agent(model)

    langfuse = connect_langfuse()
    from langfuse.langchain import CallbackHandler

    callbacks = [CallbackHandler()]

    fieldnames, rows = load_rows()
    todo = [row for row in rows if not row["actual_output"].strip()]
    if not todo:
        print(f"{GOLDEN_CSV} 의 actual_output 이 이미 모두 채워져 있습니다.")
        return

    print(
        f"이 실행은 모델 호출 최소 {len(todo)}회(문항별 도구 호출 포함 시 더 늘어남), "
        f"Langfuse 에 trace 기록을 남깁니다. 진행합니다."
    )

    for index, row in enumerate(todo, start=1):
        question = row["input"]
        with langfuse.start_as_current_observation(
            name="gold-agent-question",
            as_type="agent",
            input={"question": question},
            metadata={"row_index": index},
        ) as span:
            output = answer(agent, question, callbacks)
            row["actual_output"] = output
            span.update(output={"actual_output": output})
        save_rows(fieldnames, rows)
        print(f"[{index}/{len(todo)}] {question[:40]}... -> {len(output)}자")

    langfuse.flush()
    print(f"{GOLDEN_CSV} 의 actual_output {len(todo)}건을 채웠습니다.")


if __name__ == "__main__":
    main()
