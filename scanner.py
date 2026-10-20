"""沪深主板、创业板、北交所 14:30 盘中候选扫描。"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import sys
import time
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
WINDOW_START = clock_time(14, 30)
WINDOW_END = clock_time(14, 45)
WAIT_START = clock_time(13, 0)
BOARDS = {
    "SH": ("stock_sh_a_spot_em", 1000),
    "SZ": ("stock_sz_a_spot_em", 1500),
    "BJ": ("stock_bj_a_spot_em", 100),
}
FIELDS = ["code", "name", "board", "price", "change_pct", "turnover_pct", "amount_cny", "volume_lots"]
LABELS = {"SH": "沪市主板", "SZ_MAIN": "深市主板", "GEM": "创业板", "BJ": "北交所"}


def retry(label, fn, attempts=3, delay=2):
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts:
                raise RuntimeError(f"{label} 连续失败 {attempts} 次") from exc
            print(f"{label} 第 {attempt} 次失败：{exc}; {delay * 2 ** (attempt - 1)} 秒后重试", file=sys.stderr)
            time.sleep(delay * 2 ** (attempt - 1))


def in_scan_window(moment: datetime) -> bool:
    local_time = moment.astimezone(TZ).time()
    return WINDOW_START <= local_time <= WINDOW_END


def trading_day(day: date, calendar=None) -> bool:
    if calendar is None:
        import exchange_calendars as xcals
        # 2026 年中国交易日由已发布的 XSHG 日历覆盖；越界时由库抛错，避免误判。
        calendar = xcals.get_calendar("XSHG", start=(day - timedelta(days=31)).isoformat(), end=day.isoformat())
    return any(session.date() == day for session in calendar.sessions)


def board_for(source: str, code: str) -> str | None:
    if not re.fullmatch(r"\d{6}", code):
        return None
    if source == "SH" and code.startswith("60"):
        return "SH"
    if source == "SZ" and (code.startswith(("000", "002", "003", "004")) or 1200 <= int(code) <= 1999):
        return "SZ_MAIN"
    if source == "SZ" and 300000 <= int(code) <= 309799:
        return "GEM"
    if source == "BJ" and (code.startswith(("4", "8")) or code.startswith("920")):
        return "BJ"
    return None


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def normalize(source: str, records: list[dict]) -> list[dict]:
    output = []
    for raw in records:
        code = str(raw.get("代码", "")).strip()
        board = board_for(source, code)
        name = str(raw.get("名称", "")).strip()
        if not board or not name or name.lower() in ("nan", "none", "-"):
            continue
        output.append({
            "code": code,
            "name": name,
            "board": board,
            "price": number(raw.get("最新价")),
            "change_pct": number(raw.get("涨跌幅")),
            "turnover_pct": number(raw.get("换手率")),
            "amount_cny": number(raw.get("成交额")),
            "volume_lots": number(raw.get("成交量")),
        })
    return output


def fetch_universe(ak) -> tuple[list[dict], dict]:
    rows, counts = [], {}
    for source, (method_name, minimum) in BOARDS.items():
        frame = retry(source, getattr(ak, method_name))
        required = {"代码", "名称", "最新价", "涨跌幅", "换手率", "成交额", "成交量"}
        if frame is None or frame.empty or not required.issubset(frame.columns):
            raise RuntimeError(f"{source} 行情为空或缺少必要字段")
        current = normalize(source, frame.to_dict("records"))
        if len(current) < minimum:
            raise RuntimeError(f"{source} 仅有 {len(current)} 条合格股票，低于防截断阈值 {minimum}")
        counts[source] = len(current)
        rows.extend(current)
    codes = [row["code"] for row in rows]
    if len(codes) != len(set(codes)):
        raise RuntimeError("不同市场出现重复股票代码，请检查数据源")
    return sorted(rows, key=lambda row: row["code"]), counts


def select(rows: list[dict], config: dict) -> list[dict]:
    # 自定义筛选条件的扩展点：修改本函数，并为新增条件添加配置项。
    found = [row for row in rows if all((
        row["price"] is not None and row["price"] > 0,
        row["change_pct"] is not None and row["change_pct"] >= config["min_change_pct"],
        row["turnover_pct"] is not None and row["turnover_pct"] >= config["min_turnover_pct"],
        row["amount_cny"] is not None and row["amount_cny"] >= config["min_amount_cny"],
    ))]
    return sorted(found, key=lambda row: (-row["change_pct"], -row["amount_cny"], row["code"]))


def fmt(value, digits=2):
    return "—" if value is None else f"{value:,.{digits}f}"


def table(rows: list[dict], limit: int) -> tuple[str, str]:
    markdown = ["| 代码 | 名称 | 板块 | 现价 | 涨跌幅 | 换手率 | 成交额（亿元） |", "|---|---|---|---:|---:|---:|---:|"]
    web = []
    for row in rows[:limit]:
        cells = [row["code"], row["name"], LABELS[row["board"]], fmt(row["price"]), fmt(row["change_pct"]) + "%", fmt(row["turnover_pct"]) + "%", fmt(row["amount_cny"] / 1e8)]
        markdown.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
        web.append("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in cells) + "</tr>")
    return "\n".join(markdown), "\n".join(web)


def write_outputs(root: Path, day: date, captured: datetime, rows: list[dict], chosen: list[dict], counts: dict, config: dict):
    data = root / "docs" / "data"
    data.mkdir(parents=True, exist_ok=True)
    board_counts = {label: sum(row["board"] == key for row in rows) for key, label in LABELS.items()}
    summary = {
        "trade_date": day.isoformat(), "captured_at_beijing": captured.isoformat(timespec="seconds"),
        "scan_type": "intraday_1430", "scheduled_time_beijing": "14:30",
        "source": "AKShare / Eastmoney", "universe_count": len(rows), "selected_count": len(chosen),
        "boards": board_counts, "source_counts": counts, "filters": config,
        "missing_numeric_rows": sum(any(row[field] is None for field in FIELDS[3:]) for row in rows),
    }
    for filename, content in (("universe.json", rows), ("selected.json", chosen), ("summary.json", summary)):
        (data / filename).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for filename, content in (("universe.csv", rows), ("selected.csv", chosen)):
        with (data / filename).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(content)
    md_table, html_rows = table(chosen, config["max_report_rows"])
    timestamp = captured.strftime("%Y-%m-%d %H:%M")
    intro = f"交易日：{day} · 抓取完成：北京时间 {timestamp} · 股票池 {len(rows)} 只 · 命中 {len(chosen)} 只。"
    report = (f"# A 股 14:30 盘中候选观察\n\n{intro}\n\n"
              f"覆盖沪深主板、创业板、北交所；排除科创板。默认条件：涨跌幅 ≥ {config['min_change_pct']}%、"
              f"换手率 ≥ {config['min_turnover_pct']}%、成交额 ≥ {config['min_amount_cny'] / 1e8:g} 亿元。"
              "这是初步量价候选，不代表已满足历史涨停、爆量锚点、板块主线等完整战法条件。"
              "缺失筛选字段的股票仍保留在全量数据中，但不会进入命中名单。\n\n"
              f"{md_table if chosen else '今日无符合条件的股票。'}\n\n"
              "[全量 CSV](docs/data/universe.csv) · [全量 JSON](docs/data/universe.json) · "
              "[筛选 CSV](docs/data/selected.csv) · [筛选 JSON](docs/data/selected.json)\n\n"
              "行情接口不提供每只股票的独立报价时间，可能延迟；本报告是盘中观察，不是买入指令。\n")
    (root / "REPORT.md").write_text(report, encoding="utf-8")
    body = (f"<p class='meta'>{html.escape(intro)}</p>"
            f"<p>范围：沪深主板、创业板、北交所。条件：涨跌幅 ≥ {config['min_change_pct']}%；"
            f"换手率 ≥ {config['min_turnover_pct']}%；成交额 ≥ {config['min_amount_cny'] / 1e8:g} 亿元。</p>"
            "<nav><a href='data/universe.csv'>全量 CSV</a><a href='data/universe.json'>全量 JSON</a>"
            "<a href='data/selected.csv'>筛选 CSV</a><a href='data/selected.json'>筛选 JSON</a></nav>"
            + ("<div class='scroll'><table><thead><tr><th>代码</th><th>名称</th><th>板块</th><th>现价</th><th>涨跌幅</th><th>换手率</th><th>成交额（亿元）</th></tr></thead><tbody>"
               + html_rows + "</tbody></table></div>" if chosen else "<p>今日无符合条件的股票。</p>"))
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>A 股 14:30 盘中观察 · {day}</title><style>
:root{{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#17213a;background:#f5f7fb}}
body{{max-width:1000px;margin:auto;padding:.85rem}}h1{{font-size:1.3rem}}.meta{{color:#52637e}}nav{{display:flex;flex-wrap:wrap;gap:.6rem;margin:1.25rem 0}}a{{display:inline-flex;align-items:center;min-height:2.75rem;box-sizing:border-box;color:#1559a8;background:#e8f0fd;padding:.55rem .8rem;border-radius:.5rem;text-decoration:none}}.scroll{{overflow-x:auto;background:white;border-radius:.75rem;box-shadow:0 2px 14px #17213a12}}table{{border-collapse:collapse;width:100%;min-width:670px}}th,td{{padding:.7rem .65rem;text-align:left;border-bottom:1px solid #e8edf4;white-space:nowrap}}th{{background:#edf2fa;position:sticky;top:0}}footer{{margin-top:1.5rem;font-size:.85rem;color:#64748b}}@media(min-width:600px){{body{{padding:1rem}}h1{{font-size:1.55rem}}}}
</style></head><body><h1>A 股 14:30 盘中候选观察</h1>{body}<footer>初步量价候选；尚未验证完整战法。第三方报价可能延迟，本页面不是买入指令。</footer></body></html>"""
    (root / "docs" / "index.html").write_text(page, encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--status-file", type=Path, help="供 GitHub Actions 区分新报告与休市跳过")
    parser.add_argument("--wait-until-window", action="store_true", help="13:00 后启动时等待至北京时间 14:30")
    parser.add_argument("--require-window", action="store_true", help="交易日错过扫描窗口时报错，供定时任务识别")
    args = parser.parse_args(argv)
    if args.status_file:
        args.status_file.write_text("false", encoding="utf-8")
    now = datetime.now(TZ)
    if args.wait_until_window and WAIT_START <= now.time() < WINDOW_START:
        target = now.replace(hour=14, minute=30, second=0, microsecond=0)
        while now < target:
            remaining = (target - now).total_seconds()
            print(f"等待北京时间 14:30 扫描，剩余约 {math.ceil(remaining / 60)} 分钟。", flush=True)
            time.sleep(min(60, remaining))
            now = datetime.now(TZ)
    if not in_scan_window(now):
        message = "当前不在北京时间 14:30–14:45 盘中扫描窗口"
        if args.require_window and trading_day(now.date()):
            print(message + "，交易日报告未生成。", file=sys.stderr)
            return 1
        print(message + "，跳过。")
        return 0
    if not trading_day(now.date()):
        print(f"{now.date()} 不是交易日，跳过。")
        return 0
    import akshare as ak
    config = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8"))
    rows, counts = fetch_universe(ak)
    completed = datetime.now(TZ)
    if not in_scan_window(completed):
        print("行情抓取完成时已超过 14:45，跳过本次发布。")
        return 0
    chosen = select(rows, config)
    summary = write_outputs(args.output, now.date(), completed, rows, chosen, counts, config)
    if args.status_file:
        args.status_file.write_text("true", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

