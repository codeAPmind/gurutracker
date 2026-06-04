# 投资大佬跟单系统 — 技术设计文档

> **目标**：构建一个自动化系统，追踪投资大佬的持仓变动与公开发言，通过 DeepSeek 提取投资信号，实时推送到飞书群。
>
> **定位**：个人/小团队使用的轻量 MVP，6天可上线，月成本 ￥80-150。

---

## 1. 系统总览

```
┌─────────────────────────────────────────────────────────────┐
│                      定时调度器 (APScheduler)                 │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │ SEC 13F  │  │ ARK每日  │  │ 社交媒体  │  │ 国会交易  │    │
│  │  爬虫    │  │ 交易下载  │  │  监控     │  │  监控     │    │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘    │
│       │             │             │              │           │
│       └─────────────┴──────┬──────┴──────────────┘           │
│                            ▼                                 │
│                  ┌──────────────────┐                        │
│                  │  原始数据队列     │                        │
│                  │  (Redis / 内存)   │                        │
│                  └────────┬─────────┘                        │
│                           ▼                                  │
│                  ┌──────────────────┐                        │
│                  │  DeepSeek 处理层  │                        │
│                  │  信号提取+置信度   │                        │
│                  └────────┬─────────┘                        │
│                           ▼                                  │
│              ┌────────────┴────────────┐                     │
│              ▼                         ▼                     │
│    ┌──────────────────┐    ┌──────────────────┐             │
│    │  飞书机器人推送    │    │  SQLite 信号存储  │             │
│    │  (Webhook)        │    │  (历史查询)       │             │
│    └──────────────────┘    └──────────────────┘             │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 项目结构

```
guru-tracker/
├── config/
│   ├── settings.py          # 全局配置（API Key、Webhook、调度频率）
│   └── gurus.yaml           # 跟踪目标配置（大佬列表、数据源、阈值）
├── collectors/
│   ├── base.py              # 采集器基类（统一接口）
│   ├── sec_13f.py           # SEC 13F 持仓采集
│   ├── sec_form4.py         # SEC Form 4 内部人交易采集
│   ├── ark_trades.py        # ARK Invest 每日交易采集
│   ├── congress.py          # 美国国会议员交易采集
│   └── social_media.py      # 社交媒体发言采集（X / 雪球）
├── processor/
│   ├── deepseek_engine.py   # DeepSeek API 调用封装
│   ├── signal_extractor.py  # 从原始数据提取投资信号
│   ├── confidence_scorer.py # 置信度评分模型
│   └── deduplicator.py      # 去重与合并
├── notifier/
│   ├── feishu_bot.py        # 飞书机器人推送
│   └── card_templates.py    # 飞书消息卡片模板
├── storage/
│   ├── models.py            # 数据模型定义
│   └── db.py                # SQLite 操作封装
├── scheduler.py             # 主调度入口
├── requirements.txt
├── docker-compose.yml
└── README.md
```

---

## 3. 配置文件设计

### 3.1 `config/settings.py`

```python
import os

# DeepSeek API
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"  # 使用 DeepSeek-V3，性价比最高

# 飞书
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_WEBHOOK_SECRET = os.getenv("FEISHU_WEBHOOK_SECRET", "")  # 可选签名校验

# 数据库
DB_PATH = "data/signals.db"

# 调度频率
SCHEDULE = {
    "sec_13f":      {"cron": "0 8 * * 1"},        # 每周一 8:00（13F 季度更新，周检即可）
    "sec_form4":    {"interval_hours": 6},          # 每 6 小时
    "ark_trades":   {"cron": "0 7,20 * * 1-5"},    # 工作日 7:00 和 20:00（开盘前+收盘后）
    "congress":     {"interval_hours": 12},         # 每 12 小时
    "social_media": {"interval_minutes": 30},       # 每 30 分钟
}

