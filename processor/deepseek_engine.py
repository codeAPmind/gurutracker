"""
DeepSeek API 封装
DeepSeek 兼容 OpenAI 格式，V3: 输入￥1/百万token，输出￥2/百万token
"""
from __future__ import annotations

import json
import logging
from config.settings import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个专业的投资信号分析助手。你的任务是从投资者的发言或交易记录中提取结构化的投资信号。

规则：
1. 只提取明确的投资相关信息，不要过度解读
2. 如果内容与投资无关，返回 {"has_signal": false}
3. ticker 使用美股代码（如 AAPL, TSLA），A股使用数字代码（如 600519）
4. position_hint 根据上下文判断仓位大小：
   - "重仓"：明确提到大量买入、核心持仓、重仓
   - "试水"：提到小量买入、观察仓、试水
   - "未知"：无法判断
5. 对于SEC/Form4/ARK等交易记录，直接从内容中提取，无需额外推断
6. 输出纯 JSON，不要包含 markdown 代码块标记

输出格式（严格 JSON）：
{
  "has_signal": true,
  "action": "买入/卖出/增持/减持/建仓/清仓/看好/看空",
  "ticker": "股票代码（不确定时用公司名代替）",
  "stock_name": "股票中文名或英文名",
  "reason": "一句话总结原因（50字以内）",
  "position_hint": "重仓/试水/未知",
  "sentiment": "看好/看空/中性",
  "raw_quote": "原文关键句（30字以内）"
}"""


def extract_signal(raw_content: str, guru_name: str, event_type: str) -> dict:
    """
    调用 DeepSeek 从原始内容中提取投资信号
    返回结构化信号字典，或 {"has_signal": false}
    """
    if not DEEPSEEK_API_KEY:
        logger.warning("未配置 DEEPSEEK_API_KEY，使用规则提取")
        return _rule_based_extract(raw_content, guru_name, event_type)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

        user_prompt = f"""投资者: {guru_name}
事件类型: {event_type}
原始内容:
{raw_content}

请提取投资信号。"""

        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=500,
        )

        text = response.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        logger.debug(f"[DeepSeek] {guru_name}: {result}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"[DeepSeek] JSON解析失败: {e}, 原文: {text[:200]}")
        return {"has_signal": False}
    except Exception as e:
        logger.error(f"[DeepSeek] API调用失败: {e}")
        return _rule_based_extract(raw_content, guru_name, event_type)


def _rule_based_extract(raw_content: str, guru_name: str, event_type: str) -> dict:
    """
    规则based提取（DeepSeek不可用时的降级方案）
    对于SEC/ARK这类结构化数据，直接从metadata提取
    """
    content_lower = raw_content.lower()

    # 从结构化内容中提取动作
    action = None
    if "建仓" in raw_content:
        action = "建仓"
    elif "清仓" in raw_content:
        action = "清仓"
    elif "增持" in raw_content:
        action = "增持"
    elif "减持" in raw_content:
        action = "减持"
    elif "买入" in raw_content or "purchase" in content_lower or "buy" in content_lower:
        action = "买入"
    elif "卖出" in raw_content or "sale" in content_lower or "sell" in content_lower:
        action = "卖出"

    if not action:
        return {"has_signal": False}

    # 提取ticker（简单规则）
    import re
    ticker_match = re.search(r'\(([A-Z]{1,5})\)', raw_content)
    ticker = ticker_match.group(1) if ticker_match else ""

    # 提取股票名
    lines = raw_content.split("\n")
    stock_name = ""
    for line in lines:
        for kw in ["股票:", "买入", "卖出", "增持", "减持", "建仓", "清仓"]:
            if kw in line and len(line) < 60:
                stock_name = line.replace(kw, "").strip()
                break
        if stock_name:
            break

    return {
        "has_signal": True,
        "action": action,
        "ticker": ticker,
        "stock_name": stock_name or ticker,
        "reason": f"{guru_name}{action}，来自{event_type}记录",
        "position_hint": "未知",
        "sentiment": "看好" if action in ("买入", "建仓", "增持") else "看空",
        "raw_quote": raw_content[:80],
    }
