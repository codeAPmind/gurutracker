"""
飞书自定义机器人 Webhook 推送

- FEISHU_WEBHOOK_URL：Guru 信号卡片
- FEISHU_TRADE_WEBHOOK：下单 / 成交 / 止损（与 YiDong AutoTrader H 同一机器人）

交易通知必须同步发送：executor 是 cron 短进程，daemon 线程会在进程退出时被杀掉。
"""

from __future__ import annotations

import requests
import time
import hmac
import hashlib
import base64
import logging
from datetime import datetime

from config.settings import (
    FEISHU_WEBHOOK_URL,
    FEISHU_WEBHOOK_SECRET,
    FEISHU_TRADE_WEBHOOK,
    GURU_TRADE_ENV,
)
from notifier.card_templates import build_signal_card, build_daily_digest_card

logger = logging.getLogger(__name__)

_PURPOSE_CN = {
    "ENTRY": "建仓",
    "take_profit": "止盈",
    "stop_loss": "止损",
    "boll_mid": "回归中轨",
    "max_hold": "到期平仓",
    "stop_escalate": "止损升级市价",
    "market_sell": "市价卖出",
    "sell": "卖出",
}


def _env_tag() -> str:
    return "🧪模拟盘" if str(GURU_TRADE_ENV).upper() != "REAL" else "💰实盘"


def _purpose_cn(purpose: str) -> str:
    return _PURPOSE_CN.get(purpose, purpose or "交易")


def _gen_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _post_webhook(webhook: str, body: dict, label: str = "飞书") -> bool:
    if not webhook:
        logger.warning("[%s] webhook 未配置，跳过推送", label)
        return False
    try:
        resp = requests.post(webhook, json=body, timeout=10)
        result = {}
        try:
            result = resp.json()
        except Exception:
            result = {"raw": resp.text[:200]}
        if resp.status_code == 200 and result.get("code", 0) == 0:
            return True
        logger.error("[%s] 推送失败 HTTP %s: %s", label, resp.status_code, result)
        return False
    except Exception as e:
        logger.error("[%s] 请求异常: %s", label, e)
        return False


def send_to_feishu(card_payload: dict) -> bool:
    if not FEISHU_WEBHOOK_URL:
        logger.warning("[飞书] 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return False

    body = {"msg_type": "interactive", "card": card_payload}

    if FEISHU_WEBHOOK_SECRET:
        timestamp = str(int(time.time()))
        sign = _gen_sign(timestamp, FEISHU_WEBHOOK_SECRET)
        body["timestamp"] = timestamp
        body["sign"] = sign

    return _post_webhook(FEISHU_WEBHOOK_URL, body, "飞书信号")


def send_text_to_feishu(text: str) -> bool:
    """发送纯文本到信号群（兼容旧调用）。交易通知请用 send_trade_text。"""
    body = {"msg_type": "text", "content": {"text": text}}
    return _post_webhook(FEISHU_WEBHOOK_URL, body, "飞书信号")


def send_trade_text(text: str) -> bool:
    """交易群纯文本。"""
    body = {"msg_type": "text", "content": {"text": text}}
    ok = _post_webhook(FEISHU_TRADE_WEBHOOK, body, "飞书交易")
    if ok:
        logger.info("[飞书交易] 文本已发送")
    return ok


def send_trade_card(title: str, content: str, color: str = "blue") -> bool:
    """交易群卡片，对齐 YiDong AutoTrader H 的 card 格式。"""
    body = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}}
            ],
        },
    }
    ok = _post_webhook(FEISHU_TRADE_WEBHOOK, body, "飞书交易")
    if ok:
        logger.info("[飞书交易] 卡片已发送: %s", title)
    return ok


