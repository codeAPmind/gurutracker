"""
社交媒体采集器
- 雪球: requests + BeautifulSoup
- X/Twitter: 需要账号Cookie或第三方API
"""
from __future__ import annotations

import requests
import re
import json
import logging
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from collectors.base import BaseCollector, RawEvent
from storage.db import get_checkpoint, set_checkpoint

logger = logging.getLogger(__name__)

INVESTMENT_KEYWORDS = [
    "买入", "卖出", "持仓", "建仓", "加仓", "减仓", "清仓",
    "看好", "看空", "估值", "便宜", "贵了", "低估", "高估",
    "buy", "sell", "long", "short", "position", "bullish", "bearish",
    "stock", "share", "valuation", "undervalued", "overvalued",
    "持有", "投资", "股票", "仓位", "回购",
]

XUEQIU_BASE = "https://xueqiu.com"


class XueqiuCollector(BaseCollector):

    def __init__(self, user_id: str, guru_name: str):
        self.user_id = user_id
        self.guru_name = guru_name
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://xueqiu.com/",
            "Accept": "application/json, text/plain, */*",
        })
        # 注入已登录的 Cookie
        from config.settings import XUEQIU_COOKIE
        if XUEQIU_COOKIE:
            self.session.headers["Cookie"] = XUEQIU_COOKIE

    def get_source_name(self) -> str:
        return f"xueqiu:{self.guru_name}"

    def collect(self) -> list[RawEvent]:
        checkpoint_key = f"xueqiu:{self.user_id}"
        last_post_id = get_checkpoint(checkpoint_key)

        # 有 Cookie 时跳过匿名初始化
        from config.settings import XUEQIU_COOKIE
        if not XUEQIU_COOKIE:
            self._init_cookie()

        posts = self._fetch_timeline()
        if not posts:
            return []

        events = []
        new_last = last_post_id

        for post in posts:
            post_id = str(post.get("id", ""))
            if post_id == last_post_id:
                break

            text = post.get("text", "") or post.get("description", "") or ""
            text = self._clean_html(text)

            # 关键词过滤
            if not self._has_investment_keyword(text):
                continue

            created_at = post.get("created_at", 0)
            if created_at:
                ts = datetime.fromtimestamp(created_at / 1000)
            else:
                ts = datetime.now()

            # 仅处理最近7天的帖子
            if ts < datetime.now() - timedelta(days=7):
                break

            url = f"{XUEQIU_BASE}/statuses/{post_id}"

            events.append(RawEvent(
                guru_name=self.guru_name,
                event_type="speech",
                source="xueqiu",
                raw_content=text,
                timestamp=ts,
                url=url,
                metadata={
                    "post_id": post_id,
                    "platform": "xueqiu",
                    "like_count": post.get("like_count", 0),
                    "retweet_count": post.get("retweet_count", 0),
                },
            ))

            if not new_last or new_last == last_post_id:
                new_last = post_id

        if new_last and new_last != last_post_id:
            set_checkpoint(checkpoint_key, new_last)

        logger.info(f"[雪球] {self.guru_name}: 发现 {len(events)} 条投资相关发言")
        return events

    def _init_cookie(self):
        """访问首页获取必要Cookie"""
        try:
            self.session.get(XUEQIU_BASE, timeout=10)
        except Exception as e:
            logger.warning(f"[雪球] 初始化Cookie失败: {e}")

    def _fetch_timeline(self) -> list[dict]:
        try:
            url = f"{XUEQIU_BASE}/v4/statuses/user_timeline.json"
            params = {
                "user_id": self.user_id,
                "page": 1,
                "count": 20,
                "type": -1,
            }
            resp = self.session.get(url, params=params, timeout=15)
            # WAF 拦截时返回 HTML，检测一下
            content_type = resp.headers.get("content-type", "")
            if "html" in content_type or resp.text.strip().startswith("<"):
                logger.warning(f"[雪球] WAF拦截，切换DeepSeek搜索模式")
                return self._fetch_via_deepseek()
            resp.raise_for_status()
            data = resp.json()
            return data.get("statuses", [])
        except Exception as e:
            logger.error(f"[雪球] 获取时间线失败 user_id={self.user_id}: {e}")
            return self._fetch_via_deepseek()

    def _fetch_via_deepseek(self) -> list[dict]:
        """WAF 拦截时的降级方案：用 DeepSeek 联网搜索雪球最新发言"""
        from config.settings import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
        if not DEEPSEEK_API_KEY:
            return []
        try:
            from openai import OpenAI
            import json as _json
            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

            query = f"雪球用户 {self.guru_name} 大道无形我有型 最新发言 投资 股票 2025 2026"
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": query}],
                extra_body={"search_enabled": True},
                max_tokens=1500,
                temperature=0.1,
            )
            search_text = resp.choices[0].message.content or ""

            # 提取结构化帖子
            extract_resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": f"""从以下内容中提取 {self.guru_name} 在雪球上的投资相关发言，输出JSON数组（无markdown）：
