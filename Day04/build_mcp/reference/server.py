"""강사용 기준 MCP 서버. 도구 공개와 업무 처리를 연결한다."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from evidence import EvidenceError, EvidenceStore
from mcp.server import MCPServer
from pydantic import BaseModel
from workflow import Workflow, WorkflowError


def create_server(db_path: Path, evidence_dir: Path | None = None) -> MCPServer:
    evidence = EvidenceStore(evidence_dir or db_path.parent / "evidence")
    workflow = Workflow(db_path, evidence)

    async def record_tools(ctx, call_next):
        if ctx.method != "tools/call":
            return await call_next(ctx)
        # 입력 검증 전부터 감싸므로 형식을 어긴 호출도 기록한다.
        params = dict(ctx.params or {})
        # 메타데이터·환경변수·통신 헤더는 기록하지 않는다.
        request = {key: params[key] for key in ("name", "arguments") if key in params}
        try:
            result = await call_next(ctx)
        except Exception as error:
            evidence.record_call(request, {"isError": True, "error": str(error)})
            raise
        serialized = (
            result.model_dump(mode="json", by_alias=True)
            if isinstance(result, BaseModel)
            else result
        )
        evidence.record_call(request, serialized)
        # 응답을 반환하기 전에 저장을 끝낸다. 저장 실패를 성공으로 반환하지 않는다.
        return result

    mcp = MCPServer(name="requirements-lab", middleware=[record_tools])

    def invoke(action: Callable[[], dict]) -> dict[str, Any]:
        try:
            return action()
        except (WorkflowError, EvidenceError) as error:
            return error.result
        # DB 오류 등 예상하지 못한 실패는 SDK의 도구 오류로 전달한다.
        # 업무 오류와 실행 실패를 모두 success=true로 바꾸지 않는다.

    @mcp.tool()
    def start_interview(goal: str) -> dict[str, Any]:
        """새 업무의 목표를 저장하고 세션 ID와 첫 질문을 반환한다."""
        return invoke(lambda: workflow.start_interview(goal))

    @mcp.tool()
    def record_answer(
        session_id: str,
        field: Literal["constraints", "acceptance_criteria"],
        answer: str,
    ) -> dict[str, Any]:
        """세션의 제약 또는 완료 기준을 기록한다. 확정 뒤에는 수정할 수 없다."""
        return invoke(lambda: workflow.record_answer(session_id, field, answer))

    @mcp.tool()
    def get_session(session_id: str) -> dict[str, Any]:
        """세션을 조회하고 근거 파일을 복구한다. DB가 없으면 근거에서 복원한다."""
        return invoke(lambda: workflow.get_session(session_id))

    @mcp.tool()
    def freeze_seed(session_id: str) -> dict[str, Any]:
        """제약과 완료 기준이 기록된 명세를 확정한다. 업무 구현을 실행하지 않는다."""
        return invoke(lambda: workflow.freeze_seed(session_id))

    @mcp.tool()
    def list_evidence(session_id: str = "") -> dict[str, Any]:
        """근거 파일의 ID·제목·경로를 조회한다. 세션 ID 생략 시 세션 목록을 반환한다."""
        return invoke(lambda: evidence.list(session_id))

    @mcp.tool()
    def read_evidence(session_id: str, evidence_id: str) -> dict[str, Any]:
        """근거 파일의 본문을 읽는다. 이후 작업은 이 내용과 근거 ID를 사용한다."""
        return invoke(lambda: evidence.read(session_id, evidence_id))

    @mcp.tool()
    def save_evidence(
        session_id: str, title: str, content: str, source_evidence_id: str = ""
    ) -> dict[str, Any]:
        """작성한 보고서·계획·메모의 전체 본문을 파일로 저장한다. 원본 근거 ID도 연결할 수 있다."""
        return invoke(
            lambda: evidence.save(session_id, title, content, source_evidence_id)
        )

    return mcp


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path.cwd() / "evidence",
        help="독립적으로 보관하고 다음 작업에 전달할 근거 폴더 (기본: 현재 폴더/evidence)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().parent / "work" / "sessions.sqlite3",
        help="세션 기록을 저장할 SQLite 파일",
    )
    args = parser.parse_args()
    # stdio의 stdout은 MCP 통신에 사용한다. 진단 로그는 stderr에 쓴다.
    create_server(args.db.resolve(), args.evidence_dir.resolve()).run(transport="stdio")
