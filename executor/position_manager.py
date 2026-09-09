"""
Guru Tracker 持仓管理（SQLite）+ 止盈/止损/到期检查

策略参数（可通过 env 覆盖，基于 2026-06~09 历史信号 sweep 出的参数）：
  GURU_TAKE_PROFIT_PCT  默认 0.08 (+8%)
  GURU_STOP_LOSS_PCT    默认 -0.05 (-5%)
  GURU_MAX_HOLD_DAYS    默认 10（交易日），到期无论盈亏市价平仓
  GURU_BUDGET_USD       每笔买入金额，默认 1000

sweep 依据（回调>=20% + 仓位>0.10%，持仓10日，n=46）：
  胜率 63%，均值 +6.8%，Sharpe 2.3，PF 2.14
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

TAKE_PROFIT_PCT = float(os.getenv("GURU_TAKE_PROFIT_PCT", "0.08"))
STOP_LOSS_PCT = float(os.getenv("GURU_STOP_LOSS_PCT", "-0.05"))
MAX_HOLD_DAYS = int(os.getenv("GURU_MAX_HOLD_DAYS", "10"))
BUDGET_USD = float(os.getenv("GURU_BUDGET_USD", "1000"))

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
        return cur.lastrowid


def mark_closing(pos_id: int, exit_order_id: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE guru_positions SET status='closing', exit_order_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (exit_order_id, pos_id),
        )


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
        logger.info("[pos] closed pos_id=%d exit=%.4f pnl=%.2f%%", pos_id, exit_price, pnl_pct or 0)


def get_open_positions() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM guru_positions WHERE status IN ('open', 'closing') ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def already_holding(ticker: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT id FROM guru_positions WHERE ticker=? AND status IN ('open', 'closing') LIMIT 1",
            (ticker,),
        ).fetchone()
    return row is not None


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

    max_hold: 持仓达到 MAX_HOLD_DAYS 交易日，无论盈亏强制平仓
    （sweep 依据：10日持有窗口的胜率/收益指标是在此周期下验证的，
    超期持有会脱离已验证的参数区间）
    """
    entry = pos.get("entry_price")
    if not entry or entry <= 0:
        return None
    chg = (current_price - entry) / entry
    if chg >= TAKE_PROFIT_PCT:
        return "take_profit"
    if chg <= STOP_LOSS_PCT:
        return "stop_loss"

    entry_date = pos.get("entry_date")
    if entry_date and _trading_days_held(entry_date) >= MAX_HOLD_DAYS:
        return "max_hold"
    return None
