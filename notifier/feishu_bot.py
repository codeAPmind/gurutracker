"""
飞书自定义机器人 Webhook 推送
"""

import requests
import time
import hmac
import hashlib
import base64
import logging
from config.settings import FEISHU_WEBHOOK_URL, FEISHU_WEBHOOK_SECRET
from notifier.card_templates import build_signal_card, build_daily_digest_card

logger = logging.getLogger(__name__)


def _gen_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


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

    try:
        resp = requests.post(FEISHU_WEBHOOK_URL, json=body, timeout=10)
        result = resp.json()
        if resp.status_code == 200 and result.get("code") == 0:
            return True
        else:
            logger.error(f"[飞书] 推送失败: {result}")
            return False
    except Exception as e:
        logger.error(f"[飞书] 请求异常: {e}")
        return False


def send_text_to_feishu(text: str) -> bool:
    """发送纯文本消息（用于错误告警）"""
    if not FEISHU_WEBHOOK_URL:
        return False
    body = {"msg_type": "text", "content": {"text": text}}
    try:
        resp = requests.post(FEISHU_WEBHOOK_URL, json=body, timeout=10)
        return resp.status_code == 200 and resp.json().get("code") == 0
    except Exception as e:
        logger.error(f"[飞书] 文本消息发送失败: {e}")
        return False


def push_signal(signal) -> bool:
    card = build_signal_card(signal)
    ok = send_to_feishu(card)
    if ok:
        logger.info(f"[飞书] 推送成功: {signal.guru_name} {signal.action} {signal.ticker}")
    return ok


def push_daily_digest(signals: list) -> bool:
    if not signals:
        return True
    # 将Signal对象列表转为dict（兼容card_templates）
    signal_dicts = []
    for s in signals:
        if hasattr(s, "__dict__"):
            signal_dicts.append(s.__dict__)
        elif isinstance(s, dict):
            signal_dicts.append(s)
    card = build_daily_digest_card(signal_dicts)
    return send_to_feishu(card)
