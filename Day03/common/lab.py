"""실험 조건을 코드로 정의하고 실습 그래프를 실행한다.

가르치는 것:
- 실험 조건을 코드로 선언하는 실행 방식. 조건은 LabConfig 필드와 주석으로 남고,
  변경은 파일 diff로 검토되며, 실행 기록(result.json)에 조건·trace·비용이 함께 저장된다.
- 실행기가 지켜야 하는 측정 계약: 실패 실행도 숨기지 않고 기록하고, 예산·동시성을
  명시적으로 주입하며, 실행 행마다 코드·자료 provenance를 남긴다.

이 실습은 CLI 플래그로 조건을 바꾸지 않는다. 각 실습 파일 상단의 LabConfig 값을
직접 편집하고 파일을 다시 실행한다. 이 방식을 고수하는 이유:
- 어떤 조건으로 실행했는지가 코드와 주석으로 남는다(실행 기록에도 자동 저장됨).
- 비교 규칙인 "한 번에 하나의 변수만 바꾼다"를 파일 diff로 서로 검토할 수 있다.
- 조건을 바꾼 이유를 주석으로 남겨 두면 나중에 결과 해석이 흔들리지 않는다.

모듈 구성:
- LabConfig     한 번의 실험을 정의하는 모든 값(질문·backend·예산·대조 조건).
- settings_from LabConfig를 Settings로 바꾼다. .env 키 로드와 workspace 무결성 검사 포함.
- execute       그래프를 실제로 실행해 trace·metrics·결과를 한 행(dict)으로 반환.
- run_lab       execute 결과를 outputs/ 아래 result.json으로 저장하고 요약을 출력.

그래프 파일(build 함수)은 제어 흐름만 담당하고, 검색·모델 호출·적재 같은
메커니즘은 Service가 공유한다. 구조별 성능 차이이 메커니즘 차이로 해석되지
않도록 모든 구조가 같은 Service 계약을 통과한다.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from .config import DAY03, LAB, Settings, viking_url
from .contracts import Request
from .exa_client import ExaClient
from .ingestion import Ingestion
from .provenance import run_provenance
from .rag_backend import Backend, IndexPending
from .runtime import BudgetExceeded, Meter, Models
from .service import Service
from .source_registry import Registry, digest

# 비교 실행기가 부를 수 있는 구조 이름 → 실습 파일. 학생 구현 파일(05/07)도 여기에
# 등록되므로, 구현 전에 09_compare를 돌리면 해당 구조에서 NotImplementedError가 나온다.
FILES = {
    "baseline": "01_baseline_rag.py",
    "adaptive": "02_adaptive_rag.py",
    "sequential": "03_sequential_rag.py",
    "router": "04_router_rag.py",
    "parallel": "05_parallel_rag.py",
    "supervisor": "06_supervisor_rag.py",
    "handoff": "07_handoff_rag.py",
    "web": "08_web_enrichment.py",
}


@dataclass
class LabConfig:
    """한 번의 실습 실행을 정의하는 값. 실습 파일 상단에서 인스턴스를 만들어 편집한다.

    기본값은 어디서든 안전하게 돌아가는 조건(local backend, 웹 없음)이다.
    값을 바꿀 때는 어떤 가설을 확인하려는지 옆에 주석으로 남긴다.
    """

    # ── 질문과 스코프 ────────────────────────────────────────────────────────
    question: str = "알파 서비스의 현재 가용률 목표와 서비스 크레딧 신청 기한은?"
    entity: str = "알파"  # 고객별 계약 필터. 베타 사례는 "베타".
    as_of: str = "2026-09-08"  # 기준일. 이 날짜에 유효한 문서 버전만 검색된다.
    thread: str = "default"  # 대화 스레드. 고객·기준일과 함께 scope 해시가 되어 대화를 격리한다.
    # EXA 공개 검색 주제. 비어 있으면 웹을 쓰지 않는다. 내부 질문은 EXA로 절대 나가지
    # 않으며, 이 값만 외부 검색 질의로 사용된다.
    public_topic: str = ""

    # ── 검색 backend와 작업 영역 ────────────────────────────────────────────
    # "local": 동봉 문서에 대한 어휘 검색. 키·서버 없이 모든 실습이 동작한다.
    #          실제 모델은 호출하며, 벡터 검색 성능으로 해석하지 않는다.
    # "viking": OpenViking 서버 검색. viking_env로 서버 URL/키 파일을 지정한다.
    backend: str = "local"
    # 코퍼스·대화·적재 상태가 저장되는 디렉터리. 서로 다른 실험은 서로 다른 workspace에서
    # 시작해야 앞 실험의 웹 적재가 뒷실험에 영향을 주지 않는다.
    workspace: str = "work/rag/default"
    viking_env: str = ""  # backend="viking"일 때만: 서버 URL/문서 키 .env 파일 경로.
    # True면 이 workspace에 내부 문서를 새로 적재한다. 준비된 workspace를 재사용할 때는
    # False로 둔다(재적재·재임베딩 비용을 피한다).
    ingest: bool = False

    # ── 웹 보강(08_web_enrichment) ──────────────────────────────────────────
    web: str = "off"  # "off" / "transient"(이번 응답의 근거로만 사용) / "persist"(저장 후 재검색).
    # EXA 결과를 받아들일 공식 도메인만. 호스트 이름 형식이며 스킴/경로/포함 공격은 거절된다.
    domains: str = "www.python-httpx.org,docs.python.org"
    web_max_age_hours: int = 168  # 저장한 웹 문서를 다시 조회 없이 재사용하는 기간.
    max_web_documents: int = 3  # 요청당 새로 적재할 웹 문서 상한(1~5).
    # "live": 실제 EXA 호출. "replay": 같은 질의·옵션으로 기록한 응답을 재생(웹 지연 없음).
    exa_mode: str = "live"
    exa_cache: str = "work/rag/exa-snapshots"  # EXA 응답 snapshot 저장 디렉터리.

    # ── 실행 예산(부모와 자식 Worker가 함께 공유) ────────────────────────────
    max_calls: int = 32  # 모델 호출 상한. planner·Worker·reviewer·judge 합산이다.
    max_tools: int = 180  # find/grep/read/EXA/적재 등 도구 호출 총상한.
    seconds: float = 360.0  # 전체 실행 마감(초). 마감 뒤 도착한 응답은 채택하지 않는다.
    context_bytes: int = 20000  # 원문 Context 최대 바이트. UTF-8 기준이며 ID/개행도 포함된다.
    model: str = ""  # 빈 문자열이면 .env의 OPENROUTER_MODEL 또는 기본 모델.

    # ── 구조별 대조 조건(실험에서 한 번에 하나만 바꾼다) ─────────────────────
    # 병렬(05)에서 동시 실행 대신 순차 실행. 그래프·분해는 같고 동시 실행 수만 1이 된다.
    serial_workers: bool = False
    no_replan: bool = False  # Supervisor(06)의 재계획을 끄고 실패를 그대로 노출한다.
    no_owner_memory: bool = False  # Handoff(07)의 다음 턴 담당자 기억을 끄고 general에서 시작한다.
    context_mode: str = "evidence"  # "evidence": 원문 전달 / "summary": 요약만 전달(06).
    skill: bool = False  # Worker 프롬프트에 근거 조사 절차 Skill 문구를 추가하는 대조 조건.
    # Supervisor(06) 오류 주입. "operations:timeout"(한 번 실패 후 회복) 또는
    # "operations:permanent_timeout"(계속 실패). 빈 문자열이면 주입하지 않는다.
    fault: str = ""

    # ── 병렬 비교의 엄밀화(05) ──────────────────────────────────────────────
    # 분해 계획을 저장·재생하는 파일 경로. 지정하면:
    #   첫 실행  — 모델이 만든 분해를 이 파일에 저장한다.
    #   이후 실행 — 모델을 부르지 않고 저장된 분해를 그대로 재생한다.
    # 병렬과 순차(serial_workers=True) 비교에서 분해가 다르면 "동시 실행의 효과"와
    # "분해가 달라진 효과"가 섞인다. 같은 plan_file로 두 실행을 맞춰야 엄밀한 비교가 된다.
    plan_file: str = ""

    # ── 결과 기록 ───────────────────────────────────────────────────────────
    # runs.jsonl/summary에 남을 구조 이름. 빈 문자열이면 실행 파일의 구조 이름을 쓴다.
    # 예: 05를 serial_workers=True로 돌릴 때 label="parallel-serial"로 구분하면
    # 13_mini_pjt에서 parallel과 parallel-serial의 짝 비교가 가능하다.
    label: str = ""


def settings_from(config: LabConfig) -> Settings:
    """LabConfig를 Settings로 변환한다. 검증 실패는 실행 전에 여기서 끝낸다."""
    if config.viking_env:
        # 서버 키는 Day-03/.env가 아니라 지정한 파일에서만 읽는다. 기존 키를 덮어쓰지
        # 않기 위해서이며, 파일이 비어 있으면 바로 오류로 막는다.
        values = dotenv_values(DAY03 / config.viking_env)
        for key in ("OPENVIKING_URL", "OPENVIKING_API_KEY"):
            if not values.get(key):
                raise ValueError("선택한 서버 설정이 비었습니다: " + key)
            os.environ[key] = values[key]
    domains = tuple(d.strip().lower() for d in config.domains.split(",") if d.strip())
    if not domains or any("/" in d or ":" in d for d in domains):
        raise ValueError("domains에는 호스트 이름만 사용하세요.")
    workspace = Path(config.workspace)
    if not workspace.is_absolute():
        workspace = DAY03 / workspace
    s = Settings(
        backend=config.backend,
        workspace=workspace,
        web=config.web,
        model=config.model,
        max_calls=config.max_calls,
        max_tools=config.max_tools,
        seconds=config.seconds,
        context_bytes=config.context_bytes,
        domains=domains,
        web_max_age_hours=config.web_max_age_hours,
        max_additions=config.max_web_documents,
    )
    # workspace는 backend/서버별로 고정된다. 같은 workspace에 다른 서버를 붙이는 실수를
    # 방지한다. 바꾸려면 새 workspace를 만들지 말고 새 디렉터리를 지정한다.
    marker = s.workspace / "backend.json"
    server = viking_url() if s.backend == "viking" else None
    record = {"backend": s.backend, "server": server}
    if marker.exists() and json.loads(marker.read_text()) != record:
        raise ValueError("작업 영역의 backend/서버가 다릅니다. 새 workspace를 지정하세요.")
    marker.write_text(json.dumps(record))
    return s


def request_from(config: LabConfig) -> Request:
    return Request(
        question=config.question,
        entity=config.entity,
        as_of=config.as_of,
        thread=config.thread,
        public_topic=config.public_topic,
    )


def load_build(name):
    """구조 이름으로 실습 파일의 build(service) 함수를 불러온다.

    모듈 실행이 아니라 파일 경로로 직접 로드한다. 각 실습 파일은 앞 파일의 변수를
    요구하지 않으므로 어떤 순서로든 단독 실행할 수 있다.
    """
    spec = importlib.util.spec_from_file_location("rag_lab_" + name, LAB / FILES[name])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build


def _parse_fault(fault: str) -> dict[str, str]:
    """"role:kind" 형태의 오류 주입 설정을 {role: kind}로 바꾼다. 검증도 여기서 끝낸다."""
    if not fault:
        return {}
    role, kind = fault.split(":", 1)
    if kind not in {"timeout", "permanent_timeout"}:
        raise ValueError("지원하지 않는 fault: " + kind)
    return {role: kind}


def execute(architecture: str, config: LabConfig, request: Request | None = None, build=None):
    """하나의 구조를 실제로 실행하고 결과 행을 반환한다.

    반환하는 행에는 요청·설정·trace·metrics·초기/최종 코퍼스가 모두 들어 있다.
    실패 실행도 빈 답변으로 숨기지 않고 status와 stack 위치를 남긴다. 비교 집계에서
    실패한 실행을 분모에서 빼는 편향을 막기 위해서다.
    """
    settings = settings_from(config)
    request = request or request_from(config)
    meter = Meter(settings)
    registry = Registry(settings.workspace / "registry.sqlite")
    backend = Backend(settings, registry, meter)
    initial = registry.snapshot()
    # 동일 thread 이름을 써도 고객/기준일이 바뀌면 과거 담당자와 대화를 섞지 않는다.
    scope = digest(request.entity + "|" + request.as_of + "|" + request.thread)
    history = registry.history(scope)
    result = None
    try:
        backend.prepare(ingest=config.ingest)
        initial = registry.snapshot()
        models = Models(settings, meter)
        exa = ExaClient(
            settings,
            meter,
            DAY03 / config.exa_cache if not Path(config.exa_cache).is_absolute() else Path(config.exa_cache),
            mode=config.exa_mode,
        )
        ingestion = Ingestion(settings, registry, backend, meter, models, exa)
        service = Service(
            settings,
            request,
            backend,
            models,
            meter,
            ingestion,
            history,
            context_mode=config.context_mode,
            skill=config.skill,
            plan_file=_resolve(config.plan_file),
            no_replan=config.no_replan,
            no_owner_memory=config.no_owner_memory,
            faults=_parse_fault(config.fault),
        )
        graph = (build or load_build(architecture))(service)
        # recursion_limit는 그래프 루프 상한, max_concurrency는 동시 Worker 수다.
        # 모델·도구 호출 수와 시간 상한은 별도로 공유 Meter가 검사한다.
        state = graph.invoke(
            {"question": request.question, "results": {}, "revision": 0},
            config={
                "recursion_limit": 40,
                "max_concurrency": 1 if config.serial_workers else 4,
            },
        )
        result = state["result"]
        # 최종 원문·답변 상태만 다음 턴에 전달한다. Worker 중간 대화는 저장하지 않는다.
        history["owner"] = state.get("owner", history.get("owner", "general"))
        history["turns"] = (
            history["turns"]
            + [
                {
                    "question": request.question,
                    "answer": result.get("text", ""),
                    "status": result["status"],
                    "missing": result.get("missing", []),
                }
            ]
        )[-4:]
        registry.save_history(scope, history)
    except Exception as exc:
        # 실패를 성공/빈 답변으로 바꾸지 않는다. 예외 종류와 비밀 값 없는 stack 위치만 남긴다.
        result = {
            "status": "budget_exceeded"
            if isinstance(exc, BudgetExceeded)
            else "index_pending"
            if isinstance(exc, IndexPending)
            else "failed",
            "error": type(exc).__name__,
            "text": "",
            "answer": {"claims": [], "missing": [], "conflicts": []},
            "evidence": [e.model_dump() for e in backend.evidence.values()],
            "missing": [],
        }
        meter.event(
            "run_failed",
            error=type(exc).__name__,
            frames=[
                f"{Path(f.filename).name}:{f.name}:{f.lineno}"
                for f in traceback.extract_tb(exc.__traceback__)[-8:]
            ],
        )
    finally:
        backend.close()
    return {
        # label을 지정하지 않았으면 구조 이름 그대로 기록한다.
        "architecture": config.label or architecture,
        "mode": "live",
        "exa_mode": config.exa_mode,
        "backend": settings.backend,
        # 실행 시점의 코드·자료 hash. 키 값은 포함되지 않는다(provenance 참고).
        "provenance": _provenance_snapshot(),
        "request": request.model_dump(),
        "model": settings.model,
        "web_mode": settings.web,
        "settings": {
            "max_calls": settings.max_calls,
            "max_tools": settings.max_tools,
            "seconds": settings.seconds,
            "context_bytes": settings.context_bytes,
            "domains": settings.domains,
            "skill": config.skill,
            "web_max_age_hours": settings.web_max_age_hours,
            "max_web_documents": settings.max_additions,
            "context_mode": config.context_mode,
            "serial_workers": config.serial_workers,
            "no_replan": config.no_replan,
            "no_owner_memory": config.no_owner_memory,
        },
        "initial_corpus": initial,
        "final_corpus": registry.snapshot(),
        "result": result,
        "metrics": meter.summary(),
        "trace": meter.events,
        "documents": [
            {
                "id": r["id"],
                "kind": r["kind"],
                "hash": r["hash"],
                "url": r["url"],
                "state": r["state"],
            }
            for r in registry.rows(ready=False)
        ],
    }


def _resolve(path: str) -> Path | None:
    """상대 경로는 Day03 기준으로 절대 경로를 만든다. 빈 문자열은 None(기능 끄기)."""
    if not path:
        return None
    p = Path(path)
    return p if p.is_absolute() else DAY03 / p


# 실행 시점의 코드·자료 hash. 프로세스마다 한 번만 계산한다(코드는 실행 중 바뀌지
# 않는다고 본다). 13_mini_pjt의 짝 비교가 이 값을 "implementation" 조건으로 대조해
# "설정이 같고 코드만 바뀐 전후"를 검증할 수 있게 행마다 기록한다.
_PROVENANCE = None


def _provenance_snapshot():
    global _PROVENANCE
    if _PROVENANCE is None:
        _PROVENANCE = run_provenance()
    return _PROVENANCE


def output_dir() -> Path:
    """실행 결과를 저장할 고유 디렉터리를 만든다. 실행마다 새 디렉터리가 생긴다."""
    p = DAY03 / "outputs/rag-comparison" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    p.mkdir(parents=True)
    return p


def run_lab(name: str, config: LabConfig, build=None) -> dict:
    """실습 파일의 진입점. 실행하고 result.json을 남기고 요약을 출력한다.

    실패 상태(failed/budget_exceeded/index_pending)면 0이 아닌 exit code로 끝난다.
    스크립트를 이어서 연결할 때 실패를 조용히 넘어가지 않기 위해서다.
    """
    row = execute(name, config, build=build)
    out = output_dir()
    (out / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "architecture": row["architecture"],
                "status": row["result"]["status"],
                "answer": row["result"].get("text", ""),
                "missing": row["result"].get("missing", []),
                "error": row["result"].get("error"),
                "calls": row["metrics"]["calls"],
                "elapsed_s": row["metrics"]["elapsed_s"],
                "output": str(out),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if row["result"]["status"] in {"failed", "budget_exceeded", "index_pending"}:
        # 학생 구현 파일(05/07)을 구현 전에 실행한 경우, 계약 주석과 검증 테스트로 안내한다.
        # 실패 자체는 정상이다. 알고리즘 과제(10-12)는 과제 파일 실행이 곧 테스트다.
        if row["result"].get("error") == "NotImplementedError":
            print(
                "\n아직 구현되지 않은 학생 과제 파일입니다. 파일 상단의 주석(계약·실패 상황)을 "
                "읽고 TODO를 채운 뒤 다시 실행하세요.\n"
                "그래프 과제(05/07) 검증: uv run pytest challenge_tests/test_graph_challenge.py -q"
            )
        raise SystemExit(1)
    return row


def compare(
    config: LabConfig,
    architectures: list[str],
    cases: list[str],
    repeats: int = 3,
    judge: bool = True,
    seed_workspace: str = "",
):
    """같은 사례를 여러 구조에서 반복 실행하고 비교 산출물을 남긴다.

    - 실행 입력(cases)과 사후 기대 사실(gold)은 분리되어 있고, 정답표는 judge 호출에만
      제공된다. Agent 입력으로 절대 전달되지 않는다.
    - 구조별 workspace를 나누고 seed 코퍼스 snapshot을 복사해 앞 구조의 웹 적재가
      뒤 구조에 공짜 이득을 주지 않게 한다. seed는 문서 상태만 복사하고 대화는 복사하지 않는다.
    - 반복마다 구조 실행 순서를 뒤집는다. 실행 순서가 결과에 영향을 주지 않는지 확인용이다.
    - 실패/보류 행도 즉시 runs.jsonl에 추가한다. 중간 종료해도 성공 실행만 남지 않게 한다.

    judge=True면 별도 모델이 기대 사실과 원문을 대조하고 그 호출 비용은 evaluation_metrics에
    따로 기록된다. 사람의 원문 대조를 대체하지 않는다.
    """
    from .evaluation import evaluate, load_jsonl, summarize

    if not 1 <= repeats <= 5:
        raise ValueError("repeats는 1~5")
    if not set(architectures) <= set(FILES):
        raise ValueError("알 수 없는 architecture: " + str(set(architectures) - set(FILES)))
    cases_table = {r["id"]: r for r in load_jsonl(LAB / "data/cases.jsonl")}
    gold = {r["id"]: r for r in load_jsonl(LAB / "data/gold.jsonl")}
    if not set(cases) <= set(cases_table):
        raise ValueError("알 수 없는 case: " + str(set(cases) - set(cases_table)))
    out = output_dir()
    root = _resolve(config.workspace)
    rows = []
    for repeat in range(repeats):
        order = architectures if repeat % 2 == 0 else list(reversed(architectures))
        for route in order:
            for case_id in cases:
                case = cases_table[case_id]
                # 사례·구조·반복마다 workspace를 나눈다. dataclasses.replace로 원본을
                # 바꾸지 않고 한 가지(경로)만 다른 설정을 만든다.
                cell = dataclasses.replace(
                    config,
                    workspace=str(root / out.name / str(repeat) / route / case_id),
                    label="",
                )
                settings = settings_from(cell)
                if seed_workspace:
                    seed = _resolve(seed_workspace)
                    backend_a = json.loads((seed / "backend.json").read_text())
                    backend_b = json.loads((settings.workspace / "backend.json").read_text())
                    if backend_a != backend_b:
                        raise ValueError("seed backend/서버가 다릅니다.")
                    Registry(settings.workspace / "registry.sqlite").seed_from(
                        Registry(seed / "registry.sqlite")
                    )
                for turn, question in enumerate(case.get("turns", [case["question"]])):
                    request = Request(
                        question=question,
                        entity=case.get("entity", config.entity),
                        as_of=case.get("as_of", config.as_of),
                        thread=case_id,
                        public_topic=case.get("public_topic", ""),
                    )
                    row = execute(route, cell, request)
                    row.update(case_id=case_id, repeat=repeat, turn=turn)
                    # 다중 턴 문제의 gold는 마지막 질문 기준이다. 첫 질문을 같은 gold로
                    # 채점하면 올바른 중간 답변을 오답으로 세는 평가 오류가 생긴다.
                    if turn == len(case.get("turns", [case["question"]])) - 1:
                        judge_meter = Meter(settings)
                        judge_model = Models(settings, judge_meter) if judge else None
                        try:
                            row["evaluation"] = evaluate(row["result"], gold[case_id], judge_model)
                        except Exception as exc:
                            row["evaluation"] = {"error": type(exc).__name__}
                        row["evaluation_metrics"] = judge_meter.summary()
                    rows.append(row)
                    with (out / "runs.jsonl").open("a") as f:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(
                        f"{repeat + 1} {row['architecture']} {case_id}/{turn + 1}: "
                        f"{row['result']['status']}",
                        flush=True,
                    )
    import csv

    summary = summarize(rows)
    with (out / "summary.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    lines = [
        "# RAG 구조 비교",
        "",
        f"실제 모델 실행 / EXA {config.exa_mode} / backend {config.backend} / web {config.web}",
        "",
        "complete는 자체 검토 상태다. 정답률은 judge 평가와 사람의 원문 검토를 함께 읽는다.",
        "",
        "| 구조 | 실행 | 완료 | 보류 | 오류 | 평균 지연(s) | 평균 모델 호출 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {s['architecture']} | {s['runs']} | {s['completed']} | {s['uncertain']} | "
        f"{s['errors']} | {s['mean_latency_s']} | {s['mean_model_calls']} |"
        for s in summary
    ]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n")
    print("결과:", out)
    if any(r["result"]["status"] in {"failed", "budget_exceeded", "index_pending"} for r in rows):
        raise SystemExit(1)
    return rows
