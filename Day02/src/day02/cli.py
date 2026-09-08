"""One CLI for local service control, preprocessing, ingestion, retrieval and RAG."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from day02.errors import Day02Error
from day02.settings import Settings, write_json


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def doctor(settings):
    from day02.evidence import Metadata
    from day02.ingestion.pipeline import ingest
    from day02.openviking.client import VikingClient
    directory = settings.root / "outputs" / "doctor"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "probe.md"
    path.write_text("# Day02 연결 확인\n검증 표식은 DAY02-NATIVE-RAG-CHECK입니다.\n", encoding="utf-8")
    catalog = directory / "catalog.json"
    write_json(catalog, [{"path": str(path.relative_to(settings.root)), "metadata": Metadata(
        doc_id="doctor-probe", title="Day02 연결 확인", source="generated:doctor",
    ).model_dump(mode="json")}])
    with VikingClient.configured(settings) as client:
        client.ls("viking://resources")
        manifest = ingest(settings, client, catalog, namespace="doctor")
        hits = client.find("DAY02-NATIVE-RAG-CHECK 검증 표식", manifest["target_uri"], 3)
        leaves = [h["uri"] for h in hits if h.get("uri") in manifest["documents"]]
        if not leaves or "DAY02-NATIVE-RAG-CHECK" not in client.read(leaves[0]):
            raise Day02Error("문서 적재 후 검색/원문 검증 실패")
        return {"status": "passed", "url": client.url,
                "checks": ["authenticated-document-access", "upload", "indexed-search", "original-read"]}


def parser():
    root = argparse.ArgumentParser(prog="day02", description="DeepAgents · OpenViking · Skills RAG")
    sub = root.add_subparsers(dest="command", required=True)
    server = sub.add_parser("server", help="Docker 없는 개인 서버 제어")
    server.add_argument("action", choices=["init", "start", "status", "logs", "stop", "restart"])
    server.add_argument("--lines", type=int, default=40)
    sub.add_parser("doctor", help="실제 적재·검색·원문 조회 검증")
    prepare = sub.add_parser("prepare", help="문서를 Markdown 및 출처 catalog로 변환")
    prepare.add_argument("path", type=Path)
    prepare.add_argument("--entity", default="공통")
    prepare.add_argument("--pages", help="PDF/PPTX의 1-based 페이지/슬라이드 목록, 예: 1,2")
    ingest = sub.add_parser("ingest", help="catalog 문서를 OpenViking에 적재")
    ingest.add_argument("--catalog", type=Path, default=Path("data/business/catalog.json"))
    ingest.add_argument("--namespace", default="business")
    for command in ["search", "ask"]:
        task = sub.add_parser(command)
        task.add_argument("question")
        task.add_argument("--entity", default="알파")
        task.add_argument("--as-of", type=date.fromisoformat, default=date.today())
        task.add_argument("--namespace", default="business")
        if command == "ask":
            task.add_argument("--format", default="auto", choices=[
                "auto", "grounded-qa", "comparison", "procedure", "table-analysis", "insufficient-evidence",
            ])
            task.add_argument("--session")
            task.add_argument("--json", action="store_true")
            task.add_argument("--timeout", type=float, default=180)
    skills = sub.add_parser("skills", help="답변 Skills 확인")
    skills.add_argument("--validate", action="store_true")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    settings = Settings.load()
    try:
        if args.command == "server":
            from day02.openviking import server
            if args.action == "logs":
                print(server.logs(settings, args.lines))
                return 0
            if args.action == "restart":
                server.stop(settings)
                value = server.start(settings)
            else:
                function = server.initialize if args.action == "init" else getattr(server, args.action)
                value = function(settings)
            print_json(value)
        elif args.command == "doctor":
            print_json(doctor(settings))
        elif args.command == "prepare":
            from day02.ingestion.pipeline import prepare
            pages = [int(p) - 1 for p in args.pages.split(",")] if args.pages else None
            if pages is not None and any(p < 0 for p in pages):
                raise ValueError("페이지는 1부터 시작합니다.")
            path = args.path if args.path.is_absolute() else settings.root / args.path
            print_json({"catalog": str(prepare(settings, path, entity=args.entity, pages=pages))})
        elif args.command == "ingest":
            from day02.ingestion.pipeline import ingest
            from day02.openviking.client import VikingClient
            path = args.catalog if args.catalog.is_absolute() else settings.root / args.catalog
            with VikingClient.configured(settings) as client:
                result = ingest(settings, client, path, namespace=args.namespace)
            print_json({"target_uri": result["target_uri"], "documents": len(result["documents"]),
                        "reused": result["reused"]})
        elif args.command == "search":
            from day02.ingestion.pipeline import load_manifest
            from day02.openviking.client import VikingClient
            from day02.tools.search import RetrievalContext
            with VikingClient.configured(settings) as client:
                context = RetrievalContext(client, load_manifest(settings, client, args.namespace),
                                           entity=args.entity, as_of=args.as_of)
                print_json(context.search(args.question))
        elif args.command == "ask":
            from day02.agents.rag import ask
            from day02.evidence import Budget
            if args.timeout <= 0:
                raise ValueError("timeout은 양수여야 합니다.")
            result = ask(settings, args.question, entity=args.entity, as_of=args.as_of,
                         namespace=args.namespace, answer_format=args.format, session=args.session,
                         budget=Budget(max_seconds=args.timeout))
            print_json(result) if args.json else print(result["markdown"])
        elif args.command == "skills":
            import yaml
            from day02.validation import skill_spec
            records = []
            for path in sorted((settings.root / "skills").glob("*/SKILL.md")):
                text = path.read_text(encoding="utf-8")
                frontmatter = yaml.safe_load(text.split("---", 2)[1])
                if frontmatter.get("name") != path.parent.name or not frontmatter.get("description"):
                    raise ValueError(f"Skill frontmatter 오류: {path.name}")
                spec = skill_spec(settings.root, path.parent.name)
                if not spec["sections"]:
                    raise ValueError("Skill output.json에 sections가 필요합니다.")
                records.append({"name": frontmatter["name"], "description": frontmatter["description"],
                                "sections": [s["heading"] for s in spec["sections"]]})
            if len(records) != 5:
                raise ValueError("5개 답변 Skill이 필요합니다.")
            print_json({"status": "passed", "skills": records})
        return 0
    except (Day02Error, ValueError, FileNotFoundError) as exc:
        print_json({"status": "error", "type": type(exc).__name__, "message": settings.redact(str(exc))})
        return 1
    except Exception as exc:
        print_json({"status": "error", "type": type(exc).__name__, "message": settings.redact(str(exc))})
        return 1


if __name__ == "__main__":
    sys.exit(main())
