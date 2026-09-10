"""01·02: 업무 근거 → Scenario 초안 → 사람 검수 → 동결. 모델·외부 서비스 호출 없음."""

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from business_lab.contracts import Scenario
from business_lab.dataset import DATA, POLICY_VERSION, fingerprint, load_dataset, load_fixtures
from business_lab.storage import make_run_id, save_run_report

HEADERS = ["scenario_id", "input", "fixture_id", "expected_output", "followups", "max_turns",
           "family_id", "slice", "split", "source", "policy_version", "source_trace_id",
           "source_artifact_id", "sanitization_record", "review_status", "reviewer", "review_reason"]
JSON_FIELDS = {"input", "expected_output", "followups"}
CARD_FIELDS = {"input", "fixture_id", "expected_output", "followups", "max_turns"}


def draft_cases() -> list[dict]:
    """개발용 예시를 미검수 초안으로 내보낸다. 후속 입력은 정답과 분리한 사용자 입력이다."""
    cards = [c.model_dump() for c in load_dataset(split="dev")]
    normal = deepcopy(next(c for c in cards if c["metadata"]["scenario_id"] == "normal"))
    normal["metadata"].update(scenario_id="clarification-episode", family_id="incident-normal",
                              slice="정보 보충 후 완료")
    normal["input"] = {"message": "주문을 취소하고 싶습니다.", "order_id": None,
                       "request_id": "request-clarification-episode"}
    normal["followups"] = [{"when_response": "clarification", "request": {
        "message": "주문 번호는 order-a입니다. 처음 요청한 취소를 진행해 주세요.",
        "order_id": "order-a", "request_id": "request-clarification-episode"}}]
    normal["max_turns"] = 2
    cards.append(normal)
    for card in cards:
        card["metadata"].update(review_status="pending", reviewer="", review_reason="")
    return cards


