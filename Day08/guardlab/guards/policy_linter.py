"""
사용: PolicyLinter(rules).lint(text) → [RuleHit(rule, max_prob, tokens)]   (07 에서만 쓴다)
포인트:
  1. 모델 카드 예시의 train_bizlint_v02 는 snapshot 에 없다. config.json 의 auto_map 대로 AutoModel 로 불러온다. 문서와 코드가 다르면 코드가 정답이다.
  2. 규칙 텍스트를 "Policy:\n- ...\n\nText:\n" 접두에 넣고 rule_pool 로 규칙 토큰 위치를 알려주는 방식이다.
  3. 한국어 미지원. 한국어 결과는 측정치이지 보증이 아니다.

주요 내용:
LiquidAI/LFM2.5-Encoder-350M-Policy-Linter 클라이언트.
자유문 규칙(policy) × 텍스트 토큰을 한 번의 Encoder 통과로 매칭해 규칙별 위반 후보 구간과 점수를 낸다 (슬라이드 29).
모델 카드의 언어 목록 15개에 한국어가 없다. 한국어 문서에 대한 결과는 "측정"이지 보증이 아니다.
모델 카드 예시대로 규칙을 "Policy:\\n- rule\\n...\\n\\nText:\\n" 접두에 넣고 rule_pool 을 만든다.
custom code(`modeling_bizlint_rule_matching.py`)를 trust_remote_code 로 불러오므로 revision 을 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import env
from .contracts import GuardSignal, unknown_signal

MODEL_ID = "LiquidAI/LFM2.5-Encoder-350M-Policy-Linter"


@dataclass
class RuleHit:
    rule: str
    max_prob: float
    tokens: list[tuple[str, float]] = field(default_factory=list)  # (토큰 원문, 확률) 상위 몇 개


class PolicyLinter:
    def __init__(self, rules: list[str], *, threshold: float = 0.5, revision: str | None = None):
        self.rules = list(rules)
        self.threshold = threshold
        self.revision = revision if revision is not None else env("GUARD_LINTER_REVISION")
        self._model = None
        self._tok = None
        self.calls = 0

    @property
    def model_id(self) -> str:
        return MODEL_ID

    def _load(self):
        if self._model is not None:
            return
        try:
            from huggingface_hub import snapshot_download
            from transformers import AutoTokenizer
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("transformers/huggingface_hub 가 없습니다. `uv sync` 를 다시 실행하세요.") from e
        kwargs = {"revision": self.revision} if self.revision else {}
        try:
            repo = snapshot_download(MODEL_ID, **kwargs)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Policy-Linter 다운로드 실패 ({type(e).__name__})") from e
        from transformers import AutoModel  # noqa: PLC0415

        # config.json 의 auto_map 이 AutoModel → modeling_bizlint_rule_matching.Lfm2BidirForRuleMatching 을 가리킨다.
        # (모델 카드 예시의 train_bizlint_v02 는 snapshot 에 없다. 문서와 코드가 다르면 코드가 정답이다.)
        self._tok = AutoTokenizer.from_pretrained(repo, trust_remote_code=True)
        self._model = AutoModel.from_pretrained(repo, trust_remote_code=True).eval()

    def lint(self, text: str, *, top_k: int = 6) -> list[RuleHit]:
        """규칙마다 (최대 확률, 확률 높은 토큰들)을 돌려준다. threshold 를 넘긴 규칙만 '위반 후보'다."""
        import torch  # noqa: PLC0415

        self._load()
        self.calls += 1
        prefix = "Policy:\n" + "\n".join(f"- {r}" for r in self.rules) + "\n\nText:\n"
        full = prefix + text
        enc = self._tok(full, return_offsets_mapping=True, return_tensors="pt", truncation=True, max_length=4096)
        offsets = enc.pop("offset_mapping")[0].tolist()
        rule_pool = torch.zeros(1, len(self.rules), len(offsets))
        pos = len("Policy:\n")
        for i, rule in enumerate(self.rules):
            start = pos + 2
            end = start + len(rule)
            idxs = [k for k, (a, b) in enumerate(offsets) if a < end and b > start and a != b]
            if idxs:
                rule_pool[0, i, idxs] = 1 / len(idxs)
            pos = end + 1
        with torch.no_grad():
            probs = self._model(**enc, rule_pool=rule_pool)["logits"].sigmoid()[0]
        text_start = len(prefix)
        hits: list[RuleHit] = []
        for i, rule in enumerate(self.rules):
            scored = []
            for k, (a, b) in enumerate(offsets):
                if b <= text_start or a == b:
                    continue
                scored.append((full[a:b], float(probs[k][i])))
            scored.sort(key=lambda x: -x[1])
            hits.append(RuleHit(rule=rule, max_prob=round(scored[0][1], 3) if scored else 0.0, tokens=scored[:top_k]))
        return hits

    def signal(self, text: str, stage: str = "tool_result") -> GuardSignal:
        try:
            hits = self.lint(text)
        except RuntimeError as e:
            return unknown_signal(stage, self.model_id, str(e))
        flagged = [h for h in hits if h.max_prob >= self.threshold]
        return GuardSignal(stage=stage, status="OK", risk_labels=[f"policy:{h.rule[:40]}" for h in flagged],
                           evidence_spans=[{"rule": h.rule, "max_prob": h.max_prob, "tokens": h.tokens[:3]} for h in hits],
                           score_type="rule_token_prob", score=max((h.max_prob for h in hits), default=0.0),
                           model_id=self.model_id, revision=self.revision)
