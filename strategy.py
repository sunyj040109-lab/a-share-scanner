"""从用户战法提炼的可复核量价代理指标；未知不等于未通过。"""

from __future__ import annotations

from statistics import mean


def signal(value, evidence=None, reason=None):
    return {"status": "unknown" if value is None else ("pass" if value else "fail"),
            "evidence": evidence, "reason": reason}


def evaluate_stock(quote: dict, bars: list[dict] | None, cfg: dict) -> dict:
    """只用昨日以前的历史日线与今日盘中快照，避免未来数据混入。"""
    checks = {}
    if "ST" in quote["name"].upper() or "退" in quote["name"]:
        return {"status": "excluded", "checks": {"security_type": signal(False, reason="ST 或退市整理股票需单独交易规则")}}
    if quote["price"] is None or quote["price"] <= 0 or quote["volume_lots"] is None:
        return {"status": "unknown", "checks": {"history": signal(None, reason="盘中报价缺失")}}
    if not bars or len(bars) < cfg["history_days"]:
        return {"status": "unknown", "checks": {"history": signal(None, reason=f"少于 {cfg['history_days']} 根有效历史日线")}}
    bars = bars[-cfg["history_days"]:]
    closes = [bar["close"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    ohlc = [bar[field] for bar in bars for field in ("open", "high", "low")]
    if any(x is None or x <= 0 for x in closes + volumes + ohlc):
        return {"status": "unknown", "checks": {"history": signal(None, reason="历史日线有缺失值")}}
    checks["history"] = signal(True, {"bars": len(bars), "last_date": bars[-1]["date"]})
    pct_limit = {"SH": 10, "SZ_MAIN": 10, "GEM": 20, "BJ": 30}[quote["board"]]
    limit_events = []
    for previous, current in zip(bars, bars[1:]):
        pct = 100 * (current["close"] / previous["close"] - 1)
        if pct >= pct_limit - cfg["limit_up_tolerance_pct"]:
            limit_events.append({"date": current["date"], "change_pct": round(pct, 2)})
    checks["past_limit_up"] = signal(bool(limit_events), limit_events[-3:],
        "按收盘涨幅近似识别；新股无涨跌幅限制、ST 和复权问题需人工核对")
    one_price = [bar["date"] for bar in bars if any(event["date"] == bar["date"] for event in limit_events)
                 and max(bar["open"], bar["close"], bar["high"], bar["low"]) -
                 min(bar["open"], bar["close"], bar["high"], bar["low"]) <= 0.01]
    checks["one_price_limit_up"] = signal(bool(one_price), one_price[-3:],
        "日 K 线开高低收近乎同价；无法判断封板中途是否被砸开或主力试仓")
    price = quote["price"]
    ma5, ma20, ma60 = (mean((closes + [price])[-n:]) for n in (5, 20, 60))
    old_ma20 = mean(closes[-25:-5])
    checks["uptrend"] = signal(price > ma5 > ma20 > ma60 and ma20 > old_ma20,
        {"price": price, "ma5": round(ma5, 2), "ma20": round(ma20, 2),
         "ma60": round(ma60, 2), "ma20_5d_ago": round(old_ma20, 2)})
    # 腾讯实时成交量单位为手；历史日线单位为股。
    live_volume = quote["volume_lots"] * 100
    volume_ratio = live_volume / mean(volumes[-20:])
    red_stack = price > closes[-1] and volume_ratio >= cfg["red_volume_ratio"]
    checks["red_volume_proxy"] = signal(red_stack,
        {"volume_ratio_vs_20d": round(volume_ratio, 2), "price_vs_prev_close_pct": round(100 * (price / closes[-1] - 1), 2)},
        "日级放量上涨代理；无法证明非早盘红色堆量或主力买入")
    clusters = []
    current_cluster = []
    for index, bar in enumerate(bars):
        base = mean(volumes[max(0, index - 20):index]) if index >= 5 else None
        qualifies = base is not None and bar["close"] > bar["open"] and bar["volume"] >= cfg["red_volume_ratio"] * base
        if qualifies:
            current_cluster.append(bar)
        elif current_cluster:
            if len(current_cluster) >= 2:
                clusters.append(current_cluster)
            current_cluster = []
    if len(current_cluster) >= 2:
        clusters.append(current_cluster)
    cluster = clusters[-1] if clusters else None
    zone = None if not cluster else {"from": cluster[0]["date"], "to": cluster[-1]["date"],
        "low": min(bar["low"] for bar in cluster), "high": max(bar["high"] for bar in cluster),
        "volume_weighted_close": round(sum(bar["close"] * bar["volume"] for bar in cluster) /
                                       sum(bar["volume"] for bar in cluster), 2)}
    checks["volume_cluster_cost_proxy"] = signal(None if not cluster else price >= zone["low"], zone,
        "连续放量阳线的价格区间代理，不是实际主力持仓成本")
    max_bar = max(bars, key=lambda bar: bar["volume"])
    bearish_anchor = max_bar if max_bar["close"] < max_bar["open"] else None
    checks["bearish_max_volume_anchor"] = signal(bool(bearish_anchor),
        None if bearish_anchor is None else {key: bearish_anchor[key] for key in ("date", "high", "low", "volume")},
        "120 日最大量阴线；非复权价格，除权需人工核对")
    if bearish_anchor:
        checks["anchor_unbroken"] = signal(price >= bearish_anchor["low"],
            {"price": price, "anchor_low": bearish_anchor["low"]})
        checks["volume_breakout"] = signal(price > bearish_anchor["high"] and volume_ratio >= cfg["breakout_volume_ratio"],
            {"price": price, "anchor_high": bearish_anchor["high"], "volume_ratio_vs_20d": round(volume_ratio, 2)})
    else:
        checks["anchor_unbroken"] = signal(None, reason="无最大量阴线锚点")
        checks["volume_breakout"] = signal(None, reason="无最大量阴线锚点")
    recent_upper = []
    for bar in bars[-10:]:
        body = abs(bar["close"] - bar["open"])
        upper = bar["high"] - max(bar["close"], bar["open"])
        if upper >= 2 * max(body, 0.01):
            recent_upper.append(bar["date"])
    checks["recent_upper_shadow"] = signal(bool(recent_upper), recent_upper,
        "只测上影线形态，不能判断是套人、洗盘还是试盘")
    # 跳空、倒锤、5 分钟结构及主力意图需要可靠盘中 OHLC/分钟量价或人工复核。
    for name, reason in {
        "gap_confirmation": "待读取当日 5 分钟 K 线",
        "pullback_stabilization": "待读取当日 5 分钟 K 线",
        "non_morning_volume_stack": "待读取当日 5 分钟量价",
        "five_minute_exit": "需要连续 5 分钟 K 线，且属于持仓管理",
        "operator_intent": "不能从公开量价数据确认主力真实意图",
    }.items():
        checks[name] = signal(None, reason=reason)
    # 只把用户明确写出的三个硬条件作为候选门槛；其他是观察证据。
    hard = [checks[key]["status"] for key in ("past_limit_up", "uptrend", "red_volume_proxy")]
    status = "pass" if all(x == "pass" for x in hard) else "fail"
    return {"status": status, "checks": checks}


def add_minute_evidence(result: dict, bars: list[dict] | None):
    """对候选股附加 5 分钟证据；不会把形态自动解释为主力行为。"""
    checks = result["checks"]
    if not bars or len(bars) < 8:
        return result
    bars = sorted(bars, key=lambda item: item["time"])
    first, last = bars[0], bars[-1]
    anchor = checks.get("bearish_max_volume_anchor", {}).get("evidence")
    if anchor:
        checks["gap_confirmation"] = signal(first["open"] > anchor["high"] and last["close"] > anchor["high"],
            {"open": first["open"], "latest_5m_close": last["close"], "anchor_high": anchor["high"]},
            "盘中尚未收盘，跳空保持状态可能变化")
    midday = [bar for bar in bars if bar["time"][11:16] >= "10:00"]
    stacks = []
    for index, bar in enumerate(midday):
        preceding = midday[max(0, index - 5):index]
        if len(preceding) >= 3 and bar["close"] > bar["open"] and bar["volume"] >= 2 * mean(x["volume"] for x in preceding):
            stacks.append(bar["time"])
    checks["non_morning_volume_stack"] = signal(bool(stacks), stacks[-5:],
        "5 分钟红量柱相对前 5 根放大；不能证明主动买单或主力吸筹")
    width = last["high"] - last["low"]
    body = abs(last["close"] - last["open"])
    upper = last["high"] - max(last["close"], last["open"])
    lower = min(last["close"], last["open"]) - last["low"]
    doji = width > 0 and body / width <= 0.2
    inverted_hammer = width > 0 and upper >= 2 * max(body, 0.001) and lower <= max(body, 0.001)
    pullback = bars[-4]["close"] > bars[-2]["close"]
    checks["pullback_stabilization"] = signal(pullback and (doji or inverted_hammer),
        {"time": last["time"], "doji": doji, "inverted_hammer": inverted_hammer, "prior_pullback": pullback},
        "仅识别 5 分钟回调及末根形态；尾盘入场需 14:30 后再确认")
    return result
