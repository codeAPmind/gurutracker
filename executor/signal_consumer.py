"""
Guru Tracker 信号消费 & 自动下单

调度（cron 09:35 ET，周一至周五）：
  python -m executor.signal_consumer

买入条件（AND，2026-06~09 历史信号 sweep 得出）：
1. score >= MIN_SCORE（触发阈值）
2. 价格相对近60日高点回调 >= GURU_DRAWDOWN_PCT（默认30%）
3. 该笔信号仓位占比（ARK etf_percent）> GURU_MIN_POSITION_PCT（默认0.10%）

出场规则（方案D1，2026-09-09 采用，详见 STRATEGY.md §5.6）：
1. 跌破 -15% → 止损（封尾部风险）
2. 价格回升到布林带中轨 MA20 → 均值回归完成，主出场路径（约90%）
3. 持有超 20 个交易日 → 安全兜底

为什么不用固定持有天数：买入时 %b 中位仅11%（贴近布林下轨），
回到中轨即均值回归完成，有市场含义；固定N日纯属人为设定。
实测 胜率71%→79%，最大连亏2→1笔，平均持有9.5→4.9日，
槽位周转快使同一信号流多成交36%（14→19笔）。

仓位管理：本金5000美元，单笔额度1000美元，最多同时持仓 GURU_MAX_POSITIONS（默认5）只。
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from config.settings import DB_PATH, FMP_API_KEY, FMP_STABLE_URL, GURU_TRADE_ENV
from executor.position_manager import (
    BUDGET_USD, MAX_HOLD_DAYS, MAX_POSITIONS, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    _trading_days_held, already_holding, check_exit_signal, close_position,
    get_open_positions, init_db, mark_closing, open_position,
)
from executor.futu_trader import GuruFutuTrader
from notifier.feishu_bot import send_text_to_feishu

logger = logging.getLogger(__name__)

_ENV_TAG = "🧪模拟盘" if GURU_TRADE_ENV.upper() != "REAL" else "💰实盘"


def _notify(text: str) -> None:
    """所有 executor 下单/成交通知统一走这里，方便在飞书里区分来源"""
    msg = f"【GuruTracker执行器·{_ENV_TAG}】\n{text}"
    try:
        send_text_to_feishu(msg)
    except Exception as e:
        logger.error("[consumer] 飞书通知发送失败: %s", e)

MIN_SCORE = float(os.getenv("GURU_MIN_SCORE", "50"))
DRAWDOWN_PCT = float(os.getenv("GURU_DRAWDOWN_PCT", "30"))       # 相对60日高点回调百分比（正数）
MIN_POSITION_PCT = float(os.getenv("GURU_MIN_POSITION_PCT", "0.10"))  # ARK仓位占比阈值
BOLL_PERIOD = int(os.getenv("GURU_BOLL_PERIOD", "20"))            # 布林中轨周期(布林带标准默认值)

_POS_PCT_RE = re.compile(r"占基金仓位: ([\d.]+)%")


def _us_today() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except ImportError:
        tz = timezone(timedelta(hours=-5))
    return datetime.now(tz).strftime("%Y-%m-%d")


def _fetch_pending_signals() -> list[dict]:
    """返回近2日 score >= MIN_SCORE 的 buy 信号（去重 ticker 取最高分）"""
    today = _us_today()
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT id, ticker, score, action, guru_name, event_date, raw_content, created_at
               FROM signals
               WHERE score >= ?
                 AND action IN ('buy', '买入')
                 AND DATE(created_at) >= ?
               ORDER BY score DESC""",
            (MIN_SCORE, cutoff),
        ).fetchall()
    finally:
        con.close()

    seen: dict[str, dict] = {}
    for r in rows:
        t = r["ticker"]
        if t not in seen or r["score"] > seen[t]["score"]:
            seen[t] = dict(r)
    return list(seen.values())


def _extract_position_pct(raw_content: str) -> float:
    m = _POS_PCT_RE.search(raw_content or "")
    return float(m.group(1)) if m else 0.0


