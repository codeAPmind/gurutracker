"""
Guru Tracker 信号消费 & 自动下单

调度（cron 09:35 ET，周一至周五）：
  python -m executor.signal_consumer

买入条件（三选二组合，2026-06~09 历史信号 sweep 得出，见下方 SWEEP 说明）：
1. score >= MIN_SCORE（触发阈值）
2. 价格相对近60日高点回调 >= GURU_DRAWDOWN_PCT（默认30%）
3. 该笔信号仓位占比（ARK etf_percent）> GURU_MIN_POSITION_PCT（默认0.10%）

SWEEP 依据（回调>=30% + 仓位>0.10%，持仓10日，n=30）：
  胜率 67%，均值收益 +9.2%，Sharpe 2.78，PF 2.25
放弃"连续买入"这条（单独统计样本量过小，且与仓位/回调条件高度重叠）。
本金规模较小时优先胜率，故选30%回调门槛而非20%（触发更少但更准）。

仓位管理：本金5000美元，单笔额度1000美元，最多同时持仓 GURU_MAX_POSITIONS（默认5）只。

逻辑：
1. 读取近2日 score >= MIN_SCORE 的 buy 信号
2. 过滤：回调幅度 + 仓位占比双重门槛
3. 跳过已持仓的 ticker，检查当前持仓数是否已达上限
4. 按 GURU_BUDGET_USD（默认1000）限价买入
5. 检查所有 open 持仓：止盈/止损/到期（10日）三种平仓触发
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from config.settings import DB_PATH, FMP_API_KEY, FMP_STABLE_URL
from executor.position_manager import (
    BUDGET_USD, MAX_POSITIONS, already_holding, check_exit_signal, close_position,
    get_open_positions, init_db, mark_closing, open_position,
)
from executor.futu_trader import GuruFutuTrader

logger = logging.getLogger(__name__)

MIN_SCORE = float(os.getenv("GURU_MIN_SCORE", "50"))
DRAWDOWN_PCT = float(os.getenv("GURU_DRAWDOWN_PCT", "20"))       # 相对60日高点回调百分比（正数）
MIN_POSITION_PCT = float(os.getenv("GURU_MIN_POSITION_PCT", "0.10"))  # ARK仓位占比阈值

_POS_PCT_RE = re.compile(r"占基金仓位: ([\d.]+)%")


def _us_today() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except ImportError:
        tz = timezone(timedelta(hours=-5))
    return datetime.now(tz).strftime("%Y-%m-%d")


def _fetch_pending_signals() -> list[dict]:
    """返回近2日 score >= MIN_SCORE 的 buy 信号（去重 ticker 取最高分）"""
    today = _us_today()
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT id, ticker, score, action, guru_name, event_date, raw_content, created_at
               FROM signals
               WHERE score >= ?
                 AND action IN ('buy', '买入')
                 AND DATE(created_at) >= ?
               ORDER BY score DESC""",
            (MIN_SCORE, cutoff),
        ).fetchall()
    finally:
        con.close()

    seen: dict[str, dict] = {}
    for r in rows:
        t = r["ticker"]
        if t not in seen or r["score"] > seen[t]["score"]:
            seen[t] = dict(r)
    return list(seen.values())


def _extract_position_pct(raw_content: str) -> float:
    m = _POS_PCT_RE.search(raw_content or "")
    return float(m.group(1)) if m else 0.0


def _get_drawdown_pct(ticker: str) -> Optional[float]:
    """相对近60个交易日收盘价最高点的回调百分比（正数，越大回调越深）"""
    if not FMP_API_KEY:
        logger.warning("[consumer] FMP_API_KEY 未配置，无法计算回调幅度，跳过该过滤")
        return None
    try:
        url = f"{FMP_STABLE_URL}/historical-price-eod/full"
        params = {"symbol": ticker, "apikey": FMP_API_KEY}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data or len(data) < 20:
            return None
        # FMP 返回按日期降序，最新在前
        closes = [d["close"] for d in data[:60] if d.get("close")]
        if len(closes) < 20:
            return None
        latest = closes[0]
        recent_high = max(closes)
        if recent_high <= 0:
            return None
        return (1 - latest / recent_high) * 100
    except Exception as e:
        logger.error("[consumer] 获取 %s 历史价格失败: %s", ticker, e)
        return None


