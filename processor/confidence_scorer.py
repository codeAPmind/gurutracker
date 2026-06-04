"""
置信度评分模型
综合仓位、数据源可靠性、动作强度、交叉验证四个维度
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    guru_name: str
    tier: str
    action: str
    ticker: str
    stock_name: str
    reason: str
    position_hint: str
    sentiment: str
    source: str
    confidence: str   # "高" / "中" / "低"
    score: float
    raw_content: str = ""
    url: str = ""
    event_date: str = ""   # 交易/事件的实际日期，非执行时间


SOURCE_SCORES = {
    "sec_13f": 30,
    "sec_form4": 28,
    "fmp_insider": 27,
    "fmp_13f": 25,
    "ark_trades": 25,
    "congress": 22,
    "xueqiu": 15,
    "x": 12,
}

ACTION_SCORES = {
    "建仓": 20, "清仓": 20,
    "买入": 18, "卖出": 18,
    "增持": 15, "减持": 15,
    "看好": 8, "看空": 8,
}

TIER_BONUS = {
    "长期": 5,
    "中期": 3,
    "短期": 0,
}


def score_signal(signal_data: dict, guru_config: dict, existing_signals: list, event_date: str = "") -> Signal:
    score = 0.0

    # 1. 仓位权重 (0-30分)
    position = signal_data.get("position_hint", "未知")
    if position == "重仓":
        score += 30
    elif position == "试水":
        score += 10
    else:
        score += 15

    # 2. 数据源可靠性 (0-30分)
    source = signal_data.get("source", "")
    score += SOURCE_SCORES.get(source, 10)

    # 3. 动作强度 (0-20分)
    action = signal_data.get("action", "")
    score += ACTION_SCORES.get(action, 5)

    # 4. 交叉验证 (0-20分) — 近7天其他大佬也操作了同一只
    ticker = signal_data.get("ticker", "")
    if ticker:
        cross_count = sum(
            1 for s in existing_signals
            if s.get("ticker") == ticker and s.get("guru_name") != signal_data.get("guru_name")
        )
        score += min(cross_count * 10, 20)

    # 5. 大佬层级加成 (0-5分)
    tier = guru_config.get("tier", "中期")
    score += TIER_BONUS.get(tier, 0)

    # 最终评级
    if score >= 65:
        confidence = "高"
    elif score >= 40:
        confidence = "中"
    else:
        confidence = "低"

    logger.debug(f"[评分] {signal_data.get('guru_name')} {action} {ticker}: {score:.1f}分 → {confidence}")

    return Signal(
        guru_name=signal_data.get("guru_name", ""),
        tier=tier,
        action=action,
        ticker=ticker,
        stock_name=signal_data.get("stock_name", ticker),
        reason=signal_data.get("reason", ""),
        position_hint=position,
        sentiment=signal_data.get("sentiment", "中性"),
        source=source,
        confidence=confidence,
        score=round(score, 1),
        raw_content=signal_data.get("raw_content", ""),
        url=signal_data.get("url", ""),
        event_date=event_date or signal_data.get("event_date", ""),
    )
