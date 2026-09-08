"""A fixed table operation set. Model-generated Python is never evaluated."""
from __future__ import annotations

import re
from typing import Literal

import pandas as pd
from langchain_core.tools import tool


def markdown_tables(text: str):
    blocks, current = [], []
    for line in [*text.splitlines(), ""]:
        if line.strip().startswith("|") and line.strip().endswith("|"):
            current.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        elif current:
            if len(current) >= 3 and all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in current[1]):
                if len(set(map(len, current))) != 1:
                    raise ValueError("표의 행별 열 수가 다릅니다. 원본을 확인하세요.")
                blocks.append(pd.DataFrame(current[2:], columns=current[0]))
            current = []
    return blocks


def quality_report(frame: pd.DataFrame):
    empty = frame.replace(r"^\s*$", pd.NA, regex=True)
    return {"rows": len(frame), "columns": list(frame.columns),
            "missing_cells": int(empty.isna().sum().sum()),
            "duplicate_rows": int(frame.duplicated().sum()),
            "duplicate_headers": bool(frame.columns.duplicated().any())}


def aggregate(frame: pd.DataFrame, column: str, operation: str):
    if operation not in {"count", "sum", "mean", "min", "max"}:
        raise ValueError("허용되지 않은 표 연산입니다.")
    if column not in frame.columns or frame.columns.duplicated().any():
        raise ValueError("열 이름이 없거나 중복되었습니다.")
    report = quality_report(frame)
    if not report["rows"] or report["missing_cells"] or report["duplicate_rows"]:
        raise ValueError("결측/중복이 있는 표는 집계를 보류합니다.")
    values = frame[column].astype(str).str.replace(",", "", regex=False).str.strip()
    if operation == "count":
        return {"value": len(values), "unit": "rows"}
    units = {"%" if v.endswith("%") else "number" for v in values}
    if len(units) != 1:
        raise ValueError("한 열에 단위가 혼재되어 있습니다.")
    numeric = pd.to_numeric(values.str.removesuffix("%"), errors="raise")
    result = float(getattr(numeric, operation)())
    return {"value": result, "unit": units.pop()}


def make_table_tool(context):
    @tool
    def table_query(evidence_id: str, column: str,
                    operation: Literal["count", "sum", "mean", "min", "max"], table_index: int = 0) -> dict:
        """현재 턴 원문의 Markdown 표에 고정 집계 연산만 수행한다. 열·단위·결측/중복을 확인하며 Python 코드는 받지 않는다."""
        if evidence_id not in context.evidence:
            raise ValueError("현재 턴에서 읽은 근거 ID가 필요합니다.")
        tables = markdown_tables(context.evidence[evidence_id].quote)
        if not 0 <= table_index < len(tables):
            raise ValueError("요청한 표가 없습니다.")
        frame = tables[table_index]
        result = {"evidence_id": evidence_id, "operation": operation, "column": column,
                  "quality": quality_report(frame), **aggregate(frame, column, operation)}
        context.budget.consume("table_calculation", result=result)
        return result
    return table_query
