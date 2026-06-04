"""
去重逻辑
同一大佬 + 同一股票 + 同一动作 + 24小时内 → 重复
"""
from __future__ import annotations

import logging
from storage.db import get_recent_signals

logger = logging.getLogger(__name__)


def is_duplicate(signal_data: dict) -> bool:
    guru_name = signal_data.get("guru_name", "")
    ticker = signal_data.get("ticker", "")
    action = signal_data.get("action", "")

    if not ticker:
        return False

    recent = get_recent_signals(guru_name=guru_name, ticker=ticker, hours=24)
    for existing in recent:
        if existing.get("action") == action:
            logger.debug(f"[去重] 跳过重复: {guru_name} {action} {ticker}")
            return True
    return False
