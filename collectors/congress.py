"""
美国国会议员交易采集器
数据源:
  1. Capitol Trades API (https://www.capitoltrades.com) — 免费网页
  2. QuiverQuant API — 需要API Key
"""
from __future__ import annotations

import requests
import json
import logging
from datetime import datetime, timedelta
from collectors.base import BaseCollector, RawEvent
from storage.db import get_checkpoint, set_checkpoint
from config.settings import QUIVER_API_KEY

logger = logging.getLogger(__name__)

CAPITOL_TRADES_API = "https://www.capitoltrades.com/api"
QUIVER_API = "https://api.quiverquant.com/beta"


class CongressCollector(BaseCollector):

    def __init__(self, member_id: str, guru_name: str):
        self.member_id = member_id
        self.guru_name = guru_name

    def get_source_name(self) -> str:
        return f"congress:{self.guru_name}"

    def collect(self) -> list[RawEvent]:
        checkpoint_key = f"congress:{self.member_id}"
        last_trade_id = get_checkpoint(checkpoint_key)

        trades = []
        if QUIVER_API_KEY:
            trades = self._fetch_from_quiver()
        else:
            trades = self._fetch_from_capitol_trades()

        if not trades:
            return []

        events = []
        new_last = last_trade_id

        for trade in trades:
            trade_id = str(trade.get("id", trade.get("filed_at", "")))
            if trade_id == last_trade_id:
                break

            ticker = trade.get("ticker", "")
            transaction = trade.get("transaction", trade.get("type", ""))
            amount = trade.get("amount", trade.get("size", ""))
            trans_date = trade.get("traded_at", trade.get("transaction_date", ""))

            if "Purchase" in transaction or "buy" in transaction.lower():
                action = "买入"
            elif "Sale" in transaction or "sell" in transaction.lower():
                action = "卖出"
            else:
                action = transaction

            content = (
                f"国会议员 {self.guru_name} {action} {ticker}\n"
                f"交易日期: {trans_date}\n"
                f"交易类型: {transaction}\n"
                f"交易金额: {amount}\n"
                f"股票代码: {ticker}"
            )

            try:
                ts = datetime.strptime(trans_date[:10], "%Y-%m-%d") if trans_date else datetime.now()
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
                    "transaction": transaction,
                    "amount": amount,
                    "trade_date": trans_date,
                },
            ))

            if not new_last or new_last == last_trade_id:
                new_last = trade_id

        if new_last and new_last != last_trade_id:
            set_checkpoint(checkpoint_key, new_last)

        logger.info(f"[Congress] {self.guru_name}: 发现 {len(events)} 条交易")
        return events

    def _fetch_from_quiver(self) -> list[dict]:
        try:
            # QuiverQuant API
            url = f"{QUIVER_API}/historical/congresstrading/{self.member_id}"
            headers = {"Authorization": f"Bearer {QUIVER_API_KEY}"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            return [
                {
                    "id": f"{d.get('Representative', '')}-{d.get('TransactionDate', '')}-{d.get('Ticker', '')}",
                    "ticker": d.get("Ticker", ""),
                    "transaction": d.get("Transaction", ""),
                    "amount": d.get("Amount", ""),
                    "traded_at": d.get("TransactionDate", ""),
                }
                for d in data
                if d.get("TransactionDate", "") >= cutoff
            ]
        except Exception as e:
            logger.error(f"[Congress] QuiverQuant API失败: {e}")
            return []

    def _fetch_from_capitol_trades(self) -> list[dict]:
        """从Capitol Trades获取数据（无需API Key）"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
            }
            # Capitol Trades提供politician页面
            url = f"https://www.capitoltrades.com/politicians/{self.member_id}"
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()

            # 页面内有JSON数据，需要解析
            import re
            # 尝试找到内嵌的JSON数据
            match = re.search(r'"trades"\s*:\s*(\[.*?\])', resp.text, re.DOTALL)
            if match:
                trades_data = json.loads(match.group(1))
                return trades_data[:20]  # 最近20条
        except Exception as e:
            logger.warning(f"[Congress] Capitol Trades获取失败: {e}")
        return []
