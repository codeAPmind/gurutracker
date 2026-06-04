#!/usr/bin/env python3
"""
主入口 — 支持两种运行模式:
  1. 直接运行指定采集器 (cron模式): python scheduler.py --job ark_trades
  2. APScheduler 守护进程模式: python scheduler.py --daemon
"""

import argparse
import logging
import sys
import os
from pathlib import Path

# 确保项目根目录在 Python 路径中
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import LOG_FILE, LOG_DIR, MIN_CONFIDENCE_TO_PUSH
from storage.db import init_db, get_today_signals, get_recent_signals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("scheduler")

CONFIDENCE_ORDER = {"低": 0, "中": 1, "高": 2}


def load_gurus():
    import yaml
    config_path = Path(__file__).parent / "config" / "gurus.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["gurus"]


def build_collectors(gurus: list) -> dict:
    from collectors.sec_13f import SEC13FCollector
    from collectors.sec_form4 import SECForm4Collector
    from collectors.ark_trades import ARKTradesCollector
    from collectors.congress import CongressCollector
    from collectors.congress_deepseek import CongressDeepSeekCollector
    from collectors.social_media import XueqiuCollector, XTwitterCollector, FMPCollector

    collectors = {
        "sec_13f": [],
        "sec_form4": [],
        "ark_trades": [],
        "congress": [],
        "social_media": [],
    }

    for guru in gurus:
        for src in guru.get("sources", []):
            t = src["type"]
            if t == "sec_13f":
                collectors["sec_13f"].append(SEC13FCollector(src["cik"], guru["name"]))
            elif t == "sec_form4":
                collectors["sec_form4"].append(SECForm4Collector(src["cik"], guru["name"]))
            elif t == "ark_trades":
                collectors["ark_trades"].append(ARKTradesCollector(src["fund_codes"]))
            elif t == "congress":
                # 优先用 DeepSeek 联网搜索版（无需 QuiverQuant Key）
                collectors["congress"].append(CongressDeepSeekCollector(src["member_id"], guru["name"]))
            elif t == "social_media":
                platform = src.get("platform", "")
                if platform == "xueqiu":
                    collectors["social_media"].append(XueqiuCollector(src["user_id"], guru["name"]))
                elif platform == "x":
                    username = src.get("username", "")
                    if username and username != "null":
                        collectors["social_media"].append(XTwitterCollector(username, guru["name"]))
            elif t == "fmp_13f":
                collectors.setdefault("fmp", []).append(FMPCollector(src.get("symbol", ""), guru["name"]))

    return collectors


def process_events(events: list, gurus_config: list):
    from processor.deepseek_engine import extract_signal
    from processor.confidence_scorer import score_signal
    from processor.deduplicator import is_duplicate
    from notifier.feishu_bot import push_signal
    from storage.db import save_signal

    recent_signals = get_recent_signals(hours=168)  # 近7天用于交叉验证
    pushed = 0

    for event in events:
        try:
            signal_data = extract_signal(event.raw_content, event.guru_name, event.event_type)

            if not signal_data.get("has_signal"):
                continue

            signal_data["guru_name"] = event.guru_name
            signal_data["source"] = event.source
            signal_data["raw_content"] = event.raw_content
            signal_data["url"] = event.url or ""

            # 去重
            if is_duplicate(signal_data):
                continue

            # 评分
            guru_config = next((g for g in gurus_config if g["name"] == event.guru_name), {})
            event_date = event.timestamp.strftime("%Y-%m-%d") if event.timestamp else ""
            signal = score_signal(signal_data, guru_config, recent_signals, event_date=event_date)

            # 存储
            save_signal(signal)
            logger.info(f"✅ 新信号: {signal.guru_name} {signal.action} {signal.ticker} "
                        f"[{signal.confidence}/{signal.score}分] ({signal.source})")

            # 推送
            min_level = CONFIDENCE_ORDER.get(MIN_CONFIDENCE_TO_PUSH, 1)
            signal_level = CONFIDENCE_ORDER.get(signal.confidence, 0)
            if signal_level >= min_level:
                push_signal(signal)
                pushed += 1

        except Exception as e:
            logger.error(f"处理事件失败: {e}", exc_info=True)

    return pushed


