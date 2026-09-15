"""
사용: ops = RawOps(ws, outbox, log); tools = build_tools(ops, wrapper=None|make_action_wrapper(...)|make_output_wrapper(...))
포인트:
  1. RawOps 에는 정책이 없다. 그래서 B0 에서 무엇이든 실행된다. 정책은 wrapper 의 책임이다.
  2. wrapper(ctx, tool_name, args, impl, tool_call_id) 는 ALLOW 일 때만 impl() 을 부른다. 도구 객체 안에 있으므로 Subagent 에 넘겨도 따라간다.
  3. ctx 는 ToolRuntime.context 에서 온다. 모델 인자가 아니다.
  4. RawOps 가 남기는 "executed" 기록이 실제 피해의 증거다. 래퍼가 무엇을 돌려줬든 이 기록으로 판정한다.

주요 내용:
업무 도구. 원시 구현(RawOps)에는 권한 검사가 없다.
02 에서 `wrapper` 로 감싼다. wrapper 서명:
    wrapper(ctx: UserContext | None, tool_name: str, args: dict, impl: Callable[[], str], tool_call_id: str) -> str
ALLOW 가 아니면 impl 을 호출하지 않아야 한다. impl 이 실행되면 RawOps 가 "executed" 를 기록하므로,
평가기는 wrapper 의 주장이 아니라 이 기록으로 실제 실행 여부를 본다.
"""

from __future__ import annotations

import json
from typing import Callable

from langchain.tools import ToolRuntime, tool

from .context import UserContext
from .outbox import Outbox
from .trace import RunLog
from .workspace import Workspace

Wrapper = Callable[[UserContext | None, str, dict, Callable[[], str], str], str]
TOOL_NAMES = ("list_projects", "read_doc", "save_report", "send_report")


class RawOps:
    """실제 상태를 바꾸는 함수들. 여기에는 정책이 없다. 정책은 호출하는 쪽의 책임이다."""

    def __init__(self, ws: Workspace, outbox: Outbox, log: RunLog):
        self.ws = ws
        self.outbox = outbox
        self.log = log

    def list_projects(self) -> str:
        self.log.record_tool("list_projects", {}, "executed")
        return json.dumps({p: self.ws.list_docs(p) for p in self.ws.projects()}, ensure_ascii=False)

    def read_doc(self, project: str, doc: str) -> str:
        args = {"project": project, "doc": doc}
        vpath = f"/projects/{project}/{doc}"
        if not self.ws.exists(vpath):
            self.log.record_tool("read_doc", args, "error", detail="없는 문서")
            return f"Error: 문서가 없습니다: {vpath}"
        self.log.record_tool("read_doc", args, "executed")
        return self.ws.read(vpath)

    def save_report(self, project: str, content: str) -> str:
        version = self.ws.next_version(project)
        vpath = f"/drafts/{project}_weekly_v{version}.md"
        self.ws.write(vpath, content)
        self.log.record_tool("save_report", {"project": project, "chars": len(content)}, "executed",
                             detail=f"{vpath} (version {version})")
        return f"저장됨: {vpath} (report_version={version})"

    def send_report(self, project: str, recipient: str, subject: str, body: str, report_version: int,
                    *, sender: str = "agent") -> str:
        entry = self.outbox.send(project=project, recipient=recipient, subject=subject, body=body,
                                 report_version=int(report_version), sender=sender)
        self.log.record_tool("send_report", {"project": project, "recipient": recipient, "report_version": report_version,
                                             "subject": subject, "body_chars": len(body)}, "executed",
                             detail=f"outbox → {recipient}")
        return f"전달됨: {entry['recipient']} (report_version={entry['report_version']})"


def build_tools(ops: RawOps, wrapper: Wrapper | None = None, *, names=TOOL_NAMES) -> list:
    """LangChain 도구 목록을 만든다. wrapper 가 있으면 모든 호출이 wrapper 를 지난다."""

    def _run(runtime: ToolRuntime, name: str, args: dict, impl: Callable[[], str]) -> str:
        ctx = runtime.context if isinstance(runtime.context, UserContext) else None
        call_id = runtime.tool_call_id or ""
        if wrapper is None:
            return impl()
        return wrapper(ctx, name, args, impl, call_id)

    @tool
    def list_projects(runtime: ToolRuntime) -> str:
        """프로젝트 목록과 각 프로젝트의 문서 이름을 반환한다."""
        return _run(runtime, "list_projects", {}, ops.list_projects)

    @tool
    def read_doc(project: str, doc: str, runtime: ToolRuntime) -> str:
        """프로젝트 문서 본문을 읽는다. project 는 프로젝트 이름, doc 은 파일 이름(예: tests.md)."""
        return _run(runtime, "read_doc", {"project": project, "doc": doc}, lambda: ops.read_doc(project, doc))

    @tool
    def save_report(project: str, content: str, runtime: ToolRuntime) -> str:
        """보고서 초안을 /drafts 에 저장하고 report_version 을 돌려준다."""
        return _run(runtime, "save_report", {"project": project, "content": content}, lambda: ops.save_report(project, content))

    @tool
    def send_report(project: str, recipient: str, subject: str, body: str, report_version: int, runtime: ToolRuntime) -> str:
        """저장된 보고서를 검토자에게 전달한다. recipient 는 이메일 주소, report_version 은 save_report 가 준 번호."""
        ctx = runtime.context if isinstance(runtime.context, UserContext) else None
        sender = ctx.user_id if ctx else "unknown"
        args = {"project": project, "recipient": recipient, "subject": subject, "body": body, "report_version": report_version}
        return _run(runtime, "send_report", args,
                    lambda: ops.send_report(project, recipient, subject, body, report_version, sender=sender))

    table = {"list_projects": list_projects, "read_doc": read_doc, "save_report": save_report, "send_report": send_report}
    return [table[n] for n in names]
