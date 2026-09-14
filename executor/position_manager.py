"""
Guru Tracker 持仓管理（SQLite）+ 止盈/止损/到期检查

策略参数（可通过 env 覆盖）：
  GURU_TAKE_PROFIT_PCT  默认 0 = **禁用止盈**（>0 时才启用）
  GURU_STOP_LOSS_PCT    默认 -0.15 (-15%)
  GURU_MAX_HOLD_DAYS    默认 20（交易日），D1下仅作安全兜底
  GURU_BOLL_PERIOD      默认 20，布林带中轨周期（主出场依据）
  GURU_BUDGET_USD       每笔买入金额，默认 1000

出场规则演进（2026-09-09，两轮修正）：
第1轮：原 +8%/-5% 是在"持满10日无止盈损"的回测目标上选的，与实盘严重脱节——
       实测96%仓位提前离场(均持3.6日)，胜率48%、中位-1.43%、t=0.87。
第2轮：固定持有天数本身没有市场含义。改用布林带中轨(均值回归目标)出场。

完整对比（n=13~19，含滑点/5仓位约束/already_holding）：
  A  纯持10天          : 均值+8.45% t=1.49 胜率69% 最差-22.3% 期末$6,098
  B  +15%/-15%         : 均值+5.27% t=1.58 胜率65% 最差-17.9% 期末$5,895
  C  持10日+(-15%)止损  : 均值+8.72% t=1.63 胜率71% 最差-17.9% 期末$6,221
  D1 回中轨+(-15%)止损  : 均值+5.86% t=2.31 胜率79% 最差-16.1% 期末$6,114 ← 采用

选 D1 的理由（不是因为收益更高——总收益与C基本持平）：
1. 有市场逻辑：买入时%b中位11%(贴下轨)，回中轨=均值回归完成；固定N日是人为设定
2. 稳定性显著更好：胜率71%→79%，最大连亏2→1笔
3. 样本外更稳：不利半段 C为-5.60% / D1仅-0.32%（两者利润都集中在后半段）
4. 槽位周转快：平均持有9.5→4.9日，同一信号流多成交36%(14→19笔)，
   验证速度也更快（更快累积到80~100笔门槛）

⚠️ t=2.31 看似过了门槛，但这是在同一份15~20笔样本上搜索了约33种配置后得到的，
属于 p-hacking，不能视为通过统计检验。门槛已改为前瞻样本外验证。
当前仅可用于 SIMULATE。
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
MAX_HOLD_DAYS = int(os.getenv("GURU_MAX_HOLD_DAYS", "20"))  # D1下仅作兜底
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


def check_exit_signal(pos: dict, current_price: float,
                      boll_mid: Optional[float] = None) -> Optional[str]:
    """返回 'stop_loss' / 'boll_mid' / 'max_hold' / 'take_profit' / None

    方案D1（2026-09-09 采用）出场优先级：
      1. stop_loss  跌破 STOP_LOSS_PCT(-15%)   —— 封尾部风险
      2. boll_mid   价格回升到布林带中轨(MA20) —— 均值回归完成，主出场路径(约90%)
      3. max_hold   持有超 MAX_HOLD_DAYS(20日) —— 安全兜底，防止无限持有
      take_profit 默认禁用（TAKE_PROFIT_PCT=0）

    为什么用中轨而非固定天数：买入时 %b 中位仅11%（贴近下轨），
    回到中轨即均值回归完成，有市场含义；固定N日纯属人为设定。
    实测：胜率 71%→79%，最大连亏 2→1 笔，样本外不利半段 -5.6%→-0.3%。
    详见 STRATEGY.md §5.6。
    """
    entry = pos.get("entry_price")
    if not entry or entry <= 0:
        return None
    chg = (current_price - entry) / entry

    if chg <= STOP_LOSS_PCT:
        return "stop_loss"
    if TAKE_PROFIT_PCT > 0 and chg >= TAKE_PROFIT_PCT:
        return "take_profit"
    if boll_mid and boll_mid > 0 and current_price >= boll_mid:
        return "boll_mid"

    entry_date = pos.get("entry_date")
    if entry_date and _trading_days_held(entry_date) >= MAX_HOLD_DAYS:
        return "max_hold"
    return None