def _passes_entry_filter(sig: dict) -> bool:
    """回调幅度 + 仓位占比双重门槛"""
    pos_pct = _extract_position_pct(sig.get("raw_content", ""))
    if pos_pct <= MIN_POSITION_PCT:
        logger.info("[consumer] %s 仓位占比%.2f%% <= 阈值%.2f%%，跳过",
                    sig["ticker"], pos_pct, MIN_POSITION_PCT)
        return False

    drawdown = _get_drawdown_pct(sig["ticker"])
    if drawdown is None:
        logger.warning("[consumer] %s 无法获取回调幅度，保守跳过", sig["ticker"])
        return False
    if drawdown < DRAWDOWN_PCT:
        logger.info("[consumer] %s 回调%.1f%% < 阈值%.1f%%，跳过",
                    sig["ticker"], drawdown, DRAWDOWN_PCT)
        return False

    logger.info("[consumer] %s 通过筛选: 回调=%.1f%% 仓位=%.2f%% score=%.0f",
                sig["ticker"], drawdown, pos_pct, sig["score"])
    return True


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    init_db()

    signals = _fetch_pending_signals()
    logger.info("[consumer] found %d buy signals (score>=%.0f), applying entry filter "
                "(drawdown>=%.0f%%, position>%.2f%%)", len(signals), MIN_SCORE,
                DRAWDOWN_PCT, MIN_POSITION_PCT)

    signals = [s for s in signals if _passes_entry_filter(s)]
    logger.info("[consumer] %d signals passed entry filter", len(signals))

    trader = GuruFutuTrader()
    try:
        trader.connect()
    except Exception as e:
        logger.error("[consumer] futu connect failed: %s", e)
        return

    try:
        # ── 新信号买入 ────────────────────────────────────────────────
        open_count = len(get_open_positions())
        slots_left = max(0, MAX_POSITIONS - open_count)
        if slots_left == 0:
            logger.info("[consumer] 已达最大持仓数 MAX_POSITIONS=%d（当前%d），本轮不新开仓",
                        MAX_POSITIONS, open_count)
        else:
            logger.info("[consumer] 当前持仓%d/%d，本轮最多可开%d个新仓",
                        open_count, MAX_POSITIONS, slots_left)

        for sig in signals:
            if slots_left <= 0:
                logger.info("[consumer] 持仓已满，跳过剩余信号")
                break

            ticker = sig["ticker"]
            if not ticker or ticker in ("", "N/A"):
                continue
            if already_holding(ticker):
                logger.info("[consumer] skip %s (already holding)", ticker)
                continue

            price = trader.get_price(ticker)
            if not price:
                logger.warning("[consumer] cannot get price for %s, skip", ticker)
                continue

            logger.info("[consumer] placing BUY %s score=%.0f price=%.4f budget=%.0f",
                        ticker, sig["score"], price, BUDGET_USD)

            def _on_fill(qty: int, avg_price: float, _ticker=ticker, _sig=sig, _oid_holder=[None]):
                logger.info("[consumer] filled BUY %s qty=%d avg=%.4f", _ticker, qty, avg_price)
                open_position(_ticker, _sig["id"], _oid_holder[0] or "", qty, avg_price)

            order_id = trader.place_entry_order(
                ticker=ticker,
                budget_usd=BUDGET_USD,
                ref_price=price,
                on_fill=_on_fill,
            )
            if order_id:
                _on_fill.__closure__[3].cell_contents[0] = order_id  # patch _oid_holder
                slots_left -= 1

        # ── 持仓止盈/止损/到期检查 ────────────────────────────────────
        positions = get_open_positions()
        logger.info("[consumer] checking %d open positions", len(positions))

        for pos in positions:
            if pos["status"] == "closing":
                continue
            ticker = pos["ticker"]
            price = trader.get_price(ticker)
            if not price:
                continue

            reason = check_exit_signal(pos, price)
            if not reason:
                continue

            logger.info("[consumer] EXIT signal %s for %s price=%.4f entry=%.4f reason=%s",
                        ticker, pos["id"], price, pos["entry_price"] or 0, reason)

            exit_order_id = trader.place_sell_order(
                ticker=ticker,
                qty=pos["qty"],
                ref_price=price,
                purpose=reason,
                on_fill=lambda qty, avg, _pos=pos: close_position(_pos["id"], avg),
            )
            if exit_order_id:
                mark_closing(pos["id"], exit_order_id)

    finally:
        trader.close()


if __name__ == "__main__":
    run()