def notify_order_placed(order: dict) -> bool:
    """下单成功通知。"""
    side = str(order.get("side", "")).upper()
    side_cn = "🟢买入" if side == "BUY" else "🔴卖出"
    ticker = order.get("ticker") or str(order.get("code", "")).replace("US.", "")
    purpose = _purpose_cn(str(order.get("purpose", "")))
    qty = int(order.get("qty", 0) or 0)
    price = float(order.get("price", 0) or 0)
    title = f"{side_cn} {ticker} [{purpose}] · GuruTracker {_env_tag()}"
    lines = [
        f"**策略**: GuruTracker  {_env_tag()}",
        f"**股票**: `{ticker}`",
        f"**方向**: {side_cn}  **用途**: {purpose}",
        f"**数量**: {qty} 股",
        f"**委托价**: ${price:.2f}",
        f"**金额**: ${qty * price:,.0f}",
        f"**订单ID**: {order.get('order_id', 'N/A')}",
        f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    return send_trade_card(title, "\n".join(lines), "green" if side == "BUY" else "orange")


def notify_order_filled(order: dict) -> bool:
    """成交回报通知。"""
    side = str(order.get("side", "")).upper()
    side_cn = "🟢买入" if "BUY" in side else "🔴卖出"
    ticker = order.get("ticker") or str(order.get("code", "")).replace("US.", "")
    purpose = _purpose_cn(str(order.get("purpose", "")))
    qty = int(order.get("filled_qty", 0) or 0)
    price = float(order.get("filled_price", 0) or 0)
    title = f"✅ 成交 {ticker} {side_cn} · GuruTracker {_env_tag()}"
    lines = [
        f"**策略**: GuruTracker  {_env_tag()}",
        f"**股票**: `{ticker}`",
        f"**方向**: {side_cn}  **用途**: {purpose}",
        f"**成交量**: {qty} 股 @ ${price:.2f}",
        f"**成交金额**: ${qty * price:,.0f}",
        f"**订单ID**: {order.get('order_id', '')}",
        f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    pnl = order.get("pnl_pct")
    if pnl is not None:
        lines.insert(-2, f"**盈亏**: {float(pnl):+.2f}%")
    return send_trade_card(title, "\n".join(lines), "green")


def notify_trade_error(title: str, detail: str) -> bool:
    """下单/连接失败告警。"""
    content = f"**策略**: GuruTracker  {_env_tag()}\n{detail}"
    return send_trade_card(f"🚨 {title} · GuruTracker {_env_tag()}", content, "red")


def notify_reconcile(summary: dict) -> bool:
    """启动对账结果。有幽灵仓/补记/未跟踪/查询失败时用橙色，否则灰色。"""
    ghosts = summary.get("ghosts") or []
    adopted = summary.get("adopted") or []
    qty_fixed = summary.get("qty_fixed") or []
    unmanaged = summary.get("unmanaged") or []
    reset_closing = summary.get("reset_closing") or []
    query_failed = bool(summary.get("query_failed"))
    lines = [
        f"**策略**: GuruTracker  {_env_tag()}",
        f"**券商持仓**: {summary.get('broker_count', 0)} 只",
        f"**本地跟踪**: {summary.get('local_count', 0)} 只",
    ]
    if query_failed:
        lines.append("**券商查询失败，本轮未改本地持仓（避免误删）**")
        if summary.get("error"):
            lines.append(f"**原因**: {summary['error']}")
    if ghosts:
        lines.append("**幽灵仓（库有券商无，已清）**: " +
                     ", ".join(f"{g['ticker']} {g['qty']}股" for g in ghosts))
    if adopted:
        lines.append("**补记（券商有、库无或 pending）**: " +
                     ", ".join(f"{a['ticker']} {a['qty']}股" for a in adopted))
    if qty_fixed:
        lines.append("**数量已按券商修正**: " +
                     ", ".join(f"{q['ticker']} {q['from_qty']}→{q['to_qty']}" for q in qty_fixed))
    if reset_closing:
        lines.append("**closing 改回 open（卖单丢失，重新纳入出场）**: " +
                     ", ".join(reset_closing))
    if unmanaged:
        lines.append("**未跟踪（不自动卖，可能是别的策略/手建）**: " +
                     ", ".join(f"{u['ticker']} {u['qty']}股" for u in unmanaged))
    if not (query_failed or ghosts or adopted or qty_fixed or unmanaged or reset_closing):
        lines.append("账一致，无需修正。")
    color = "orange" if (query_failed or ghosts or adopted or unmanaged or reset_closing) else "grey"
    return send_trade_card(f"📒 持仓对账 · GuruTracker {_env_tag()}", "\n".join(lines), color)


def notify_run_summary(buy_placed: int, sell_placed: int, open_count: int, max_positions: int) -> bool:
    title = f"📋 GuruTracker 本轮结束 · {_env_tag()}"
    content = "\n".join([
        f"**买单**: {buy_placed} 笔",
        f"**卖单**: {sell_placed} 笔",
        f"**当前持仓**: {open_count}/{max_positions}",
        f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ])
    color = "blue" if (buy_placed or sell_placed) else "grey"
    return send_trade_card(title, content, color)


def push_signal(signal) -> bool:
    card = build_signal_card(signal)
    ok = send_to_feishu(card)
    if ok:
        logger.info(f"[飞书] 推送成功: {signal.guru_name} {signal.action} {signal.ticker}")
    return ok


def push_daily_digest(signals: list) -> bool:
    if not signals:
        return True
    signal_dicts = []
    for s in signals:
        if hasattr(s, "__dict__"):
            signal_dicts.append(s.__dict__)
        elif isinstance(s, dict):
            signal_dicts.append(s)
    card = build_daily_digest_card(signal_dicts)
    return send_to_feishu(card)