# 置信度阈值：低于此值不推送
MIN_CONFIDENCE_TO_PUSH = "中"  # "低" / "中" / "高"
```

### 3.2 `config/gurus.yaml`

```yaml
gurus:
  # === 长期跟踪 ===
  - name: "巴菲特"
    name_en: "Warren Buffett"
    tier: "长期"
    sources:
      - type: "sec_13f"
        cik: "0001067983"          # Berkshire Hathaway CIK
      - type: "sec_form4"
        cik: "0001067983"
    thresholds:
      heavy_position_pct: 5.0      # 持仓占比 > 5% 视为重仓
      significant_change_pct: 20.0  # 变动 > 20% 视为显著

  - name: "段永平"
    name_en: "Duan Yongping"
    tier: "长期"
    sources:
      - type: "social_media"
        platform: "xueqiu"
        user_id: "大道无形我有型"    # 雪球 ID
      - type: "social_media"
        platform: "x"
        username: "YPDuan"
    thresholds:
      heavy_position_pct: 3.0

  # === 中期跟踪 ===
  - name: "ARK Invest"
    name_en: "Cathie Wood"
    tier: "中期"
    sources:
      - type: "ark_trades"
        fund_codes: ["ARKK", "ARKW", "ARKG", "ARKF", "ARKX"]
    thresholds:
      significant_shares: 50000     # 单次交易 > 5万股视为重大

  - name: "黄仁勋"
    name_en: "Jensen Huang"
    tier: "中期"
    sources:
      - type: "sec_form4"
        cik: "0001045810"          # NVIDIA CIK
      - type: "social_media"
        platform: "x"
        username: "null"           # 黄仁勋无 X，仅跟踪 SEC

  # === 短期跟踪 ===
  - name: "佩洛西"
    name_en: "Nancy Pelosi"
    tier: "短期"
    sources:
      - type: "congress"
        member_id: "pelosi"
    thresholds:
      significant_amount_usd: 100000

  - name: "马斯克"
    name_en: "Elon Musk"
    tier: "短期"
    sources:
      - type: "social_media"
        platform: "x"
        username: "elonmusk"
      - type: "sec_form4"
        cik: "0001318605"          # Tesla CIK
```

---

## 4. 数据采集层（Collectors）

### 4.1 采集器基类

```python
# collectors/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class RawEvent:
    """采集器输出的统一数据格式"""
    guru_name: str           # 大佬名称
    event_type: str          # "trade" | "speech" | "filing"
    source: str              # "sec_13f" | "ark_trades" | "x" | "xueqiu" | "congress"
    raw_content: str         # 原始内容（交易记录文本 / 发言原文）
    timestamp: datetime      # 事件时间
    url: Optional[str] = None  # 原始链接
    metadata: Optional[dict] = None  # 额外元数据（股票代码、金额等已知字段）

class BaseCollector(ABC):
    """所有采集器的基类"""

    @abstractmethod
    def collect(self) -> list[RawEvent]:
        """执行采集，返回新事件列表"""
        pass

    @abstractmethod
    def get_last_checkpoint(self) -> str:
        """获取上次采集的断点（用于增量采集）"""
        pass
```

### 4.2 SEC 13F 采集器

```python
# collectors/sec_13f.py
"""
数据源: SEC EDGAR (https://efts.sec.gov/LATEST/search-index?q=...)
免费，无需 API Key，但需设置 User-Agent（SEC 要求）。

核心逻辑：
1. 通过 CIK 查询最新 13F-HR 文件
2. 解析 XML 格式的持仓表（informationTable）
3. 与上一季度对比，计算增减持
"""

import requests
import xml.etree.ElementTree as ET
from collectors.base import BaseCollector, RawEvent

SEC_HEADERS = {
    "User-Agent": "GuruTracker bot@example.com",  # SEC 要求提供联系方式
    "Accept-Encoding": "gzip, deflate",
}
EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
EDGAR_FILING = "https://www.sec.gov/cgi-bin/browse-edgar"

class SEC13FCollector(BaseCollector):
    def __init__(self, cik: str, guru_name: str):
        self.cik = cik
        self.guru_name = guru_name

    def collect(self) -> list[RawEvent]:
        # 1. 获取最新 13F 文件列表
        # GET https://efts.sec.gov/LATEST/search-index?q=%2213F-HR%22&dateRange=custom&startdt=2025-01-01&forms=13F-HR&entities={cik}
        #
        # 2. 下载 informationTable.xml
        # 解析 <infoTable> 中的每个 <nameOfIssuer>, <value>, <shrsOrPrnAmt>
        #
        # 3. 从 SQLite 加载上季度持仓，计算 diff
        #    - 新增持仓 → "建仓"
        #    - 持仓增加 > 20% → "增持"
        #    - 持仓减少 > 20% → "减持"
        #    - 持仓消失 → "清仓"
        #
        # 4. 返回 RawEvent 列表
        pass  # 实现时填充
