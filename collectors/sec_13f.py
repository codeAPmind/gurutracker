"""
SEC 13F 季度持仓采集器
数据源: SEC EDGAR (免费，无需API Key)
13F 每季度提交一次，45天延迟，每周检查一次即可
"""
from __future__ import annotations

import requests
import xml.etree.ElementTree as ET
import time
import json
import logging
from datetime import datetime
from collectors.base import BaseCollector, RawEvent
from storage.db import get_holdings_snapshot, save_holdings_snapshot, get_checkpoint, set_checkpoint

logger = logging.getLogger(__name__)

SEC_HEADERS = {
    "User-Agent": "GuruTracker research@gurutracker.local",
    "Accept-Encoding": "gzip, deflate",
}

EDGAR_API = "https://data.sec.gov/submissions"
EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"


class SEC13FCollector(BaseCollector):

    def __init__(self, cik: str, guru_name: str):
        self.cik = cik.lstrip("0").zfill(10)  # 统一格式为10位
        self.guru_name = guru_name

    def get_source_name(self) -> str:
        return f"sec_13f:{self.guru_name}"

    def collect(self) -> list[RawEvent]:
        # 获取最新13F文件
        filing = self._get_latest_13f()
        if not filing:
            logger.info(f"[SEC 13F] {self.guru_name}: 未找到新的13F文件")
            return []

        accession_no, period = filing["accessionNumber"], filing["period"]

        # 检查是否已处理过这份文件
        checkpoint_key = f"sec_13f:{self.cik}"
        last_accession = get_checkpoint(checkpoint_key)
        if last_accession == accession_no:
            logger.info(f"[SEC 13F] {self.guru_name}: {period} 已处理，跳过")
            return []

        # 下载并解析持仓表
        holdings = self._parse_holdings(accession_no)
        if not holdings:
            return []

        prev_quarter = self._prev_quarter(period)
        has_prev_snapshot = bool(get_holdings_snapshot(self.guru_name, prev_quarter))

        # 保存本季度快照（无论如何都要存）
        self._save_snapshot(holdings, period)
        set_checkpoint(checkpoint_key, accession_no)

        if not has_prev_snapshot:
            # 首次导入：只建立基准快照，不生成信号（否则全部持仓都会误报为"建仓"）
            logger.info(f"[SEC 13F] {self.guru_name}: {period} 首次导入，已建立基准快照（{len(holdings)} 只股票），下次更新时才生成信号")
            return []

        # 与上季度对比，生成变动事件
        events = self._diff_holdings(holdings, period)
        logger.info(f"[SEC 13F] {self.guru_name}: {period} 发现 {len(events)} 条变动")
        return events

    def _get_latest_13f(self) -> dict | None:
        url = f"{EDGAR_API}/CIK{self.cik}.json"
        try:
            resp = requests.get(url, headers=SEC_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"[SEC 13F] 获取提交记录失败 CIK={self.cik}: {e}")
            return None

        filings = data.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        accessions = filings.get("accessionNumber", [])
        periods = filings.get("reportDate", [])
        dates = filings.get("filingDate", [])

        for i, form in enumerate(forms):
            if form == "13F-HR":
                return {
                    "accessionNumber": accessions[i].replace("-", ""),
                    "period": periods[i],
                    "filingDate": dates[i],
                }
        return None

    def _parse_holdings(self, accession_no: str) -> list[dict]:
        cik_clean = self.cik.lstrip("0")
        acc_formatted = f"{accession_no[:10]}-{accession_no[10:12]}-{accession_no[12:]}"
        base_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{accession_no}"
        time.sleep(0.1)

        # 从 index.htm 解析真实的 informationTable XML 文件名
        info_table_url = self._find_info_table_url(base_url, acc_formatted)
        if not info_table_url:
            logger.error(f"[SEC 13F] 找不到 informationTable 文件: {accession_no}")
            return []

        try:
            time.sleep(0.1)
            resp = requests.get(info_table_url, headers=SEC_HEADERS, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[SEC 13F] 下载持仓表失败: {e}")
            return []

        return self._parse_xml(resp.text)

    def _find_info_table_url(self, base_url: str, acc_formatted: str) -> str | None:
        """从 filing index 页面找到真实的 informationTable XML 文件名"""
        import re
        index_url = f"https://www.sec.gov/Archives/edgar/data/{self.cik.lstrip('0')}/{acc_formatted.replace('-','')}/{acc_formatted}-index.htm"
        try:
            resp = requests.get(index_url, headers=SEC_HEADERS, timeout=15)
            resp.raise_for_status()
            # 找所有 href 里指向 xml 的链接，排除 primary_doc 和 xsl 子目录
            matches = re.findall(r'href="(/Archives/[^"]+\.xml)"', resp.text)
            for m in matches:
                # 跳过 xsl 渲染版本（路径包含 xsl）和 primary_doc
                if "xsl" not in m and "primary_doc" not in m:
                    return f"https://www.sec.gov{m}"
            # fallback: 带 xsl 目录的也尝试
            for m in matches:
                if "primary_doc" not in m:
                    return f"https://www.sec.gov{m}"
        except Exception as e:
            logger.warning(f"[SEC 13F] 解析index失败: {e}")
        return None

    def _parse_xml(self, xml_text: str) -> list[dict]:
        holdings = []
        try:
            # SEC XML有命名空间
            root = ET.fromstring(xml_text)
            ns = {"ns": root.tag.split("}")[0].strip("{") if "}" in root.tag else ""}

            def find_text(elem, tag):
                ns_tag = f"{{{ns['ns']}}}{tag}" if ns["ns"] else tag
                child = elem.find(ns_tag)
                if child is None:
                    child = elem.find(tag)  # 无命名空间fallback
                return child.text.strip() if child is not None and child.text else ""

            for info_table in root.iter():
                if "infoTable" not in info_table.tag:
                    continue
                name = find_text(info_table, "nameOfIssuer")
                cusip = find_text(info_table, "cusip")
                value = find_text(info_table, "value")
                shares_elem = info_table.find(".//{*}shrsOrPrnAmt/{*}sshPrnamt") or info_table.find("shrsOrPrnAmt/sshPrnamt")
                shares = shares_elem.text.strip() if shares_elem is not None and shares_elem.text else "0"

                if name:
                    holdings.append({
                        "name": name,
                        "cusip": cusip,
                        "value_thousands": int(value.replace(",", "")) if value else 0,
                        "shares": int(shares.replace(",", "")) if shares else 0,
                    })
        except Exception as e:
            logger.error(f"[SEC 13F] XML解析失败: {e}")
        return holdings

    def _diff_holdings(self, new_holdings: list[dict], period: str) -> list[RawEvent]:
        events = []
        prev_quarter = self._prev_quarter(period)
        old_holdings = {h["cusip"]: h for h in get_holdings_snapshot(self.guru_name, prev_quarter)}
        new_map = {h["cusip"]: h for h in new_holdings}

        total_value = sum(h["value_thousands"] for h in new_holdings) or 1

        changes = []

        for cusip, new in new_map.items():
            pct = new["value_thousands"] / total_value * 100
            old = old_holdings.get(cusip)
            if old is None:
                changes.append(("建仓", new, None, pct))
            else:
                old_val = old["value_thousands"] or 1
                chg_pct = (new["value_thousands"] - old_val) / old_val * 100
                if chg_pct >= 20:
                    changes.append(("增持", new, old, pct))
                elif chg_pct <= -20:
                    changes.append(("减持", new, old, pct))

        for cusip, old in old_holdings.items():
            if cusip not in new_map:
                changes.append(("清仓", old, old, 0))

        for action, holding, old_holding, pct in changes:
            content_parts = [
                f"{self.guru_name} {action} {holding['name']}",
                f"股票: {holding['name']} (CUSIP: {holding['cusip']})",
                f"市值: ${holding['value_thousands']:,}千 (占比 {pct:.1f}%)",
                f"股数: {holding['shares']:,}",
                f"报告季度: {period}",
            ]
            if old_holding and action in ("增持", "减持"):
                content_parts.append(f"上季度市值: ${old_holding['value_thousands']:,}千")

            events.append(RawEvent(
                guru_name=self.guru_name,
                event_type="trade",
                source="sec_13f",
                raw_content="\n".join(content_parts),
                timestamp=datetime.now(),
                url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={self.cik}&type=13F-HR",
                metadata={
                    "action": action,
                    "ticker": holding["name"],
                    "cusip": holding["cusip"],
                    "value_thousands": holding["value_thousands"],
                    "portfolio_pct": round(pct, 2),
                    "period": period,
                },
            ))
        return events

    def _save_snapshot(self, holdings: list[dict], period: str):
        quarter = self._period_to_quarter(period)
        total_value = sum(h["value_thousands"] for h in holdings) or 1
        for h in holdings:
            save_holdings_snapshot(
                guru_name=self.guru_name,
                quarter=quarter,
                ticker=h["name"],
                cusip=h["cusip"],
                shares=h["shares"],
                market_value=h["value_thousands"] * 1000,
                portfolio_pct=h["value_thousands"] / total_value * 100,
            )

    @staticmethod
    def _period_to_quarter(period: str) -> str:
        """2025-03-31 → 2025Q1"""
        if not period:
            return ""
        parts = period.split("-")
        if len(parts) < 2:
            return period
        month = int(parts[1])
        q = (month - 1) // 3 + 1
        return f"{parts[0]}Q{q}"

    @staticmethod
    def _prev_quarter(period: str) -> str:
        """2025-03-31 → 2024Q4"""
        if not period:
            return ""
        parts = period.split("-")
        year, month = int(parts[0]), int(parts[1])
        q = (month - 1) // 3 + 1
        if q == 1:
            return f"{year - 1}Q4"
        return f"{year}Q{q - 1}"