def _fetch_closes(ticker: str) -> Optional[list]:
    """拉取 FMP 日线收盘价（按日期降序，closes[0]=最新）。买入/卖出共用，每票每轮只请求一次。"""
    if not FMP_API_KEY:
        logger.warning("[consumer] FMP_API_KEY 未配置，无法获取历史价格")
        return None
    url = f"{FMP_STABLE_URL}/historical-price-eod/full"
    try:
        params = {"symbol": ticker, "apikey": FMP_API_KEY}
        logger.debug("[consumer][API] GET %s?symbol=%s", url, ticker)
        t0 = datetime.now()
        resp = requests.get(url, params=params, timeout=15)
        elapsed_ms = (datetime.now() - t0).total_seconds() * 1000
        logger.debug("[consumer][API] FMP historical-price-eod %s → HTTP %d (%.0fms)",
                     ticker, resp.status_code, elapsed_ms)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            logger.warning("[consumer][API] FMP %s 无返回数据", ticker)
            return None
        closes = [d["close"] for d in data[:80] if d.get("close")]
        if len(closes) < 25:
            logger.warning("[consumer][API] FMP %s 有效收盘价不足 (%d条)", ticker, len(closes))
            return None
        return closes
    except Exception as e:
        logger.error("[consumer][API] FMP historical-price-eod %s 失败: %s", ticker, e)
        return None


def _get_drawdown_pct(ticker: str, closes: Optional[list] = None) -> Optional[float]:
    """相对近60个交易日收盘价最高点的回调百分比（正数，越大回调越深）"""
    if closes is None:
        closes = _fetch_closes(ticker)
    if not closes:
        return None
    window = closes[:60]
    latest, recent_high = window[0], max(window)
    if recent_high <= 0:
        return None
    dd = (1 - latest / recent_high) * 100
    logger.debug("[consumer] %s 回调计算: 最新=%.2f 近60日高=%.2f → 回调%.1f%%",
                 ticker, latest, recent_high, dd)
    return dd


def _get_boll_mid(ticker: str, closes: Optional[list] = None) -> Optional[float]:
    """布林带中轨 = MA(N) 收盘均线。

    关键口径：跳过 closes[0]。线上在 09:35 运行时当日尚未收盘，
    FMP 的 closes[0] 是当日盘中价（未完成bar），必须用前 N 个**已完成**收盘价，
    与回测中"用前一日收盘计算带、与当日开盘价比较"的口径一致。
    """
    if closes is None:
        closes = _fetch_closes(ticker)
    if not closes or len(closes) < BOLL_PERIOD + 1:
        return None
    completed = closes[1:BOLL_PERIOD + 1]     # 跳过当日未完成bar
    ma = sum(completed) / len(completed)
    logger.debug("[consumer] %s 布林中轨: MA%d=%.4f (用%s~%s的已完成收盘)",
                 ticker, BOLL_PERIOD, ma, len(completed), "前1日")
    return ma


def _passes_entry_filter(sig: dict) -> bool:
    """回调幅度 + 仓位占比双重门槛"""
    ticker = sig["ticker"]
    pos_pct = _extract_position_pct(sig.get("raw_content", ""))

    logger.debug("[信号评估] %s score=%.0f 来源=%s 信号日=%s",
                 ticker, sig["score"], sig.get("guru_name", ""), sig.get("event_date", ""))

    if pos_pct <= MIN_POSITION_PCT:
        logger.info("[信号过滤] ❌ %s 仓位占比%.2f%% <= 阈值%.2f%%（条件2未过）",
                    ticker, pos_pct, MIN_POSITION_PCT)
        return False

    drawdown = _get_drawdown_pct(ticker, closes=_fetch_closes(ticker))
    if drawdown is None:
        logger.warning("[信号过滤] ❌ %s 无法获取回调幅度（FMP数据缺失），保守跳过", ticker)
        return False
    if drawdown < DRAWDOWN_PCT:
        logger.info("[信号过滤] ❌ %s 回调%.1f%% < 阈值%.1f%%（条件3未过）仓位=%.2f%%",
                    ticker, drawdown, DRAWDOWN_PCT, pos_pct)
        return False

    logger.info("[信号过滤] ✅ %s 通过全部条件: score=%.0f 回调=%.1f%%(>=%.0f%%) 仓位=%.2f%%(>%.2f%%)",
                ticker, sig["score"], drawdown, DRAWDOWN_PCT, pos_pct, MIN_POSITION_PCT)
    return True


