"""기존 계약 검사. 수정 대신 별도 파일에 반례를 추가한다."""

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from csv_report.report import Sale, read_sales, summarize


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.start = date(2026, 7, 1)
        self.end = date(2026, 7, 31)

    def test_reads_csv(self):
        path = Path(__file__).resolve().parents[1] / "data" / "sales.csv"
        self.assertEqual(len(read_sales(path)), 7)

    def test_includes_start_and_end(self):
        sales = [
            Sale(self.start, "가온", Decimal("10.25"), "paid"),
            Sale(self.end, "가온", Decimal("20.75"), "paid"),
        ]
        self.assertEqual(summarize(sales, self.start, self.end)["total"], "31.00")

    def test_ignores_cancelled_and_outside_dates(self):
        sales = [
            Sale(date(2026, 6, 30), "가온", Decimal(100), "paid"),
            Sale(date(2026, 7, 10), "가온", Decimal(200), "cancelled"),
            Sale(date(2026, 8, 1), "가온", Decimal(300), "paid"),
        ]
        self.assertEqual(
            summarize(sales, self.start, self.end), {"customers": {}, "total": "0.00"}
        )

    def test_decimal_and_refund(self):
        sales = [
            Sale(date(2026, 7, 10), "누리", Decimal("0.30"), "paid"),
            Sale(date(2026, 7, 12), "누리", Decimal("-0.10"), "paid"),
        ]
        self.assertEqual(summarize(sales, self.start, self.end)["total"], "0.20")

    def test_sorts_customers(self):
        sales = [
            Sale(date(2026, 7, 10), name, Decimal(1), "paid") for name in ("나", "가")
        ]
        self.assertEqual(
            list(summarize(sales, self.start, self.end)["customers"]), ["가", "나"]
        )

    def test_rejects_reversed_range(self):
        with self.assertRaises(ValueError):
            summarize([], self.end, self.start)


if __name__ == "__main__":
    unittest.main()
