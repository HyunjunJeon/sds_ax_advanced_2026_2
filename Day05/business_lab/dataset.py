"""검수한 시나리오와 초기 상태를 로드하고 비교 조건의 해시를 고정한다."""

import hashlib
import json
from pathlib import Path

from business_lab.contracts import Scenario

DATA = Path(__file__).parent / "data"
POLICY_VERSION = "order-policy-v1"


def fingerprint(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_dataset(path: Path = DATA / "scenarios.json", *, split: str = "dev") -> list[Scenario]:
    """검수 조건과 가족 단위 분할을 검사한다. 미검수 문항을 조용히 섞지 않는다."""
    if split not in {"dev", "holdout"}:
        raise ValueError("split은 dev 또는 holdout입니다.")
    cards = [Scenario.model_validate(row) for row in json.loads(path.read_text())]
    ids, families = set(), {}
    for card in cards:
        meta = card.metadata
        if meta.scenario_id in ids:
            raise ValueError(f"중복 scenario_id: {meta.scenario_id}")
        ids.add(meta.scenario_id)
        if meta.family_id in families and families[meta.family_id] != meta.split:
            raise ValueError("같은 family_id가 dev와 holdout에 걸쳐 있습니다.")
        families[meta.family_id] = meta.split
    selected = [card for card in cards if card.metadata.split == split]
    if not selected:
        raise ValueError("선택한 분할에 시나리오가 없습니다.")
    for card in selected:
        meta = card.metadata
        if meta.review_status != "approved" or not all(
            text.strip() for text in (meta.reviewer, meta.source, meta.review_reason)
        ):
            raise ValueError(f"검수가 끝나지 않은 시나리오: {meta.scenario_id}")
        if meta.policy_version != POLICY_VERSION:
            raise ValueError("시나리오와 도구 정책 버전이 다릅니다.")
    return selected


def load_fixtures(path: Path = DATA / "fixtures.json") -> dict:
    """fixture는 환경 구성만 포함한다. 기대 답변·평가 판정은 포함하지 않는다."""
    return json.loads(path.read_text())
