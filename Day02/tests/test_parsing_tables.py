import pandas as pd
import pytest

from day02.ingestion.chunking import bounded_chunks, fixed_chunks, section_chunks
from day02.ingestion.parsers import parse_file
from day02.settings import ROOT
from day02.tools.tables import aggregate, markdown_tables


@pytest.mark.parametrize("chunker", [fixed_chunks, section_chunks, bounded_chunks])
def test_chunks_preserve_original_offsets(chunker):
    text = "# 제목\n첫 문단.\n\n## 표\n|항목|값|\n|---|---|\n|A|10|\n예외 사항\n"
    for chunk in chunker(text):
        assert text[chunk.start:chunk.end] == chunk.text


def test_pptx_physical_slide_mapping():
    units = parse_file(ROOT / "data/samples/day02.pptx", pages=[38])
    assert len(units) == 1 and units[0].slide == 39
    assert "E99" in units[0].text and "스키마" in units[0].text


def test_hwpx_native_text():
    units = parse_file(ROOT / "data/samples/alpha-sla.hwpx")
    assert "20일" in "\n".join(u.text for u in units)
    assert all(u.page is None for u in units)  # XML sections are not physical pages.


def test_pdf_page_coordinate():
    units = parse_file(ROOT / "data/samples/slides-excerpt.pdf", pages=[1])
    assert units[0].page == 2
    assert "Context" in units[0].text


def test_table_aggregate_and_units():
    frame = markdown_tables("|등급|비율|\n|---|---|\n|A|10%|\n|B|20%|\n")[0]
    assert aggregate(frame, "비율", "mean") == {"value": 15.0, "unit": "%"}


def test_table_rejects_code_and_mixed_units():
    frame = pd.DataFrame({"value": ["1", "2%"]})
    with pytest.raises(ValueError):
        aggregate(frame, "value", "__import__('os')")
    with pytest.raises(ValueError):
        aggregate(frame, "value", "sum")


def test_duplicate_table_hold():
    frame = pd.DataFrame({"value": ["1", "1"]})
    with pytest.raises(ValueError, match="집계를 보류"):
        aggregate(frame, "value", "sum")
