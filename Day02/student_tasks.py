"""학생 구현 과제의 공개 API — 07/08번 파일과 WORKSHEET.md를 함께 읽는다.

아래 NotImplementedError는 수강생이 구현할 지점이다. 기본 00~06 실습과 src/day02
프로덕션 경로는 이 모듈을 import하지 않으므로 예제 실행에는 영향을 주지 않는다.
과제별 공개 반례 테스트는 challenge_tests/에 있고, 항상 green이어야 하는 회귀검사
tests/와 분리되어 있다.

알고리즘을 여기서 통과한 뒤 실제 검색·검증 경로에 연결하는 것까지가 과제다
(연결 위치: src/day02/tools/search.py의 candidate_policy, src/day02/agents/rag.py의
extra_claim_validation). 고객/기준일 필터, 인용 검증, 턴 예산을 우회해 점수를 올리는
변경은 허용하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class DocumentFacts:
    """검색 합류가 알아야 하는 문서 사실. 검색 점수가 아니라 관리 메타데이터다.

    version은 마침표 구분 정수(예: "1.0", "2.0"). family는 추가 약정·기본 조항처럼
    함께 읽어야 하는 문서 집합 표시이며, 같은 family+title에서 여러 버전이 동시에
    유효하면 최신 version만 남는다(겹친 유효 기간은 원본 오류로 간주한다).
    """

    uri: str
    title: str
    entity: str = "공통"
    version: str = "1.0"
    valid_from: date = date(1900, 1, 1)
    valid_until: date | None = None
    status: Literal["approved", "draft", "withdrawn"] = "approved"
    family: str = ""


def fuse_and_rank(
    rankings: list[list[str]],
    facts: dict[str, DocumentFacts],
    *,
    entity: str,
    as_of: date,
    limit: int = 6,
    per_family: int = 2,
    weights: list[float] | None = None,
    constant: int = 60,
) -> list[str]:
    """과제 07: 여러 검색기의 순위를 범위 필터 아래에서 합류해 최종 문서 순서를 반환한다.

    규칙(순서대로 적용):
    1. 입력 검증. rankings는 비어 있지 않아야 하고(개별 순위가 비어 있는 것은 허용),
       순위에 등장하는 모든 URI가 facts에 있어야 한다. LLM reranker가 만든 미등록 ID는
       ValueError로 거절한다. weights는 rankings와 길이가 같고 음수가 아니며,
       constant > 0, 1 <= limit <= 10, per_family >= 1, facts의 version은 마침표 구분
       정수, valid_until은 valid_from 이후여야 한다.
    2. 범위 필터. status가 approved이고 entity가 {entity, "공통"}에 속하며
       valid_from <= as_of < valid_until(종료가 None이면 상한 없음)인 문서만 생존한다.
    3. 버전 대체. 같은 (family, title) 그룹에서(family가 빈 문서는 URI 단독 그룹)
       여러 버전이 생존해 있으면 가장 높은 version 하나만 남긴다.
    4. 합류 점수. RRF: score(uri) = sum(weights[i] / (constant + rank_i)). rank는
       각 순위에서의 첫 등장 위치(1 시작)다.
    5. 다양성 배치. 각 생존 family의 최고 점수 문서를 점수순(동점은 URI 사전순)으로
       먼저 하나씩 배치하고, 남은 생존 문서를 점수순으로 family당 per_family개까지
       채운다. 결과 길이는 limit을 넘지 않는다(family가 빈 문서는 각자 자기 family).
    6. 이 함수는 rankings와 facts를 변경하지 않는다.

    실패 상황(이 정책이 지켜야 할 것):
    - 만료된 이전 버전이 키워드 때문에 최상위에 올라와도 최신 버전으로 대체된다.
    - 한 family의 유사 문서 여러 개가 한도를 독점해 유일한 다른 family 근거를
      굶기지 않는다.
    - 동점 결과는 URI 사전순으로 항상 같게 나온다.

    TODO 7-1: 입력 검증과 범위 필터, 버전 대체를 먼저 만든다.
    TODO 7-2: RRF 점수와 다양성 배치를 구현한다. 출력은 list[str]로 결정적이어야 한다.
    TODO 7-3: src/day02/tools/search.py의 candidate_policy로 연결해 03_search.py의
    dense+sparse 결과와 비교하고 차이를 설명한다.
    """
    raise NotImplementedError("과제 07: fuse_and_rank를 구현하세요. 07_fusion_student.py 참고")


@dataclass(frozen=True)
class EvidenceCard:
    """한 턴에서 확보한 근거. content_hash는 quote의 SHA-256 hexdigest다.

    kind가 visual_observation인 근거는 시각 모델의 관찰이므로 행 좌표 검증 없이
    관찰 텍스트의 해시와 적용 범위만 검증한다.
    """

    evidence_id: str
    uri: str
    quote: str
    content_hash: str
    kind: Literal["source_text", "visual_observation"] = "source_text"
    entity: str = "공통"
    valid_from: date = date(1900, 1, 1)
    valid_until: date | None = None
    status: Literal["approved", "draft", "withdrawn"] = "approved"


@dataclass(frozen=True)
class Claim:
    """답변의 한 주장. numeric은 수치·단위를 포함한 주장 표시다."""

    text: str
    evidence_ids: tuple[str, ...] = ()
    numeric: bool = False


@dataclass(frozen=True)
class TurnContext:
    """검증 대상 턴의 상태. 이전 턴 근거는 evidence에 없다(있어서는 안 된다)."""

    evidence: dict[str, EvidenceCard]
    entity: str
    as_of: date
    searched: bool = False


def validate_claims(claims: list[Claim], turn: TurnContext) -> list[str]:
    """과제 08: 답변 주장의 인용을 검증하고 오류 목록을 검사 순서대로 반환한다.

    검사 순서(오류는 중복 제거해 순서대로 모은다):
    1. turn.searched가 False면 "현재 턴에서 검색을 실행해야 합니다.".
    2. evidence_ids가 빈 주장은 "근거가 없는 주장: <주장 앞 32자>".
    3. 각 인용 ID: turn.evidence에 없으면 "현재 턴에 없는 인용 ID: <id>". 있으면
       content_hash가 quote의 SHA-256 hexdigest와 다르면 "인용문 해시 불일치: <id>".
       해시가 맞으면 entity/유효 기간/status 범위를 벗어났으면
       "적용 범위를 벗어난 인용: <id>"(기준은 DocumentFacts와 같다).
    4. numeric 주장의 인용이 모두 visual_observation면
       "수치 주장에는 원문 텍스트 근거가 필요합니다: <주장 앞 32자>".
    5. 이 함수는 claims와 turn을 변경하지 않는다. 오류가 없으면 []를 반환한다.

    실패 상황(이 검증이 지켜야 할 것):
    - 이전 턴 인용 ID를 이번 턴 답변에 재사용하면 현재 턴에 없는 ID로 거절된다.
    - 원문 수정 뒤 온 인용문은 해시 불일치로 걸린다.
    - 시각 관찰만으로 수치를 단정하지 못하게 한다.

    TODO 8-1: 검사 1~3을 구현한다. hashlib.sha256(quote.encode("utf-8")).hexdigest().
    TODO 8-2: 검사 4와 오류 중복 제거·순서를 구현한다.
    TODO 8-3: src/day02/agents/rag.py의 extra_claim_validation으로 연결해 04의
    답변 재검증을 관찰한다.
    """
    raise NotImplementedError("과제 08: validate_claims를 구현하세요. 08_citation_student.py 참고")


@dataclass(frozen=True)
class Excluded:
    """as_of 시점에 유효하지 않은 문서와 그 이유. 이유는 진단용 근거다."""

    uri: str
    reason: Literal["other_entity", "status", "not_yet", "expired", "superseded"]


@dataclass(frozen=True)
class EffectiveSet:
    """as_of에 실제로 존재하는 문서 세계. 검색은 이 집합 안에서만 일어나야 한다."""

    documents: tuple[DocumentFacts, ...]
    excluded: tuple[Excluded, ...]


def effective_documents(catalog: dict[str, DocumentFacts], *, entity: str, as_of: date) -> EffectiveSet:
    """과제 A: as_of 기준일에 유효한 문서 집합과 제외 이유를 결정적으로 반환한다.

    규칙(순서대로 적용):
    1. 제외 이유는 한 문서당 하나만, 다음 판정 순서로 정한다. (1) entity가
       {entity, "공통"} 밖이면 other_entity. (2) status가 approved가 아니면 status.
       (3) valid_from > as_of면 not_yet. (4) valid_until이 있고 as_of >= valid_until이면
       expired. 유효 기간은 반개구간 [valid_from, valid_until)이다.
    2. 유효 후보를 (family, title) 그룹으로 묶고(family가 빈 문서는 (uri, title)
       단독 그룹), 그룹에 두 개 이상 있으면 valid_from이 가장 늦은 문서만 남긴다.
       같으면 uri 사전순 최대. 밀려난 문서는 superseded.
    3. documents는 (title, uri) 오름차순, excluded는 uri 오름차순으로 정렬한다.
    4. 이 함수는 catalog를 변경하지 않는다. 빈 catalog는 빈 집합을 반환한다.

    실패 상황(이 선택이 지켜야 할 것):
    - 기준일이 하루 지나(2026-06-30 → 07-01) 유효 SLA가 v1에서 v2로 바뀌어야 한다.
    - draft 개정안은 미래 유효일과 무관하게 답변 근거가 되지 않는다.
    - 유효 기간이 겹치는 catalog 오류가 있어도 최신 valid_from 문서로 결정된다.

    TODO A1: 이유 판정 순서와 반개구간 경계를 정확히 구현한다.
    TODO A2: (family, title) 겹침 해소와 superseded 처리를 구현한다.
    TODO A3: data/business/catalog.json을 읽어 as_of 6/30, 7/1, 7/15, 9/8의 집합
    변화를 관찰하고, day02 ask --as-of로 답변 기한(15일/20일) 변화와 대조한다.
    """
    raise NotImplementedError("과제 A: effective_documents를 구현하세요. 10_version_challenge.py 참고")


@dataclass(frozen=True)
class TableBlock:
    """표 근거의 선택 단위. 데이터 행 블록은 단위·각주 블록을 requires로 가질 수 있다.

    covers는 확인해야 할 조건 태그(가용률 구간, 적용 대상 등)이고 사후 정답표가
    아니다. requires 관계의 의미상 정확성은 공개 알고리즘 테스트가 보증하지 않는다.
    """

    id: str
    text: str
    covers: frozenset[str] = frozenset()
    requires: frozenset[str] = frozenset()


def render_table_context(blocks: list[TableBlock], selected: tuple[str, ...]) -> str:
    """공개된 비용 함수. UTF-8 본문뿐 아니라 ID/개행도 근거 예산에 포함한다.

    모든 선택 알고리즘이 같은 렌더러를 써야 바이트 절감을 비교할 수 있다.
    표 조각을 중간에서 자르거나 ID를 없애 비용 함수를 피하는 것은 정답이 아니다.
    """
    by_id = {block.id: block for block in blocks}
    return "".join(f"[{key}]\n{by_id[key].text}\n" for key in sorted(selected))


def select_table_evidence(
    blocks: list[TableBlock], required: frozenset[str], max_bytes: int
) -> tuple[str, ...]:
    """과제 B: 의존성을 만족하는 예산 내 최적 표 근거 집합의 정렬된 ID tuple을 반환한다.

    목적함수 우선순위: (1) required 중 덮은 조건 수 최대화, (2) 렌더링 바이트 최소화,
    (3) ID tuple의 사전순 최소화. 완전 커버가 불가능해도 부분해를 반환해야 한다.
    선택된 모든 블록의 requires가 함께 선택되어야 하며 최대 24개 블록을 다룬다.
    중복 ID/알 수 없는 의존성/순환/음수 예산은 선택 전 ValueError로 거절한다.
    선택되지 않을 블록의 잘못된 의존성도 거절한다.

    실패 상황(이 선택이 지켜야 할 것):
    - 단위 행이 잘린 표 조각이 관련도 점수가 높아도, 단위 블록과 함께 선택되거나
      예산 때문에 전체가 배제된다. 조건 없는 수치가 근거로 남지 않는다.
    - 유사한 표 조각 여러 개가 예산을 독점해 필수 묶음(본표+각주)을 밀어내지 못한다.

    TODO B1: 의존성 폐쇄와 입력 검증을 만든다.
    TODO B2: 중복 커버 때문에 단순 점수/길이 정렬이 실패하는 반례를 고려해 탐색한다.
    TODO B3: sla-a-v2.md의 표를 tools/tables.py로 파싱해 블록·covers·requires를
    구성하고, 실제 검색 결과의 근거 바이트 예산과 비교한다.
    """
    raise NotImplementedError("과제 B: select_table_evidence를 구현하세요. 11_table_challenge.py 참고")


class SessionLedger:
    """과제 C: SQLite에 턴별 근거 지문을 남겨 문서 갱신을 감지한다.

    세션 대화 자체는 day02.session.Sessions가 담당한다. 이 장부는 각 턴이 어떤
    근거를 어느 원문 해시로 인용했는지 기록하고, 이후 턴에서 원문이 바뀌었으면
    낡은 인용을 stale로 보고한다. 프로세스를 다시 떠도 기록이 유지되어야 한다.

    TODO C1: schema와 트랜잭션을 설계한다. record_turn 하나가 한 트랜잭션이다.
    TODO C2: stale_evidence에서 원문이 사라진 경우(현재 해시를 알 수 없음)도
    보수적으로 stale로 본다.
    TODO C3: 05_multiturn.py 실행 전후로 기록하고, 문서를 수정·재적재한 뒤
    stale_evidence가 이전 턴 인용을 잡는지 확인한다.
    """

    def __init__(self, path):
        self.path = Path(path)
        # TODO: CREATE TABLE IF NOT EXISTS와 연결 정책을 구현한다. 테스트는 임시 DB를 사용한다.

    def record_turn(self, session: str, scope: dict, *, question: str,
                    evidence: dict[str, tuple[str, str]]) -> int:
        """한 턴을 기록하고 세션·스코프 안에서 단조 증가하는 턴 번호를 반환한다.

        evidence는 evidence_id -> (uri, content_hash)다. session은 영문/숫자/-/_의
        1~64자, question은 비어 있지 않은 1~4000자, content_hash는 비어 있으면
        ValueError다. 스코프 키는 json 직렬화(정렬된 키)로 비교한다.
        재오픈한 뒤에도 턴 번호는 이어서 증가한다.
        """
        raise NotImplementedError("과제 C1: record_turn을 구현하세요")

    def stale_evidence(self, session: str, scope: dict,
                       current: dict[str, str]) -> tuple[tuple[str, int], ...]:
        """현재 원문 해시(current: uri -> hash)와 달라진 이전 인용을 반환한다.

        해당 session·scope의 턴들만 본다. uri가 current에 없거나 해시가 다르면 그
        (evidence_id, turn)이 stale다. 결과는 (turn, evidence_id) 오름차순으로
        결정적으로 정렬한다. 이 보고는 낡은 인용을 다시 쓰지 못하게 하는 재검색
        근거일 뿐, 외부 저장소를 되돌리지 않는다.
        """
        raise NotImplementedError("과제 C2: stale_evidence를 구현하세요")

    def history(self, session: str, scope: dict, limit: int = 4) -> list[dict]:
        """최근 limit턴을 turn 오름차순으로 [{"turn", "question", "evidence"}]로 반환한다.

        limit < 1이면 ValueError다. 잘못된 session ID도 ValueError다. 대화 본문은
        저장하지 않는다(질문과 인용 ID만).
        """
        raise NotImplementedError("과제 C3: history를 구현하세요")
