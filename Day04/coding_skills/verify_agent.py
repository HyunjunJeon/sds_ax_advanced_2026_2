"""실제 모델과 에이전트 프로세스로 코딩 Skill 네 시나리오를 검사한다 (유료 API)."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from run_files import copy_project, new_run_id, project_files, save_json

LAB = Path(__file__).resolve().parent
DAY04 = LAB.parent
TEST_COMMAND = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]

# 평가 입력은 이 검사 프로세스만 사용한다. 에이전트의 실행 경로에 넣지 않는다.
BOUNDARY_CHECK = """
from datetime import date
from decimal import Decimal
from csv_report.report import Sale, summarize
d = date(2027, 2, 12)
rows = [Sale(d, "새고객", Decimal("19.37"), "paid"),
        Sale(d, "새고객", Decimal("-3.11"), "paid"),
        Sale(d, "다른고객", Decimal("8.04"), "paid"),
        Sale(d, "제외", Decimal("99"), "cancelled"),
        Sale(date(2027, 2, 11), "이전", Decimal("900"), "paid"),
        Sale(date(2027, 2, 13), "이후", Decimal("800"), "paid")]
assert summarize(rows, d, d) == {
    "customers": {"다른고객": "8.04", "새고객": "16.26"}, "total": "24.30"}
