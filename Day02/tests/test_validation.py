import json
from dataclasses import replace

from day02.agents.factory import TrackedSkillsBackend
from day02.settings import ROOT
from day02.settings import Settings
from day02.validation import AnswerPayload, Section, Statement, render_answer, validate_answer


def answer_for(context):
    output = context.search("신청")
    reference = output["evidence"][1]["evidence_id"]
    context.loaded_skills.add("grounded-qa")
    return AnswerPayload(skill="grounded-qa", status="answered", sections=[
        Section(heading="결론", items=[Statement(text="신청 기한은 다음 달 20일입니다.", evidence_ids=[reference])]),
        Section(heading="설명", items=[Statement(text="추가 약정을 따릅니다.", evidence_ids=[reference])]),
    ])


def test_valid_answer_and_fabricated_reference(context):
    answer = answer_for(context)
    assert validate_answer(answer, context, ROOT) == []
    answer.sections[0].items[0].evidence_ids = ["E-fabricated"]
    assert any("없는 인용 ID" in e for e in validate_answer(answer, context, ROOT))


def test_unread_skill_and_wrong_format(context):
    answer = answer_for(context)
    context.loaded_skills.clear()
    errors = validate_answer(answer, context, ROOT, "comparison")
    assert any("read_file" in e for e in errors)
    assert any("지정한 답변 형식" in e for e in errors)


def test_skill_file_changes_layout_without_python_change(context, tmp_path):
    answer = answer_for(context)
    folder = tmp_path / "skills" / "grounded-qa"
    folder.mkdir(parents=True)
    spec = json.loads((ROOT / "skills/grounded-qa/output.json").read_text())
    spec["sections"][0]["heading"] = "핵심 답변"
    (folder / "output.json").write_text(json.dumps(spec))
    assert validate_answer(answer, context, tmp_path)
    answer.sections[0].heading = "핵심 답변"
    assert validate_answer(answer, context, tmp_path) == []
    assert "## 핵심 답변" in render_answer(answer, context, tmp_path)


def test_partial_read_does_not_count_as_loaded_skill(context):
    backend = TrackedSkillsBackend(ROOT / "skills", context)
    backend.read("/grounded-qa/SKILL.md", limit=2)
    assert "grounded-qa" not in context.loaded_skills
    backend.read("/grounded-qa/SKILL.md")
    assert "grounded-qa" in context.loaded_skills


def test_skill_backend_cannot_read_parent_env(context):
    backend = TrackedSkillsBackend(ROOT / "skills", context)
    result = backend.read("/../.env")
    assert result.error


def test_insufficient_requires_reason_and_skill(context):
    context.search("신청")
    context.loaded_skills.add("insufficient-evidence")
    answer = AnswerPayload(skill="insufficient-evidence", status="insufficient",
                           sections=[Section(heading="확인된 사실")], missing=["질문의 가격 정보가 없습니다."])
    assert validate_answer(answer, context, ROOT) == []
    answer.missing.clear()
    assert validate_answer(answer, context, ROOT)


def test_deepagents_factory_constructs_with_pinned_sdk(context):
    from day02.agents.factory import build_agent
    settings = replace(Settings.load(), api_key="test-only-no-network")
    agent = build_agent(settings, context)
    assert agent.name == "day02-rag"