def write_workbook(cards: list[dict], path: Path) -> None:
    """초안을 작성한다. 기존 검수 파일을 덮어쓰지 않는다. JSON 열도 사람이 수정할 수 있다."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    if path.exists():
        raise FileExistsError(f"이미 존재하는 검수 파일: {path}")
    book = Workbook()
    instructions = book.active
    instructions.title = "작성안내"
    for row in [
        ["목적", "업무 문서·초기 상태에 근거해 Scenario 초안을 검수합니다. 기대 상태는 Agent 출력과 다릅니다."],
        ["자료", "business_lab/data/interview.md / order_policy.md / fixtures.json"],
        ["예상 답변", "expected_output은 강사 제공 초안입니다. 앵커링에 유의하고 정책과 숫자를 직접 대조하세요."],
        ["JSON 열", "input, expected_output, followups는 유효한 JSON이어야 합니다. 수식은 사용하지 마세요."],
        ["검수", "review_status를 approved/pending/rejected로 정하고 reviewer와 review_reason을 직접 입력하세요."],
        ["범위", "fixture_id는 실행 초기 조건, expected_output은 평가자만 보는 기준입니다."],
        ["추가 사례", "새 scenario_id를 사용하세요. 같은 사건의 변형은 family_id를 유지하세요."],
        ["후속 턴", "followups는 사용자 입력 규칙입니다. 모든 턴에서 request_id를 유지합니다."],
    ]:
        instructions.append(row)
    instructions.column_dimensions["A"].width = 18
    instructions.column_dimensions["B"].width = 100
    sheet = book.create_sheet("시나리오")
    sheet.append(HEADERS)
    for card in cards:
        values = card | card["metadata"]
        sheet.append([json.dumps(values.get(k, [] if k == "followups" else {}), ensure_ascii=False)
                      if k in JSON_FIELDS else values.get(k) for k in HEADERS])
    sheet.freeze_panes = "C2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="203864")
        sheet.column_dimensions[cell.column_letter].width = 52 if cell.value in JSON_FIELDS else 24
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        sheet.row_dimensions[row[0].row].height = 115
    status = DataValidation(type="list", formula1='"pending,approved,rejected"', allow_blank=False)
    sheet.add_data_validation(status)
    status.add(f"O2:O{max(2, len(cards) + 1)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)


def read_workbook(path: Path) -> list[dict]:
    """검수 행을 읽는다. 중간 빈 행·잘못된 JSON·수식은 조용히 제거하지 않는다."""
    from openpyxl import load_workbook

    book = load_workbook(path, data_only=False)
    sheet = book["시나리오"]
    if [c.value for c in sheet[1]] != HEADERS:
        raise ValueError("시나리오 열 구성이 달라졌습니다. 01의 원래 열을 유지하세요.")
    cards = []
    for cells in sheet.iter_rows(min_row=2):
        if all(c.value is None for c in cells):
            continue
        if any(c.data_type == "f" for c in cells):
            raise ValueError(f"{cells[0].row}행에 수식이 있습니다. 검수 값은 직접 입력하세요.")
        values = {k: c.value for k, c in zip(HEADERS, cells)}
        for key in JSON_FIELDS:
            try:
                values[key] = json.loads(values[key] or ("[]" if key == "followups" else "{}"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{cells[0].row}행 {key}의 JSON이 유효하지 않습니다.") from exc
        for key in ("reviewer", "review_reason", "review_status", "source", "scenario_id", "family_id", "slice"):
            values[key] = str(values[key] or "").strip()
        cards.append({k: values[k] for k in CARD_FIELDS} | {
            "metadata": {k: values[k] for k in HEADERS if k not in CARD_FIELDS}})
    if not cards:
        raise ValueError("시나리오가 비어 있습니다.")
    return cards


def freeze(cards: list[dict], fixtures: dict, out: Path, *, scenario_ids=None, split="dev") -> dict:
    """선택 문항의 검수·초기 상태·분할을 검사하고 승인된 자료만 스냅샷으로 동결한다."""
    ids = [c["metadata"]["scenario_id"] for c in cards]
    if any(not sid for sid in ids) or len(set(ids)) != len(ids):
        raise ValueError("scenario_id가 비었거나 중복됩니다.")
    selected_ids = ids if scenario_ids is None else list(scenario_ids)
    if not selected_ids or len(set(selected_ids)) != len(selected_ids) or set(selected_ids) - set(ids):
        raise ValueError("선택 문항이 비었거나 중복/알 수 없는 ID가 있습니다.")
    families = {}
    # 강사 제공 다른 분할도 대조해, 같은 사건이 dev와 holdout에 걸치지 않게 한다.
    other = [c.model_dump() for c in load_dataset(split="holdout" if split == "dev" else "dev")]
    for row in [*cards, *other]:
        meta = row["metadata"]
        family = meta["family_id"]
        if family in families and families[family] != meta["split"]:
            raise ValueError("같은 family_id가 dev와 holdout에 걸쳐 있습니다.")
        families[family] = meta["split"]
    approved = []
    semantic_keys = set()
    for raw in cards:
        if raw["metadata"]["scenario_id"] not in selected_ids:
            continue
        card = Scenario.model_validate(raw)
        meta = card.metadata
        if meta.split != split or meta.review_status != "approved" or not all(
            v.strip() for v in (meta.reviewer, meta.review_reason, meta.source, meta.family_id)
        ):
            raise ValueError(f"검수·분할 확인 필요: {meta.scenario_id}. 담당자·근거를 직접 작성하세요.")
        if meta.policy_version != POLICY_VERSION or card.fixture_id not in fixtures:
            raise ValueError("정책 버전 또는 fixture_id가 유효하지 않습니다.")
        if meta.source_artifact_id and not meta.sanitization_record:
            raise ValueError("Trace 승격 사례에는 비식별화 확인 기록이 필요합니다.")
        meaning = card.model_dump(include={"input", "fixture_id", "expected_output", "followups", "max_turns"})
        meaning["input"].pop("request_id")
        for followup in meaning["followups"]:
            followup["request"].pop("request_id")
        identity = fingerprint(meaning)
        if identity in semantic_keys:
            raise ValueError("ID만 다른 중복 사례가 있습니다. 기존 사례에 연결하거나 다른 조건을 명세하세요.")
        semantic_keys.add(identity)
        approved.append(card.model_dump())
    snapshot = {"cards": approved, "fixtures": {c["fixture_id"]: fixtures[c["fixture_id"]] for c in approved},
                "policy": (DATA / "order_policy.md").read_text(), "schema_version": "course-v2", "split": split}
    document = {"run_id": make_run_id(), "kind": "approved_dataset", "snapshot_hash": fingerprint(snapshot),
                "snapshot": snapshot, "coverage": dict(Counter(c["metadata"]["slice"] for c in approved)),
                "reviewed_ids": selected_ids, "not_selected_ids": [sid for sid in ids if sid not in selected_ids]}
    save_run_report(document, out)
    return document


def load_frozen(path: Path) -> dict:
    """동결 이후 파일 내용이 변했거나 스키마가 다른 경우 실행 전에 차단한다."""
    data = json.loads(path.read_text())
    if data.get("kind") != "approved_dataset" or data.get("snapshot_hash") != fingerprint(data.get("snapshot")):
        raise ValueError("승인 데이터가 아니거나 동결한 내용이 변경됐습니다. 02에서 새 버전을 만드세요.")
    if data["snapshot"].get("schema_version") != "course-v2":
        raise ValueError("다른 Scenario 스키마 버전입니다.")
    return data
