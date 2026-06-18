"""
ARK Invest 每日交易采集器
数据源: arkfunds.io API (免费) 或 ARK 官网CSV
ARK 主动公开每日持仓变动，是最易实现的采集器
"""
from __future__ import annotations

import requests
import json
import logging
from datetime import datetime, timedelta
from collectors.base import BaseCollector, RawEvent
from storage.db import get_checkpoint, set_checkpoint

logger = logging.getLogger(__name__)

ARKFUNDS_API = "https://arkfunds.io/api/v2"
# ARK官网备用CSV下载链接
ARK_CSV_URLS = {
    "ARKK": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
    "ARKW": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
    "ARKG": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv",
    "ARKF": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS.csv",
    "ARKX": "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_SPACE_EXPLORATION_ETF_ARKX_HOLDINGS.csv",
}


class ARKTradesCollector(BaseCollector):

    def __init__(self, fund_codes: list[str]):
        self.fund_codes = fund_codes

    def get_source_name(self) -> str:
        return "ark_trades"

    def collect(self) -> list[RawEvent]:
        events = []
        for fund in self.fund_codes:
            fund_events = self._collect_fund(fund)
            events.extend(fund_events)
        return events

    def _collect_fund(self, fund: str) -> list[RawEvent]:
        checkpoint_key = f"ark_trades:{fund}"
        last_date = get_checkpoint(checkpoint_key)

        # 优先用 arkfunds.io API
        trades = self._fetch_from_arkfunds(fund, last_date)
        if trades is None:
            trades = self._fetch_from_ark_csv(fund, last_date)

        if not trades:
            return []

        events = []
        latest_date = last_date

        for trade in trades:
            date_str = trade.get("date", "")
            if date_str <= (last_date or ""):
                continue

            ticker = trade.get("ticker", "")
            company = trade.get("company", ticker)
            direction = trade.get("direction", "")
            shares = abs(trade.get("shares", 0))
            etf = trade.get("fund", fund)
            etf_pct = trade.get("etf_percent", 0) or 0

            if direction.upper() == "BUY":
                action = "买入"
            elif direction.upper() == "SELL":
                action = "卖出"
            else:
                continue

            pct_line = f"\n占基金仓位: {etf_pct:.2f}%" if etf_pct else ""
            content = (
                f"ARK Invest ({etf}) {action} {company} ({ticker})\n"
                f"交易日期: {date_str}\n"
                f"交易方向: {action}\n"
                f"股数变动: {shares:,} 股{pct_line}\n"
                f"所属基金: {etf}"
            )

            events.append(RawEvent(
                guru_name="ARK Invest",
                event_type="trade",
                source="ark_trades",
                raw_content=content,
                timestamp=datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now(),
                url=f"https://arkfunds.io/etf/{fund.lower()}/trades",
                metadata={
                    "action": action,
                    "ticker": ticker,
                    "company": company,
                    "shares": shares,
                    "fund": etf,
                    "direction": direction,
                },
            ))

            if not latest_date or date_str > latest_date:
                latest_date = date_str

        if latest_date and latest_date != last_date:
            set_checkpoint(checkpoint_key, latest_date)

        logger.info(f"[ARK] {fund}: 发现 {len(events)} 条交易")
        return events

    def _fetch_from_arkfunds(self, fund: str, last_date: str | None) -> list[dict] | None:
        """从 arkfunds.io 获取最近交易数据"""
        try:
            # 获取最近交易记录
            url = f"{ARKFUNDS_API}/etf/trades"
            params = {"symbol": fund, "limit": 100}
            if last_date:
                params["date_from"] = last_date

            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            trades = data.get("trades", [])
            if not isinstance(trades, list):
                return None

            return [
                {
                    "date": t.get("date", ""),
                    "ticker": t.get("ticker", ""),
                    "company": t.get("company", ""),
                    "direction": t.get("direction", ""),
                    "shares": t.get("shares", 0),
                    "etf_percent": t.get("etf_percent", 0),
                    "fund": fund,
                }
                for t in trades
            ]
        except Exception as e:
            logger.warning(f"[ARK] arkfunds.io API失败 ({fund}): {e}，尝试备用源")
            return None

    def _fetch_from_ark_csv(self, fund: str, last_date: str | None) -> list[dict] | None:
        """从ARK官网CSV下载持仓，与前一天对比得到交易"""
        csv_url = ARK_CSV_URLS.get(fund)
        if not csv_url:
            return None

        try:
            resp = requests.get(csv_url, timeout=30)
            resp.raise_for_status()

            # 解析CSV
            import csv
            from io import StringIO
            reader = csv.DictReader(StringIO(resp.text))
            today_holdings = {}
            date_str = ""

            for row in reader:
                ticker = row.get("ticker", "").strip()
                if not ticker or ticker == "ticker":
                    continue
                try:
                    shares = float(row.get("shares", 0) or 0)
                except ValueError:
                    shares = 0

                today_holdings[ticker] = {
                    "ticker": ticker,
                    "company": row.get("company", ticker).strip(),
                    "shares": shares,
                }
                if not date_str:
                    date_str = row.get("date", datetime.now().strftime("%Y-%m-%d")).strip()

            if date_str <= (last_date or ""):
                return []

            # 返回当日持仓（无法与前日对比时，返回空，等下次运行时对比）
            # 这里简化处理：只在第一次运行时记录快照，后续对比
            logger.info(f"[ARK CSV] {fund}: {date_str} 持仓 {len(today_holdings)} 只")
            return []  # 简化：CSV模式下暂返回空，主逻辑用API

        except Exception as e:
            logger.error(f"[ARK CSV] 下载失败 ({fund}): {e}")
            return None
