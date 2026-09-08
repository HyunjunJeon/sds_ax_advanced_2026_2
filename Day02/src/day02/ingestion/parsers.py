"""PDF/PPTX/HWPX/Markdown parsers. Original coordinates and extraction method are explicit."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from day02.errors import ConfigurationError, Day02Error


@dataclass
class ParsedUnit:
    text: str
    method: str
    page: int | None = None
    slide: int | None = None


def file_hash(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_hwpx(path: Path):
    """Native HWPX XML text/table path. Sections are not mislabeled as physical pages."""
    units = []
    def local(tag):
        return tag.rsplit("}", 1)[-1]
    def text(node):
        return "".join(child.text or "" for child in node.iter() if local(child.tag) == "t")
    def paragraph(node):
        table = next((x for x in node.iter() if local(x.tag) == "tbl"), None)
        if table is None:
            return text(node)
        rows = []
        for row in table:
            if local(row.tag) == "tr":
                rows.append([text(cell).replace("|", "\\|") for cell in row if local(cell.tag) == "tc"])
        if not rows:
            return text(node)
        width = max(map(len, rows))
        rows = [r + [""] * (width - len(r)) for r in rows]
        result = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        result += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(result)
    with zipfile.ZipFile(path) as archive:
        sections = sorted(n for n in archive.namelist() if n.startswith("Contents/section") and n.endswith(".xml"))
        for name in sections:
            data = archive.read(name)
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                raise Day02Error("외부 엔티티가 포함된 HWPX는 지원하지 않습니다.")
            root = ET.fromstring(data)
            paragraphs = [paragraph(child) for child in root if local(child.tag) == "p"]
            content = "\n\n".join(p for p in paragraphs if p.strip())
            if content:
                units.append(ParsedUnit(content, "hwpx-xml-text-table"))
    if not units:
        raise Day02Error("HWPX 본문을 추출하지 못했습니다.")
    return units


def parse_file(path: Path, *, pages: list[int] | None = None, output_dir: Path | None = None):
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return [ParsedUnit(path.read_text(encoding="utf-8"), "authored")]
    if suffix == ".pdf":
        import pymupdf
        import pymupdf4llm
        pymupdf4llm.use_layout(False)
        with pymupdf.open(path) as pdf:
            selected = pages if pages is not None else list(range(len(pdf)))
        result = pymupdf4llm.to_markdown(str(path), pages=selected, page_chunks=True,
                                       write_images=False, force_text=True, show_progress=False)
        return [ParsedUnit(item["text"], "pymupdf4llm", page=index + 1)
                for index, item in zip(selected, result, strict=True) if item["text"].strip()]
    if suffix == ".pptx":
        from markitdown import MarkItDown
        import re
        content = MarkItDown().convert(str(path)).text_content
        pieces = re.split(r"<!--\s*Slide number:\s*(\d+)\s*-->", content)
        if len(pieces) < 3:
            raise Day02Error("MarkItDown의 슬라이드 좌표를 찾지 못했습니다.")
        selected = None if pages is None else {p + 1 for p in pages}
        return [ParsedUnit(pieces[i + 1].strip(), "markitdown-pptx", slide=int(pieces[i]))
                for i in range(1, len(pieces), 2)
                if pieces[i + 1].strip() and (selected is None or int(pieces[i]) in selected)]
    if suffix == ".hwpx":
        return parse_hwpx(path)
    if suffix == ".hwp":
        from day02.settings import ROOT
        local_binary = ROOT / "bin" / ("rhwp.exe" if os.name == "nt" else "rhwp")
        binary = os.getenv("RHWP_BIN") or (str(local_binary) if local_binary.is_file() else shutil.which("rhwp"))
        if not binary or not Path(binary).is_file():
            raise ConfigurationError("HWP 바이너리는 rhwp CLI가 필요합니다. RHWP_BIN을 설정하세요. HWPX는 바로 실행할 수 있습니다.")
        if output_dir is None:
            raise ConfigurationError("HWP 변환 산출물 경로가 필요합니다.")
        output_dir.mkdir(parents=True, exist_ok=True)
        converted = subprocess.run([binary, "export-markdown", str(path), "-o", str(output_dir)],
                                   capture_output=True, text=True, timeout=120)
        if converted.returncode:
            raise Day02Error("rhwp export-markdown 변환 실패")
        units = [ParsedUnit(p.read_text(encoding="utf-8"), "rhwp-cli") for p in sorted(output_dir.glob("*.md"))]
        if not units:
            raise Day02Error("rhwp Markdown 산출물이 없습니다.")
        return units
    raise ConfigurationError(f"지원하지 않는 입력 형식: {suffix}")