assert list(summarize(rows, d, d)["customers"]) == ["다른고객", "새고객"]
assert summarize([], d, d) == {"customers": {}, "total": "0.00"}
print("같은 날·환불·취소·기간 밖·새 고객·빈 결과 검사 통과")
"""


class Verification:
    def __init__(self):
        run_id = new_run_id()
        self.root = DAY04 / "evidence" / "coding-validation" / run_id
        self.work = DAY04 / "work" / "coding-validation" / run_id
        self.root.mkdir(parents=True)
        self.work.mkdir(parents=True)
        self.checks = []
        self.cases = []
        self.case_dirs = {}

    def check(self, name: str, condition: bool) -> None:
        self.checks.append({"name": name, "passed": bool(condition)})
        print(f"[{'통과' if condition else '실패'}] {name}", flush=True)

    def command(self, label: str, workspace: Path, args: list[str], *, stdin=None):
        result = subprocess.run(
            args,
            cwd=workspace,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        save_json(
            self.root / f"{label}.json",
            {
                "command": args,
                "cwd": str(workspace),
                "input": stdin,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
        return result

    def case(self, name: str, workspace: Path, prompt: str) -> dict:
        run_dir = self.root / name
        self.case_dirs[name] = run_dir
        command = [
            sys.executable,
            str(LAB / "agent.py"),
            "--workspace",
            str(workspace),
            "--run-dir",
            str(run_dir),
            "--prompt",
            prompt,
        ]
        started = time.monotonic()
        with (self.root / f"{name}.log").open("x", encoding="utf-8") as log:
            process = subprocess.Popen(
                command, cwd=DAY04, stdout=log, stderr=subprocess.STDOUT
            )
            try:
                exit_code = process.wait(timeout=240)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                exit_code = -1
        trace = run_dir / "trace.jsonl"
        messages = (
            [json.loads(line) for line in trace.read_text().splitlines()]
            if trace.exists()
            else []
        )
        calls = [
            (i, call)
            for i, record in enumerate(messages)
            for call in record.get("tool_calls", [])
        ]
        outputs = {
            record.get("tool_call_id"): record
            for record in messages
            if record.get("role") == "tool"
        }
        commands = sorted(
            [
                json.loads(path.read_text())
                for path in (run_dir / "commands").glob("*.json")
            ],
            key=lambda record: record["started_at"],
        )
        model_calls = sum(record.get("role") == "ai" for record in messages)
        self.cases.append(
            {
                "name": name,
                "agent_pid": process.pid,
                "exit_code": exit_code,
                "seconds": round(time.monotonic() - started, 2),
                "model_responses": model_calls,
            }
        )
        self.check(
            f"{name}: 에이전트 정상 종료",
            exit_code == 0 and any(r["kind"] == "completed" for r in messages),
        )
        discovered = {
            n for r in messages if r["kind"] == "skills_discovered" for n in r["names"]
        }
        self.check(
            f"{name}: 세 Skill metadata 발견",
            discovered == {"debug-python", "review-python", "add-python-feature"},
        )
        for filename in ("trace.jsonl", "files.json", "changes.patch"):
            self.check(f"{name}: {filename} 저장", (run_dir / filename).is_file())
        return {
            "dir": run_dir,
            "messages": messages,
            "calls": calls,
            "outputs": outputs,
            "commands": commands,
        }

    def read(self, case: dict, path: str) -> bool:
        for _, call in case["calls"]:
            if call["name"] != "read_file" or call["args"].get("file_path") != path:
                continue
            output = case["outputs"].get(call["id"], {})
            text = output.get("content", "")
            if (
                text
                and output.get("status") != "error"
                and not text.lstrip().startswith("Error")
            ):
                return True
        return False

    def failed_then_passed(self, case: dict) -> bool:
        tests = [r for r in case["commands"] if "unittest" in r["command"]]
        return bool(
            tests
            and tests[-1]["exit_code"] == 0
            and any(r["exit_code"] != 0 for r in tests[:-1])
        )

    def failure_before_source_edit(self, case: dict) -> bool:
        failed_call_positions = []
        source_edit_positions = []
        for index, call in case["calls"]:
            if call["name"] == "execute" and "unittest" in call["args"].get(
                "command", ""
            ):
                output = case["outputs"].get(call["id"], {}).get("content", "")
                if "FAILED" in output:
                    failed_call_positions.append(index)
            if call["name"] in {"edit_file", "write_file"} and call["args"].get(
                "file_path", ""
            ).startswith("/csv_report/"):
                source_edit_positions.append(index)
        return bool(
            failed_call_positions
            and source_edit_positions
            and min(failed_call_positions) < min(source_edit_positions)
        )

    def verify(self) -> None:
        baseline = LAB / "project"
        original = project_files(baseline)
        workspaces = {}
        for name in ("concept", "review", "debug"):
            workspace = self.work / name
            copy_project(baseline, workspace)
            workspaces[name] = workspace
        result = self.command("initial-tests", workspaces["debug"], TEST_COMMAND)
        self.check(
            "초기 테스트: 6개 실행, 기간 경계 1개 실패",
            result.returncode != 0
            and "Ran 6 tests" in result.stderr
            and "FAILED (failures=1)" in result.stderr
            and "test_includes_start_and_end" in result.stderr,
        )

        concept = self.case(
            "01_concept",
            workspaces["concept"],
            "Python의 회귀 테스트가 무엇인지 짧게 설명해 줘. 프로젝트 조사나 수정 요청은 아니야.",
        )
        self.check(
            "개념 설명: 업무 Skill 본문을 읽지 않음",
            not any(
                call["name"] == "read_file"
                and "/skills/" in call["args"].get("file_path", "")
                for _, call in concept["calls"]
            ),
        )
        self.check("개념 설명: 셸 실행 없음", not concept["commands"])
        self.check(
            "개념 설명: 프로젝트 보존", project_files(workspaces["concept"]) == original
        )

        review = self.case(
            "02_review",
            workspaces["review"],
            "이 CSV 매출 집계 프로그램을 코드 리뷰해 줘. README 계약과 실제 테스트를 기준으로 결함을 확인하고 리뷰 파일을 남겨 줘. 코드·테스트·README는 수정하지 마.",
        )
        self.check(
            "리뷰: Skill 본문 읽기 성공",
            self.read(review, "/skills/review-python/SKILL.md"),
        )
        self.check(
            "리뷰: 양식 읽기 성공",
            self.read(review, "/skills/review-python/assets/review-template.md"),
        )
        self.check(
            "리뷰: 기존 실패 실제 관찰",
            any(
                r["exit_code"] != 0 and "FAILED" in r["output"]
                for r in review["commands"]
            ),
        )
        self.check(
            "리뷰: 프로젝트 보존", project_files(workspaces["review"]) == original
        )
        review_file = review["dir"] / "artifacts" / "review.md"
        review_text = review_file.read_text() if review_file.is_file() else ""
        self.check(
            "리뷰: 코드 위치와 종료일 문제를 보고서에 기록",
            "report.py" in review_text and "종료일" in review_text,
        )

        debug = self.case(
            "03_debug",
            workspaces["debug"],
            "CSV 매출 집계의 기간 경계 테스트가 실패해. 실제 실패를 재현하고 원인을 고쳐 줘. 기존 tests/test_report.py는 보존하고 추가 반례는 새 테스트 파일에 작성해 줘. 검사 결과와 변경 이유를 보고서에 남겨 줘.",
        )
        self.check(
            "수정: Skill 본문 읽기 성공",
            self.read(debug, "/skills/debug-python/SKILL.md"),
        )
        self.check(
            "수정: 경계 참고 자료 읽기 성공",
            self.read(debug, "/skills/debug-python/references/boundaries.md"),
        )
        self.check(
            "수정: 실제 실패 후 실제 테스트 통과", self.failed_then_passed(debug)
        )
        self.check(
            "수정: 소스 수정 전에 실패 재현", self.failure_before_source_edit(debug)
        )
        after_debug = project_files(workspaces["debug"])
        self.check(
            "수정: 기존 테스트 원문 보존",
            after_debug.get("tests/test_report.py") == original["tests/test_report.py"],
        )
        self.check(
            "수정: 새 회귀 테스트 추가",
            any(
                name.startswith("tests/test_") and name not in original
                for name in after_debug
            ),
        )
        self.check(
            "수정: 실제 소스 변경",
            after_debug.get("csv_report/report.py") != original["csv_report/report.py"],
        )
        self.check(
            "수정: diff 도구 호출",
            any(call["name"] == "show_diff" for _, call in debug["calls"]),
        )
        self.check(
            "수정: 보고서 파일 저장",
            (debug["dir"] / "artifacts" / "fix-report.md").is_file(),
        )
        result = self.command("debug-tests", workspaces["debug"], TEST_COMMAND)
        self.check(
            "수정: 별도 프로세스에서 전체 테스트 통과",
            result.returncode == 0 and "skipped=" not in result.stderr,
        )
        result = self.command(
            "debug-boundaries",
            workspaces["debug"],
            [sys.executable, "-"],
            stdin=BOUNDARY_CHECK,
        )
        self.check("수정: 새 날짜·금액·고객으로 독립 검사 통과", result.returncode == 0)

        feature_workspace = self.work / "feature"
        copy_project(workspaces["debug"], feature_workspace)
        feature = self.case(
            "04_feature",
            feature_workspace,
            'CLI에 선택 옵션 --customer를 추가해 줘. 지정하면 이름이 정확히 일치하는 고객만 집계하고, 미지정하면 기존 전체 결과를 유지해. 없는 고객은 customers={}, total="0.00"이야. 기간 양끝 포함과 paid 조건·환불 합산을 유지해. 새 테스트에서 실패를 먼저 확인하고 구현한 뒤 전체 테스트와 CLI를 실제 실행해 줘. 기존 테스트는 보존하고 README와 기능 보고서도 작성해 줘.',
        )
        self.check(
            "기능: Skill 본문 읽기 성공",
            self.read(feature, "/skills/add-python-feature/SKILL.md"),
        )
        self.check("기능: 새 테스트 실패 후 통과", self.failed_then_passed(feature))
        self.check(
            "기능: 소스 수정 전에 새 테스트 실패 확인",
            self.failure_before_source_edit(feature),
        )
        feature_files = project_files(feature_workspace)
        self.check(
            "기능: 이전 단계의 모든 테스트 원문 보존",
            all(
                feature_files.get(name) == data
                for name, data in after_debug.items()
                if name.startswith("tests/")
            ),
        )
        self.check(
            "기능: 새 테스트 추가",
            any(
                name.startswith("tests/test_") and name not in after_debug
                for name in feature_files
            ),
        )
        self.check(
            "기능: README에 옵션 설명",
            b"--customer" in feature_files.get("README.md", b""),
        )
        self.check(
            "기능: 보고서 저장",
            (feature["dir"] / "artifacts" / "feature-report.md").is_file(),
        )
        result = self.command("feature-tests", feature_workspace, TEST_COMMAND)
        self.check(
            "기능: 별도 프로세스 전체 테스트 통과",
            result.returncode == 0 and "skipped=" not in result.stderr,
        )
        self.feature_cli(feature_workspace)
        self.check("실습 원본 파일 보존", project_files(baseline) == original)
        self.check(
            "각 시나리오는 별도 에이전트 프로세스",
            len({c["agent_pid"] for c in self.cases}) == 4,
        )

    def feature_cli(self, workspace: Path) -> None:
        csv_path = self.root / "independent-sales.csv"
        csv_path.write_text(
            "date,customer,amount,status\n2028-03-04,Orion,17.93,paid\n2028-03-04,Orion,-2.08,paid\n2028-03-04,Vega,6.14,paid\n2028-03-04,Orion,80,cancelled\n2028-03-03,Orion,90,paid\n2028-03-05,Orion,70,paid\n",
            encoding="utf-8",
        )
        base = [
            sys.executable,
            "-m",
            "csv_report",
            "--input",
            str(csv_path),
            "--start",
            "2028-03-04",
            "--end",
            "2028-03-04",
        ]
        cases = [
            (
                "default",
                [],
                {"customers": {"Orion": "15.85", "Vega": "6.14"}, "total": "21.99"},
            ),
            (
                "selected",
                ["--customer", "Orion"],
                {"customers": {"Orion": "15.85"}, "total": "15.85"},
            ),
            ("unknown", ["--customer", "Unknown"], {"customers": {}, "total": "0.00"}),
            ("exact-name", ["--customer", "Ori"], {"customers": {}, "total": "0.00"}),
        ]
        for label, option, expected in cases:
            result = self.command(f"feature-cli-{label}", workspace, base + option)
            try:
                actual = json.loads(result.stdout)
            except json.JSONDecodeError:
                actual = None
            self.check(
                f"기능 CLI: {label}", result.returncode == 0 and actual == expected
            )

    def report(self) -> bool:
        passed = bool(self.checks) and all(check["passed"] for check in self.checks)
        save_json(
            self.root / "report.json",
            {"passed": passed, "checks": self.checks, "cases": self.cases},
        )
        print(f"[보고서] {self.root / 'report.json'}", flush=True)
        return passed


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    verification = Verification()
    print(
        "[비용] 실제 모델 약 25–45회 호출, 약 2–5분 예상. 네 시나리오를 실행합니다.",
        flush=True,
    )
    print(f"[근거 폴더] {verification.root}", flush=True)
    try:
        verification.verify()
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - 실패를 숨기지 않고 보고서와 종료 코드로 남긴다.
        verification.check(f"검사 중 예외: {type(error).__name__}", False)
    return 0 if verification.report() else 1


if __name__ == "__main__":
    raise SystemExit(main())