```

**关键实现提示**：
- SEC EDGAR 完全免费，但限速 10 请求/秒，加 `time.sleep(0.1)`
- 13F 每季度提交一次（45天延迟），数据格式为 XML
- 需要本地存储上一季度的持仓快照用于对比
- 参考库: `sec-api`（npm）或直接用 `requests` 调 EDGAR REST API

### 4.3 ARK 每日交易采集器

```python
# collectors/ark_trades.py
"""
数据源: ARK Invest 官方每日交易 CSV
URL: https://arkfunds.io/api/v2/etf/holdings?symbol=ARKK
备选: https://cathiesark.com/ark-combined-holdings-of-all-etfs (网页爬取)

这是最容易实现的采集器——ARK 主动公开每日交易。
"""

import requests
import csv
from io import StringIO
from collectors.base import BaseCollector, RawEvent

# arkfunds.io 提供免费 API
ARKFUNDS_API = "https://arkfunds.io/api/v2/etf/holdings"

class ARKTradesCollector(BaseCollector):
    def __init__(self, fund_codes: list[str]):
        self.fund_codes = fund_codes  # ["ARKK", "ARKW", ...]

    def collect(self) -> list[RawEvent]:
        events = []
        for fund in self.fund_codes:
            # GET https://arkfunds.io/api/v2/etf/holdings?symbol={fund}
            # 返回 JSON: { "symbol": "ARKK", "holdings": [...] }
            #
            # 每条 holding 包含:
            #   ticker, company, cusip, shares, market_value, weight, weight_rank
            #
            # 对比昨日数据，找出:
            #   - 新买入的股票
            #   - 完全卖出的股票
            #   - 份额变动显著的股票（>10%）
            #
            # 生成 RawEvent，metadata 中包含:
            #   {"ticker": "TSLA", "shares_change": 142000, "direction": "buy", "fund": "ARKK"}
            pass
        return events
```

**关键实现提示**：
- `arkfunds.io` 免费 API，无需 Key
- 备选方案: 直接下载 ARK 官网 CSV (`https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv`)
- 需要本地缓存昨日持仓用于对比

### 4.4 国会交易采集器

```python
# collectors/congress.py
"""
数据源选项:
1. QuiverQuant API (https://www.quiverquant.com/) — 免费额度
2. Capitol Trades (https://www.capitoltrades.com/) — 网页爬取
3. House/Senate 官方披露网站 — 结构复杂，不推荐直接爬

推荐 QuiverQuant，数据结构化程度最高。
"""

import requests
from collectors.base import BaseCollector, RawEvent

QUIVER_API = "https://api.quiverquant.com/beta"

class CongressCollector(BaseCollector):
    def __init__(self, member_id: str, guru_name: str):
        self.member_id = member_id
        self.guru_name = guru_name

    def collect(self) -> list[RawEvent]:
        # GET /beta/historical/congresstrading/{member_id}
        # Headers: {"Authorization": "Bearer {QUIVER_API_KEY}"}
        #
        # 返回交易记录:
        #   Representative, Ticker, Transaction (Purchase/Sale),
        #   Amount ($1,001-$15,000 等区间), TransactionDate
        #
        # 过滤最近 7 天新增记录
        # 生成 RawEvent
        pass
```

### 4.5 社交媒体采集器

```python
# collectors/social_media.py
"""
最复杂的采集器，需要处理多个平台。

平台策略:
- X (Twitter): 使用 Playwright 无头浏览器（API 收费太贵）
  备选: Nitter 实例 或 RapidAPI 的 Twitter API
- 雪球: requests + BeautifulSoup（雪球反爬较弱）
- Reddit: 官方 API（PRAW 库，免费额度够用）

重点: 只采集"投资相关"发言，用关键词预过滤再送 DeepSeek。
"""

import requests
from bs4 import BeautifulSoup
from collectors.base import BaseCollector, RawEvent

# 投资关键词预过滤（减少无效 API 调用）
INVESTMENT_KEYWORDS = [
    "买入", "卖出", "持仓", "建仓", "加仓", "减仓", "清仓",
    "看好", "看空", "估值", "便宜", "贵了",
    "buy", "sell", "long", "short", "position", "bullish", "bearish",
    "stock", "share", "valuation", "undervalued", "overvalued",
]

