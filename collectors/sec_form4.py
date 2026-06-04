"""
SEC Form 4 内部人交易采集器
内部人（高管/董事/大股东）买卖股票必须在2天内申报
数据源: SEC EDGAR (免费)
"""
from __future__ import annotations

import requests
import xml.etree.ElementTree as ET
import time
import logging
from datetime import datetime, timedelta
from collectors.base import BaseCollector, RawEvent
from storage.db import get_checkpoint, set_checkpoint

logger = logging.getLogger(__name__)

SEC_HEADERS = {
    "User-Agent": "GuruTracker research@gurutracker.local",
    "Accept-Encoding": "gzip, deflate",
}

EDGAR_API = "https://data.sec.gov/submissions"


class SECForm4Collector(BaseCollector):

    def __init__(self, cik: str, guru_name: str):
        self.cik = cik.lstrip("0").zfill(10)
        self.guru_name = guru_name

    def get_source_name(self) -> str:
        return f"sec_form4:{self.guru_name}"

    def collect(self) -> list[RawEvent]:
        checkpoint_key = f"sec_form4:{self.cik}"
        last_accession = get_checkpoint(checkpoint_key)

        filings = self._get_recent_form4s()
        if not filings:
            return []

        events = []
        new_last = last_accession

        for filing in filings:
            accession_no = filing["accessionNumber"].replace("-", "")
            if accession_no == last_accession:
                break  # 已处理过后面的

            transactions = self._parse_form4(accession_no, filing["filingDate"])
            events.extend(transactions)

            if new_last == last_accession:
                new_last = accession_no  # 记录最新的

        if new_last != last_accession:
            set_checkpoint(checkpoint_key, new_last)

        logger.info(f"[Form4] {self.guru_name}: 发现 {len(events)} 条内部人交易")
        return events

    def _get_recent_form4s(self) -> list[dict]:
        url = f"{EDGAR_API}/CIK{self.cik}.json"
        try:
            time.sleep(0.1)
            resp = requests.get(url, headers=SEC_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"[Form4] 获取提交记录失败: {e}")
            return []

        filings = data.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        accessions = filings.get("accessionNumber", [])
        dates = filings.get("filingDate", [])

        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        results = []

        for i, form in enumerate(forms):
            if form == "4" and dates[i] >= cutoff:
                results.append({
                    "accessionNumber": accessions[i],
                    "filingDate": dates[i],
                })
        return results

    def _parse_form4(self, accession_no: str, filing_date: str) -> list[RawEvent]:
        cik_clean = self.cik.lstrip("0")
        xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{accession_no}/{accession_no[:10]}-{accession_no[10:12]}-{accession_no[12:]}.xml"

        # Form 4 XML文件名不固定，通过index找
        index_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={self.cik}&type=4&dateb=&owner=include&count=5&output=atom"

        # 尝试标准路径
        try:
            time.sleep(0.1)
            # 获取filing index
            index_json_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{accession_no}/{accession_no}-index.json"
            resp = requests.get(index_json_url, headers=SEC_HEADERS, timeout=15)
            if resp.status_code == 200:
                index_data = resp.json()
                for f in index_data.get("directory", {}).get("item", []):
                    if f.get("name", "").endswith(".xml") and "form4" not in f.get("name", "").lower():
                        xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{accession_no}/{f['name']}"
                        break

            resp = requests.get(xml_url, headers=SEC_HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"[Form4] 下载XML失败 {accession_no}: {e}")
            return []

        return self._parse_xml(resp.text, filing_date, accession_no, cik_clean)

    def _parse_xml(self, xml_text: str, filing_date: str, accession_no: str, cik_clean: str) -> list[RawEvent]:
        events = []
        try:
            root = ET.fromstring(xml_text)

            # 报告人信息
            reporter_name = ""
            name_elem = root.find(".//reportingOwner/reportingOwnerId/rptOwnerName")
            if name_elem is not None:
                reporter_name = name_elem.text.strip()

            # 股票信息
            ticker = ""
            ticker_elem = root.find(".//issuer/issuerTradingSymbol")
            if ticker_elem is not None:
                ticker = ticker_elem.text.strip()

            company_name = ""
            company_elem = root.find(".//issuer/issuerName")
            if company_elem is not None:
                company_name = company_elem.text.strip()

            # 遍历交易记录
            for trans in root.findall(".//nonDerivativeTransaction"):
                sec_code = ""
                sec_elem = trans.find(".//transactionCoding/transactionCode")
                if sec_elem is not None:
                    sec_code = sec_elem.text.strip()

                # P=Purchase, S=Sale, A=Award, D=Disposition
                if sec_code not in ("P", "S"):
                    continue

                shares_text = ""
                shares_elem = trans.find(".//transactionAmounts/transactionShares/value")
                if shares_elem is not None:
                    shares_text = shares_elem.text.strip()

                price_text = ""
                price_elem = trans.find(".//transactionAmounts/transactionPricePerShare/value")
                if price_elem is not None:
                    price_text = price_elem.text.strip()

                trans_date = ""
                date_elem = trans.find(".//transactionDate/value")
                if date_elem is not None:
                    trans_date = date_elem.text.strip()

                action = "买入" if sec_code == "P" else "卖出"
                try:
                    shares = float(shares_text)
                    price = float(price_text) if price_text else 0
                    total = shares * price
                except (ValueError, TypeError):
                    shares, price, total = 0, 0, 0

                content = (
                    f"{self.guru_name} 旗下内部人 {reporter_name} {action} {company_name} ({ticker})\n"
                    f"交易时间: {trans_date}\n"
                    f"股数: {shares:,.0f} 股\n"
                    f"价格: ${price:.2f}\n"
                    f"总额: ${total:,.0f}\n"
                    f"申报人: {reporter_name}"
                )

                events.append(RawEvent(
                    guru_name=self.guru_name,
                    event_type="trade",
                    source="sec_form4",
                    raw_content=content,
                    timestamp=datetime.strptime(trans_date, "%Y-%m-%d") if trans_date else datetime.now(),
                    url=f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{accession_no}/",
                    metadata={
                        "action": action,
                        "ticker": ticker,
                        "company": company_name,
                        "shares": shares,
                        "price": price,
                        "total_usd": total,
                        "reporter": reporter_name,
                    },
                ))
        except Exception as e:
            logger.error(f"[Form4] XML解析失败: {e}")
        return events
