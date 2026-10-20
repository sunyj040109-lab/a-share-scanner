import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scanner import board_for, fetch_universe, in_scan_window, main, normalize, number, select, trading_day, write_outputs


class FakeCalendar:
    @property
    def sessions(self):
        return [datetime(2026, 10, 16), datetime(2026, 10, 19)]


class FakeFrame:
    columns = ["代码", "名称", "最新价", "涨跌幅", "换手率", "成交额", "成交量"]
    empty = False

    def to_dict(self, kind):
        return [{"代码": "600000", "名称": "甲", "最新价": 10, "涨跌幅": 4, "换手率": 3, "成交额": 2e8, "成交量": 100}]


class FakeAK:
    def stock_sh_a_spot_em(self):
        return FakeFrame()


class ScannerTests(unittest.TestCase):
    def test_security_classes(self):
        self.assertEqual(board_for("SH", "600000"), "SH")
        self.assertIsNone(board_for("SH", "688001"))
        self.assertEqual(board_for("SZ", "301001"), "GEM")
        self.assertEqual(board_for("SZ", "004001"), "SZ_MAIN")
        self.assertIsNone(board_for("SZ", "001050"))
        self.assertIsNone(board_for("SZ", "309900"))
        self.assertIsNone(board_for("SZ", "159001"))
        self.assertEqual(board_for("BJ", "920001"), "BJ")
        self.assertEqual(board_for("BJ", "836149"), "BJ")
        self.assertIsNone(board_for("BJ", "600000"))

    def test_missing_data_stays_in_universe_but_not_selection(self):
        rows = normalize("SZ", [
            {"代码": "300001", "名称": "甲", "最新价": 10, "涨跌幅": 4, "换手率": 3, "成交额": 200000000, "成交量": 100},
            {"代码": "300002", "名称": "乙", "最新价": "-", "涨跌幅": 4, "换手率": 3, "成交额": 200000000, "成交量": 100},
            {"代码": "159001", "名称": "ETF", "最新价": 10, "涨跌幅": 4, "换手率": 3, "成交额": 200000000, "成交量": 100},
        ])
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[1]["price"])
        config = {"min_change_pct": 3, "min_turnover_pct": 2, "min_amount_cny": 100000000, "max_report_rows": 100}
        self.assertEqual([r["code"] for r in select(rows, config)], ["300001"])
        self.assertIsNone(number(float("nan")))

    def test_calendar_and_reports(self):
        self.assertTrue(trading_day(date(2026, 10, 16), FakeCalendar()))
        self.assertFalse(trading_day(date(2026, 10, 17), FakeCalendar()))
        row = normalize("SH", [{"代码": "600000", "名称": "浦发银行", "最新价": 10, "涨跌幅": 4, "换手率": 3, "成交额": 200000000, "成交量": 100}])[0]
        config = {"min_change_pct": 3, "min_turnover_pct": 2, "min_amount_cny": 100000000, "max_report_rows": 100}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = write_outputs(root, date(2026, 10, 16), datetime(2026, 10, 16, 14, 32, tzinfo=ZoneInfo("Asia/Shanghai")), [row], [row], {"SH": 1}, config)
            self.assertEqual(summary["universe_count"], 1)
            self.assertEqual(summary["scan_type"], "intraday_1430")
            self.assertEqual(json.loads((root / "docs/data/universe.json").read_text(encoding="utf-8"))[0]["code"], "600000")
            self.assertIn("viewport", (root / "docs/index.html").read_text(encoding="utf-8"))

    def test_truncated_market_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "低于防截断阈值"):
            fetch_universe(FakeAK())

    def test_intraday_window(self):
        tz = ZoneInfo("Asia/Shanghai")
        self.assertFalse(in_scan_window(datetime(2026, 10, 16, 14, 29, tzinfo=tz)))
        self.assertTrue(in_scan_window(datetime(2026, 10, 16, 14, 30, tzinfo=tz)))
        self.assertTrue(in_scan_window(datetime(2026, 10, 16, 14, 44, tzinfo=tz)))
        self.assertFalse(in_scan_window(datetime(2026, 10, 16, 14, 46, tzinfo=tz)))

    def test_scheduled_run_reports_missed_window(self):
        with patch("scanner.in_scan_window", return_value=False), patch("scanner.trading_day", return_value=True):
            self.assertEqual(main(["--require-window"]), 1)
        with patch("scanner.in_scan_window", return_value=False), patch("scanner.trading_day", return_value=False):
            self.assertEqual(main(["--require-window"]), 0)


if __name__ == "__main__":
    unittest.main()