class XueqiuCollector(BaseCollector):
    """雪球采集器（段永平等华人投资者的主要平台）"""

    def __init__(self, user_id: str, guru_name: str):
        self.user_id = user_id
        self.guru_name = guru_name
        self.base_url = "https://xueqiu.com"

    def collect(self) -> list[RawEvent]:
        # 1. GET /v4/statuses/user_timeline.json?user_id={user_id}&page=1
        #    需要携带 Cookie（先访问首页获取）
        #
        # 2. 解析返回的帖子列表
        #    过滤: 只保留包含 INVESTMENT_KEYWORDS 的帖子
        #
        # 3. 对每条投资相关帖子生成 RawEvent
        #    raw_content = 帖子全文
        #    metadata = {"platform": "xueqiu", "post_id": "...", "retweet_count": N}
        pass

class XTwitterCollector(BaseCollector):
    """X/Twitter 采集器（马斯克等）"""

    def __init__(self, username: str, guru_name: str):
        self.username = username
        self.guru_name = guru_name

    def collect(self) -> list[RawEvent]:
        # 方案 A: Playwright 无头浏览器
        #   - 打开 https://x.com/{username}
        #   - 滚动加载最新推文
        #   - 解析 DOM 提取文本
        #   - 缺点: 慢，容易被封
        #
        # 方案 B (推荐): 使用第三方 API
        #   - RapidAPI 上的 Twitter API（$10/月，5000请求）
        #   - 或自建 Nitter 实例解析 RSS
        #
        # 无论哪种方案，都先用 INVESTMENT_KEYWORDS 过滤
        pass
```

---

## 5. AI 处理层（DeepSeek）

### 5.1 DeepSeek API 封装

```python
# processor/deepseek_engine.py
"""
DeepSeek API 封装。DeepSeek 兼容 OpenAI 格式，可直接用 openai 库。
DeepSeek-V3: 输入 ￥1/百万token，输出 ￥2/百万token（极其便宜）
"""

from openai import OpenAI
from config.settings import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
)

def extract_signal(raw_content: str, guru_name: str, event_type: str) -> dict:
    """
    调用 DeepSeek 从原始内容中提取投资信号。

    返回示例:
    {
        "has_signal": true,
        "action": "买入",
        "ticker": "AAPL",
        "stock_name": "苹果",
        "reason": "段永平认为苹果估值合理，长期持有价值显著",
        "position_hint": "重仓",    # "重仓" / "试水" / "未知"
        "sentiment": "看好",        # "看好" / "看空" / "中性"
        "raw_quote": "原文关键句摘录（30字以内）"
    }
    """

    system_prompt = """你是一个专业的投资信号分析助手。你的任务是从投资者的发言或交易记录中提取结构化的投资信号。

规则：
1. 只提取明确的投资相关信息，不要过度解读
2. 如果内容与投资无关，返回 {"has_signal": false}
3. ticker 使用美股代码（如 AAPL, TSLA），A股使用数字代码（如 600519）
4. position_hint 根据上下文判断仓位大小：
   - "重仓"：明确提到大量买入、核心持仓、重仓
   - "试水"：提到小量买入、观察仓、试水
   - "未知"：无法判断
5. 输出纯 JSON，不要包含 markdown 代码块标记

输出格式（严格 JSON）：
{
  "has_signal": true/false,
  "action": "买入/卖出/增持/减持/建仓/清仓/看好/看空",
  "ticker": "股票代码",
  "stock_name": "股票中文名",
  "reason": "一句话总结原因（50字以内）",
  "position_hint": "重仓/试水/未知",
  "sentiment": "看好/看空/中性",
  "raw_quote": "原文关键句（30字以内）"
}"""

    user_prompt = f"""投资者: {guru_name}
事件类型: {event_type}
原始内容:
{raw_content}

请提取投资信号。"""

    response = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,  # 低温度，确保输出稳定
        max_tokens=500,
    )

    import json
    text = response.choices[0].message.content.strip()
    # 兜底：去除可能的 markdown 代码块
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)
```

### 5.2 置信度评分

```python
# processor/confidence_scorer.py
"""
置信度评分模型 — 核心策略逻辑

评分维度:
1. 仓位权重 (position_weight)   — 重仓 > 试水
2. 信息时效 (recency)           — 越新越有价值
3. 大佬层级 (guru_tier)         — 长期/价值型大佬的信号更稳
4. 数据源可靠性 (source_trust)  — SEC 文件 > 社交媒体发言
5. 交叉验证 (cross_validation)  — 多个大佬同时看好某只股票
"""

