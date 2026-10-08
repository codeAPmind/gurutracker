"""FINRA 日度空头成交比（short / total），用作买入否决。

09:35 ET 运行时当日 CNMS 文件尚未公布，只用已完成交易日。
近 N 日均值缺失时不否决（数据问题不应误杀信号）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

from config.settings import BASE_DIR

logger = logging.getLogger(__name__)

FINRA_CNMS = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt"
CACHE_DIR = BASE_DIR / "data" / "finra_cnms"
_LOOKBACK_CALENDAR_DAYS = 18
_TIMEOUT = 12

_session: requests.Session | None = None


def _sess() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers["User-Agent"] = "GuruTracker/1.0 (research; short-volume veto)"
        _session = s
    return _session


def _us_today() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except ImportError:
        tz = timezone(timedelta(hours=-4))
    return datetime.now(tz).strftime("%Y-%m-%d")


def _candidate_dates(asof: str | None = None) -> list[str]:
    """asof 当天不含（09:35 时当日文件还没有）。"""
    end = datetime.strptime(asof or _us_today(), "%Y-%m-%d")
    dates = []
    d = end - timedelta(days=1)
    start = end - timedelta(days=_LOOKBACK_CALENDAR_DAYS)
    while d >= start:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return dates


def _load_day(day: str) -> pd.DataFrame | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ymd = day.replace("-", "")
    path = CACHE_DIR / f"CNMSshvol{ymd}.txt"
    text = None
    if path.exists() and path.stat().st_size > 80:
        text = path.read_text(errors="replace")
    else:
        url = FINRA_CNMS.format(ymd=ymd)
        try:
            r = _sess().get(url, timeout=_TIMEOUT)
            if r.status_code != 200 or "Symbol" not in r.text[:80]:
                logger.debug("[short_volume] %s HTTP %s，跳过", day, r.status_code)
                return None
            text = r.text
            path.write_text(text)
        except Exception as e:
            logger.warning("[short_volume] 下载 %s 失败: %s", day, e)
            return None
    try:
        df = pd.read_csv(StringIO(text), sep="|")
    except Exception as e:
        logger.warning("[short_volume] 解析 %s 失败: %s", day, e)
        return None
    df.columns = [c.strip() for c in df.columns]
    if "Symbol" not in df.columns or "ShortVolume" not in df.columns:
        return None
    df["ticker"] = df["Symbol"].astype(str).str.upper()
    df["short"] = pd.to_numeric(df["ShortVolume"], errors="coerce")
    df["total"] = pd.to_numeric(df["TotalVolume"], errors="coerce")
    df = df[(df["total"] > 0) & df["short"].notna()]
    df["sr"] = df["short"] / df["total"]
    return df[["ticker", "sr"]]


def short_ratio_m5(ticker: str, n: int = 5, asof: str | None = None) -> float | None:
    """最近 n 个有数据交易日的空头成交比均值。不足 n 天返回 None。"""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None
    vals: list[float] = []
    used: list[str] = []
    for day in _candidate_dates(asof):
        df = _load_day(day)
        if df is None:
            continue
        row = df.loc[df["ticker"] == ticker]
        if row.empty:
            continue
        vals.append(float(row["sr"].iloc[0]))
        used.append(day)
        if len(vals) >= n:
            break
    if len(vals) < n:
        logger.warning("[short_volume] %s 只有 %d/%d 日数据（%s），不否决",
                       ticker, len(vals), n, ",".join(used) or "无")
        return None
    m = sum(vals) / len(vals)
    logger.info("[short_volume] %s m%d=%.3f 日=%s", ticker, n, m, ",".join(used))
    return m
