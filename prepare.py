"""14:00 预取 120 根已完成的日线；失败股票在报告中保留 unknown。"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path

from scanner import TZ, fetch_universe, number, trading_day

_bse_lock = threading.Lock()
_bse_next_slot = 0.0


def wait_bse_slot():
    """控制北交所东方财富请求速率，避免共享出口批量触发断连。"""
    global _bse_next_slot
    with _bse_lock:
        now = time.monotonic()
        delay = max(0.0, _bse_next_slot - now)
        _bse_next_slot = max(now, _bse_next_slot) + 2.5
    if delay:
        time.sleep(delay)


def history_symbol(row):
    return {"SH": "sh", "SZ_MAIN": "sz", "GEM": "sz", "BJ": "bj"}[row["board"]] + row["code"]


def fetch_bars(ak, row, as_of: date, history_days: int):
    # 多留 90 个日历日以覆盖周末、休市；只取昨日及以前的 120 根日线。
    start = (as_of - timedelta(days=history_days * 2)).strftime("%Y%m%d")
    end = as_of.strftime("%Y%m%d")
    providers = ("sina", "eastmoney") if row["board"] == "BJ" else ("tencent", "eastmoney")
    failures = []
    for provider in providers:
        try:
            if provider == "tencent":
                frame = ak.stock_zh_a_hist_tx(symbol=history_symbol(row), start_date=start,
                                              end_date=end, adjust="", timeout=12)
            elif provider == "sina":
                wait_bse_slot()
                frame = ak.stock_zh_a_daily(symbol=history_symbol(row), start_date=start,
                                            end_date=end, adjust="")
            else:
                if row["board"] == "BJ":
                    wait_bse_slot()
                frame = ak.stock_zh_a_hist(symbol=row["code"], period="daily",
                                           start_date=start, end_date=end, adjust="", timeout=12)
                frame = frame.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                    "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"})
            if frame is None or frame.empty:
                raise ValueError("empty history")
            frame = frame[frame["date"].astype(str) < as_of.isoformat()].tail(history_days)
            result = []
            for raw in frame.to_dict("records"):
                bar = {"date": str(raw["date"]), **{field: number(raw[field])
                    for field in ("open", "close", "high", "low", "volume", "amount")}}
                if provider == "eastmoney" and bar["volume"] is not None:
                    bar["volume"] *= 100  # 东方财富日线以手计。
                result.append(bar)
            return row["code"], result, None
        except Exception as exc:
            failures.append(f"{provider} {type(exc).__name__}: {str(exc)[:70]}")
            time.sleep(0.25)
    return row["code"], None, "; ".join(failures)


def prepare(ak, rows, as_of: date, cfg: dict, deadline: datetime | None = None):
    result = {"trade_date": as_of.isoformat(), "source": "AKShare / Tencent, Eastmoney and Sina daily history",
              "history_days": cfg["history_days"], "bars": {}, "errors": {}, "attempted": 0,
              "index_bars": {}, "sector_bars": {}}
    for symbol in ("sh000001", "sz399001", "sz399006"):
        try:
            frame = ak.stock_zh_a_hist_tx(symbol=symbol,
                start_date=(as_of - timedelta(days=45)).strftime("%Y%m%d"),
                end_date=as_of.strftime("%Y%m%d"), timeout=12)
            frame = frame[frame["date"].astype(str) < as_of.isoformat()].tail(25)
            result["index_bars"][symbol] = [
                {"date": str(raw["date"]), "close": number(raw["close"]), "volume": number(raw["volume"])}
                for raw in frame.to_dict("records")]
        except Exception as exc:
            result["errors"][symbol] = f"指数历史 {type(exc).__name__}: {str(exc)[:70]}"
    eligible = rows
    if not eligible:
        return result
    # 先探测三个市场；单只失败不能推断整个板块不可用。
    probe = []
    for board in ("SH", "SZ_MAIN", "BJ"):
        sample = next((row for row in eligible if row["board"] == board), None)
        if sample:
            probe.append(fetch_bars(ak, sample, as_of, cfg["history_days"]))
    for code, bars, error in probe:
        result["attempted"] += 1
        if error:
            result["errors"][code] = error
        else:
            result["bars"][code] = bars
    if not any(bars for _, bars, _ in probe):
        result["source_unavailable"] = True
        return result
    remaining = [row for row in eligible if row["code"] not in result["bars"]
                 and row["code"] not in result["errors"]]
    def collect(batch, workers):
        if not batch:
            return
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {pool.submit(fetch_bars, ak, row, as_of, cfg["history_days"]): row for row in batch}
            for future in as_completed(futures):
                code, bars, error = future.result()
                result["attempted"] += 1
                if error:
                    result["errors"][code] = error
                else:
                    result["bars"][code] = bars
                if result["attempted"] % 250 == 0:
                    print(f"历史日线完成 {result['attempted']}/{len(eligible)}，失败 {len(result['errors'])}", flush=True)
                if deadline and datetime.now(TZ) >= deadline:
                    result["deadline_reached"] = True
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    # 北交所新浪请求先于沪深；失败时回退东方财富，两者均限速。
    bse = [row for row in remaining if row["board"] == "BJ"]
    main = [row for row in remaining if row["board"] != "BJ"]
    collect(bse, min(2, cfg["history_workers"]))
    if not result.get("deadline_reached"):
        collect(main, cfg["history_workers"])
    result["completed_at_beijing"] = datetime.now(TZ).isoformat(timespec="seconds")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample", type=int, default=0, help="只取前 N 只股票做接口测试")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--start-early", action="store_true", help="13 点后预取昨日以前的日线，14:24 截止")
    args = parser.parse_args(argv)
    now = datetime.now(TZ)
    if not trading_day(now.date()):
        print("休市，跳过历史预取")
        return 0
    if not args.no_wait and not args.start_early and now.time() < clock_time(14, 0):
        target = now.replace(hour=14, minute=0, second=0, microsecond=0)
        while now < target:
            time.sleep(min(60, (target - now).total_seconds()))
            now = datetime.now(TZ)
    if now.time() >= clock_time(14, 24) and not args.no_wait:
        print("已错过历史预取窗口；盘中报告将把历史证据标为未知。", file=sys.stderr)
        return 0
    import akshare as ak
    import requests
    # AKShare 的腾讯起始年份探测未显式设置 timeout；为整个预取进程加默认超时。
    original_get = requests.get
    def bounded_get(*call_args, **call_kwargs):
        call_kwargs.setdefault("timeout", 12)
        return original_get(*call_args, **call_kwargs)
    requests.get = bounded_get
    cfg = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
    rows, _ = fetch_universe(ak)
    if args.sample:
        boards = ("SH", "SZ_MAIN", "GEM", "BJ")
        per_board = max(1, args.sample // len(boards))
        sampled = []
        for board in boards:
            sampled.extend([row for row in rows if row["board"] == board][:per_board])
        rows = sampled[:args.sample]
    deadline = None if args.no_wait else now.replace(hour=14, minute=24, second=0, microsecond=0)
    result = prepare(ak, rows, now.date(), cfg, deadline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in ("bars", "errors", "index_bars", "sector_bars")}, ensure_ascii=False), flush=True)
    print(f"历史日线成功 {len(result['bars'])} 只，失败 {len(result['errors'])} 只", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