from dataclasses import dataclass

@dataclass
class Signal:
    guru_name: str
    tier: str            # "短期" / "中期" / "长期"
    action: str          # "买入" / "卖出" / ...
    ticker: str
    stock_name: str
    reason: str
    position_hint: str   # "重仓" / "试水" / "未知"
    sentiment: str
    source: str          # "sec_13f" / "ark_trades" / "x" / ...
    confidence: str      # 最终评分: "高" / "中" / "低"
    score: float         # 数值评分 0-100

def score_signal(signal_data: dict, guru_config: dict, existing_signals: list) -> Signal:
    """
    综合评分逻辑
    """
    score = 0.0

    # 1. 仓位权重 (0-30分)
    position = signal_data.get("position_hint", "未知")
    if position == "重仓":
        score += 30
    elif position == "试水":
        score += 10
    else:
        score += 15  # 未知给中间值

    # 2. 数据源可靠性 (0-30分)
    source_scores = {
        "sec_13f": 30,     # SEC 文件最可靠（真金白银）
        "sec_form4": 28,   # 内部人交易也很可靠
        "ark_trades": 25,  # ARK 官方公开数据
        "congress": 22,    # 国会披露（有延迟）
        "xueqiu": 15,      # 社交媒体发言
        "x": 12,           # Twitter 发言（噪音多）
    }
    score += source_scores.get(signal_data.get("source", ""), 10)

    # 3. 动作强度 (0-20分)
    action = signal_data.get("action", "")
    action_scores = {
        "建仓": 20, "清仓": 20,   # 从无到有/从有到无，信号最强
        "买入": 18, "卖出": 18,
        "增持": 15, "减持": 15,
        "看好": 8, "看空": 8,      # 纯发言，较弱
    }
    score += action_scores.get(action, 5)

    # 4. 交叉验证 (0-20分)
    # 如果最近 7 天内有其他大佬也操作了同一只股票，加分
    ticker = signal_data.get("ticker", "")
    cross_count = sum(
        1 for s in existing_signals
        if s.get("ticker") == ticker and s.get("guru_name") != signal_data.get("guru_name")
    )
    score += min(cross_count * 10, 20)

    # 最终评级
    if score >= 65:
        confidence = "高"
    elif score >= 40:
        confidence = "中"
    else:
        confidence = "低"

    return Signal(
        guru_name=signal_data["guru_name"],
        tier=guru_config.get("tier", "中期"),
        action=action,
        ticker=ticker,
        stock_name=signal_data.get("stock_name", ""),
        reason=signal_data.get("reason", ""),
        position_hint=position,
        sentiment=signal_data.get("sentiment", "中性"),
        source=signal_data.get("source", ""),
        confidence=confidence,
        score=round(score, 1),
    )
```

### 5.3 去重逻辑

```python
# processor/deduplicator.py
"""
去重规则：
1. 同一大佬 + 同一股票 + 同一动作 + 24小时内 → 合并为一条
2. 社交媒体转发/引用同一条 → 只保留原始
3. 13F 季度报告 → 同一季度只处理一次
"""

from storage.db import get_recent_signals
from datetime import datetime, timedelta

def is_duplicate(signal_data: dict) -> bool:
    recent = get_recent_signals(
        guru_name=signal_data["guru_name"],
        ticker=signal_data["ticker"],
        hours=24,
    )
    for existing in recent:
        if existing["action"] == signal_data["action"]:
            return True
    return False
```

---

## 6. 飞书推送层

### 6.1 飞书机器人封装

```python
# notifier/feishu_bot.py
"""
飞书自定义机器人 Webhook 推送。

配置步骤:
1. 飞书群 → 设置 → 群机器人 → 添加自定义机器人
2. 复制 Webhook URL
3. （可选）设置签名校验
"""

import requests
import time
import hmac
import hashlib
import base64
from config.settings import FEISHU_WEBHOOK_URL, FEISHU_WEBHOOK_SECRET
from notifier.card_templates import build_signal_card, build_daily_digest_card