def run() -> None:
    log_level = os.getenv("GURU_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    logger.info("=" * 70)
    logger.info("[启动] GuruTracker 执行器 | 环境=%s | 美东时间=%s",
                _ENV_TAG, _us_today())
    logger.info("[配置] 单笔预算=$%.0f 最大持仓=%d只 | 买入: score>=%.0f 回调>=%.0f%% 仓位>%.2f%%",
                BUDGET_USD, MAX_POSITIONS, MIN_SCORE, DRAWDOWN_PCT, MIN_POSITION_PCT)
    logger.info("=" * 70)

    init_db()

    signals = _fetch_pending_signals()
    logger.info("[信号读取] 从 signals.db 取到 %d 条待评估买入信号 (score>=%.0f, 近2日)",
                len(signals), MIN_SCORE)
    for s in signals:
        logger.info("  · %-8s score=%-4.0f 信号日=%s 来源=%s",
                    s["ticker"], s["score"], s.get("event_date", "?"), s.get("guru_name", "?"))

    signals = [s for s in signals if _passes_entry_filter(s)]
    logger.info("[信号过滤] 最终 %d 条通过全部条件%s",
                len(signals),
                "：" + ", ".join(s["ticker"] for s in signals) if signals else "（本轮无买入）")

    trader = GuruFutuTrader()
    try:
        trader.connect()
    except Exception as e:
        logger.error("[启动失败] Futu 连接失败，本轮终止: %s", e)
        return

    buy_placed, sell_placed = 0, 0
    try:
        # ── 新信号买入 ────────────────────────────────────────────────
        logger.info("-" * 70)
        logger.info("[买入阶段] 开始")
        open_count = len(get_open_positions())
        slots_left = max(0, MAX_POSITIONS - open_count)
        if slots_left == 0:
            logger.info("[买入阶段] 已达最大持仓数 %d/%d，本轮不新开仓", open_count, MAX_POSITIONS)
        else:
            logger.info("[买入阶段] 当前持仓 %d/%d，本轮最多可开 %d 个新仓",
                        open_count, MAX_POSITIONS, slots_left)

        for sig in signals:
            if slots_left <= 0:
                logger.info("[买入阶段] 持仓槽位已用尽，跳过剩余 %d 个信号", len(signals) - buy_placed)
                break

            ticker = sig["ticker"]
            if not ticker or ticker in ("", "N/A"):
                logger.warning("[买入阶段] 跳过无效 ticker: %r", ticker)
                continue
            if already_holding(ticker, trader=trader):
                logger.info("[买入阶段] ⏭️  %s 已持仓，跳过", ticker)
                continue

            price = trader.get_price(ticker)
            if not price:
                logger.warning("[买入阶段] ⚠️  %s 无法获取当前价格，跳过", ticker)
                continue

            logger.info("[买入阶段] 📤 准备买入 %s: score=%.0f 现价=$%.2f 预算=$%.0f 预计=%d股",
                        ticker, sig["score"], price, BUDGET_USD, int(BUDGET_USD / price))

            def _on_fill(qty: int, avg_price: float, _ticker=ticker, _sig=sig, _oid_holder=[None]):
                logger.info("[买入成交] ✅ %s qty=%d 均价=$%.4f 金额=$%.0f order=%s",
                            _ticker, qty, avg_price, qty * avg_price, _oid_holder[0] or "?")
                open_position(_ticker, _sig["id"], _oid_holder[0] or "", qty, avg_price)
                _notify(
                    f"✅ 买入成交\n"
                    f"股票: {_ticker}\n"
                    f"数量: {qty} 股\n"
                    f"成交价: ${avg_price:.2f}\n"
                    f"金额: ${qty * avg_price:,.0f}\n"
                    f"信号分数: {_sig['score']:.0f}"
                )

            order_id = trader.place_entry_order(
                ticker=ticker,
                budget_usd=BUDGET_USD,
                ref_price=price,
                on_fill=_on_fill,
            )
            if order_id:
                _on_fill.__closure__[3].cell_contents[0] = order_id  # patch _oid_holder
                slots_left -= 1
                buy_placed += 1
                _notify(
                    f"📤 已下单 BUY\n"
                    f"股票: {ticker}\n"
                    f"参考价: ${price:.2f}\n"
                    f"预算: ${BUDGET_USD:,.0f}\n"
                    f"order_id: {order_id}"
                )
            else:
                logger.error("[买入阶段] ❌ %s 下单失败", ticker)
                _notify(
                    f"⚠️ 买入下单失败\n"
                    f"股票: {ticker}\n"
                    f"参考价: ${price:.2f}  预算: ${BUDGET_USD:,.0f}\n"
                    f"信号分数: {sig['score']:.0f}\n"
                    f"请查看 logs/cron.log 中 place_order 的错误详情"
                )

        # ── 持仓止盈/止损/到期检查 ────────────────────────────────────
        logger.info("-" * 70)
        positions = get_open_positions()
        tp_desc = f"止盈+{TAKE_PROFIT_PCT*100:.0f}%" if TAKE_PROFIT_PCT > 0 else "止盈已禁用"
        logger.info("[卖出阶段] 检查 %d 个持仓 (回中轨MA%d / %s / 止损%.0f%% / 上限%d日)",
                    len(positions), BOLL_PERIOD, tp_desc, STOP_LOSS_PCT * 100, MAX_HOLD_DAYS)

        for pos in positions:
            if pos["status"] == "closing":
                logger.info("[卖出阶段] ⏭️  %s 已在平仓中(order=%s)，跳过",
                            pos["ticker"], pos.get("exit_order_id", "?"))
                continue
            ticker = pos["ticker"]
            price = trader.get_price(ticker)
            if not price:
                logger.warning("[卖出阶段] ⚠️  %s 无法获取现价，本轮跳过检查", ticker)
                continue

            entry = pos["entry_price"] or 0
            pnl = (price / entry - 1) * 100 if entry else 0
            held = _trading_days_held(pos["entry_date"]) if pos.get("entry_date") else 0
            boll_mid = _get_boll_mid(ticker)

            reason = check_exit_signal(pos, price, boll_mid=boll_mid)
            if not reason:
                mid_txt = f" 中轨=${boll_mid:.2f}(差{(boll_mid/price-1)*100:+.1f}%)" if boll_mid else " 中轨=N/A"
                logger.info("[卖出阶段] 持有中 %-8s 现价=$%-8.2f 成本=$%-8.2f 浮盈=%+6.2f%% 持有%d日%s",
                            ticker, price, entry, pnl, held, mid_txt)
                continue

            reason_label = {"take_profit": "止盈", "stop_loss": "止损",
                            "boll_mid": "回归中轨", "max_hold": "到期平仓"}.get(reason, reason)
            logger.info("[卖出阶段] 🔔 %s 触发【%s】现价=$%.2f 成本=$%.2f 盈亏=%+.2f%% 持有%d日",
                        ticker, reason_label, price, entry, pnl, held)

            def _on_exit_fill(qty: int, avg_price: float, _pos=pos, _ticker=ticker, _reason=reason_label):
                close_position(_pos["id"], avg_price)
                entry = _pos.get("entry_price") or 0
                pnl_pct = (avg_price / entry - 1) * 100 if entry else 0
                logger.info("[卖出成交] 🔴 %s qty=%d 均价=$%.4f 成本=$%.4f 盈亏=%+.2f%% (%s)",
                            _ticker, qty, avg_price, entry, pnl_pct, _reason)
                _notify(
                    f"🔴 卖出成交（{_reason}）\n"
                    f"股票: {_ticker}\n"
                    f"数量: {qty} 股\n"
                    f"成交价: ${avg_price:.2f}\n"
                    f"买入价: ${entry:.2f}\n"
                    f"盈亏: {pnl_pct:+.2f}%"
                )

            exit_order_id = trader.place_sell_order(
                ticker=ticker,
                qty=pos["qty"],
                ref_price=price,
                purpose=reason,
                on_fill=_on_exit_fill,
            )
            if exit_order_id:
                mark_closing(pos["id"], exit_order_id)
                sell_placed += 1
                _notify(
                    f"📤 已下单 SELL（触发: {reason_label}）\n"
                    f"股票: {ticker}\n"
                    f"数量: {pos['qty']} 股\n"
                    f"参考价: ${price:.2f}\n"
                    f"买入价: ${pos['entry_price'] or 0:.2f}\n"
                    f"order_id: {exit_order_id}"
                )
            else:
                logger.error("[卖出阶段] ❌ %s 卖单下单失败（%s）", ticker, reason_label)
                _notify(
                    f"🚨 卖出下单失败（{reason_label}）\n"
                    f"股票: {ticker}  持仓{pos['qty']}股\n"
                    f"现价: ${price:.2f}  成本: ${entry:.2f}  盈亏: {pnl:+.2f}%\n"
                    f"⚠️ 仓位仍未平，请人工确认"
                )

    finally:
        logger.info("-" * 70)
        logger.info("[本轮结束] 买单%d笔 卖单%d笔 | 当前持仓%d/%d | 环境=%s",
                    buy_placed, sell_placed, len(get_open_positions()), MAX_POSITIONS, _ENV_TAG)
        logger.info("=" * 70)
        trader.close()


if __name__ == "__main__":
    run()
