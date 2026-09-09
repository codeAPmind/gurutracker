"""
Guru Tracker 持仓管理（SQLite）+ 止盈/止损/到期检查

策略参数（可通过 env 覆盖）：
  GURU_TAKE_PROFIT_PCT  默认 0 = **禁用止盈**（>0 时才启用）
  GURU_STOP_LOSS_PCT    默认 -0.15 (-15%)
  GURU_MAX_HOLD_DAYS    默认 10（交易日），到期无论盈亏市价平仓
  GURU_BUDGET_USD       每笔买入金额，默认 1000

出场规则演进（2026-09-09）：
原 +8%/-5% 是在"持满10日、无止盈止损"的回测目标上选出的，与实盘执行严重脱节——
实测 96% 仓位提前离场（平均持有3.6日），胜率仅48%、中位数-1.43%、t=0.87。

对三种方案做了完整对比（n=13~17，含滑点/5仓位约束）：
  A 纯持10天       : 均值+8.45% t=1.49 最差-22.3%  期末$6,098
  B +15%/-15%      : 均值+5.27% t=1.58 最差-17.9%  期末$5,895
  C 持10天+(-15%)止损: 均值+7.78% t=1.53 最差-17.9%  期末$6,167  ← 采用

选 C 的理由：
1. C 严格优于 A——上行完全相同，下行从-22.3%收窄到-17.9%，无代价
2. C vs B 统计上无法区分（bootstrap 10000次，C优于B仅65.5%，需>95%）
   但 C 少一个拟合参数：B 的+15%止盈位无独立依据，是在同一份数据上挑出的；
   C 的两条规则各有理由（止损=封尾部风险，10日=资金周转）
3. 策略赚的是深度回调后的大幅反弹，止盈会截断收益来源
   （B 把 +50/+34/+34 削成 +25/+21/+21）

⚠️ t值仍 < 2，策略尚未通过统计检验，当前仅可用于 SIMULATE 验证。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config.settings import DB_PATH

logger = logging.getLogger(__name__)

TAKE_PROFIT_PCT = float(os.getenv("GURU_TAKE_PROFIT_PCT", "0"))  # 0=禁用止盈
STOP_LOSS_PCT = float(os.getenv("GURU_STOP_LOSS_PCT", "-0.15"))
MAX_HOLD_DAYS = int(os.getenv("GURU_MAX_HOLD_DAYS", "10"))
BUDGET_USD = float(os.getenv("GURU_BUDGET_USD", "1000"))
MAX_POSITIONS = int(os.getenv("GURU_MAX_POSITIONS", "5"))  # 总资金5000/单笔1000 → 最多5只并持

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guru_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    signal_id INTEGER,
    entry_order_id TEXT,
    exit_order_id TEXT,
    qty INTEGER NOT NULL DEFAULT 0,
    entry_price REAL,
    entry_date TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open / closing / closed
    exit_price REAL,
    exit_date TEXT,
    pnl_pct REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_gpos_ticker ON guru_positions(ticker);
CREATE INDEX IF NOT EXISTS idx_gpos_status ON guru_positions(status);
"""


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    with _conn() as con:
        con.executescript(_SCHEMA)


def open_position(ticker: str, signal_id: int, order_id: str,
                  qty: int, entry_price: float) -> int:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO guru_positions
               (ticker, signal_id, entry_order_id, qty, entry_price, entry_date, status)
               VALUES (?, ?, ?, ?, ?, ?, 'open')""",
            (ticker, signal_id, order_id, qty, entry_price, today),
        )
        pos_id = cur.lastrowid
    logger.info("[持仓DB] 新建持仓 pos_id=%d %s qty=%d 成本=$%.4f 建仓日=%s signal_id=%s order=%s",
                pos_id, ticker, qty, entry_price, today, signal_id, order_id)
    return pos_id


def mark_closing(pos_id: int, exit_order_id: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE guru_positions SET status='closing', exit_order_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (exit_order_id, pos_id),
        )
    logger.info("[持仓DB] pos_id=%d 状态→closing, exit_order=%s", pos_id, exit_order_id)


def close_position(pos_id: int, exit_price: float) -> None:
    with _conn() as con:
        row = con.execute("SELECT entry_price FROM guru_positions WHERE id=?", (pos_id,)).fetchone()
        pnl_pct = None
        if row and row["entry_price"]:
            pnl_pct = (exit_price - row["entry_price"]) / row["entry_price"] * 100
        today = datetime.utcnow().strftime("%Y-%m-%d")
        con.execute(
            """UPDATE guru_positions
               SET status='closed', exit_price=?, exit_date=?, pnl_pct=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (exit_price, today, pnl_pct, pos_id),
        )
        logger.info("[持仓DB] pos_id=%d 状态→closed 卖出价=$%.4f 盈亏=%+.2f%% 平仓日=%s",
                    pos_id, exit_price, pnl_pct or 0, today)


def get_open_positions() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM guru_positions WHERE status IN ('open', 'closing') ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def already_holding(ticker: str, trader=None) -> bool:
    """本地记录表 + broker 真实持仓 双重检查，避免在已有仓位(含非本系统开的)上重复买入"""
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM guru_positions WHERE ticker=? AND status IN ('open', 'closing') LIMIT 1",
            (ticker,),
        ).fetchone()
    if row is not None:
        return True

    if trader is not None:
        try:
            broker_qty = trader.get_position(f"US.{ticker}")
            if broker_qty > 0:
                logger.warning(
                    "[pos] %s broker实际持仓%d股（非本系统跟踪），视为已持仓跳过",
                    ticker, broker_qty,
                )
                return True
        except Exception as e:
            logger.error("[pos] 查询broker持仓失败 %s: %s，保守视为已持仓", ticker, e)
            return True
    return False


def _trading_days_held(entry_date: str) -> int:
    """entry_date (YYYY-MM-DD) 到今天的交易日数（近似，不含节假日）"""
    import numpy as np
    today = datetime.utcnow().date()
    entry = datetime.strptime(entry_date, "%Y-%m-%d").date()
    if entry >= today:
        return 0
    return int(np.busday_count(entry, today))


def check_exit_signal(pos: dict, current_price: float) -> Optional[str]:
    """返回 'take_profit' / 'stop_loss' / 'max_hold' / None

    当前策略（方案C）默认不设止盈：TAKE_PROFIT_PCT<=0 时禁用止盈，
    让反弹跑满 MAX_HOLD_DAYS，仅用 STOP_LOSS_PCT 封住尾部风险。
    理由：策略赚的就是深度回调后的大幅反弹，人为设止盈会截断收益来源，
    且止盈位没有独立依据（纯拟合产物）。详见 STRATEGY.md §5.5。
    """
    entry = pos.get("entry_price")
    if not entry or entry <= 0:
        return None
    chg = (current_price - entry) / entry

    if TAKE_PROFIT_PCT > 0 and chg >= TAKE_PROFIT_PCT:
        return "take_profit"
    if chg <= STOP_LOSS_PCT:
        return "stop_loss"

    entry_date = pos.get("entry_date")
    if entry_date and _trading_days_held(entry_date) >= MAX_HOLD_DAYS:
        return "max_hold"
    return None