def _gen_sign(timestamp: str, secret: str) -> str:
    """飞书签名校验"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")

def send_to_feishu(card_payload: dict) -> bool:
    """发送卡片消息到飞书"""
    body = {"msg_type": "interactive", "card": card_payload}

    # 如果设置了签名校验
    if FEISHU_WEBHOOK_SECRET:
        timestamp = str(int(time.time()))
        sign = _gen_sign(timestamp, FEISHU_WEBHOOK_SECRET)
        body["timestamp"] = timestamp
        body["sign"] = sign

    resp = requests.post(FEISHU_WEBHOOK_URL, json=body, timeout=10)
    return resp.status_code == 200 and resp.json().get("code") == 0

def push_signal(signal) -> bool:
    """推送单条投资信号"""
    card = build_signal_card(signal)
    return send_to_feishu(card)

def push_daily_digest(signals: list) -> bool:
    """推送每日汇总"""
    card = build_daily_digest_card(signals)
    return send_to_feishu(card)
```

### 6.2 飞书卡片模板

```python
# notifier/card_templates.py
"""
飞书消息卡片模板。
参考文档: https://open.feishu.cn/document/common-capabilities/message-card/message-cards-content
"""

def build_signal_card(signal) -> dict:
    """构建单条信号卡片"""

    # 颜色映射
    header_colors = {
        "买入": "green", "建仓": "green", "增持": "green", "看好": "green",
        "卖出": "red", "清仓": "red", "减持": "red", "看空": "red",
    }
    confidence_emoji = {"高": "🟢", "中": "🟡", "低": "🟠"}
    tier_tags = {"短期": "⚡ 短期", "中期": "📈 中期", "长期": "🏛️ 长期"}

    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"📊 {signal.guru_name} {signal.action} {signal.ticker}"
            },
            "template": header_colors.get(signal.action, "blue"),
        },
        "elements": [
            # 核心信息
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{signal.guru_name}** ({signal.tier})\n"
                        f"动作: **{signal.action}** **{signal.stock_name}** (`{signal.ticker}`)\n"
                        f"原因: {signal.reason}"
                    ),
                },
            },
            {"tag": "hr"},
            # 元数据行
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**置信度**: {confidence_emoji.get(signal.confidence, '⚪')} {signal.confidence} ({signal.score}分)"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**层级**: {tier_tags.get(signal.tier, signal.tier)}"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**数据源**: {signal.source}"
                        }
                    },
                    {
                        "is_short": True,
                        "text": {
                            "tag": "lark_md",
                            "content": f"**仓位**: {signal.position_hint}"
                        }
                    },
                ],
            },
            {"tag": "hr"},
            # 免责声明
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "⚠️ 仅供信息参考，不构成投资建议。投资有风险，决策需谨慎。"
                    }
                ],
            },
        ],
    }

def build_daily_digest_card(signals: list) -> dict:
    """构建每日汇总卡片"""

    # 按置信度分组统计
    high = [s for s in signals if s.confidence == "高"]
    mid = [s for s in signals if s.confidence == "中"]

    lines = []
    for s in sorted(signals, key=lambda x: x.score, reverse=True)[:10]:
        emoji = "🟢" if s.confidence == "高" else "🟡" if s.confidence == "中" else "🟠"
        lines.append(f"{emoji} **{s.guru_name}** {s.action} `{s.ticker}` — {s.reason[:30]}")

    return {
        "header": {
            "title": {"tag": "plain_text", "content": "📋 今日投资信号汇总"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"共 **{len(signals)}** 条信号 | 🟢 高置信 {len(high)} 条 | 🟡 中置信 {len(mid)} 条",
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(lines) if lines else "今日暂无显著信号",
                },
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "⚠️ 仅供参考，不构成投资建议"}
                ],
            },
        ],
    }
```

---

## 7. 数据存储

```python
# storage/models.py
"""
使用 SQLite，轻量且无需额外部署。
后期如需多用户可迁移到 PostgreSQL。
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guru_name TEXT NOT NULL,
    tier TEXT,                      -- "短期" / "中期" / "长期"
    action TEXT NOT NULL,           -- "买入" / "卖出" / "增持" / ...
    ticker TEXT NOT NULL,
    stock_name TEXT,
    reason TEXT,
    position_hint TEXT,
    sentiment TEXT,
    source TEXT NOT NULL,
    confidence TEXT NOT NULL,       -- "高" / "中" / "低"
    score REAL,
    raw_content TEXT,               -- 原始内容（用于回溯）
    url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_signals_guru ON signals(guru_name);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_confidence ON signals(confidence);

