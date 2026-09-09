"""원문을 읽고 기간별 고객 매출을 집계한다."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Sale:
    day: date
    customer: str
    amount: Decimal
    status: str


def read_sales(path: Path) -> list[Sale]:
    with path.open(encoding="utf-8", newline="") as source:
        return [
            Sale(
                date.fromisoformat(row["date"]),
                row["customer"],
                Decimal(row["amount"]),
                row["status"],
            )
            for row in csv.DictReader(source)
        ]


def summarize(sales: list[Sale], start: date, end: date) -> dict:
    """시작일과 종료일을 포함해 paid 거래의 고객별 합계를 반환한다."""
    if start > end:
        raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
    amounts = defaultdict(Decimal)
    for sale in sales:
        if sale.status == "paid" and start <= sale.day < end:
            amounts[sale.customer] += sale.amount
    return {
        "customers": {name: f"{amounts[name]:.2f}" for name in sorted(amounts)},
        "total": f"{sum(amounts.values(), Decimal(0)):.2f}",
    }
