"""沪深主板、创业板、北交所 14:25 盘中战法候选扫描。"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
WINDOW_START = clock_time(14, 25)
WINDOW_END = clock_time(14, 35)
WAIT_START = clock_time(13, 0)
MIN_SOURCE_COUNTS = {"SH": 1000, "SZ": 1500, "BJ": 100}
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


def normalize_tx(records: list[dict]) -> list[dict]:
    output = []
    for raw in records:
        raw_code = str(raw.get("code", "")).strip().lower()
        source, code = raw_code[:2].upper(), raw_code[2:]
        board = board_for(source, code)
        name = str(raw.get("name", "")).strip()
        if not board or not name or name.lower() in ("nan", "none", "-"):
            continue
        amount = number(raw.get("turnover"))
        output.append({
            "code": code, "name": name, "board": board,
            "price": number(raw.get("zxj")),
            "change_pct": number(raw.get("zdf")),
            "turnover_pct": number(raw.get("hsl")),
            # 腾讯 turnover 的单位为万元，报告统一使用元。
            "amount_cny": None if amount is None else amount * 10000,
            "volume_lots": number(raw.get("volume")),
        })
    return output


def fetch_universe(ak) -> tuple[list[dict], dict]:
    frame = retry("Tencent 沪深京", ak.stock_zh_a_spot_tx)
    required = {"code", "name", "zxj", "zdf", "hsl", "turnover", "volume"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        raise RuntimeError("Tencent 行情为空或缺少必要字段")
    rows = normalize_tx(frame.to_dict("records"))
    counts = {
        "SH": sum(row["board"] == "SH" for row in rows),
        "SZ": sum(row["board"] in ("SZ_MAIN", "GEM") for row in rows),
        "BJ": sum(row["board"] == "BJ" for row in rows),
    }
    for source, minimum in MIN_SOURCE_COUNTS.items():
        if counts[source] < minimum:
            raise RuntimeError(f"{source} 仅有 {counts[source]} 条合格股票，低于防截断阈值 {minimum}")
    codes = [row["code"] for row in rows]
    if len(codes) != len(set(codes)):
        raise RuntimeError("不同市场出现重复股票代码，请检查数据源")
    return sorted(rows, key=lambda row: row["code"]), counts


def load_history(path: Path | None, day: date) -> dict:
    if path is None or not path.exists():
        return {"bars": {}, "errors": {}, "missing": "14:00 历史预取文件不存在"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("trade_date") != day.isoformat() or not isinstance(payload.get("bars"), dict):
            raise ValueError("历史预取日期不匹配或结构错误")
        return payload
    except (OSError, ValueError) as exc:
        return {"bars": {}, "errors": {}, "missing": str(exc)}


def fetch_minutes(ak, rows: list[dict], day: date, workers: int) -> dict:
    def one(row):
        try:
            frame = ak.stock_zh_a_hist_min_em(symbol=row["code"], period="5", adjust="",
                start_date=f"{day} 09:30:00", end_date=f"{day} 14:25:00")
            if frame is None or frame.empty:
                return row["code"], None
            bars = []
            for raw in frame.to_dict("records"):
                bar = {"time": str(raw["时间"]), **{key: number(raw[field]) for key, field in
                    (("open", "开盘"), ("close", "收盘"), ("high", "最高"), ("low", "最低"), ("volume", "成交量"))}}
                if all(bar[key] is not None for key in ("open", "close", "high", "low", "volume")):
                    bars.append(bar)
            return row["code"], bars
        except Exception as exc:
            print(f"{row['code']} 5 分钟 K 线不可用：{type(exc).__name__}", file=sys.stderr)
            return row["code"], None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, rows))


def analyze(rows: list[dict], history: dict, config: dict, ak, day: date):
    from strategy import add_minute_evidence, evaluate_stock

    results = {row["code"]: evaluate_stock(row, history["bars"].get(row["code"]), config) for row in rows}
    chosen = [row for row in rows if results[row["code"]]["status"] == "pass"]
    chosen.sort(key=lambda row: (-row["amount_cny"] if row["amount_cny"] is not None else 0, row["code"]))
    minute_targets = chosen[:config["max_minute_stocks"]]
    minute_bars = fetch_minutes(ak, minute_targets, day, config["minute_workers"]) if minute_targets else {}
    for row in minute_targets:
        add_minute_evidence(results[row["code"]], minute_bars.get(row["code"]))
    coverage = {state: sum(result["status"] == state for result in results.values())
                for state in ("pass", "fail", "unknown", "excluded")}
    coverage["history_available"] = sum(code in history["bars"] for code in results)
    coverage["history_by_board"] = {
        label: {"available": sum(row["board"] == board and row["code"] in history["bars"] for row in rows),
                "total": sum(row["board"] == board for row in rows)}
        for board, label in LABELS.items()
    }
    coverage["minute_available"] = sum(bool(minute_bars.get(code)) for code in minute_bars)
    coverage["minute_attempted"] = len(minute_targets)
    if history.get("missing") or history.get("source_unavailable") or history.get("deadline_reached"):
        coverage["history_note"] = history.get("missing") or ("历史接口不可用" if history.get("source_unavailable") else "历史抓取触及 14:24 截止时间")
    return chosen, results, coverage


def fetch_sectors(ak, day: date, config: dict) -> list[dict]:
    try:
        frame = retry("同花顺行业", ak.stock_board_industry_summary_ths, attempts=2, delay=1)
        required = {"板块", "涨跌幅", "上涨家数", "下跌家数", "领涨股"}
        if frame is None or frame.empty or not required.issubset(frame.columns):
            return []
        sectors = []
        for raw in frame.to_dict("records"):
            change = number(raw["涨跌幅"])
            if change is None:
                continue
            sectors.append({"name": str(raw["板块"]), "change_pct": change,
                "up_count": int(number(raw["上涨家数"]) or 0),
                "down_count": int(number(raw["下跌家数"]) or 0),
                "leader": str(raw["领涨股"]), "three_day_return_pct": None,
                "up_days_of_three": None})
        sectors.sort(key=lambda item: -item["change_pct"])
        leaders = sectors[:config["max_sector_report"]]
        def enrich(item):
            try:
                frame = ak.stock_board_industry_index_ths(symbol=item["name"],
                    start_date=(day - timedelta(days=14)).strftime("%Y%m%d"), end_date=day.strftime("%Y%m%d"))
                frame = frame[frame["日期"].astype(str) < day.isoformat()].tail(4)
                closes = [number(value) for value in frame["收盘价"].tolist()]
                if len(closes) == 4 and all(x is not None and x > 0 for x in closes):
                    item["three_day_return_pct"] = round(100 * (closes[-1] / closes[0] - 1), 2)
                    item["up_days_of_three"] = sum(b > a for a, b in zip(closes, closes[1:]))
            except Exception as exc:
                print(f"{item['name']} 行业历史不可用：{type(exc).__name__}", file=sys.stderr)
            return item
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(enrich, leaders))
    except Exception as exc:
        print(f"板块数据不可用：{type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def evaluate_market(ak, rows: list[dict], history: dict, sectors: list[dict], config: dict) -> dict:
    from strategy import signal

    result = {"up_count": sum((row["change_pct"] or 0) > 0 for row in rows),
              "down_count": sum((row["change_pct"] or 0) < 0 for row in rows),
              "amount_cny": sum(row["amount_cny"] or 0 for row in rows), "checks": {}}
    checks = result["checks"]
    checks["leading_sector"] = signal(None if not any(item["three_day_return_pct"] is not None for item in sectors) else
        any(item["change_pct"] >= config["leading_sector_change_pct"] and
            (item["three_day_return_pct"] or 0) > 0 and (item["up_days_of_three"] or 0) >= 2 for item in sectors),
        sectors[:3], "同花顺行业涨幅与最近三日持续性代理")
    try:
        spot = ak.stock_zh_index_spot_sina()
        raw = next(item for item in spot.to_dict("records") if item["代码"] == "sh000001")
        prior = history.get("index_bars", {}).get("sh000001", [])
        if len(prior) < 20:
            raise ValueError("上证指数历史不足 20 日")
        current = number(raw["最新价"])
        volume = number(raw["成交量"])
        previous_volumes = [bar["volume"] for bar in prior[-20:]]
        if current is None or volume is None or any(v is None or v <= 0 for v in previous_volumes):
            raise ValueError("上证指数行情字段缺失")
        ma5 = sum([bar["close"] for bar in prior[-4:]] + [current]) / 5
        recent_low = min(previous_volumes[-5:])
        low_volume_seen = (recent_low <= config["low_volume_ratio"] *
            (sum(previous_volumes) / len(previous_volumes)))
        volume_expand = volume >= config["rebound_volume_ratio"] * recent_low
        checks["above_ma5"] = signal(current > ma5, {"index_price": current, "ma5": round(ma5, 2)})
        checks["volume_rebound"] = signal(low_volume_seen and volume_expand,
            {"recent_low_volume_seen": low_volume_seen, "today_volume_vs_recent_low": round(volume / recent_low, 2)},
            "以 14:25 的盘中量比较最近五日最低全天量，尾盘结果仍可能变化")
    except Exception as exc:
        checks["above_ma5"] = signal(None, reason=f"指数数据不足：{type(exc).__name__}")
        checks["volume_rebound"] = signal(None, reason=f"指数数据不足：{type(exc).__name__}")
    statuses = [check["status"] for check in checks.values()]
    result["reversal_status"] = "pass" if all(x == "pass" for x in statuses) else "unknown" if "unknown" in statuses else "fail"
    return result


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


def write_outputs(root: Path, day: date, captured: datetime, rows: list[dict], chosen: list[dict],
                  counts: dict, config: dict, analysis: dict | None = None,
                  coverage: dict | None = None, sectors: list[dict] | None = None,
                  market: dict | None = None):
    data = root / "docs" / "data"
    data.mkdir(parents=True, exist_ok=True)
    board_counts = {label: sum(row["board"] == key for row in rows) for key, label in LABELS.items()}
    summary = {
        "trade_date": day.isoformat(), "captured_at_beijing": captured.isoformat(timespec="seconds"),
        "scan_type": "intraday_strategy", "scheduled_time_beijing": "14:25",
        "source": "AKShare / Tencent spot + Tencent/Eastmoney/Sina daily + Eastmoney 5m + THS sectors",
        "universe_count": len(rows), "selected_count": len(chosen),
        "boards": board_counts, "source_counts": counts, "filters": config,
        "missing_numeric_rows": sum(any(row[field] is None for field in FIELDS[3:]) for row in rows),
        "coverage": coverage or {}, "market": market or {}, "sector_count": len(sectors or []),
    }
    for filename, content in (("universe.json", rows), ("selected.json", chosen), ("summary.json", summary)):
        (data / filename).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for filename, content in (("universe.csv", rows), ("selected.csv", chosen)):
        with (data / filename).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(content)
    evidence = {row["code"]: analysis[row["code"]] for row in chosen} if analysis else {}
    (data / "analysis.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    statuses = []
    for row in rows:
        result = analysis.get(row["code"], {}) if analysis else {}
        checks = result.get("checks", {})
        statuses.append({"code": row["code"], "name": row["name"], "board": row["board"],
            "status": result.get("status", "unknown"),
            "failed_checks": ",".join(key for key, check in checks.items() if check["status"] == "fail"),
            "unknown_checks": ",".join(key for key, check in checks.items() if check["status"] == "unknown"),
            "reason": (checks.get("history", {}).get("reason") or
                       checks.get("security_type", {}).get("reason") or "")})
    with (data / "status.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=("code", "name", "board", "status", "failed_checks", "unknown_checks", "reason"))
        writer.writeheader()
        writer.writerows(statuses)
    (data / "sectors.json").write_text(json.dumps(sectors or [], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_table, html_rows = table(chosen, config["max_report_rows"])
    timestamp = captured.strftime("%Y-%m-%d %H:%M")
    intro = f"交易日：{day} · 抓取完成：北京时间 {timestamp} · 股票池 {len(rows)} 只 · 历史日线覆盖 {(coverage or {}).get('history_available', 0)} 只 · 硬条件候选 {len(chosen)} 只。"
    board_coverage = "；".join(f"{label} {part['available']}/{part['total']}" for label, part in
        (coverage or {}).get("history_by_board", {}).items())
    sector_lines = [f"- {item['name']}：盘中涨跌幅 {fmt(item['change_pct'])}%，此前三日涨跌幅 {fmt(item['three_day_return_pct'])}%，上涨/下跌 {item['up_count']}/{item['down_count']}，领涨 {item['leader']}。" for item in (sectors or [])[:10]]
    detail_lines = []
    for row in chosen[:20] if analysis else []:
        checks = evidence[row["code"]]["checks"]
        limit_events = checks.get("past_limit_up", {}).get("evidence") or []
        latest_limit = limit_events[-1]["date"] if limit_events else "—"
        ratio = (checks.get("red_volume_proxy", {}).get("evidence") or {}).get("volume_ratio_vs_20d")
        minute = checks.get("non_morning_volume_stack", {}).get("status", "unknown")
        anchor = checks.get("bearish_max_volume_anchor", {}).get("evidence")
        detail_lines.append(f"- **{row['code']} {row['name']}**：近 120 日涨停代理最近 {latest_limit}；20 日量比 {ratio}；最大量阴线锚点 {anchor['date'] + ' / 高点 ' + str(anchor['high']) if anchor else '未出现'}；非早盘 5 分钟红量 {minute}。")
    market_checks = (market or {}).get("checks", {})
    market_text = (f"上涨 {(market or {}).get('up_count', '—')} 只、下跌 {(market or {}).get('down_count', '—')} 只；"
                   f"盘中成交额 {(market or {}).get('amount_cny', 0) / 1e12:.2f} 万亿元。"
                   f"大盘反转三条件：上证站上 5 日线 {market_checks.get('above_ma5', {}).get('status', 'unknown')}；"
                   f"地量后放量 {market_checks.get('volume_rebound', {}).get('status', 'unknown')}；"
                   f"持续领涨行业 {market_checks.get('leading_sector', {}).get('status', 'unknown')}。"
                   f"综合状态 {(market or {}).get('reversal_status', 'unknown')}；这只是规则检查，不代替仓位决策。")
    report = (f"# A 股 14:25 盘中战法观察\n\n{intro}\n\n"
              "覆盖沪深主板、创业板、北交所；排除科创板，ST/退市整理暂不参与硬条件候选。"
              "按用户《选股模式与交易系统》执行四层漏斗；候选需同时满足近 120 日日线涨停代理、上涨趋势、当日放量上涨代理。"
              "缺数据记为 **unknown**，不判作失败；信号是量价代理，不是主力行为事实。\n\n"
              f"## 一、大盘择时\n\n{market_text}\n\n"
              "## 二、板块方向\n\n" + ("\n".join(sector_lines) if sector_lines else "板块数据暂不可用，主线判断为 unknown。") + "\n\n"
              "以下是同花顺行业当时涨幅排序，不等于用户自定义的 70–80 个主流板块池；个股与板块映射仍需核验。\n\n"
              f"## 三、个股候选\n\n{md_table if chosen else '无已验证同时满足三个硬条件的候选；请查看覆盖数，不能将缺数据解释为无机会。'}\n\n"
              + ("\n".join(detail_lines) if detail_lines else "") + "\n\n"
              "## 四、买卖节奏与数据边界\n\n"
              "5 分钟 K 线仅对候选中的前若干只抓取，已在 analysis.json 标出跳空、非早盘红量和回调形态。"
              "14:25 报告无法确认 MD 中‘首次开仓必须在 14:30 之后’，也无法读取个人持仓、成本或判断主力真实意图；14:30 后须再人工复核。"
              "止损线、做 T、五分钟卖点需要持仓和连续盘中数据，报告不发交易指令。\n\n"
              f"历史日线分市场覆盖：{board_coverage or '未知'}。详细覆盖与缺数原因：{json.dumps(coverage or {}, ensure_ascii=False)}。\n\n"
              "[全量 CSV](docs/data/universe.csv) · [候选 CSV](docs/data/selected.csv) · [逐股证据](docs/data/analysis.json) · "
              "[全市场状态](docs/data/status.csv) · [板块排名](docs/data/sectors.json) · [机器摘要](docs/data/summary.json)\n\n"
              "第三方盘中报价可能延迟；抓取完成时间不等于每只股票的真实报价时间。本报告仅供研究，不构成投资建议。\n")
    (root / "REPORT.md").write_text(report, encoding="utf-8")
    body = (f"<p class='meta'>{html.escape(intro)}</p>"
            "<p>近 120 日涨停代理 + 上涨趋势 + 放量上涨代理同时成立，才列为候选。缺数据记为 unknown。</p>"
            f"<p>{html.escape(market_text)}</p>"
            "<h2>领先行业</h2><ul>" + "".join(f"<li>{html.escape(line[2:])}</li>" for line in sector_lines) + "</ul>"
            "<nav><a href='data/universe.csv'>全量 CSV</a><a href='data/universe.json'>全量 JSON</a>"
            "<a href='data/selected.csv'>候选 CSV</a><a href='data/status.csv'>全市场状态</a><a href='data/analysis.json'>逐股证据</a></nav>"
            + ("<div class='scroll'><table><thead><tr><th>代码</th><th>名称</th><th>板块</th><th>现价</th><th>涨跌幅</th><th>换手率</th><th>成交额（亿元）</th></tr></thead><tbody>"
               + html_rows + "</tbody></table></div>" if chosen else "<p>今日无符合条件的股票。</p>"))
    if analysis and chosen:
        cards = []
        for row in chosen[:20]:
            checks = analysis[row["code"]]["checks"]
            ratio = (checks.get("red_volume_proxy", {}).get("evidence") or {}).get("volume_ratio_vs_20d")
            anchor = checks.get("bearish_max_volume_anchor", {}).get("evidence")
            minute = checks.get("non_morning_volume_stack", {}).get("status", "unknown")
            cards.append(f"<article class='card'><strong>{html.escape(row['name'])} {row['code']}</strong>"
                         f"<span>{html.escape(LABELS[row['board']])} · {fmt(row['change_pct'])}% · {fmt(row['amount_cny'] / 1e8 if row['amount_cny'] else None)} 亿元</span>"
                         f"<span>20 日量比 {html.escape(str(ratio))} · 最大量阴线 {html.escape(anchor['date'] if anchor else '无')}</span>"
                         f"<span>非早盘红量：{html.escape(minute)} · 回调形态：{html.escape(checks.get('pullback_stabilization', {}).get('status', 'unknown'))}</span></article>")
        body += "<h2>候选证据</h2><div class='cards'>" + "".join(cards) + "</div>"
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>A 股 14:25 战法观察 · {day}</title><style>
:root{{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#17213a;background:#f5f7fb}}
body{{max-width:1000px;margin:auto;padding:.85rem}}h1{{font-size:1.3rem}}.meta{{color:#52637e}}nav{{display:flex;flex-wrap:wrap;gap:.6rem;margin:1.25rem 0}}a{{display:inline-flex;align-items:center;min-height:2.75rem;box-sizing:border-box;color:#1559a8;background:#e8f0fd;padding:.55rem .8rem;border-radius:.5rem;text-decoration:none}}.scroll{{overflow-x:auto;background:white;border-radius:.75rem;box-shadow:0 2px 14px #17213a12}}table{{border-collapse:collapse;width:100%;min-width:670px}}th,td{{padding:.7rem .65rem;text-align:left;border-bottom:1px solid #e8edf4;white-space:nowrap}}th{{background:#edf2fa;position:sticky;top:0}}.cards{{display:grid;gap:.7rem}}.card{{display:grid;gap:.45rem;background:#fff;padding:1rem;border-radius:.8rem;box-shadow:0 2px 14px #17213a12}}.card span{{color:#52637e;font-size:.92rem}}footer{{margin-top:1.5rem;font-size:.85rem;color:#64748b}}@media(min-width:600px){{body{{padding:1rem}}h1{{font-size:1.55rem}}.cards{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
</style></head><body><h1>A 股 14:25 盘中战法观察</h1>{body}<footer>14:30 后买点需人工复核；第三方报价可能延迟。本页面不是买入指令。</footer></body></html>"""
    (root / "docs" / "index.html").write_text(page, encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--status-file", type=Path, help="供 GitHub Actions 区分新报告与休市跳过")
    parser.add_argument("--history-file", type=Path, help="14:00 预取的 120 日日线 JSON")
    parser.add_argument("--wait-until-window", action="store_true", help="预取后等待至北京时间 14:25")
    parser.add_argument("--require-window", action="store_true", help="交易日错过扫描窗口时报错，供定时任务识别")
    args = parser.parse_args(argv)
    if args.status_file:
        args.status_file.write_text("false", encoding="utf-8")
    now = datetime.now(TZ)
    if args.wait_until_window and WAIT_START <= now.time() < WINDOW_START:
        target = now.replace(hour=14, minute=25, second=0, microsecond=0)
        while now < target:
            remaining = (target - now).total_seconds()
            print(f"等待北京时间 14:25 盘中快照，剩余约 {math.ceil(remaining / 60)} 分钟。", flush=True)
            time.sleep(min(60, remaining))
            now = datetime.now(TZ)
    if not in_scan_window(now):
        message = "当前不在北京时间 14:25–14:35 盘中扫描窗口"
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
    history = load_history(args.history_file, now.date())
    chosen, analysis, coverage = analyze(rows, history, config, ak, now.date())
    sectors = fetch_sectors(ak, now.date(), config)
    market = evaluate_market(ak, rows, history, sectors, config)
    completed = datetime.now(TZ)
    if not in_scan_window(completed):
        print("分析完成时已超过 14:35，跳过本次发布。", file=sys.stderr)
        return 0
    summary = write_outputs(args.output, now.date(), completed, rows, chosen, counts, config,
                            analysis, coverage, sectors, market)
    if args.status_file:
        args.status_file.write_text("true", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