CREATE TABLE IF NOT EXISTS holdings_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guru_name TEXT NOT NULL,
    quarter TEXT NOT NULL,          -- "2025Q1"
    ticker TEXT NOT NULL,
    shares BIGINT,
    market_value REAL,
    portfolio_pct REAL,             -- 占投资组合百分比
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(guru_name, quarter, ticker)
);

CREATE TABLE IF NOT EXISTS collector_checkpoints (
    collector_name TEXT PRIMARY KEY,
    last_checkpoint TEXT,            -- 断点标记（时间戳/页码/文件ID等）
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
```

---

## 8. 主调度器

```python
# scheduler.py
"""
主入口，使用 APScheduler 管理所有定时任务。
"""

import yaml
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import SCHEDULE, MIN_CONFIDENCE_TO_PUSH
from collectors.sec_13f import SEC13FCollector
from collectors.ark_trades import ARKTradesCollector
from collectors.congress import CongressCollector
from collectors.social_media import XueqiuCollector, XTwitterCollector
from processor.deepseek_engine import extract_signal
from processor.confidence_scorer import score_signal
from processor.deduplicator import is_duplicate
from notifier.feishu_bot import push_signal, push_daily_digest
from storage.db import save_signal, get_today_signals, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("scheduler")

CONFIDENCE_ORDER = {"低": 0, "中": 1, "高": 2}

def load_gurus():
    with open("config/gurus.yaml", "r") as f:
        return yaml.safe_load(f)["gurus"]

def build_collectors(gurus: list) -> dict:
    """根据配置构建采集器实例"""
    collectors = {
        "sec_13f": [],
        "ark_trades": [],
        "congress": [],
        "social_media": [],
    }
    for guru in gurus:
        for src in guru.get("sources", []):
            if src["type"] == "sec_13f":
                collectors["sec_13f"].append(SEC13FCollector(src["cik"], guru["name"]))
            elif src["type"] == "ark_trades":
                collectors["ark_trades"].append(ARKTradesCollector(src["fund_codes"]))
            elif src["type"] == "congress":
                collectors["congress"].append(CongressCollector(src["member_id"], guru["name"]))
            elif src["type"] == "social_media":
                if src["platform"] == "xueqiu":
                    collectors["social_media"].append(XueqiuCollector(src["user_id"], guru["name"]))
                elif src["platform"] == "x" and src.get("username"):
                    collectors["social_media"].append(XTwitterCollector(src["username"], guru["name"]))
    return collectors

def process_events(events: list, gurus_config: list):
    """处理采集到的事件 → 提取信号 → 评分 → 推送"""
    recent_signals = get_today_signals()

    for event in events:
        try:
            # 1. DeepSeek 提取信号
            signal_data = extract_signal(event.raw_content, event.guru_name, event.event_type)

            if not signal_data.get("has_signal"):
                continue

            signal_data["guru_name"] = event.guru_name
            signal_data["source"] = event.source

            # 2. 去重
            if is_duplicate(signal_data):
                logger.info(f"跳过重复信号: {event.guru_name} {signal_data.get('ticker')}")
                continue

            # 3. 评分
            guru_config = next((g for g in gurus_config if g["name"] == event.guru_name), {})
            signal = score_signal(signal_data, guru_config, recent_signals)

            # 4. 存储
            save_signal(signal)
            logger.info(f"新信号: {signal.guru_name} {signal.action} {signal.ticker} [置信度:{signal.confidence}]")

            # 5. 推送（达到阈值才推送）
            min_level = CONFIDENCE_ORDER.get(MIN_CONFIDENCE_TO_PUSH, 1)
            signal_level = CONFIDENCE_ORDER.get(signal.confidence, 0)
            if signal_level >= min_level:
                push_signal(signal)
                logger.info(f"已推送飞书: {signal.ticker}")

        except Exception as e:
            logger.error(f"处理事件失败: {e}", exc_info=True)

def run_collector_group(group_name: str, collectors_map: dict, gurus_config: list):
    """运行一组采集器"""
    collectors = collectors_map.get(group_name, [])
    all_events = []
    for collector in collectors:
        try:
            events = collector.collect()
            all_events.extend(events)
        except Exception as e:
            logger.error(f"采集器 {collector.__class__.__name__} 失败: {e}")
    if all_events:
        process_events(all_events, gurus_config)

def run_daily_digest(gurus_config: list):
    """每日汇总推送"""
    signals = get_today_signals()
    if signals:
        push_daily_digest(signals)
        logger.info(f"每日汇总已推送: {len(signals)} 条信号")

def main():
    init_db()
    gurus_config = load_gurus()
    collectors_map = build_collectors(gurus_config)

    scheduler = BlockingScheduler()

    # 注册各采集任务
    for group, config in SCHEDULE.items():
        if "cron" in config:
            trigger = CronTrigger.from_crontab(config["cron"])
        elif "interval_hours" in config:
            trigger = IntervalTrigger(hours=config["interval_hours"])
        elif "interval_minutes" in config:
            trigger = IntervalTrigger(minutes=config["interval_minutes"])
        else:
            continue

        scheduler.add_job(
            run_collector_group,
            trigger=trigger,
            args=[group, collectors_map, gurus_config],
            id=f"collector_{group}",
            name=f"采集: {group}",
        )

    # 每日晚 21:00 推送汇总
    scheduler.add_job(
        run_daily_digest,
        trigger=CronTrigger(hour=21, minute=0),
        args=[gurus_config],
        id="daily_digest",
        name="每日汇总",
    )

    logger.info("调度器启动，已注册任务:")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.name} | {job.trigger}")

    scheduler.start()