[{{"text": "发言原文", "created_at": 时间戳毫秒或0}}]
如无则返回 []

内容：
{search_text[:3000]}"""}],
                temperature=0.1,
                max_tokens=800,
            )
            text = extract_resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
            posts = _json.loads(text)
            logger.info(f"[雪球-DS] {self.guru_name}: DeepSeek搜索找到 {len(posts)} 条发言")
            return posts if isinstance(posts, list) else []
        except Exception as e:
            logger.error(f"[雪球-DS] DeepSeek搜索失败: {e}")
            return []

    def _clean_html(self, text: str) -> str:
        """去除HTML标签"""
        if not text:
            return ""
        soup = BeautifulSoup(text, "html.parser")
        return soup.get_text(separator=" ").strip()

    def _has_investment_keyword(self, text: str) -> bool:
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in INVESTMENT_KEYWORDS)


class XTwitterCollector(BaseCollector):
    """
    X/Twitter 采集器
    方案: 使用 nitter.net RSS (无需API Key)
    备选: RapidAPI Twitter API (付费)
    """

    NITTER_INSTANCES = [
        "https://nitter.net",
        "https://nitter.1d4.us",
        "https://nitter.kavin.rocks",
    ]

    def __init__(self, username: str, guru_name: str):
        self.username = username
        self.guru_name = guru_name

    def get_source_name(self) -> str:
        return f"x:{self.guru_name}"

    def collect(self) -> list[RawEvent]:
        if not self.username or self.username == "null":
            return []

        checkpoint_key = f"x:{self.username}"
        last_tweet_id = get_checkpoint(checkpoint_key)

        tweets = self._fetch_via_nitter_rss()
        if not tweets:
            logger.warning(f"[X] {self.username}: 无法获取推文（Nitter实例可能不可用）")
            return []

        events = []
        new_last = last_tweet_id

        for tweet in tweets:
            tweet_id = tweet.get("id", "")
            if tweet_id == last_tweet_id:
                break

            text = tweet.get("text", "")
            if not self._has_investment_keyword(text):
                continue

            ts = tweet.get("timestamp", datetime.now())

            events.append(RawEvent(
                guru_name=self.guru_name,
                event_type="speech",
                source="x",
                raw_content=text,
                timestamp=ts,
                url=tweet.get("url", f"https://x.com/{self.username}"),
                metadata={
                    "tweet_id": tweet_id,
                    "platform": "x",
                    "username": self.username,
                },
            ))

            if not new_last or new_last == last_tweet_id:
                new_last = tweet_id

        if new_last and new_last != last_tweet_id:
            set_checkpoint(checkpoint_key, new_last)

        logger.info(f"[X] {self.username}: 发现 {len(events)} 条投资相关推文")
        return events

    def _fetch_via_nitter_rss(self) -> list[dict]:
        """通过 Nitter RSS 获取推文"""
        import feedparser

        for instance in self.NITTER_INSTANCES:
            try:
                rss_url = f"{instance}/{self.username}/rss"
                feed = feedparser.parse(rss_url)

                if not feed.entries:
                    continue

                tweets = []
                for entry in feed.entries[:20]:
                    link = entry.get("link", "")

                    # 过滤转发：Nitter RSS 的转发条目 URL 属于原作者而非被追踪用户
                    # 例如追踪 elonmusk，转发条目 link 为 nitter.net/OtherUser/status/...
                    if self.username.lower() not in link.lower():
                        continue

                    # 过滤文本中以 "RT @" 开头的转发
                    raw_title = entry.get("title", "")
                    if raw_title.strip().startswith("RT @"):
                        continue

                    tweet_id = entry.get("id", link).split("/")[-1]
                    text = entry.get("summary", raw_title)
                    text = BeautifulSoup(text, "html.parser").get_text(separator=" ").strip()

                    pub_date = entry.get("published_parsed")
                    if pub_date:
                        ts = datetime(*pub_date[:6])
                    else:
                        ts = datetime.now()

                    # 只处理7天内的
                    if ts < datetime.now() - timedelta(days=7):
                        continue

                    tweets.append({
                        "id": tweet_id,
                        "text": text,
                        "timestamp": ts,
                        "url": link,
                    })

                return tweets
            except Exception as e:
                logger.warning(f"[X] Nitter实例 {instance} 失败: {e}")
                continue

        return []

    def _has_investment_keyword(self, text: str) -> bool:
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in INVESTMENT_KEYWORDS)


class FMPCollector(BaseCollector):
    """
    FMP (Financial Modeling Prep) 数据源
    可获取: 13F持仓、内部人交易、机构持仓变动等
    需要 FMP_API_KEY
    """

    def __init__(self, symbol: str, guru_name: str):
        self.symbol = symbol
        self.guru_name = guru_name
        from config.settings import FMP_API_KEY, FMP_BASE_URL, FMP_STABLE_URL
        self.api_key = FMP_API_KEY
        self.base_url = FMP_BASE_URL
        self.stable_url = FMP_STABLE_URL

    def get_source_name(self) -> str:
        return f"fmp:{self.guru_name}"

    def collect(self) -> list[RawEvent]:
        if not self.api_key:
            logger.warning(f"[FMP] 未配置 FMP_API_KEY，跳过 {self.guru_name}")
            return []

        # 注意：机构持仓端点（institutional-holder / institutional-ownership）
        # 在 Starter 套餐下已被限制，且与 sec_13f 采集器功能重复，故不再调用。
        return self._fetch_insider_trades()

    def _fetch_insider_trades(self) -> list[RawEvent]:
        """获取内部人交易"""
        try:
            url = f"{self.stable_url}/insider-trading/search"
            params = {"symbol": self.symbol, "limit": 20, "apikey": self.api_key}
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            checkpoint_key = f"fmp_insider:{self.symbol}"
            last_date = get_checkpoint(checkpoint_key)

            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            events = []

            for trade in data:
                date_str = trade.get("transactionDate", "")
                if not date_str or date_str < cutoff:
                    continue
                if date_str <= (last_date or ""):
                    continue

                insider_name = trade.get("reportingName", "")
                trans_type = trade.get("transactionType", "")
                shares = abs(trade.get("securitiesTransacted", 0) or 0)
                price = trade.get("price", 0) or 0

                if "P-Purchase" in trans_type:
                    action = "买入"
                elif "S-Sale" in trans_type:
                    action = "卖出"
                else:
                    continue

                content = (
                    f"内部人 {insider_name} {action} {self.symbol}\n"
                    f"交易日期: {date_str}\n"
                    f"股数: {shares:,}\n"
                    f"价格: ${price:.2f}\n"
                    f"总额: ${shares * price:,.0f}"
                )

                events.append(RawEvent(
                    guru_name=self.guru_name,
                    event_type="trade",
                    source="fmp_insider",
                    raw_content=content,
                    timestamp=datetime.strptime(date_str, "%Y-%m-%d"),
                    metadata={
                        "action": action,
                        "ticker": self.symbol,
                        "insider": insider_name,
                        "shares": shares,
                        "price": price,
                    },
                ))

            if events:
                set_checkpoint(checkpoint_key, data[0].get("transactionDate", ""))

            return events
        except Exception as e:
            logger.error(f"[FMP] 内部人交易获取失败 {self.symbol}: {e}")
            return []
