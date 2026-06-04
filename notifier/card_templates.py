"""飞书消息卡片模板"""


def build_signal_card(signal) -> dict:
    header_colors = {
        "买入": "green", "建仓": "green", "增持": "green", "看好": "green",
        "卖出": "red", "清仓": "red", "减持": "red", "看空": "red",
    }
    confidence_emoji = {"高": "🟢", "中": "🟡", "低": "🟠"}
    tier_tags = {"短期": "⚡ 短期", "中期": "📈 中期", "长期": "🏛️ 长期"}

    ticker_display = f"`{signal.ticker}`" if signal.ticker else ""
    title = f"📊 {signal.guru_name} {signal.action} {signal.ticker or signal.stock_name}"

    fields = [
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
    ]

    event_date = getattr(signal, "event_date", "")
    date_line = f"\n交易日期: {event_date}" if event_date else ""

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**{signal.guru_name}** ({signal.tier})\n"
                    f"动作: **{signal.action}** {signal.stock_name} {ticker_display}{date_line}\n"
                    f"原因: {signal.reason}"
                ),
            },
        },
        {"tag": "hr"},
        {"tag": "div", "fields": fields},
    ]

    if getattr(signal, "url", ""):
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看原始数据"},
                "multi_url": {
                    "url": signal.url,
                    "pc_url": signal.url,
                    "android_url": signal.url,
                    "ios_url": signal.url,
                },
                "type": "default",
            }]
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": "⚠️ 仅供信息参考，不构成投资建议。投资有风险，决策需谨慎。"
        }],
    })

    return {
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": header_colors.get(signal.action, "blue"),
        },
        "elements": elements,
    }


def build_daily_digest_card(signals: list) -> dict:
    high = [s for s in signals if s.get("confidence") == "高"]
    mid = [s for s in signals if s.get("confidence") == "中"]

    sorted_signals = sorted(signals, key=lambda x: x.get("score", 0), reverse=True)[:10]
    lines = []
    for s in sorted_signals:
        conf = s.get("confidence", "低")
        emoji = "🟢" if conf == "高" else "🟡" if conf == "中" else "🟠"
        guru = s.get("guru_name", "")
        action = s.get("action", "")
        ticker = s.get("ticker", "")
        reason = (s.get("reason", "") or "")[:30]
        lines.append(f"{emoji} **{guru}** {action} `{ticker}` — {reason}")

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
                    "content": (
                        f"共 **{len(signals)}** 条信号 | "
                        f"🟢 高置信 {len(high)} 条 | "
                        f"🟡 中置信 {len(mid)} 条"
                    ),
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
                "elements": [{"tag": "plain_text", "content": "⚠️ 仅供参考，不构成投资建议"}],
            },
        ],
    }
