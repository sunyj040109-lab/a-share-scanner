import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scanner import (board_for, fetch_minutes, fetch_universe, in_scan_window, main,
                     normalize, normalize_minute_bars, normalize_tx, number,
                     parse_tencent_index_quote, trading_day, write_outputs)
from strategy import add_minute_evidence, evaluate_stock
from prepare import fetch_bars


class FakeCalendar:
    @property
    def sessions(self):
        return [datetime(2026, 10, 16), datetime(2026, 10, 19)]


class FakeFrame:
    columns = ["code", "name", "zxj", "zdf", "hsl", "turnover", "volume"]
    empty = False

    def to_dict(self, kind):
        return [{"code": "sh600000", "name": "甲", "zxj": 10, "zdf": 4, "hsl": 3, "turnover": 20000, "volume": 100}]


class FakeAK:
    def stock_zh_a_spot_tx(self):
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

    def test_missing_data_stays_in_universe_and_is_unknown(self):
        rows = normalize("SZ", [
            {"代码": "300001", "名称": "甲", "最新价": 10, "涨跌幅": 4, "换手率": 3, "成交额": 200000000, "成交量": 100},
            {"代码": "300002", "名称": "乙", "最新价": "-", "涨跌幅": 4, "换手率": 3, "成交额": 200000000, "成交量": 100},
            {"代码": "159001", "名称": "ETF", "最新价": 10, "涨跌幅": 4, "换手率": 3, "成交额": 200000000, "成交量": 100},
        ])
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[1]["price"])
        config = {"history_days": 120, "limit_up_tolerance_pct": 0.2,
                  "red_volume_ratio": 1.5, "breakout_volume_ratio": 1.5}
        self.assertEqual(evaluate_stock(rows[1], None, config)["status"], "unknown")
        self.assertIsNone(number(float("nan")))

    def test_tencent_units_and_board_filter(self):
        rows = normalize_tx([
            {"code": "sh600000", "name": "甲", "zxj": "10", "zdf": "4", "hsl": "3", "turnover": "20000", "volume": "100"},
            {"code": "sh688001", "name": "科创板", "zxj": "10"},
            {"code": "bj920001", "name": "乙", "zxj": "8", "turnover": "-"},
        ])
        self.assertEqual([row["code"] for row in rows], ["600000", "920001"])
        self.assertEqual(rows[0]["amount_cny"], 200000000)
        self.assertIsNone(rows[1]["amount_cny"])

    def test_index_quote_requires_current_trading_date(self):
        fields = [""] * 31
        fields[3], fields[6], fields[30] = "3658.03", "437691963", "20261022142500"
        payload = ('v_sh000001="' + "~".join(fields) + '";').encode("gbk")
        quote = parse_tencent_index_quote(payload, date(2026, 10, 22))
        self.assertEqual(quote["volume"], 437691963)
        with self.assertRaisesRegex(ValueError, "日期"):
            parse_tencent_index_quote(payload, date(2026, 10, 23))

    def test_minute_fallback_rejects_stale_and_future_bars(self):
        day = date(2026, 10, 22)
        times = [f"{day} 09:{minute:02d}:00" for minute in range(35, 60, 5)]
        times += [f"{day} 10:{minute:02d}:00" for minute in range(0, 20, 5)]
        times += [f"{day} 14:{minute:02d}:00" for minute in (15, 20, 25, 30)]
        rows = [{"day": stamp, "open": "10", "close": "10.1", "high": "10.2",
                 "low": "9.9", "volume": "100"} for stamp in times]
        frame = pd.DataFrame(rows)
        bars = normalize_minute_bars(frame, "Sina", day)
        self.assertEqual(bars[-1]["time"], "2026-10-22 14:25:00")
        self.assertEqual(len(bars), len(rows) - 1)
        self.assertIsNone(normalize_minute_bars(frame.iloc[:8], "Sina", day))
        class AlternateAK:
            def stock_zh_a_minute(self, **kwargs):
                raise ConnectionError("Sina unavailable")
            def stock_zh_a_hist_min_em(self, **kwargs):
                return frame.rename(columns={"day": "时间", "open": "开盘", "close": "收盘",
                                             "high": "最高", "low": "最低", "volume": "成交量"})
        fetched = fetch_minutes(AlternateAK(), [{"code": "605488", "board": "SH"}], day, 1)
        self.assertEqual(fetched["605488"][0]["source"], "Eastmoney")

    def test_history_source_fallback_preserves_share_units(self):
        dates = pd.bdate_range("2026-04-01", periods=121)
        records = [{"date": stamp.date(), "open": 10, "close": 10, "high": 10,
                    "low": 10, "volume": 1000, "amount": 10000} for stamp in dates]
        class Source:
            def stock_zh_a_hist_tx(self, **kwargs):
                return pd.DataFrame(records)
            def stock_zh_a_hist(self, **kwargs):
                raise ConnectionError("Eastmoney unavailable")
            def stock_zh_a_daily(self, **kwargs):
                return pd.DataFrame(records)
        class SourceFallback(Source):
            def stock_zh_a_daily(self, **kwargs):
                raise ConnectionError("Sina unavailable")
            def stock_zh_a_hist(self, **kwargs):
                return pd.DataFrame(records).rename(columns={"date": "日期", "open": "开盘",
                    "close": "收盘", "high": "最高", "low": "最低", "volume": "成交量", "amount": "成交额"})
        with patch("prepare.wait_bse_slot"):
            _, sh, sh_error = fetch_bars(Source(), {"code": "600000", "board": "SH"}, date(2026, 10, 22), 120)
            _, bj, bj_error = fetch_bars(Source(), {"code": "920001", "board": "BJ"}, date(2026, 10, 22), 120)
            _, bj_fallback, fallback_error = fetch_bars(SourceFallback(), {"code": "920002", "board": "BJ"}, date(2026, 10, 22), 120)
        self.assertIsNone(sh_error)
        self.assertIsNone(bj_error)
        self.assertIsNone(fallback_error)
        self.assertEqual(len(sh), 120)
        self.assertEqual(len(bj), 120)
        self.assertEqual(sh[-1]["volume"], 1000)
        self.assertEqual(bj[-1]["volume"], 1000)
        self.assertEqual(bj_fallback[-1]["volume"], 100000)

    def test_calendar_and_reports(self):
        self.assertTrue(trading_day(date(2026, 10, 16), FakeCalendar()))
        self.assertFalse(trading_day(date(2026, 10, 17), FakeCalendar()))
        row = normalize("SH", [{"代码": "600000", "名称": "浦发银行", "最新价": 10, "涨跌幅": 4, "换手率": 3, "成交额": 200000000, "成交量": 100}])[0]
        config = {"max_report_rows": 100}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = write_outputs(root, date(2026, 10, 16), datetime(2026, 10, 16, 14, 32, tzinfo=ZoneInfo("Asia/Shanghai")), [row], [row], {"SH": 1}, config)
            self.assertEqual(summary["universe_count"], 1)
            self.assertEqual(summary["scan_type"], "intraday_strategy")
            self.assertEqual(json.loads((root / "docs/data/universe.json").read_text(encoding="utf-8"))[0]["code"], "600000")
            self.assertIn("viewport", (root / "docs/index.html").read_text(encoding="utf-8"))

    def test_truncated_market_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "低于防截断阈值"):
            fetch_universe(FakeAK())

    def test_intraday_window(self):
        tz = ZoneInfo("Asia/Shanghai")
        self.assertFalse(in_scan_window(datetime(2026, 10, 16, 14, 24, tzinfo=tz)))
        self.assertTrue(in_scan_window(datetime(2026, 10, 16, 14, 25, tzinfo=tz)))
        self.assertTrue(in_scan_window(datetime(2026, 10, 16, 14, 34, tzinfo=tz)))
        self.assertFalse(in_scan_window(datetime(2026, 10, 16, 14, 36, tzinfo=tz)))

    def test_strategy_requires_history_and_keeps_unknown_separate(self):
        cfg = {"history_days": 120, "limit_up_tolerance_pct": 0.2,
               "red_volume_ratio": 1.5, "breakout_volume_ratio": 1.5}
        bars = []
        for index in range(120):
            close = round(10 + 0.03 * index, 2)
            if index == 60:
                close = 12.95
            bars.append({"date": f"2026-{1 + index // 30:02d}-{1 + index % 30:02d}",
                         "open": 12 if index == 40 else close - 0.02,
                         "close": close, "high": max(close, 12.1 if index == 40 else (close if index == 60 else close + 0.03)),
                         "low": close - 0.1, "volume": 5000 if index == 40 else 1000})
        quote = {"code": "600000", "name": "样本", "board": "SH", "price": 14,
                 "volume_lots": 20}
        result = evaluate_stock(quote, bars, cfg)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["checks"]["bearish_max_volume_anchor"]["status"], "pass")
        self.assertEqual(result["checks"]["operator_intent"]["status"], "unknown")
        self.assertEqual(evaluate_stock(quote, bars[:-1], cfg)["status"], "unknown")
        self.assertEqual(evaluate_stock({**quote, "name": "*ST 样本"}, bars, cfg)["status"], "excluded")

    def test_strategy_report_carries_evidence_and_coverage(self):
        row = normalize("SH", [{"代码": "600000", "名称": "样本", "最新价": 14,
            "涨跌幅": 2, "换手率": 3, "成交额": 200000000, "成交量": 20}])[0]
        analysis = {"600000": {"status": "pass", "checks": {
            "past_limit_up": {"status": "pass", "evidence": [{"date": "2026-10-01"}]},
            "red_volume_proxy": {"status": "pass", "evidence": {"volume_ratio_vs_20d": 2}},
            "bearish_max_volume_anchor": {"status": "pass", "evidence": {"date": "2026-09-01", "high": 12}},
            "non_morning_volume_stack": {"status": "unknown"},
        }}}
        cfg = {"max_report_rows": 10, "history_days": 120}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = write_outputs(root, date(2026, 10, 22), datetime(2026, 10, 22, 14, 26,
                tzinfo=ZoneInfo("Asia/Shanghai")), [row], [row], {"SH": 1}, cfg,
                analysis, {"pass": 1, "history_available": 1}, [],
                {"up_count": 1, "down_count": 0, "amount_cny": 200000000})
            self.assertEqual(summary["coverage"]["history_available"], 1)
            self.assertIn("2026-09-01", (root / "REPORT.md").read_text(encoding="utf-8"))
            self.assertIn("class='card'", (root / "docs/index.html").read_text(encoding="utf-8"))
            self.assertEqual(json.loads((root / "docs/data/analysis.json").read_text(encoding="utf-8"))["600000"]["status"], "pass")
            self.assertIn("600000", (root / "docs/data/status.csv").read_text(encoding="utf-8-sig"))

    def test_scheduled_run_reports_missed_window(self):
        with patch("scanner.in_scan_window", return_value=False), patch("scanner.trading_day", return_value=True):
            self.assertEqual(main(["--require-window"]), 1)
        with patch("scanner.in_scan_window", return_value=False), patch("scanner.trading_day", return_value=False):
            self.assertEqual(main(["--require-window"]), 0)


if __name__ == "__main__":
    unittest.main()


