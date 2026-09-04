"""
Guru Tracker 信号消费 & 自动下单

调度（cron 09:35 ET，周一至周五）：
  python -m executor.signal_consumer

逻辑：
1. 读取昨日（上一个交易日）score >= MIN_SCORE 的 buy 信号
2. 跳过已持仓的 ticker
3. 按 GURU_BUDGET_USD 限价买入
4. 检查所有 open 持仓，触发止盈/止损
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from config.settings import DB_PATH
from executor.position_manager import (
    BUDGET_USD, already_holding, check_exit_signal, close_position,
    get_open_positions, init_db, mark_closing, open_position,
)
from executor.futu_trader import GuruFutuTrader

logger = logging.getLogger(__name__)

MIN_SCORE = float(os.getenv("GURU_MIN_SCORE", "50"))


def _us_today() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except ImportError:
        tz = timezone(timedelta(hours=-5))
    return datetime.now(tz).strftime("%Y-%m-%d")


def _fetch_pending_signals() -> list[dict]:
    """返回昨日及今日 score >= MIN_SCORE 的 buy 信号（去重 ticker 取最高分）"""
    today = _us_today()
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT id, ticker, score, action, guru_name, created_at
               FROM signals
               WHERE score >= ?
                 AND action IN ('buy', '买入')
                 AND DATE(created_at) >= ?
               ORDER BY score DESC""",
            (MIN_SCORE, cutoff),
        ).fetchall()
    finally:
        con.close()

    # 每个 ticker 只保留最高分信号
    seen: dict[str, dict] = {}
    for r in rows:
        t = r["ticker"]
        if t not in seen or r["score"] > seen[t]["score"]:
            seen[t] = dict(r)
    return list(seen.values())


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    init_db()

    signals = _fetch_pending_signals()
    logger.info("[consumer] found %d buy signals (score>=%.0f)", len(signals), MIN_SCORE)

    trader = GuruFutuTrader()
    try:
        trader.connect()
    except Exception as e:
        logger.error("[consumer] futu connect failed: %s", e)
        return

    try:
        # ── 新信号买入 ────────────────────────────────────────────────
        for sig in signals:
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

            order_id = None

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

        # ── 持仓止盈/止损检查 ─────────────────────────────────────────
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
