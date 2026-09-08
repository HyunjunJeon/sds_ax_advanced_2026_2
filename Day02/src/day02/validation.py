"""Skill-owned layout, structural contracts, and a separate semantic support review."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from day02.evidence import digest
from day02.settings import read_json

SkillName = Literal["grounded-qa", "comparison", "procedure", "table-analysis", "insufficient-evidence"]


class Statement(BaseModel):
    label: str = ""
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class Section(BaseModel):
    heading: str
    items: list[Statement] = Field(default_factory=list)


class AnswerPayload(BaseModel):
    skill: SkillName
    status: Literal["answered", "insufficient"]
    sections: list[Section]
    missing: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class SupportReview(BaseModel):
    supported: bool
    reasons: list[str] = Field(default_factory=list)


def skill_spec(root, name):
    if name not in {"grounded-qa", "comparison", "procedure", "table-analysis", "insufficient-evidence"}:
        raise ValueError("알 수 없는 답변 Skill입니다.")
    spec = read_json(root / "skills" / name / "output.json")
    sections = spec.get("sections", [])
    headings = [s.get("heading") for s in sections]
    if not headings or any(not isinstance(h, str) or not h.strip() for h in headings) or len(set(headings)) != len(headings):
        raise ValueError("Skill의 절 제목은 비어 있지 않고 서로 달라야 합니다.")
    for section in sections:
        if section.get("style") not in {"table", "steps", "paragraph"}:
            raise ValueError("Skill style은 table/steps/paragraph 중 하나여야 합니다.")
        if section["style"] == "table" and len(section.get("columns", [])) != 3:
            raise ValueError("표 형식은 label/text/evidence에 대응하는 열 3개가 필요합니다.")
    for field in ["missing_heading", "conflicts_heading", "sources_heading"]:
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise ValueError(f"Skill의 {field} 설정이 필요합니다.")
    return spec


def validate_answer(answer: AnswerPayload, context, root, requested_format: str = "auto"):
    errors = []
    if answer.skill not in context.loaded_skills:
        errors.append(f"{answer.skill}/SKILL.md 전체 본문을 read_file로 읽어야 합니다.")
    if context.search_count == 0:
        errors.append("현재 턴에서 search_knowledge를 실행해야 합니다.")
    if requested_format != "auto" and answer.status == "answered" and answer.skill != requested_format:
        errors.append(f"지정한 답변 형식 {requested_format}을 사용하세요.")
    expected = [s["heading"] for s in skill_spec(root, answer.skill)["sections"]]
    if [s.heading for s in answer.sections] != expected:
        errors.append(f"Skill에 정의된 절 제목과 순서를 지키세요: {expected}")
    statements = [item for section in answer.sections for item in section.items]
    if answer.status == "answered" and not statements:
        errors.append("답변에는 근거가 있는 주장이 필요합니다.")
    if answer.status == "answered" and any(not s.items for s in answer.sections):
        errors.append("답변의 각 필수 절에는 근거가 있는 내용이 필요합니다.")
    if answer.status == "insufficient":
        if answer.skill != "insufficient-evidence" or not (answer.missing or answer.conflicts):
            errors.append("근거 부족 답변은 insufficient-evidence Skill과 부족/상충 이유가 필요합니다.")
    elif answer.skill == "insufficient-evidence":
        errors.append("insufficient-evidence Skill은 insufficient 상태여야 합니다.")
    for item in statements:
        for reference in item.evidence_ids:
            evidence = context.evidence.get(reference)
            if evidence is None:
                errors.append(f"현재 턴에 없는 인용 ID: {reference}")
            elif digest(evidence.quote) != evidence.content_hash:
                errors.append(f"인용문 해시 불일치: {reference}")
            elif not evidence.metadata.applies(context.entity, context.as_of, context.domain):
                errors.append(f"적용 범위를 벗어난 인용: {reference}")
    return list(dict.fromkeys(errors))


def render_answer(answer: AnswerPayload, context, root):
    spec = skill_spec(root, answer.skill)
    lines = []
    used = set()
    for section, section_spec in zip(answer.sections, spec["sections"], strict=True):
        lines.append(f"## {section.heading}")
        lines.append("")
        if section_spec["style"] == "table" and section.items:
            columns = section_spec.get("columns", ["항목", "내용", "근거"])
            lines += ["| " + " | ".join(columns) + " |", "| --- | --- | --- |"]
        for index, item in enumerate(section.items, 1):
            used.update(item.evidence_ids)
            refs = " ".join(f"[{ref}]" for ref in item.evidence_ids)
            if section_spec["style"] == "table":
                label, text = item.label.replace("|", "\\|"), item.text.replace("|", "\\|").replace("\n", "<br>")
                lines.append(f"| {label} | {text} | {refs} |")
            else:
                prefix = f"{index}. " if section_spec["style"] == "steps" else ""
                lines.append(f"{prefix}{item.text} {refs}")
                lines.append("")
        if not section.items:
            lines.append(spec.get("empty_section", "확인된 근거 없음"))
        lines.append("")
    for key in ["missing", "conflicts"]:
        if getattr(answer, key):
            lines += [f"## {spec[key + '_heading']}", ""]
            lines += [f"- {value}" for value in getattr(answer, key)]
            lines.append("")
    if used:
        lines += [f"## {spec['sources_heading']}", ""]
        for reference in sorted(used):
            e = context.evidence[reference]
            location = f"page {e.page}" if e.page else f"slide {e.slide}" if e.slide else "text"
            coordinate = "시각 모델 관찰" if e.kind == "visual_observation" else f"L{e.start_line}–{e.end_line}"
            version = "버전 미확인" if e.version == "unknown" else f"v{e.version}"
            lines.append(f"- [{reference}] {e.source} · {location} · {coordinate} · "
                         f"{version} · `{e.uri}`")
    return "\n".join(lines).strip()