if __name__ == "__main__":
    main()
```

---

## 9. Docker 部署

```yaml
# docker-compose.yml
version: "3.8"

services:
  guru-tracker:
    build: .
    container_name: guru-tracker
    restart: unless-stopped
    environment:
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
      - FEISHU_WEBHOOK_URL=${FEISHU_WEBHOOK_URL}
      - FEISHU_WEBHOOK_SECRET=${FEISHU_WEBHOOK_SECRET}
    volumes:
      - ./data:/app/data          # SQLite 数据持久化
      - ./config:/app/config      # 配置文件
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Playwright 依赖（社交媒体爬虫需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libasound2 libxshmfence1 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium

COPY . .

CMD ["python", "scheduler.py"]
```

```
# requirements.txt
openai>=1.0.0
requests>=2.31.0
beautifulsoup4>=4.12.0
pyyaml>=6.0
pandas>=2.0.0
apscheduler>=3.10.0
playwright>=1.40.0
lxml>=4.9.0
```

---

## 10. 部署清单

### 上线前配置

| 步骤 | 操作 | 预计耗时 |
|:---|:---|:---|
| 1 | 注册 DeepSeek 账号，获取 API Key（`platform.deepseek.com`） | 5 分钟 |
| 2 | 飞书群添加自定义机器人，获取 Webhook URL | 5 分钟 |
| 3 | 购买云服务器（2核4G，阿里云/腾讯云 ￥50-100/月） | 10 分钟 |
| 4 | 创建 `.env` 文件填入 Key | 2 分钟 |
| 5 | `docker-compose up -d` 启动服务 | 5 分钟 |

### 月度运营成本

| 项目 | 说明 | 费用 |
|:---|:---|:---|
| 云服务器 | 2核4G | ￥50-100 |
| DeepSeek API | 约 5000 次调用/月 | ￥10-30 |
| 第三方数据 API | QuiverQuant 免费额度 / RapidAPI Twitter | ￥0-70 |
| 飞书机器人 | 免费 | ￥0 |
| **合计** | | **￥60-200/月** |

---

## 11. 实现优先级建议

给 Sonnet 实现时，建议按以下顺序逐步交付：

**第一步（Day 1-2）：跑通最小闭环**
- 实现 `ARKTradesCollector`（最简单的数据源）
- 实现 `deepseek_engine.py`
- 实现 `feishu_bot.py`
- 手动运行一次，确认飞书能收到消息

**第二步（Day 3-4）：补齐核心数据源**
- 实现 `SEC13FCollector`
- 实现 `CongressCollector`
- 实现 `confidence_scorer.py`
- 接入 SQLite 存储

**第三步（Day 5-6）：社交媒体 + 调度器**
- 实现 `XueqiuCollector`
- 实现 `XTwitterCollector`（可选，反爬难度较高）
- 配置 `scheduler.py` 定时任务
- Docker 部署上线

**第四步（后续迭代）：**
- Web 仪表盘（Streamlit 或 React）
- 每周策略周报自动生成
- 回测模块（跟踪信号历史准确率）