def run_job(job_name: str):
    """运行指定的采集任务（供 cron 调用）"""
    logger.info(f"🚀 开始任务: {job_name}")
    init_db()

    gurus_config = load_gurus()
    collectors_map = build_collectors(gurus_config)

    job_map = {
        "sec_13f": collectors_map.get("sec_13f", []),
        "sec_form4": collectors_map.get("sec_form4", []),
        "ark_trades": collectors_map.get("ark_trades", []),
        "congress": collectors_map.get("congress", []),
        "social_media": collectors_map.get("social_media", []),
        "fmp": collectors_map.get("fmp", []),
        "all": [c for cs in collectors_map.values() for c in cs],
    }

    target_collectors = job_map.get(job_name, [])
    if not target_collectors:
        logger.warning(f"未找到任务 '{job_name}' 或没有配置的采集器")
        return

    all_events = []
    for collector in target_collectors:
        events = collector.safe_collect()
        all_events.extend(events)
        logger.info(f"  [{collector.get_source_name()}] 采集到 {len(events)} 条事件")

    if all_events:
        pushed = process_events(all_events, gurus_config)
        logger.info(f"✅ 任务完成: {job_name} | 事件={len(all_events)} 已推送={pushed}")
    else:
        logger.info(f"任务完成: {job_name} | 无新事件")


def run_daily_digest():
    """每日汇总推送"""
    from notifier.feishu_bot import push_daily_digest
    init_db()
    signals = get_today_signals()
    if signals:
        push_daily_digest(signals)
        logger.info(f"📋 每日汇总已推送: {len(signals)} 条信号")
    else:
        logger.info("今日暂无信号，跳过汇总推送")


def run_daemon():
    """APScheduler 守护进程模式"""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    from config.settings import SCHEDULE

    init_db()
    gurus_config = load_gurus()
    collectors_map = build_collectors(gurus_config)

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")

    trigger_map = {
        "sec_13f":      CronTrigger.from_crontab(SCHEDULE["sec_13f"]["cron"]),
        "sec_form4":    IntervalTrigger(hours=SCHEDULE["sec_form4"]["interval_hours"]),
        "ark_trades":   CronTrigger.from_crontab(SCHEDULE["ark_trades"]["cron"]),
        "congress":     IntervalTrigger(hours=SCHEDULE["congress"]["interval_hours"]),
        "social_media": IntervalTrigger(minutes=SCHEDULE["social_media"]["interval_minutes"]),
    }

    for group, trigger in trigger_map.items():
        scheduler.add_job(
            run_job,
            trigger=trigger,
            args=[group],
            id=f"collector_{group}",
            name=f"采集: {group}",
            misfire_grace_time=300,
        )

    scheduler.add_job(
        run_daily_digest,
        trigger=CronTrigger(hour=21, minute=0, timezone="Asia/Shanghai"),
        id="daily_digest",
        name="每日汇总",
    )

    logger.info("🕐 调度器启动，已注册任务:")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.name} | {job.trigger}")

    scheduler.start()


def main():
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

    parser = argparse.ArgumentParser(description="Guru Tracker — 投资大佬跟单系统")
    parser.add_argument("--job", choices=[
        "sec_13f", "sec_form4", "ark_trades", "congress", "social_media", "fmp", "all", "daily_digest"
    ], help="运行指定采集任务（适合cron调用）")
    parser.add_argument("--daemon", action="store_true", help="以APScheduler守护进程模式运行")

    args = parser.parse_args()

    if args.daemon:
        run_daemon()
    elif args.job:
        if args.job == "daily_digest":
            run_daily_digest()
        else:
            run_job(args.job)
    else:
        parser.print_help()
        print("\n示例:")
        print("  python scheduler.py --job ark_trades    # 手动运行ARK采集")
        print("  python scheduler.py --job all           # 运行所有采集器")
        print("  python scheduler.py --job daily_digest  # 发送每日汇总")
        print("  python scheduler.py --daemon            # 守护进程模式")


if __name__ == "__main__":
    main()
