"""
国会交易采集器 — DeepSeek 联网搜索版
用 DeepSeek 的 web search 能力直接查询最新国会交易披露，
无需 QuiverQuant API Key 或爬取 Capitol Trades。

DeepSeek 联网搜索: model=deepseek-chat + extra_body={"search_enabled": True}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from collectors.base import BaseCollector, RawEvent
from storage.db import get_checkpoint, set_checkpoint
from config.settings import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL

logger = logging.getLogger(__name__)

# 每位议员的搜索配置
CONGRESS_SEARCH_CONFIGS = {
    "pelosi": {
        "name_cn": "佩洛西",
        "name_en": "Nancy Pelosi",
        "query_zh": "佩洛西 国会交易 股票 最新披露 2026",
        "query_en": "Nancy Pelosi Paul Pelosi stock trades disclosure 2026",
    },
    "tuberville": {
        "name_cn": "图伯维尔",
        "name_en": "Tommy Tuberville",
        "query_zh": "Tommy Tuberville 国会交易 股票 2026",
        "query_en": "Tommy Tuberville congress stock trades 2026",
    },
}

EXTRACT_PROMPT = """你是国会交易分析助手。请从以下搜索结果中提取 {name} 的股票交易信息。

要求：
1. 只提取明确的股票买卖操作（包括期权）
2. 每笔交易单独一条，格式严格为 JSON 数组
3. 只包含 {days} 天内的交易
4. 如果没有找到交易信息，返回空数组 []

输出格式（纯 JSON 数组，无 markdown）：
[
  {{
    "date": "2026-01-16",
    "ticker": "NVDA",
    "company": "NVIDIA Corp",
    "action": "买入",
    "instrument": "股票",
    "amount_range": "$1M-$5M",
    "notes": "行权，LEAPS看涨期权"
  }}
]

action 只能是: 买入 / 卖出 / 行权 / 买入期权 / 卖出期权
instrument 只能是: 股票 / 看涨期权 / 看跌期权 / 其他

搜索内容：
{content}
"""


class CongressDeepSeekCollector(BaseCollector):

    def __init__(self, member_id: str, guru_name: str):
        self.member_id = member_id
        self.guru_name = guru_name
        self.config = CONGRESS_SEARCH_CONFIGS.get(member_id, {
            "name_cn": guru_name,
            "name_en": guru_name,
            "query_zh": f"{guru_name} 国会股票交易 最新 2026",
            "query_en": f"{guru_name} congress stock trades 2026",
        })

    def get_source_name(self) -> str:
        return f"congress_ds:{self.guru_name}"

    def collect(self) -> list[RawEvent]:
        if not DEEPSEEK_API_KEY:
            logger.warning("[Congress-DS] 未配置 DEEPSEEK_API_KEY")
            return []

        checkpoint_key = f"congress_ds:{self.member_id}"
        last_run = get_checkpoint(checkpoint_key)

        # 每12小时最多搜一次（避免浪费token）
        if last_run and last_run.startswith("20"):  # 有效时间戳才节流
            try:
                last_dt = datetime.fromisoformat(last_run)
                if datetime.now() - last_dt < timedelta(hours=12):
                    logger.info(f"[Congress-DS] {self.guru_name}: 距上次搜索不足12小时，跳过")
                    return []
            except ValueError:
                pass

        logger.info(f"[Congress-DS] {self.guru_name}: 开始联网搜索...")

        # 先用中文查，再用英文查，取结果较好的
        raw_zh = self._search(self.config["query_zh"])
        raw_en = self._search(self.config["query_en"])

        combined = f"[中文搜索结果]\n{raw_zh}\n\n[英文搜索结果]\n{raw_en}"
        trades = self._extract_trades(combined)

        set_checkpoint(checkpoint_key, datetime.now().isoformat())

        if not trades:
            logger.info(f"[Congress-DS] {self.guru_name}: 未找到近期交易")
            return []

        events = self._to_events(trades)
        logger.info(f"[Congress-DS] {self.guru_name}: 提取到 {len(events)} 条交易")
        return events

    def _search(self, query: str) -> str:
        """调用 DeepSeek 联网搜索，返回搜索摘要文本"""
        try:
            from openai import OpenAI
            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": query}],
                extra_body={"search_enabled": True},
                max_tokens=2000,
                temperature=0.1,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"[Congress-DS] DeepSeek搜索失败: {e}")
            return ""

    def _extract_trades(self, search_content: str) -> list[dict]:
        """用 DeepSeek 从搜索结果中提取结构化交易数据"""
        if not search_content.strip():
            return []

        try:
            from openai import OpenAI
            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

            prompt = EXTRACT_PROMPT.format(
                name=self.config["name_cn"],
                days=90,
                content=search_content[:4000],
            )

            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1500,
            )

            text = response.choices[0].message.content.strip()
            text = text.replace("```json", "").replace("```", "").strip()

            trades = json.loads(text)
            if not isinstance(trades, list):
                return []

            # 过滤90天内的交易
            cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            return [t for t in trades if t.get("date", "") >= cutoff]

        except json.JSONDecodeError as e:
            logger.error(f"[Congress-DS] JSON解析失败: {e}")
            return []
        except Exception as e:
            logger.error(f"[Congress-DS] 提取失败: {e}")
            return []

    def _to_events(self, trades: list[dict]) -> list[RawEvent]:
        events = []
        for t in trades:
            ticker = t.get("ticker", "")
            action = t.get("action", "")
            instrument = t.get("instrument", "股票")
            date_str = t.get("date", "")
            amount = t.get("amount_range", "")
            company = t.get("company", ticker)
            notes = t.get("notes", "")

            content = (
                f"国会议员 {self.guru_name} {action} {company} ({ticker})\n"
                f"交易日期: {date_str}\n"
                f"交易类型: {action} {instrument}\n"
                f"交易金额: {amount}\n"
                f"备注: {notes}\n"
                f"股票代码: {ticker}"
            )

            try:
                ts = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
            except ValueError:
                ts = datetime.now()

            events.append(RawEvent(
                guru_name=self.guru_name,
                event_type="trade",
                source="congress",
                raw_content=content,
                timestamp=ts,
                url=f"https://www.capitoltrades.com/politicians/{self.member_id}",
                metadata={
                    "action": action,
                    "ticker": ticker,
                    "company": company,
                    "instrument": instrument,
                    "amount": amount,
                    "notes": notes,
                    "trade_date": date_str,
                },
            ))
        return events
