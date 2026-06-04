import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent

# DeepSeek API
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# FMP (Financial Modeling Prep) API — 可用于获取股票基本面、SEC数据等
FMP_API_KEY = os.getenv("FMP_API_KEY", "")
FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

# 飞书
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_WEBHOOK_SECRET = os.getenv("FEISHU_WEBHOOK_SECRET", "")

# 数据库
DB_PATH = str(BASE_DIR / "data" / "signals.db")

# 日志
LOG_DIR = str(BASE_DIR / "logs")
LOG_FILE = str(BASE_DIR / "logs" / "guru_tracker.log")

# 调度频率（供参考，实际由 cron 控制）
SCHEDULE = {
    "sec_13f":      {"cron": "0 8 * * 1"},
    "sec_form4":    {"interval_hours": 6},
    "ark_trades":   {"cron": "0 7,20 * * 1-5"},
    "congress":     {"interval_hours": 12},
    "social_media": {"interval_minutes": 30},
}

# 置信度阈值：低于此值不推送
MIN_CONFIDENCE_TO_PUSH = "中"  # "低" / "中" / "高"

# QuiverQuant API（国会交易数据）
QUIVER_API_KEY = os.getenv("QUIVER_API_KEY", "")

# 雪球 Cookie
XUEQIU_COOKIE = os.getenv("XUEQIU_COOKIE", "")
