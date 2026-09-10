"""업무 작업명과 JSON 표준 입력으로 실행한다. 사용법은 ../SKILL.md에 있다.

Python 3.11+ 표준 라이브러리만 사용한다. MCP·모델 호출은 없다.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path

from workflow import QUESTIONS, Workflow, WorkflowError

from evidence import EvidenceError, EvidenceStore

# 모델 도구 목록이 아니라 이 프로그램의 명령 분기다.
OPERATIONS = {
    "start_interview": (Workflow, "start_interview"),
    "record_answer": (Workflow, "record_answer"),
    "get_session": (Workflow, "get_session"),
    "freeze_seed": (Workflow, "freeze_seed"),
    "list_evidence": (EvidenceStore, "list"),
    "read_evidence": (EvidenceStore, "read"),
    "save_evidence": (EvidenceStore, "save"),
}


class InputError(Exception):
    """실행 전에 고쳐야 하는 입력 형식 오류."""


def validate(operation: str, arguments: object) -> dict:
    """기존 메서드의 필수 인자·기본값을 사용하고 문자열과 field를 검사한다."""
    if operation not in OPERATIONS:
        raise InputError(f"작업명을 확인하세요: {', '.join(OPERATIONS)}")
    if not isinstance(arguments, dict):
        raise InputError("표준 입력은 JSON 객체 하나여야 합니다.")
    owner, method = OPERATIONS[operation]
    try:
        bound = inspect.signature(getattr(owner, method)).bind(None, **arguments)
    except TypeError as error:
        raise InputError(str(error)) from None
    bound.apply_defaults()
    values = dict(bound.arguments)
    values.pop("self")
    if any(not isinstance(value, str) for value in values.values()):
        raise InputError(
            "업무 인자의 값은 문자열이어야 합니다. 선택 인자는 생략하세요."
        )
    if operation == "record_answer" and values["field"] not in QUESTIONS:
        raise InputError("field는 constraints 또는 acceptance_criteria여야 합니다.")
    return values


def execute(operation: str, arguments: dict, db: Path, evidence: EvidenceStore) -> dict:
    """일곱 작업은 기존 업무 메서드로 처리한다. 인자 검사는 호출 전에 끝낸다."""
    owner, method = OPERATIONS[operation]
    instance = Workflow(db, evidence) if owner is Workflow else evidence
    return getattr(instance, method)(**arguments)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, required=True, help="재개에 사용할 SQLite 경로"
    )
    parser.add_argument(
        "--evidence-dir", type=Path, required=True, help="근거 파일 폴더"
    )
    parser.add_argument("operation", help="작업명: " + ", ".join(OPERATIONS))
    args = parser.parse_args()

    raw = ""
    request = {"name": args.operation, "arguments": None}
    evidence = None
    try:
        evidence = EvidenceStore(args.evidence_dir)
        try:
            raw = sys.stdin.read()
            request["arguments"] = json.loads(raw)
        except UnicodeError:
            raise InputError("표준 입력은 UTF-8 JSON이어야 합니다.") from None
        except ValueError:
            # 해석하지 못한 입력도 근거에 남긴다. 환경변수는 읽거나 기록하지 않는다.
            request["raw_input"] = raw
            raise InputError("표준 입력을 JSON 객체 하나로 전달하세요.") from None
        arguments = validate(args.operation, request["arguments"])
        result = execute(args.operation, arguments, args.db.resolve(), evidence)
        exit_code = 0
    except InputError as error:
        result = {"success": False, "error": "invalid_arguments", "message": str(error)}
        exit_code = 2
    except (WorkflowError, EvidenceError) as error:
        result = error.result
        exit_code = 1
    except Exception as error:  # noqa: BLE001 - 실패를 JSON과 종료 코드로 전달한다.
        result = {
            "success": False,
            "error": "execution_failed",
            "message": "저장 위치와 실행 근거를 확인하세요. 상태가 저장됐는지는 확인이 필요합니다.",
            "error_type": type(error).__name__,
        }
        exit_code = 1

    if evidence is not None:
        try:
            # 응답 전에 저장한다. 업무 인자 검증 실패도 포함한다.
            evidence.record_call(request, result, exit_code)
        except Exception as error:  # noqa: BLE001 - 기록 실패를 성공으로 반환하지 않는다.
            result = {
                "success": False,
                "error": "call_record_failed",
                "message": "호출 근거 저장에 실패했습니다. operation_result로 이미 처리된 결과를 확인하세요.",
                "error_type": type(error).__name__,
                "operation_result": result,
            }
            exit_code = 1
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
