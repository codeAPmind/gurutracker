"""
SQLite 操作封装
"""
from __future__ import annotations

import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from config.settings import DB_PATH

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guru_name TEXT NOT NULL,
    tier TEXT,
    action TEXT NOT NULL,
    ticker TEXT NOT NULL,
    stock_name TEXT,
    reason TEXT,
    position_hint TEXT,
    sentiment TEXT,
    source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    score REAL,
    raw_content TEXT,
    url TEXT,
    event_date TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_signals_guru ON signals(guru_name);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_confidence ON signals(confidence);

CREATE TABLE IF NOT EXISTS holdings_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guru_name TEXT NOT NULL,
    quarter TEXT NOT NULL,
    ticker TEXT NOT NULL,
    cusip TEXT,
    shares BIGINT,
    market_value REAL,
    portfolio_pct REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(guru_name, quarter, ticker)
);

CREATE TABLE IF NOT EXISTS collector_checkpoints (
    collector_name TEXT PRIMARY KEY,
    last_checkpoint TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript(SCHEMA)
    logger.info(f"[DB] 数据库初始化完成: {DB_PATH}")


def save_signal(signal) -> int:
    sql = """
    INSERT INTO signals
        (guru_name, tier, action, ticker, stock_name, reason, position_hint,
         sentiment, source, confidence, score, raw_content, url, event_date)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with _get_conn() as conn:
        cur = conn.execute(sql, (
            signal.guru_name, signal.tier, signal.action, signal.ticker,
            signal.stock_name, signal.reason, signal.position_hint,
            signal.sentiment, signal.source, signal.confidence, signal.score,
            getattr(signal, "raw_content", ""), getattr(signal, "url", ""),
            getattr(signal, "event_date", ""),
        ))
        return cur.lastrowid


def get_recent_signals(guru_name: str = None, ticker: str = None, hours: int = 24) -> list[dict]:
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conditions = ["created_at >= ?"]
    params = [since]

    if guru_name:
        conditions.append("guru_name = ?")
        params.append(guru_name)
    if ticker:
        conditions.append("ticker = ?")
        params.append(ticker)

    sql = f"SELECT * FROM signals WHERE {' AND '.join(conditions)} ORDER BY created_at DESC"
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_today_signals() -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    sql = "SELECT * FROM signals WHERE created_at >= ? ORDER BY score DESC"
    with _get_conn() as conn:
        rows = conn.execute(sql, (today,)).fetchall()
    return [dict(r) for r in rows]


def get_holdings_snapshot(guru_name: str, quarter: str) -> list[dict]:
    sql = "SELECT * FROM holdings_snapshot WHERE guru_name = ? AND quarter = ?"
    with _get_conn() as conn:
        rows = conn.execute(sql, (guru_name, quarter)).fetchall()
    return [dict(r) for r in rows]


def save_holdings_snapshot(guru_name: str, quarter: str, ticker: str, cusip: str = "",
                            shares: int = 0, market_value: float = 0.0, portfolio_pct: float = 0.0):
    sql = """
    INSERT INTO holdings_snapshot (guru_name, quarter, ticker, cusip, shares, market_value, portfolio_pct)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(guru_name, quarter, ticker) DO UPDATE SET
        shares = excluded.shares,
        market_value = excluded.market_value,
        portfolio_pct = excluded.portfolio_pct,
        created_at = CURRENT_TIMESTAMP
    """
    with _get_conn() as conn:
        conn.execute(sql, (guru_name, quarter, ticker, cusip, shares, market_value, portfolio_pct))


def get_checkpoint(collector_name: str) -> str | None:
    sql = "SELECT last_checkpoint FROM collector_checkpoints WHERE collector_name = ?"
    with _get_conn() as conn:
        row = conn.execute(sql, (collector_name,)).fetchone()
    return row["last_checkpoint"] if row else None


def set_checkpoint(collector_name: str, checkpoint: str):
    sql = """
    INSERT INTO collector_checkpoints (collector_name, last_checkpoint, updated_at)
    VALUES (?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(collector_name) DO UPDATE SET
        last_checkpoint = excluded.last_checkpoint,
        updated_at = CURRENT_TIMESTAMP
    """
    with _get_conn() as conn:
        conn.execute(sql, (collector_name, checkpoint))
