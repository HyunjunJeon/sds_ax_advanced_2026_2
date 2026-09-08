"""Preparation is local; ingestion uploads immutable text and verifies remote leaves."""
from __future__ import annotations

from pathlib import Path

from filelock import FileLock

from day02.errors import ConfigurationError, ServiceError
from day02.evidence import Metadata, digest
from day02.ingestion.chunking import bounded_chunks
from day02.ingestion.parsers import file_hash, parse_file
from day02.settings import Settings, read_json, write_json


def prepare(settings: Settings, path: Path, *, entity: str = "공통", pages: list[int] | None = None):
    path = path.resolve()
    source_hash = file_hash(path)
    sidecar = path.with_suffix(path.suffix + ".metadata.json")
    managed = {}
    if sidecar.exists():
        supplied = read_json(sidecar)
        if supplied.get("source_sha256") != source_hash:
            raise ConfigurationError("문서와 관리 메타데이터의 source_sha256이 다릅니다.")
        allowed = {"title", "domain", "entity", "version", "valid_from", "valid_until", "status", "topics"}
        managed = {key: value for key, value in supplied.items() if key in allowed}
    records = []
    try:
        source_name = path.relative_to(settings.root).as_posix()
    except ValueError:
        source_name = path.name
    directory = settings.root / "outputs" / "parsed" / f"{path.stem}-{source_hash[:12]}"
    directory.mkdir(parents=True, exist_ok=True)
    for unit_index, unit in enumerate(parse_file(path, pages=pages, output_dir=directory / "rhwp")):
        for index, chunk in enumerate(bounded_chunks(unit.text)):
            filename = f"unit-{unit_index+1:03d}-chunk-{index+1:03d}.md"
            target = directory / filename
            target.write_text(chunk.text, encoding="utf-8")
            metadata = Metadata.model_validate({
                "doc_id": f"{path.stem}-{unit_index+1}-{index+1}", "title": path.stem,
                "entity": entity, **managed,
                "source": source_name, "source_sha256": source_hash,
                "page": unit.page, "slide": unit.slide, "extraction_method": unit.method,
                "source_start_line": unit.text[:chunk.start].count("\n") + 1,
                "source_end_line": unit.text[:chunk.end].count("\n") + 1,
            })
            records.append({"path": str(target.relative_to(settings.root)),
                            "metadata": metadata.model_dump(mode="json")})
    if not records:
        raise ConfigurationError("추출된 텍스트가 없습니다. 스캔 문서는 시각 전사 경로로 확인하세요.")
    catalog = directory / "catalog.json"
    write_json(catalog, records)
    return catalog


def load_catalog(settings: Settings, catalog: Path):
    records = read_json(catalog)
    stems = set()
    for record in records:
        path = (settings.root / record["path"]).resolve()
        if not path.is_relative_to(settings.root) or not path.is_file():
            raise ConfigurationError("catalog 문서 경로는 Day02 내부의 실제 파일이어야 합니다.")
        Metadata.model_validate(record["metadata"])
        if path.stem in stems:
            raise ConfigurationError("한 catalog의 문서 파일명(stem)은 서로 달라야 합니다.")
        stems.add(path.stem)
    if not records:
        raise ConfigurationError("빈 catalog는 적재할 수 없습니다.")
    return records


def ingest(settings: Settings, client, catalog: Path, *, namespace: str = "business"):
    if not namespace or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in namespace):
        raise ConfigurationError("namespace에는 영문 소문자, 숫자, -와 _만 사용하세요.")
    records = load_catalog(settings, catalog)
    fingerprint = digest("\n".join(
        (settings.root / r["path"]).read_text(encoding="utf-8") + str(r["metadata"])
        for r in records
    ))[:16]
    target = f"viking://resources/day02-{namespace}-{fingerprint}"
    output = settings.root / "outputs" / "manifests" / f"{namespace}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(output) + ".lock", timeout=5):
        manifest = {}
        reused = 0
        for record in records:
            path = settings.root / record["path"]
            requested = f"{target}/{path.stem}"
            leaf = f"{requested}/{path.name}"
            raw = path.read_text(encoding="utf-8")
            try:
                remote = client.read(leaf, limit=max(1000, len(raw.splitlines()) + 10))
            except ServiceError as error:
                if error.status_code != 404:
                    raise
                remote = None
            if remote is not None and remote.strip() == raw.strip():
                reused += 1
            else:
                result = client.add_file(path, requested)
                entries = client.ls(result.get("root_uri", requested))
                leaves = [e["uri"] for e in entries if isinstance(e, dict)
                          and e.get("uri", "").endswith(".md")
                          and not e["uri"].rsplit("/", 1)[-1].startswith(".")]
                if len(leaves) != 1:
                    raise ServiceError(f"적재 후 원문 leaf가 하나가 아닙니다: {requested}")
                leaf = leaves[0]
                remote = client.read(leaf, limit=max(1000, len(raw.splitlines()) + 10))
                if remote.strip() != raw.strip():
                    raise ServiceError(f"적재 원문과 서버 원문이 다릅니다: {leaf}")
            snapshot = settings.root / "outputs" / "source-cache" / f"{digest(remote)}.md"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(remote, encoding="utf-8")
            manifest[leaf] = {"metadata": record["metadata"], "source_hash": digest(raw),
                              "remote_hash": digest(remote), "line_count": len(remote.splitlines()),
                              "snapshot": str(snapshot)}
            # Persist completed documents after every upload so a later run can inspect partial work.
            write_json(output, {"namespace": namespace, "server_url": client.url,
                                "target_uri": target, "complete": False, "documents": manifest})
        result = {"namespace": namespace, "server_url": client.url, "target_uri": target,
                  "complete": True, "documents": manifest, "reused": reused}
        write_json(output, result)
    return result


def load_manifest(settings: Settings, client, namespace: str = "business"):
    if not namespace or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in namespace):
        raise ConfigurationError("잘못된 namespace입니다.")
    path = settings.root / "outputs" / "manifests" / f"{namespace}.json"
    if not path.exists():
        raise ConfigurationError(f"{namespace} manifest가 없습니다. 먼저 day02 ingest를 실행하세요.")
    result = read_json(path)
    if not result.get("complete") or result["server_url"] != client.url:
        raise ConfigurationError("manifest의 적재 상태/서버 주소가 현재 연결과 다릅니다. 재적재하세요.")
    return result
